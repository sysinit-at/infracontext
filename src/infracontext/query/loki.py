"""Loki query plugin using HTTP API."""

import re
import time
from typing import Any

import requests

from infracontext.query.base import QueryPlugin, QueryResult


class LokiPlugin(QueryPlugin):
    """Query Loki via HTTP API."""

    source_type = "loki"

    def query(
        self,
        source_config: dict,
        node_selector: str,
        query_type: str = "logs",
        logql: str | None = None,
        since: str = "1h",
        limit: int = 100,
        grep: str | None = None,
        **kwargs,
    ) -> QueryResult:
        """Query Loki for logs using logcli.

        Args:
            source_config: Must contain 'addr' (e.g., http://loki:3100)
            node_selector: LogQL selector (e.g., '{service_name="web"}')
            query_type: 'logs' or 'labels'
            logql: Custom LogQL query (overrides node_selector)
            since: Time range (e.g., "1h", "30m", "2d")
            limit: Max log entries to return
            grep: Filter pattern to add (becomes |= "pattern")
        """
        addr = source_config.get("addr", "").rstrip("/")
        if not addr:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=source_config.get("name", "loki"),
                error="Missing 'addr' in source config",
            )

        if query_type == "labels":
            return self._query_labels(addr, source_config)

        # Build LogQL query
        if logql:
            query = logql
        else:
            query = node_selector
            if grep:
                query = f'{query} |= "{grep}"'

        result = self._execute_query(addr, query, since, limit, source_config)
        return result

    def _execute_query(self, addr: str, query: str, since: str, limit: int, source_config: dict) -> QueryResult:
        """Execute a LogQL query via Loki HTTP API."""
        parsed_since = self._parse_since(since)
        if parsed_since is None:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=source_config.get("name", "loki"),
                error=f"Invalid --since value '{since}'. Use formats like 30m, 1h, 2d.",
            )

        end_ns = int(time.time() * 1_000_000_000)
        start_ns = end_ns - (parsed_since * 1_000_000_000)
        url = f"{addr}/loki/api/v1/query_range"
        headers = self._build_headers(source_config)
        verify_ssl = self._get_verify_ssl(source_config)
        params = {
            "query": query,
            "start": str(start_ns),
            "end": str(end_ns),
            "limit": str(limit),
            "direction": "backward",
        }

        try:
            response = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=(10, 60),
                verify=verify_ssl,
            )
            payload = response.json()
            if response.status_code >= 400:
                error = payload.get("error") if isinstance(payload, dict) else None
                return QueryResult(
                    success=False,
                    source_type=self.source_type,
                    source_name=source_config.get("name", "loki"),
                    error=error or f"HTTP {response.status_code}",
                )

            if not isinstance(payload, dict) or payload.get("status") != "success":
                error = payload.get("error") if isinstance(payload, dict) else None
                return QueryResult(
                    success=False,
                    source_type=self.source_type,
                    source_name=source_config.get("name", "loki"),
                    error=error or "Invalid Loki response",
                )

            logs: list[dict[str, Any]] = []
            data = payload.get("data", {})
            results = data.get("result", []) if isinstance(data, dict) else []
            for stream in results:
                if not isinstance(stream, dict):
                    continue
                stream_labels = stream.get("stream")
                values = stream.get("values", [])
                if not isinstance(values, list):
                    continue
                for value in values:
                    if not isinstance(value, list) or len(value) < 2:
                        continue
                    logs.append(
                        {
                            "timestamp": value[0],
                            "line": value[1],
                            "labels": stream_labels if isinstance(stream_labels, dict) else {},
                        }
                    )

            return QueryResult(
                success=True,
                source_type=self.source_type,
                source_name=source_config.get("name", "loki"),
                data={"logs": logs, "count": len(logs)},
            )
        except requests.Timeout:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=source_config.get("name", "loki"),
                error="Query timeout (60s)",
            )
        except requests.RequestException as e:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=source_config.get("name", "loki"),
                error=f"Request failed: {e}",
            )
        except ValueError as e:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=source_config.get("name", "loki"),
                error=f"Invalid JSON response: {e}",
            )
        except Exception as e:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=source_config.get("name", "loki"),
                error=str(e),
            )

    def _query_labels(self, addr: str, source_config: dict) -> QueryResult:
        """Query available labels."""
        url = f"{addr}/loki/api/v1/labels"
        headers = self._build_headers(source_config)
        verify_ssl = self._get_verify_ssl(source_config)

        try:
            response = requests.get(
                url,
                headers=headers,
                timeout=(10, 30),
                verify=verify_ssl,
            )
            payload = response.json()
            if response.status_code >= 400:
                error = payload.get("error") if isinstance(payload, dict) else None
                return QueryResult(
                    success=False,
                    source_type=self.source_type,
                    source_name=source_config.get("name", "loki"),
                    error=error or f"HTTP {response.status_code}",
                )

            if not isinstance(payload, dict) or payload.get("status") != "success":
                error = payload.get("error") if isinstance(payload, dict) else None
                return QueryResult(
                    success=False,
                    source_type=self.source_type,
                    source_name=source_config.get("name", "loki"),
                    error=error or "Invalid Loki response",
                )

            data = payload.get("data", [])
            labels = [str(label) for label in data] if isinstance(data, list) else []
            return QueryResult(
                success=True,
                source_type=self.source_type,
                source_name=source_config.get("name", "loki"),
                data={"labels": labels},
            )
        except requests.Timeout:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=source_config.get("name", "loki"),
                error="Query timeout (30s)",
            )
        except requests.RequestException as e:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=source_config.get("name", "loki"),
                error=f"Request failed: {e}",
            )
        except ValueError as e:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=source_config.get("name", "loki"),
                error=f"Invalid JSON response: {e}",
            )
        except Exception as e:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=source_config.get("name", "loki"),
                error=str(e),
            )

    def _build_headers(self, source_config: dict) -> dict[str, str]:
        """Build HTTP headers for Loki requests."""
        headers: dict[str, str] = {}
        if bearer := source_config.get("bearer_token"):
            headers["Authorization"] = f"Bearer {bearer}"
        if tenant := source_config.get("tenant_id"):
            headers["X-Scope-OrgID"] = str(tenant)
        return headers

    def _get_verify_ssl(self, source_config: dict) -> bool:
        """Determine TLS verification setting from source config."""
        verify_ssl = source_config.get("verify_ssl", True)
        if source_config.get("tls_skip_verify"):
            return False
        return bool(verify_ssl)

    def _parse_since(self, since: str) -> int | None:
        """Parse duration string (e.g., 30m, 1h, 2d) into seconds."""
        match = re.fullmatch(r"(\d+)([smhdSMHD])", since.strip())
        if not match:
            return None

        value = int(match.group(1))
        unit = match.group(2).lower()
        unit_seconds = {
            "s": 1,
            "m": 60,
            "h": 3600,
            "d": 86400,
        }
        seconds = value * unit_seconds[unit]
        if seconds > 90 * 86400:  # cap at 90 days
            return None
        return seconds
