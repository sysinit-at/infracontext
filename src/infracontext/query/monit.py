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
        **kwargs,
    ) -> QueryResult:
        """Query Monit for service status.

        Args:
            ssh_target: SSH alias or host to connect to (for SSH mode)
            port: Monit HTTP port for SSH mode (default: 2812)
            url: Direct Monit HTTP URL (e.g., http://monit.example.com:2812)
            credential: Keychain credential account for HTTP basic auth (format: user:pass)
            service: Specific service name to query
        """
        if url:
            return self._query_direct(url, credential, service)
        elif ssh_target:
            return self._query_via_ssh(ssh_target, port, service)
        else:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name="monit",
                error="Either ssh_target or url must be provided",
            )

    def _query_direct(self, url: str, credential: str | None, service: str | None) -> QueryResult:
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

        verify_ssl = status_url.startswith("https://")

        try:
            response = requests.get(
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
                    error="HTTP error (check URL and auth)",
                )

            return self._parse_status(response.text, url, service)
        except requests.Timeout:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=f"monit@{url}",
                error="Request timeout (30s)",
            )
        except requests.RequestException as e:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=f"monit@{url}",
                error=f"Request failed: {e}",
            )
        except Exception as e:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=f"monit@{url}",
                error=str(e),
            )

    def _query_via_ssh(self, ssh_target: str, port: int, service: str | None) -> QueryResult:
        """Query Monit via SSH to localhost."""
        cmd = [
            "ssh",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "BatchMode=yes",
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
        except Exception as e:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=f"monit@{ssh_target}",
                error=str(e),
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
