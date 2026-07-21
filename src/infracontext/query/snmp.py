"""SNMP query plugin (added in ic 0.4.0).

Reads *live* device status over SNMP for the incident hot path. Where the SNMP
*source* plugin (item C) walks broadly to discover and persist nodes, this
plugin does the smallest walk that answers "is this device healthy right now?":

- ``status`` (default): sysName + sysUpTime (one system walk) and an interface
  up/down count (one ifOperStatus walk) — two walks, kept tight so a degraded
  device fails fast during an incident.
- ``interfaces``: the per-interface table (name, admin/oper status, speed,
  alias) merged from ifTable + ifXTable — two walks.

Transport and credentials are shared with the source plugin: the same puresnmp
client and the same keychain-backed credential scheme
(``snmp:<source>:community`` for v2c, ``snmp:<source>:auth`` / ``:priv`` for v3),
via :func:`infracontext.sources.snmp.snmp_credentials`. The single network seam
is :meth:`SNMPQueryPlugin._walk`, which every test replaces with a canned-walk
fake. Timeouts are deliberately tight (see :data:`DEFAULT_QUERY_TIMEOUT`): a
non-responsive device must fail in a couple of seconds, not tens.
"""

from __future__ import annotations

import asyncio
from typing import Any

from infracontext.query.base import QueryPlugin, QueryResult
from infracontext.sources.snmp import (
    _IF_STATUS,
    IF_TABLE,
    IF_X_TABLE,
    SYS,
    SNMPError,
    _as_int,
    _index_sort_key,
    _speed_mbps,
    _tabulate,
    _text,
    parse_system,
    snmp_credentials,
)

# IF-MIB ifOperStatus column (1.3.6.1.2.1.2.2.1.8): walking just this column is
# the cheapest way to count interfaces up/down without pulling the full table.
IF_OPER_STATUS = f"{IF_TABLE}.8"

# ifTable columns.
_IF_DESCR = 2
_IF_SPEED = 5
_IF_ADMIN = 7
_IF_OPER = 8
# ifXTable columns.
_IFX_NAME = 1
_IFX_HIGH_SPEED = 15
_IFX_ALIAS = 18

# ifOperStatus / ifAdminStatus codes (RFC 2863): 1 = up, 2 = down.
_STATUS_UP = 1
_STATUS_DOWN = 2

DEFAULT_PORT = 161
# Tight per-walk budget — this is the incident hot path. A single retry absorbs
# the odd dropped UDP datagram without turning a dead device into a ~10s hang.
DEFAULT_QUERY_TIMEOUT = 2
DEFAULT_QUERY_RETRIES = 1

_VALID_QUERY_TYPES = ("status", "interfaces")


class SNMPQueryPlugin(QueryPlugin):
    """Query a network device over SNMP for live status / interface state."""

    source_type = "snmp"

    #: Per-walk timeout / retries (instance attrs so they stay visible and
    #: overridable). Read once in :meth:`_awalk`.
    timeout: int = DEFAULT_QUERY_TIMEOUT
    retries: int = DEFAULT_QUERY_RETRIES

    def query(
        self,
        source_config: dict,
        node_selector: str,
        query_type: str = "status",
        **kwargs,
    ) -> QueryResult:
        """Query an SNMP device for a node.

        Args:
            source_config: SNMP source config (carries ``name`` for credentials).
            node_selector: The device host to walk (the node's snmp
                observability ``instance``).
            query_type: ``status`` (default) or ``interfaces``.
        """
        source_name = source_config.get("name", "snmp")
        host = (node_selector or "").strip()
        if not host:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=source_name,
                error="No SNMP host: the node has no 'snmp' observability instance to query.",
            )
        if query_type not in _VALID_QUERY_TYPES:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=source_name,
                error=f"Unknown query_type: {query_type}. Use: {', '.join(_VALID_QUERY_TYPES)}",
            )

        try:
            data = (
                self._interfaces(source_config, host)
                if query_type == "interfaces"
                else self._status(source_config, host)
            )
        except SNMPError as e:
            # A walk failure (timeout, unreachable, bad credential) degrades to
            # a clear one-line error, like the other query sections.
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=source_name,
                error=str(e),
            )
        return QueryResult(
            success=True,
            source_type=self.source_type,
            source_name=source_name,
            data=data,
        )

    # ── query shapes (pure once the walk is done) ──────────────────

    def _status(self, source_config: dict, host: str) -> dict[str, Any]:
        """sysName + sysUpTime and an interface up/down count (two walks)."""
        system = parse_system(self._walk(source_config, host, SYS))
        counts = _count_oper_status(self._walk(source_config, host, IF_OPER_STATUS))

        uptime_ticks = system["sys_uptime"]
        data: dict[str, Any] = {
            "sys_name": system["sys_name"],
            "sys_uptime_ticks": uptime_ticks,
            "interface_counts": counts,
        }
        human = _format_uptime(uptime_ticks if isinstance(uptime_ticks, int) else None)
        if human:
            data["sys_uptime"] = human
        if system["sys_descr"]:
            data["sys_descr"] = system["sys_descr"]
        if system["sys_location"]:
            data["sys_location"] = system["sys_location"]
        return data

    def _interfaces(self, source_config: dict, host: str) -> dict[str, Any]:
        """Per-interface table merged from ifTable + ifXTable (two walks)."""
        base = _tabulate(self._walk(source_config, host, IF_TABLE), IF_TABLE)
        ext = _tabulate(self._walk(source_config, host, IF_X_TABLE), IF_X_TABLE)
        interfaces: list[dict[str, Any]] = []
        for index in sorted(base, key=_index_sort_key):
            cols = base[index]
            xcols = ext.get(index, {})
            entry: dict[str, Any] = {
                "name": _text(xcols.get(_IFX_NAME)) or _text(cols.get(_IF_DESCR)) or index,
                "admin": _IF_STATUS.get(_as_int(cols.get(_IF_ADMIN)) or 0, ""),
                "oper": _IF_STATUS.get(_as_int(cols.get(_IF_OPER)) or 0, ""),
                "speed_mbps": _speed_mbps(xcols.get(_IFX_HIGH_SPEED), cols.get(_IF_SPEED)),
                "alias": _text(xcols.get(_IFX_ALIAS)),
            }
            interfaces.append({k: v for k, v in entry.items() if v not in ("", None)})
        return {"interfaces": interfaces, "total": len(interfaces)}

    # ── transport (the single real-network seam; faked in tests) ───

    def _walk(self, source_config: dict, host: str, base_oid: str) -> list[tuple[str, object]]:
        """Blocking SNMP walk of ``base_oid`` on ``host`` (raises SNMPError).

        The only method that touches the network — every test replaces it with
        a canned-walk fake, so the plugin's logic is exercised without a device.
        """
        from puresnmp.exc import Timeout

        try:
            return asyncio.run(self._awalk(source_config, host, base_oid))
        except Timeout as e:
            raise SNMPError(f"SNMP timeout after {self.timeout}s querying {host}") from e
        except SNMPError:
            raise
        except Exception as e:  # noqa: BLE001 - puresnmp raises a wide variety
            raise SNMPError(f"SNMP walk of {base_oid} on {host} failed: {e}") from e

    async def _awalk(self, source_config: dict, host: str, base_oid: str) -> list[tuple[str, object]]:
        from puresnmp import Client, PyWrapper

        client = Client(
            host, snmp_credentials(source_config), port=int(source_config.get("port", DEFAULT_PORT))
        )
        wrapper = PyWrapper(client)
        out: list[tuple[str, object]] = []
        with client.reconfigure(timeout=self.timeout, retries=self.retries):
            async for varbind in wrapper.walk(base_oid):
                out.append((str(varbind.oid), varbind.value))
        return out


# ── pure helpers ────────────────────────────────────────────────────


def _count_oper_status(oper_rows: list[tuple[str, object]]) -> dict[str, int]:
    """Count interfaces up/down/other from an ifOperStatus column walk."""
    up = down = other = 0
    for _oid, value in oper_rows:
        code = _as_int(value)
        if code == _STATUS_UP:
            up += 1
        elif code == _STATUS_DOWN:
            down += 1
        else:
            other += 1
    counts = {"total": up + down + other, "up": up, "down": down}
    if other:
        counts["other"] = other
    return counts


def _format_uptime(ticks: int | None) -> str | None:
    """Render sysUpTime (TimeTicks, hundredths of a second) as ``1d 2h 3m``."""
    if ticks is None or ticks < 0:
        return None
    seconds = ticks // 100
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)
