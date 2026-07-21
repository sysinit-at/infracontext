"""Tests for the relationship constraint matrix."""

from __future__ import annotations

from infracontext.models.node import COMPUTE_NODE_TYPES, Node, NodeType
from infracontext.models.relationship import (
    RELATIONSHIP_CONSTRAINTS,
    Relationship,
    RelationshipType,
    get_valid_relationship_types,
)


class TestMatrixConsistency:
    def test_every_referenced_node_type_exists(self):
        """A typo in a constraint key would silently make the pair unreachable."""
        valid = {t.value for t in NodeType}
        for src, tgt in RELATIONSHIP_CONSTRAINTS:
            assert src in valid, f"unknown source type in constraints: {src}"
            assert tgt in valid, f"unknown target type in constraints: {tgt}"

    def test_every_referenced_relationship_type_exists(self):
        valid = {t.value for t in RelationshipType}
        for pair, rel_types in RELATIONSHIP_CONSTRAINTS.items():
            for rel in rel_types:
                assert rel in valid, f"unknown relationship type {rel} for {pair}"


class TestStorageMountGaps:
    """Compute nodes of any flavor can mount NFS — the matrix must allow it.

    These pairs were missing and forced real infra data (LXC containers and
    physical hosts with NFS mounts) into constraint violations.
    """

    def test_all_compute_types_can_mount_nfs_shares(self):
        for src in ("vm", "lxc_container", "oci_container", "physical_host"):
            assert "mounts" in get_valid_relationship_types(src, "nfs_share"), src

    def test_service_cluster_can_mount_nfs_shares(self):
        assert "mounts" in get_valid_relationship_types("service_cluster", "nfs_share")

    def test_physical_host_can_mount_generic_storage(self):
        assert "mounts" in get_valid_relationship_types("physical_host", "storage")


class TestClusterMembership:
    def test_vm_and_lxc_can_be_members_of_service_cluster(self):
        assert "member_of" in get_valid_relationship_types("vm", "service_cluster")
        assert "member_of" in get_valid_relationship_types("lxc_container", "service_cluster")

    def test_containers_can_connect_to_vm_services(self):
        """An app in an LXC/OCI container talking to a database VM is
        ordinary topology — found unexpressible against real infra."""
        assert "connects_to" in get_valid_relationship_types("lxc_container", "vm")
        assert "connects_to" in get_valid_relationship_types("oci_container", "vm")

    def test_lxc_can_run_on_hypervisor_cluster(self):
        allowed = get_valid_relationship_types("lxc_container", "hypervisor_cluster")
        assert "runs_on" in allowed
        assert "member_of" in allowed


class TestApplicationLayer:
    def test_applications_can_target_compute_and_each_other(self):
        """Business applications map onto VMs/clusters and consume other
        apps — previously unexpressible, forcing app-layer modeling out of
        the graph entirely."""
        assert "runs_on" in get_valid_relationship_types("application", "vm")
        assert "runs_on" in get_valid_relationship_types("application", "service_cluster")
        assert "depends_on" in get_valid_relationship_types("application", "vm")
        assert "depends_on" in get_valid_relationship_types("application", "application")


class TestServiceClusterOutbound:
    def test_clusters_have_outbound_dependencies(self):
        """A web farm cluster talks to databases, LBs, other clusters, and
        storage servers — found unexpressible against real topology."""
        assert "connects_to" in get_valid_relationship_types("service_cluster", "vm")
        assert "connects_to" in get_valid_relationship_types("service_cluster", "service_cluster")
        assert "connects_to" in get_valid_relationship_types("service_cluster", "physical_host")
        assert "routes_to" in get_valid_relationship_types("vm", "service_cluster")
        assert "connects_to" in get_valid_relationship_types("vm", "lxc_container")
        assert "depends_on" in get_valid_relationship_types("application", "physical_host")


class TestNetworkDevice:
    def test_network_device_is_a_node_type(self):
        assert NodeType("network_device") is NodeType.NETWORK_DEVICE

    def test_network_device_connectivity_pairs(self):
        assert "connects_to" in get_valid_relationship_types("network_device", "network")
        assert "routes_to" in get_valid_relationship_types("network_device", "network_device")
        assert "connects_to" in get_valid_relationship_types("physical_host", "network_device")
        assert "connects_to" in get_valid_relationship_types("network_device", "physical_host")


class TestPhysicalLayer:
    """Physical/datacenter layer node types and their placement/power edges
    (added in ic 0.4.0)."""

    def test_new_node_types_exist(self):
        assert NodeType("site") is NodeType.SITE
        assert NodeType("rack") is NodeType.RACK
        assert NodeType("pdu") is NodeType.PDU
        assert NodeType("ups") is NodeType.UPS

    def test_new_types_are_not_compute(self):
        # Facility/power assets carry no SSH triage -- keep them out of the
        # compute set so doctor never nags them for ssh_alias/observability.
        for t in (NodeType.SITE, NodeType.RACK, NodeType.PDU, NodeType.UPS):
            assert t not in COMPUTE_NODE_TYPES

    def test_located_in_containment_pairs(self):
        for src in ("physical_host", "network_device", "pdu", "ups"):
            assert "located_in" in get_valid_relationship_types(src, "rack"), src
            assert "located_in" in get_valid_relationship_types(src, "site"), src
        assert "located_in" in get_valid_relationship_types("rack", "site")

    def test_powered_by_pairs(self):
        assert "powered_by" in get_valid_relationship_types("physical_host", "pdu")
        assert "powered_by" in get_valid_relationship_types("network_device", "pdu")
        assert "powered_by" in get_valid_relationship_types("pdu", "ups")
        assert "powered_by" in get_valid_relationship_types("pdu", "pdu")  # daisy chain
        assert "powered_by" in get_valid_relationship_types("ups", "site")  # building feed

    def test_ups_is_both_located_in_and_powered_by_site(self):
        allowed = get_valid_relationship_types("ups", "site")
        assert "located_in" in allowed
        assert "powered_by" in allowed

    def test_bmc_manages_host(self):
        allowed = get_valid_relationship_types("network_device", "physical_host")
        assert "manages" in allowed
        assert "connects_to" in allowed  # existing pairing preserved

    def test_contains_is_the_reverse_direction_of_located_in(self):
        for tgt in ("physical_host", "network_device", "pdu", "ups"):
            assert "contains" in get_valid_relationship_types("rack", tgt), tgt
        assert "contains" in get_valid_relationship_types("site", "rack")


class TestPhysicalLayerConventions:
    """Free-form conventions (ic 0.4.0): a node's attributes.hardware namespace
    and a connects_to edge's local/remote port attributes are stored in the
    existing free-form attribute dicts -- no new model fields, so they must
    round-trip through the models unchanged."""

    def test_hardware_namespace_round_trips_in_node_attributes(self):
        hardware = {
            "manufacturer": "Dell",
            "model": "PowerEdge R750",
            "serial": "ABC123",
            "asset_tag": "DC1-0042",
            "u_height": 2,
            "rack_position": 14,
            "rack_face": "front",
            "firmware": "2.10.2",
            "is_full_depth": True,
        }
        node = Node(
            id="physical_host:h1",
            slug="h1",
            type=NodeType.PHYSICAL_HOST,
            name="Host 1",
            attributes={"hardware": hardware},
        )
        assert node.attributes["hardware"] == hardware

    def test_port_attributes_round_trip_on_connects_to_edge(self):
        rel = Relationship(
            source="physical_host:h1",
            target="network_device:sw1",
            type=RelationshipType.CONNECTS_TO,
            attributes={"local_port": "eno1", "remote_port": "Gi1/0/14"},
        )
        assert rel.attributes["local_port"] == "eno1"
        assert rel.attributes["remote_port"] == "Gi1/0/14"
