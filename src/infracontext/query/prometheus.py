"""Prometheus query plugin using HTTP API."""

from typing import Any

import requests

from infracontext.query.base import (
    QueryPlugin,
    QueryResult,
    describe_http_error,
    resolve_bearer_token,
    resolve_verify_ssl,
)

# The PromQL templates below contain literal ``{...}`` label matchers, so they
# cannot use ``str.format`` (its ``{`` is a field-name delimiter and raises
# ``ValueError: unexpected '{' in field name``). We embed this sentinel and
# swap it via ``str.replace`` instead, which treats braces as ordinary text.
_INSTANCE_SENTINEL = "__INSTANCE__"


class PrometheusPlugin(QueryPlugin):
    """Query Prometheus via HTTP API."""

    source_type = "prometheus"

    # Common queries for node health. ``__INSTANCE__`` is replaced with the
    # instance label value at query time (see _INSTANCE_SENTINEL above).
    DEFAULT_QUERIES = {
        "up": 'up{instance="__INSTANCE__"}',
        "cpu": '100 - (avg by(instance) (irate(node_cpu_seconds_total{instance="__INSTANCE__",mode="idle"}[5m])) * 100)',
        "memory": '(1 - node_memory_MemAvailable_bytes{instance="__INSTANCE__"} / node_memory_MemTotal_bytes{instance="__INSTANCE__"}) * 100',
        "disk": '100 - (node_filesystem_avail_bytes{instance="__INSTANCE__",fstype!~"tmpfs|overlay"} / node_filesystem_size_bytes{instance="__INSTANCE__",fstype!~"tmpfs|overlay"}) * 100',
        "load": 'node_load1{instance="__INSTANCE__"}',
    }

    def query(
        self,
        source_config: dict,
        node_selector: str,
        query_type: str = "status",
        promql: str | None = None,
        **kwargs,
    ) -> QueryResult:
        """Query Prometheus for node metrics.

        Args:
            source_config: Must contain 'addr' (e.g., http://prometheus:9090)
            node_selector: Instance label value (e.g., "web-server:9100")
            query_type: One of: status, cpu, memory, disk, load, custom
            promql: Custom PromQL query (required if query_type is 'custom')
        """
        addr = source_config.get("addr", "").rstrip("/")
        if not addr:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=source_config.get("name", "prometheus"),
                error="Missing 'addr' in source config",
            )

        # Build query
        if promql:
            query = promql
        elif query_type == "status":
            # Query multiple metrics at once
            results = {}
            for metric_name, query_template in self.DEFAULT_QUERIES.items():
                q = query_template.replace(_INSTANCE_SENTINEL, node_selector)
                result = self._execute_query(addr, q, source_config)
                if result.get("status") == "success":
                    results[metric_name] = self._extract_value(result)
            return QueryResult(
                success=True,
                source_type=self.source_type,
                source_name=source_config.get("name", "prometheus"),
                data=results,
            )
        elif query_type in self.DEFAULT_QUERIES:
            query = self.DEFAULT_QUERIES[query_type].replace(_INSTANCE_SENTINEL, node_selector)
        else:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=source_config.get("name", "prometheus"),
                error=f"Unknown query_type: {query_type}. Use: {', '.join(self.DEFAULT_QUERIES.keys())}, or 'custom' with promql=",
            )

        result = self._execute_query(addr, query, source_config)

        if result.get("status") != "success":
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=source_config.get("name", "prometheus"),
                error=result.get("error", "Unknown error"),
            )

        return QueryResult(
            success=True,
            source_type=self.source_type,
            source_name=source_config.get("name", "prometheus"),
            data=result.get("data", {}),
        )

    def _resolve_bearer_token(self, source_config: dict) -> str | None:
        """Resolve bearer token from keychain or config."""
        return resolve_bearer_token(source_config)

    def _execute_query(self, addr: str, query: str, source_config: dict) -> dict[str, Any]:
        """Execute a PromQL query via HTTP."""
        url = f"{addr}/api/v1/query"
        headers: dict[str, str] = {}

        # Add auth if configured (keychain preferred, plaintext fallback)
        if bearer := self._resolve_bearer_token(source_config):
            headers["Authorization"] = f"Bearer {bearer}"

        try:
            response = self.session.get(
                url,
                params={"query": query},
                headers=headers,
                timeout=(10, 30),
                verify=resolve_verify_ssl(source_config),
            )
            # Surface HTTP failures (5xx/proxy HTML, empty-bodied 401) before
            # touching .json(), so a non-JSON error body is not masked as the
            # misleading "Invalid JSON response".
            if response.status_code >= 400:
                return {"status": "error", "error": describe_http_error(url, response)}
            try:
                data = response.json()
            except ValueError as e:
                return {"status": "error", "error": f"Invalid JSON response: {e}"}
            if not isinstance(data, dict):
                return {"status": "error", "error": "Invalid JSON response format"}
            return data
        except requests.Timeout:
            return {"status": "error", "error": "Query timeout"}
        except (requests.RequestException, OSError) as e:
            # Only network-ish failures become inline errors; programming bugs
            # (TypeError/KeyError/AttributeError) propagate so they surface
            # instead of hiding behind a generic str(e) message.
            return {"status": "error", "error": f"Request failed: {e}"}

    def _extract_value(self, result: dict) -> str | float | None:
        """Extract scalar value from Prometheus response."""
        try:
            data = result.get("data", {})
            if data.get("resultType") == "vector":
                results = data.get("result", [])
                if results:
                    # Return first result's value
                    return float(results[0]["value"][1])
            return None
        except (KeyError, IndexError, ValueError):
            return None
