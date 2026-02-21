"""CheckMK query plugin using REST API."""

from typing import Any
from urllib.parse import quote

import requests

from infracontext.query.base import QueryPlugin, QueryResult


class CheckMKPlugin(QueryPlugin):
    """Query CheckMK via REST API."""

    source_type = "checkmk"

    def query(
        self,
        source_config: dict,
        node_selector: str,
        query_type: str = "status",
        **kwargs,
    ) -> QueryResult:
        """Query CheckMK for host status.

        Args:
            source_config: Must contain 'api_url' and 'credential' (keychain account)
            node_selector: Host name in CheckMK
            query_type: 'status', 'services', or 'alerts'
        """
        api_url = source_config.get("api_url", "").rstrip("/")
        if not api_url:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=source_config.get("name", "checkmk"),
                error="Missing 'api_url' in source config",
            )

        # Get credentials from keychain
        credential_account = source_config.get("credential")
        auth_header = self._get_auth_header(credential_account, source_config)
        if auth_header is None:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=source_config.get("name", "checkmk"),
                error=f"Could not get credential for '{credential_account}'",
            )

        if query_type == "status":
            return self._query_host_status(api_url, node_selector, auth_header, source_config)
        elif query_type == "services":
            return self._query_services(api_url, node_selector, auth_header, source_config)
        elif query_type == "alerts":
            return self._query_alerts(api_url, node_selector, auth_header, source_config)
        else:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=source_config.get("name", "checkmk"),
                error=f"Unknown query_type: {query_type}. Use: status, services, alerts",
            )

    def _get_auth_header(self, credential_account: str | None, source_config: dict) -> str | None:
        """Get Authorization header from keychain or config."""
        # Warn about deprecated inline credentials
        if source_config.get("username") or source_config.get("secret"):
            import logging

            logging.warning(
                "Inline 'username'/'secret' in CheckMK config is deprecated. "
                "Use 'ic config credential set <account>' to store credentials in the system keychain."
            )

        if not credential_account:
            return None

        # Get from keychain
        try:
            from infracontext.credentials.keychain import get_credential

            secret = get_credential(credential_account)
            if secret:
                # CheckMK format: "Bearer username secret"
                # The credential should be stored as "username:secret"
                if ":" in secret:
                    user, passwd = secret.split(":", 1)
                    return f"Bearer {user} {passwd}"
                return f"Bearer {secret}"
        except ImportError:
            pass
        return None

    def _query_host_status(self, api_url: str, host_name: str, auth_header: str, source_config: dict) -> QueryResult:
        """Get host status."""
        url = f"{api_url}/objects/host/{quote(host_name, safe='')}"
        result = self._request_get(url, auth_header, source_config=source_config)

        if "error" in result:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=source_config.get("name", "checkmk"),
                error=result["error"],
            )

        # Extract relevant status info
        extensions = result.get("extensions", {})
        status = {
            "host_name": host_name,
            "state": extensions.get("state"),
            "has_been_checked": extensions.get("has_been_checked"),
            "last_check": extensions.get("last_check"),
            "acknowledged": extensions.get("acknowledged"),
            "in_downtime": extensions.get("scheduled_downtime_depth", 0) > 0,
        }

        return QueryResult(
            success=True,
            source_type=self.source_type,
            source_name=source_config.get("name", "checkmk"),
            data=status,
        )

    def _query_services(self, api_url: str, host_name: str, auth_header: str, source_config: dict) -> QueryResult:
        """Get services for a host."""
        # Use collection endpoint with host filter
        url = f"{api_url}/domain-types/service/collections/all"
        result = self._request_get(
            url,
            auth_header,
            params={"host_name": host_name},
            source_config=source_config,
        )

        if "error" in result:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=source_config.get("name", "checkmk"),
                error=result["error"],
            )

        # Extract service states
        services = []
        for item in result.get("value", []):
            ext = item.get("extensions", {})
            services.append(
                {
                    "description": ext.get("description"),
                    "state": ext.get("state"),
                    "state_type": ext.get("state_type"),
                    "plugin_output": ext.get("plugin_output"),
                    "acknowledged": ext.get("acknowledged"),
                }
            )

        # Summarize by state
        summary = {"ok": 0, "warn": 0, "crit": 0, "unknown": 0}
        for svc in services:
            state = svc.get("state", 3)
            if state == 0:
                summary["ok"] += 1
            elif state == 1:
                summary["warn"] += 1
            elif state == 2:
                summary["crit"] += 1
            else:
                summary["unknown"] += 1

        return QueryResult(
            success=True,
            source_type=self.source_type,
            source_name=source_config.get("name", "checkmk"),
            data={"services": services, "summary": summary},
        )

    def _query_alerts(self, api_url: str, host_name: str, auth_header: str, source_config: dict) -> QueryResult:
        """Get current problems/alerts for a host."""
        # Query services in non-OK state
        url = f"{api_url}/domain-types/service/collections/all"
        result = self._request_get(
            url,
            auth_header,
            params={"host_name": host_name, "state": ["1", "2", "3"]},
            source_config=source_config,
        )

        if "error" in result:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=source_config.get("name", "checkmk"),
                error=result["error"],
            )

        alerts = []
        for item in result.get("value", []):
            ext = item.get("extensions", {})
            alerts.append(
                {
                    "service": ext.get("description"),
                    "state": ext.get("state"),
                    "output": ext.get("plugin_output"),
                    "last_check": ext.get("last_check"),
                    "acknowledged": ext.get("acknowledged"),
                }
            )

        return QueryResult(
            success=True,
            source_type=self.source_type,
            source_name=source_config.get("name", "checkmk"),
            data={"alerts": alerts, "count": len(alerts)},
        )

    def _request_get(
        self,
        url: str,
        auth_header: str,
        *,
        params: dict[str, str | list[str]] | None = None,
        source_config: dict,
    ) -> dict[str, Any]:
        """Execute a GET request via HTTP."""
        headers = {
            "Authorization": auth_header,
            "Accept": "application/json",
        }
        verify_ssl = source_config.get("verify_ssl", True)
        if source_config.get("tls_skip_verify"):
            verify_ssl = False

        try:
            response = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=(10, 30),
                verify=verify_ssl,
            )
            data: Any
            try:
                data = response.json()
            except ValueError:
                data = None

            if response.status_code >= 400:
                if isinstance(data, dict):
                    title = data.get("title")
                    status = data.get("status")
                    if title and status:
                        return {"error": f"{status}: {title}"}
                return {"error": f"HTTP {response.status_code}"}

            if not isinstance(data, dict):
                return {"error": "Invalid JSON response format"}

            # Check for API error response
            if "title" in data and "status" in data:
                return {"error": f"{data.get('status')}: {data.get('title')}"}
            return data
        except requests.Timeout:
            return {"error": "Request timeout"}
        except requests.RequestException as e:
            return {"error": f"Request failed: {e}"}
        except Exception as e:
            return {"error": str(e)}
