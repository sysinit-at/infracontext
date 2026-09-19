"""Tests for node attachments (ic 0.6.0): model, CLI, doctor."""

from __future__ import annotations

from typer.testing import CliRunner

from infracontext.cli.main import app
from infracontext.models.node import Attachment, Node
from infracontext.storage import read_model, write_model

runner = CliRunner()


def _mknode(tmp_project, slug="web-01"):
    tmp_project.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
    node = Node(id=f"vm:{slug}", slug=slug, type="vm", name=slug, description="d")
    write_model(tmp_project.node_file("vm", slug), node)
    return tmp_project.node_file("vm", slug)


class TestAttachCli:
    def test_attach_copies_file_and_records_entry(self, tmp_project, monkeypatch_environment, tmp_path, monkeypatch):
        monkeypatch.setenv("IC_PROJECT", "testproject")
        node_file = _mknode(tmp_project)
        photo = tmp_path / "rack.jpg"
        photo.write_bytes(b"jpegdata")

        result = runner.invoke(app, ["describe", "node", "attach", "vm:web-01",
                                     str(photo), "--title", "Rack front"])

        assert result.exit_code == 0, result.output
        node = read_model(node_file, Node)
        assert len(node.attachments) == 1
        att = node.attachments[0]
        assert att.file == "attachments/vm/web-01/rack.jpg"
        assert att.title == "Rack front"
        assert (tmp_project.root / att.file).read_bytes() == b"jpegdata"

    def test_duplicate_attach_refused(self, tmp_project, monkeypatch_environment, tmp_path, monkeypatch):
        monkeypatch.setenv("IC_PROJECT", "testproject")
        _mknode(tmp_project)
        photo = tmp_path / "label.png"
        photo.write_bytes(b"x")
        runner.invoke(app, ["describe", "node", "attach", "vm:web-01", str(photo)])

        result = runner.invoke(app, ["describe", "node", "attach", "vm:web-01", str(photo)])

        assert result.exit_code == 1
        assert "already attached" in result.output

    def test_detach_removes_entry_and_file(self, tmp_project, monkeypatch_environment, tmp_path, monkeypatch):
        monkeypatch.setenv("IC_PROJECT", "testproject")
        node_file = _mknode(tmp_project)
        photo = tmp_path / "ips.txt"
        photo.write_text("10.0.0.1")
        runner.invoke(app, ["describe", "node", "attach", "vm:web-01", str(photo)])

        result = runner.invoke(app, ["describe", "node", "detach", "vm:web-01", "ips.txt"])

        assert result.exit_code == 0, result.output
        node = read_model(node_file, Node)
        assert node.attachments == []
        assert not (tmp_project.root / "attachments/vm/web-01/ips.txt").exists()


class TestDoctorAttachment:
    def test_missing_and_orphaned_attachments_flagged(self, tmp_project, monkeypatch_environment, tmp_environment):
        from infracontext.cli.doctor import run_doctor

        node_file = _mknode(tmp_project)
        node = read_model(node_file, Node)
        node = node.model_copy(update={"attachments": [
            Attachment(file="attachments/vm/web-01/gone.jpg", title="missing"),
        ]})
        write_model(node_file, node)
        orphan_dir = tmp_project.attachments_dir / "vm" / "web-01"
        orphan_dir.mkdir(parents=True)
        (orphan_dir / "orphan.txt").write_text("x")

        report = run_doctor(tmp_environment)

        msgs = [i.message for i in report.issues if i.category == "attachment"]
        assert any("missing attachment" in m for m in msgs)
        assert any("Orphaned attachment" in m for m in msgs)

    def test_escaping_attachment_path_is_an_error(self, tmp_project, monkeypatch_environment, tmp_environment):
        from infracontext.cli.doctor import run_doctor

        node_file = _mknode(tmp_project)
        node = read_model(node_file, Node)
        node = node.model_copy(update={"attachments": [
            Attachment(file="../../../etc/passwd", title="nope"),
        ]})
        write_model(node_file, node)

        report = run_doctor(tmp_environment)

        assert any(i.category == "attachment" and i.severity == "error"
                   for i in report.issues)
