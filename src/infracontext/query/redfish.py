"""Redfish query plugin (added in ic 0.4.0).

Live health rollup and power draw from a BMC's Redfish service, reusing the
:class:`~infracontext.sources.redfish.RedfishClient` (shared session, basic
auth, verify-SSL semantics) over the pooled :attr:`QueryPlugin.session`.

Query types:
- ``status``: health rollup across ComputerSystem ``Status.Health`` and the
  chassis thermal summary.
- ``power``: live ``PowerConsumedWatts`` per chassis and the total.
"""

from __future__ import annotations

from infracontext.query.base import QueryPlugin, QueryResult, resolve_basic_auth, resolve_verify_ssl
from infracontext.sources.redfish import (
    RedfishClient,
    RedfishError,
    collection_member_links,
    odata_id,
    read_power,
)

# Tight connect/read timeout: a live status/power check during an incident
# must fail fast rather than hang on an unresponsive BMC.
_QUERY_TIMEOUT: tuple[float, float] = (3.05, 8.0)

# Redfish health severity ordering (worst wins in a rollup).
_HEALTH_RANK = {"critical": 3, "warning": 2, "ok": 1}


def _health_rollup(healths: list[str | None]) -> str:
    """Worst health among the inputs; ``Unknown`` if none are recognized."""
    worst: str | None = None
    worst_rank = 0
    for health in healths:
        if not health:
            continue
        rank = _HEALTH_RANK.get(health.lower(), 0)
        if rank > worst_rank:
            worst_rank = rank
            worst = health
    return worst or "Unknown"


def _thermal_summary(client: RedfishClient, chassis: dict) -> dict | None:
    """Health of a chassis's thermal subsystem (legacy Thermal or new)."""
    thermal_link = odata_id(chassis.get("Thermal"))
    if thermal_link:
        thermal = client.get(thermal_link)
        health = (thermal.get("Status") or {}).get("Health")
        if not health:
            sensors = [
                (item.get("Status") or {}).get("Health")
                for group in ("Temperatures", "Fans")
                for item in (thermal.get(group) or [])
                if isinstance(item, dict)
            ]
            health = _health_rollup(sensors)
        return {"health": health, "source": "Thermal"}
    subsystem_link = odata_id(chassis.get("ThermalSubsystem"))
    if subsystem_link:
        subsystem = client.get(subsystem_link)
        return {"health": (subsystem.get("Status") or {}).get("Health"), "source": "ThermalSubsystem"}
    return None


def collect_status(client: RedfishClient) -> dict:
    """Health rollup across systems and chassis thermal for one BMC."""
    root = client.get("/redfish/v1/")
    healths: list[str | None] = []
    systems: list[dict] = []

    systems_link = odata_id(root.get("Systems"))
    if systems_link:
        for link in collection_member_links(client.get(systems_link)):
            system = client.get(link)
            status = system.get("Status") or {}
            health = status.get("Health")
            systems.append({"id": system.get("Id") or link, "health": health, "state": status.get("State")})
            healths.append(health)

    thermal: dict | None = None
    chassis_link = odata_id(root.get("Chassis"))
    if chassis_link:
        for link in collection_member_links(client.get(chassis_link)):
            summary = _thermal_summary(client, client.get(link))
            if summary is not None:
                thermal = summary
                healths.append(summary.get("health"))
                break

    return {"health": _health_rollup(healths), "systems": systems, "thermal": thermal}


def collect_power(client: RedfishClient) -> dict:
    """Live consumed power per chassis and the total for one BMC."""
    root = client.get("/redfish/v1/")
    per_chassis: list[dict] = []
    total: float | None = None

    chassis_link = odata_id(root.get("Chassis"))
    if chassis_link:
        for link in collection_member_links(client.get(chassis_link)):
            chassis = client.get(link)
            watts = read_power(client, chassis)
            per_chassis.append({"id": chassis.get("Id") or link, "power_watts": watts})
            if watts is not None:
                total = (total or 0.0) + watts

    return {"power_watts": total, "chassis": per_chassis}


class RedfishQueryPlugin(QueryPlugin):
    """Query a BMC's Redfish service for live health and power."""

    source_type = "redfish"

    def query(
        self,
        source_config: dict,
        node_selector: str,
        query_type: str = "status",
        **kwargs,
    ) -> QueryResult:
        """Query Redfish for a node.

        Args:
            source_config: Source config (credential + verify_ssl live here).
            node_selector: The BMC base URL (from the node's redfish
                observability ``instance``).
            query_type: ``status`` (health rollup) or ``power`` (live watts).
        """
        name = source_config.get("name", "redfish")
        base_url = (node_selector or "").rstrip("/")
        if not base_url:
            return QueryResult(
                success=False,
                source_type=self.source_type,
                source_name=name,
                error="No Redfish URL for this node (set the 'instance' of its redfish observability entry)",
            )

        client = RedfishClient(
            base_url,
            auth=resolve_basic_auth(source_config),
            verify=resolve_verify_ssl(source_config),
            timeout=_QUERY_TIMEOUT,
            session=self.session,
        )

        try:
            if query_type == "status":
                data = collect_status(client)
            elif query_type == "power":
                data = collect_power(client)
            else:
                return QueryResult(
                    success=False,
                    source_type=self.source_type,
                    source_name=name,
                    error=f"Unknown query_type: {query_type}. Use: status, power",
                )
        except RedfishError as e:
            return QueryResult(
                success=False, source_type=self.source_type, source_name=name, error=str(e)
            )

        return QueryResult(success=True, source_type=self.source_type, source_name=name, data=data)
