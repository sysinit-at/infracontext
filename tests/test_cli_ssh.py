"""Tests for the top-level ``ic ssh`` command (``run_ssh``).

``os.execvp`` is patched to record its argv instead of replacing the process,
so we can assert on the exact command and on banner routing without ever
launching ssh.
"""

from __future__ import annotations

import pytest
import typer

from infracontext.cli.ssh import run_ssh
from infracontext.models.node import Node, NodeType
from infracontext.paths import ProjectPaths
from infracontext.storage import write_model


def _capture_execvp(monkeypatch) -> dict:
    calls: dict = {}

    def fake_execvp(file, argv):
        calls["file"] = file
        calls["argv"] = list(argv)

    monkeypatch.setattr("infracontext.cli.ssh.os.execvp", fake_execvp)
    return calls


def _add_node(env, node: Node) -> None:
    paths = ProjectPaths.for_project("prod", env)
    paths.node_type_dir(node.type).mkdir(parents=True, exist_ok=True)
    write_model(paths.node_file(node.type, node.slug), node)


class TestSshArgv:
    def test_argv_uses_double_dash_and_ssh_alias(self, hotpath_env, monkeypatch):
        calls = _capture_execvp(monkeypatch)
        run_ssh("web01", command_args=[], no_banner=True)
        assert calls["file"] == "ssh"
        assert calls["argv"] == ["ssh", "--", "web-prod"]

    def test_remote_command_passthrough(self, hotpath_env, monkeypatch):
        calls = _capture_execvp(monkeypatch)
        run_ssh("web01", command_args=["uptime", "-p"], no_banner=True)
        assert calls["argv"] == ["ssh", "--", "web-prod", "uptime", "-p"]

    def test_falls_back_to_domain_then_ip(self, hotpath_env, monkeypatch):
        _add_node(
            hotpath_env,
            Node(id="vm:only-ip", slug="only-ip", type=NodeType.VM, name="Only IP", ip_addresses=["10.9.9.9"]),
        )
        calls = _capture_execvp(monkeypatch)
        run_ssh("only-ip", command_args=[], no_banner=True)
        assert calls["argv"] == ["ssh", "--", "10.9.9.9"]


class TestSshBanner:
    def test_banner_goes_to_stderr_not_stdout(self, hotpath_env, monkeypatch, capsys):
        _capture_execvp(monkeypatch)
        run_ssh("web01", command_args=[], no_banner=False)
        out, err = capsys.readouterr()
        assert "vm:web-01" in err
        assert "vm:web-01" not in out
        # Banner surfaces triage + last learning.
        assert "php-fpm" in err
        assert "pool misconfigured" in err

    def test_no_banner_suppresses_it(self, hotpath_env, monkeypatch, capsys):
        _capture_execvp(monkeypatch)
        run_ssh("web01", command_args=[], no_banner=True)
        _out, err = capsys.readouterr()
        assert "vm:web-01" not in err


class TestSshGuards:
    def test_leading_dash_target_rejected(self, hotpath_env, monkeypatch):
        _add_node(
            hotpath_env,
            Node(
                id="service:evil",
                slug="evil",
                type=NodeType.SERVICE,
                name="Evil",
                ip_addresses=["-oProxyCommand=touch /tmp/pwned"],
            ),
        )
        calls = _capture_execvp(monkeypatch)
        with pytest.raises(typer.Exit) as exc:
            run_ssh("evil", command_args=[], no_banner=True)
        assert exc.value.exit_code == 1
        assert "argv" not in calls  # never execs

    def test_no_ssh_target_errors(self, hotpath_env, monkeypatch, capsys):
        _add_node(
            hotpath_env,
            Node(id="service:orphan", slug="orphan", type=NodeType.SERVICE, name="Orphan"),
        )
        calls = _capture_execvp(monkeypatch)
        with pytest.raises(typer.Exit) as exc:
            run_ssh("orphan", command_args=[], no_banner=True)
        assert exc.value.exit_code == 1
        assert "argv" not in calls
        # Rich wraps at 80 cols under capsys; normalize before substring checks.
        out = " ".join(capsys.readouterr().out.split())
        # Names the three fields it checked and how to fix.
        assert "ssh_alias" in out
        assert "domains" in out
        assert "ip_addresses" in out
        assert "ic describe node edit service:orphan" in out
