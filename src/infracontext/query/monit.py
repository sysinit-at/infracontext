"""Monit query plugin - queries local monit via SSH."""

import subprocess
import xml.etree.ElementTree as ET

import requests

from infracontext.query.base import QueryPlugin, QueryResult

# Monit service states
MONIT_STATES = {
    0: "running",
    1: "not running",
    2: "initializing",
    3: "not monitored",
}

# Monit service types
MONIT_TYPES = {
    0: "filesystem",
    1: "directory",
    2: "file",
    3: "process",
    4: "host",
    5: "system",
    6: "fifo",
    7: "program",
    8: "network",
}


class MonitPlugin(QueryPlugin):
    """Query Monit via SSH + local HTTP or direct HTTP interface."""

    source_type = "monit"

    def query(
        self,
        ssh_target: str | None = None,
        port: int = 2812,
        url: str | None = None,
        credential: str | None = None,
        service: str | None = None,
        tls_skip_verify: bool = False,
        **kwargs,
    ) -> QueryResult:
        """Query Monit for service status.

        Args:
            ssh_target: SSH alias or host to connect to (for SSH mode)
            port: Monit HTTP port for SSH mode (default: 2812)
            url: Direct Monit HTTP URL (e.g., http://monit.example.com:2812)
            credential: Keychain credential account for HTTP basic auth (format: user:pass)
            service: Specific service name to query
            tls_skip_verify: Disable TLS certificate verification for a direct
                https:// URL (for self-signed monit endpoints). Mirrors the
                ``tls_skip_verify`` config key honored by the other plugins.
        """
        if url:
            return self._query_direct(url, credential, service, tls_skip_verify)
        elif ssh_target:
            return self._query_via_ssh(ssh_target, port, service)
        else:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name="monit",
                error="Either ssh_target or url must be provided",
            )

    def _query_direct(
        self,
        url: str,
        credential: str | None,
        service: str | None,
        tls_skip_verify: bool = False,
    ) -> QueryResult:
        """Query Monit directly via HTTP."""
        url = url.rstrip("/")
        status_url = f"{url}/_status?format=xml"

        auth: tuple[str, str] | None = None

        # Add basic auth if credential provided
        if credential:
            try:
                from infracontext.credentials.keychain import get_credential

                secret = get_credential(credential)
                if secret:
                    user, passwd = secret.split(":", 1) if ":" in secret else (secret, "")
                    auth = (user, passwd)
            except ImportError:
                pass

        # Monit's per-node URL may be http or https, so verification defaults
        # to the URL scheme (unlike prometheus/loki/checkmk, whose config
        # always carries a scheme). An explicit tls_skip_verify overrides that
        # for self-signed https endpoints, matching the other plugins' key.
        verify_ssl = status_url.startswith("https://")
        if tls_skip_verify:
            verify_ssl = False

        try:
            response = self.session.get(
                status_url,
                auth=auth,
                timeout=(10, 30),
                verify=verify_ssl,
            )
            if response.status_code >= 400:
                return QueryResult(
                    success=False,
                    source_type=self.source_type,
                    source_name=f"monit@{url}",
                    error=self._http_error(url, response),
                )

            return self._parse_status(response.text, url, service)
        except requests.Timeout:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=f"monit@{url}",
                error="Request timeout (30s)",
            )
        except (requests.RequestException, OSError) as e:
            # Network-ish failures become inline errors; programming bugs
            # (TypeError/KeyError/AttributeError) propagate rather than hiding
            # behind a generic str(e).
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=f"monit@{url}",
                error=f"Request failed: {e}",
            )

    def _http_error(self, url: str, response: requests.Response) -> str:
        """Classify a ``>= 400`` monit HTTP response into an actionable hint.

        Monit speaks XML, not JSON, so there's no structured error body to
        parse -- but the status code alone tells the operator where to look:
        auth (401/403), URL/path (404), or the server itself (5xx, with a body
        snippet). This replaces the previous catch-all "HTTP error (check URL
        and auth)" that gave no such signal.
        """
        code = response.status_code
        if code in (401, 403):
            return f"HTTP {code} from {url}: authentication failed (check the monit credential)"
        if code == 404:
            return f"HTTP {code} from {url}: not found (check the monit URL and path)"
        if code >= 500:
            snippet = (response.text or "").strip()[:200]
            detail = f": {snippet}" if snippet else ""
            return f"HTTP {code} from {url}: monit server error{detail}"
        return f"HTTP {code} from {url}: request rejected (check URL and auth)"

    def _query_via_ssh(self, ssh_target: str, port: int, service: str | None) -> QueryResult:
        """Query Monit via SSH to localhost."""
        # ssh_target originates from node YAML (ssh_alias / domains[0] /
        # ip_addresses[0]) which may come from an untrusted federated repo. A
        # value like "-oProxyCommand=..." would be parsed by ssh as an option
        # and execute arbitrary commands, so reject leading-dash targets and
        # place "--" before the target to end option parsing.
        if ssh_target.startswith("-"):
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=f"monit@{ssh_target}",
                error=(
                    f"Refusing SSH target '{ssh_target}': a leading '-' would be parsed "
                    "as an ssh option. Fix the node's ssh_alias/domain/IP."
                ),
            )

        cmd = [
            "ssh",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "BatchMode=yes",
            "--",
            ssh_target,
            f"curl -sf http://localhost:{port}/_status?format=xml",
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                if "Permission denied" in result.stderr:
                    return QueryResult(
                        success=False,
                        source_type=self.source_type,
                        source_name=f"monit@{ssh_target}",
                        error="SSH permission denied",
                    )
                if "Connection refused" in result.stderr or "curl" in result.stderr.lower():
                    return QueryResult(
                        success=False,
                        source_type=self.source_type,
                        source_name=f"monit@{ssh_target}",
                        error=f"Monit not running or not accessible on port {port}",
                    )
                return QueryResult(
                    success=False,
                    source_type=self.source_type,
                    source_name=f"monit@{ssh_target}",
                    error=f"SSH failed: {result.stderr.strip()}",
                )

            return self._parse_status(result.stdout, ssh_target, service)

        except subprocess.TimeoutExpired:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=f"monit@{ssh_target}",
                error="SSH timeout (30s)",
            )
        except OSError as e:
            # e.g. ssh binary missing (FileNotFoundError). Programming bugs
            # (TypeError/KeyError/AttributeError) propagate rather than hiding
            # behind a generic str(e).
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=f"monit@{ssh_target}",
                error=f"SSH command failed: {e}",
            )

    def _parse_status(self, xml_data: str, ssh_target: str, filter_service: str | None) -> QueryResult:
        """Parse Monit XML status response."""
        try:
            root = ET.fromstring(xml_data)
        except ET.ParseError as e:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=f"monit@{ssh_target}",
                error=f"Invalid XML from monit: {e}",
            )

        services = []
        for svc in root.findall(".//service"):
            name = svc.findtext("name", "")

            # Filter if specific service requested
            if filter_service and name.lower() != filter_service.lower():
                continue

            svc_type = int(svc.get("type", 3))
            status = int(svc.findtext("status", -1))
            monitor = int(svc.findtext("monitor", 0))

            service_data = {
                "name": name,
                "type": MONIT_TYPES.get(svc_type, f"unknown({svc_type})"),
                "status": status,
                "status_text": MONIT_STATES.get(status, f"unknown({status})"),
                "monitored": monitor == 1,
            }

            # Add type-specific info
            if svc_type == 3:  # process
                pid = svc.findtext("pid")
                uptime = svc.findtext("uptime")
                if pid:
                    service_data["pid"] = int(pid)
                if uptime:
                    service_data["uptime"] = int(uptime)

                # Memory/CPU if available
                memory = svc.find("memory")
                if memory is not None:
                    pct = memory.findtext("percenttotal")
                    if pct:
                        service_data["memory_percent"] = float(pct)

                cpu = svc.find("cpu")
                if cpu is not None:
                    pct = cpu.findtext("percenttotal")
                    if pct:
                        service_data["cpu_percent"] = float(pct)

            elif svc_type == 0:  # filesystem
                block = svc.find("block")
                if block is not None:
                    pct = block.findtext("percent")
                    if pct:
                        service_data["disk_percent"] = float(pct)

            services.append(service_data)

        # Build summary
        summary = {"total": len(services), "running": 0, "failed": 0, "not_monitored": 0}
        for s in services:
            if s["status"] == 0:
                summary["running"] += 1
            elif s["status"] == 3:
                summary["not_monitored"] += 1
            else:
                summary["failed"] += 1

        return QueryResult(
            success=True,
            source_type=self.source_type,
            source_name=f"monit@{ssh_target}",
            data={"services": services, "summary": summary},
        )
