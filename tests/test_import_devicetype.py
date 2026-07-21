"""Tests for ``ic import devicetype`` -- devicetype-library YAML -> hardware.

The command parses the physical-identity subset of a NetBox devicetype-library
YAML file and fill-only merges it into a node's ``attributes.hardware``. Port
template lists in the file are ignored by design (``extra="ignore"``).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from infracontext.cli.import_cmd import DeviceType
from infracontext.cli.import_cmd import app as import_app
from infracontext.models.node import Node, NodeType
from infracontext.storage import read_model, write_model

runner = CliRunner()


# A realistic community devicetype file: a 48-port PoE+ access switch, with the
# port template lists we deliberately skip.
SWITCH_YAML = textwrap.dedent(
    """\
    manufacturer: Cisco
    model: Catalyst 9300-48P
    slug: cisco-catalyst-9300-48p
    part_number: C9300-48P
    u_height: 1
    is_full_depth: false
    airflow: front-to-rear
    weight: 6.6
    weight_unit: kg
    subdevice_role: parent
    comments: 48-port PoE+ access switch
    interfaces:
      - name: GigabitEthernet1/0/1
        type: 1000base-t
      - name: GigabitEthernet1/0/2
        type: 1000base-t
    console-ports:
      - name: con0
        type: rj-45
    """
)


def _write_yaml(tmp_path: Path, content: str, name: str = "devicetype.yaml") -> Path:
    dt = tmp_path / name
    dt.write_text(content)
    return dt


def _make_node(tmp_project, slug: str = "sw-01", node_type=NodeType.NETWORK_DEVICE, **kwargs) -> Node:
    node = Node(
        id=f"{node_type}:{slug}",
        slug=slug,
        type=node_type,
        name=kwargs.pop("name", slug),
        **kwargs,
    )
    write_model(tmp_project.node_file(node_type, slug), node)
    return node


def _read(tmp_project, slug: str = "sw-01", node_type: str = "network_device") -> Node:
    node = read_model(tmp_project.node_file(node_type, slug), Node)
    assert node is not None
    return node


@pytest.fixture()
def env(tmp_project, monkeypatch_environment, monkeypatch):
    """Active 'testproject' with environment discovery patched to the temp dir."""
    monkeypatch.setenv("IC_PROJECT", "testproject")
    return tmp_project


# ── model ─────────────────────────────────────────────────────────


class TestDeviceTypeModel:
    def test_parses_subset_and_ignores_port_lists(self):
        dt = DeviceType.model_validate(yaml.safe_load(SWITCH_YAML))
        assert dt.manufacturer == "Cisco"
        assert dt.model == "Catalyst 9300-48P"
        assert dt.part_number == "C9300-48P"
        assert dt.u_height == 1.0
        assert dt.is_full_depth is False
        assert dt.airflow == "front-to-rear"
        assert dt.weight == 6.6
        assert dt.weight_unit == "kg"
        assert dt.subdevice_role == "parent"
        # Port lists (and slug/comments) are dropped, not stored.
        dumped = dt.model_dump()
        assert "interfaces" not in dumped
        assert "console-ports" not in dumped
        assert "comments" not in dumped

    def test_omitted_optional_fields_stay_none(self):
        dt = DeviceType.model_validate({"manufacturer": "Dell", "model": "R750"})
        # Defaults are None (not the library's own defaults) so a fill-only
        # merge never invents values the file didn't state.
        assert dt.is_full_depth is None
        assert dt.u_height is None
        assert dt.model_dump(exclude_none=True) == {"manufacturer": "Dell", "model": "R750"}


# ── fill-only merge ───────────────────────────────────────────────


class TestFillOnlyMerge:
    def test_fills_empty_hardware(self, env, tmp_path):
        _make_node(env)
        dt = _write_yaml(tmp_path, SWITCH_YAML)

        result = runner.invoke(import_app, ["devicetype", str(dt), "--node", "network_device:sw-01"])
        assert result.exit_code == 0, result.output

        hw = _read(env).attributes["hardware"]
        assert hw == {
            "manufacturer": "Cisco",
            "model": "Catalyst 9300-48P",
            "part_number": "C9300-48P",
            "u_height": 1.0,
            "is_full_depth": False,
            "airflow": "front-to-rear",
            "weight": 6.6,
            "weight_unit": "kg",
            "subdevice_role": "parent",
        }
        # Port lists never leak into the hardware namespace.
        assert "interfaces" not in hw

    def test_existing_values_are_preserved(self, env, tmp_path):
        _make_node(env, attributes={"hardware": {"manufacturer": "Acme", "model": "Custom-1"}})
        dt = _write_yaml(tmp_path, SWITCH_YAML)

        result = runner.invoke(import_app, ["devicetype", str(dt), "--node", "network_device:sw-01"])
        assert result.exit_code == 0, result.output

        hw = _read(env).attributes["hardware"]
        # Existing values win.
        assert hw["manufacturer"] == "Acme"
        assert hw["model"] == "Custom-1"
        # Absent fields get filled.
        assert hw["part_number"] == "C9300-48P"
        assert hw["airflow"] == "front-to-rear"

        flat = " ".join(result.output.split())
        assert "skipped hardware.manufacturer" in flat
        assert "filled hardware.part_number" in flat

    def test_force_overwrites_existing_values(self, env, tmp_path):
        _make_node(env, attributes={"hardware": {"manufacturer": "Acme", "model": "Custom-1"}})
        dt = _write_yaml(tmp_path, SWITCH_YAML)

        result = runner.invoke(
            import_app, ["devicetype", str(dt), "--node", "network_device:sw-01", "--force"]
        )
        assert result.exit_code == 0, result.output

        hw = _read(env).attributes["hardware"]
        assert hw["manufacturer"] == "Cisco"
        assert hw["model"] == "Catalyst 9300-48P"

        flat = " ".join(result.output.split())
        assert "overwrote hardware.manufacturer" in flat

    def test_no_change_when_all_present_without_force(self, env, tmp_path):
        full = {
            "manufacturer": "Acme",
            "model": "Custom-1",
            "part_number": "P-1",
            "u_height": 2.0,
            "is_full_depth": True,
            "airflow": "rear-to-front",
            "weight": 9.0,
            "weight_unit": "lb",
            "subdevice_role": "child",
        }
        _make_node(env, attributes={"hardware": dict(full)})
        dt = _write_yaml(tmp_path, SWITCH_YAML)

        result = runner.invoke(import_app, ["devicetype", str(dt), "--node", "network_device:sw-01"])
        assert result.exit_code == 0, result.output
        assert "No hardware fields imported" in result.output
        # Untouched.
        assert _read(env).attributes["hardware"] == full

    def test_other_attributes_untouched(self, env, tmp_path):
        _make_node(env, attributes={"role": "access-switch", "hardware": {"serial": "FCW123"}})
        dt = _write_yaml(tmp_path, SWITCH_YAML)

        result = runner.invoke(import_app, ["devicetype", str(dt), "--node", "network_device:sw-01"])
        assert result.exit_code == 0, result.output

        attrs = _read(env).attributes
        assert attrs["role"] == "access-switch"
        # Pre-existing hardware sub-key survives alongside the filled ones.
        assert attrs["hardware"]["serial"] == "FCW123"
        assert attrs["hardware"]["manufacturer"] == "Cisco"


# ── rejection of non-devicetype files ─────────────────────────────


class TestRejection:
    def test_missing_manufacturer_rejected(self, env, tmp_path):
        _make_node(env)
        dt = _write_yaml(tmp_path, "model: SomeModel\n")

        result = runner.invoke(import_app, ["devicetype", str(dt), "--node", "network_device:sw-01"])
        assert result.exit_code == 1
        assert "Not a devicetype-library YAML file" in " ".join(result.output.split())
        # Node is left untouched.
        assert "hardware" not in _read(env).attributes

    def test_missing_model_rejected(self, env, tmp_path):
        _make_node(env)
        dt = _write_yaml(tmp_path, "manufacturer: Cisco\n")

        result = runner.invoke(import_app, ["devicetype", str(dt), "--node", "network_device:sw-01"])
        assert result.exit_code == 1
        assert "Not a devicetype-library YAML file" in " ".join(result.output.split())

    def test_unrelated_yaml_rejected(self, env, tmp_path):
        _make_node(env)
        dt = _write_yaml(tmp_path, "servers:\n  - name: foo\n    ip: 10.0.0.1\n")

        result = runner.invoke(import_app, ["devicetype", str(dt), "--node", "network_device:sw-01"])
        assert result.exit_code == 1
        assert "Not a devicetype-library YAML file" in " ".join(result.output.split())

    def test_empty_file_rejected(self, env, tmp_path):
        _make_node(env)
        dt = _write_yaml(tmp_path, "")

        result = runner.invoke(import_app, ["devicetype", str(dt), "--node", "network_device:sw-01"])
        assert result.exit_code == 1
        assert "Not a devicetype-library YAML file" in " ".join(result.output.split())

    def test_missing_file_errors(self, env, tmp_path):
        _make_node(env)
        result = runner.invoke(
            import_app, ["devicetype", str(tmp_path / "nope.yaml"), "--node", "network_device:sw-01"]
        )
        assert result.exit_code == 1
        assert "File not found" in result.output


# ── node resolution ───────────────────────────────────────────────


class TestNodeResolution:
    def test_fuzzy_node_resolution(self, env, tmp_path):
        _make_node(env, slug="sw-01")
        # A distractor that must not match the fuzzy query 'sw'.
        _make_node(env, slug="db-01", node_type=NodeType.VM)
        dt = _write_yaml(tmp_path, SWITCH_YAML)

        result = runner.invoke(import_app, ["devicetype", str(dt), "--node", "sw"])
        assert result.exit_code == 0, result.output
        assert _read(env).attributes["hardware"]["manufacturer"] == "Cisco"

    def test_ambiguous_fuzzy_query_exits(self, env, tmp_path):
        _make_node(env, slug="sw-01")
        _make_node(env, slug="sw-02")
        dt = _write_yaml(tmp_path, SWITCH_YAML)

        result = runner.invoke(import_app, ["devicetype", str(dt), "--node", "sw"])
        assert result.exit_code == 1
        assert "Multiple nodes match" in result.output

    def test_unknown_node_errors(self, env, tmp_path):
        _make_node(env)
        dt = _write_yaml(tmp_path, SWITCH_YAML)

        result = runner.invoke(import_app, ["devicetype", str(dt), "--node", "network_device:ghost"])
        assert result.exit_code == 1
        assert "not found" in result.output
