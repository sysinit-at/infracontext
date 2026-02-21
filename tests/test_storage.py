"""Tests for infracontext.storage — YAML I/O, Pydantic round-trip, atomic writes."""

from pydantic import BaseModel

from infracontext.storage import (
    append_to_list,
    read_model,
    read_yaml,
    remove_from_list,
    update_yaml,
    write_model,
    write_yaml,
)


class SampleModel(BaseModel):
    name: str
    value: int = 0

    model_config = {"extra": "forbid"}


# ── read_yaml ─────────────────────────────────────────────────────


class TestReadYaml:
    def test_nonexistent_returns_empty(self, tmp_path):
        assert read_yaml(tmp_path / "nope.yaml") == {}

    def test_empty_file_returns_empty(self, tmp_path):
        f = tmp_path / "empty.yaml"
        f.write_text("")
        assert read_yaml(f) == {}

    def test_round_trip(self, tmp_path):
        f = tmp_path / "data.yaml"
        write_yaml(f, {"key": "value", "num": 42})
        assert read_yaml(f) == {"key": "value", "num": 42}


# ── write_yaml ────────────────────────────────────────────────────


class TestWriteYaml:
    def test_header_comment_preserved(self, tmp_path):
        f = tmp_path / "commented.yaml"
        write_yaml(f, {"hello": "world"}, header_comment="This is a header")
        content = f.read_text()
        assert "This is a header" in content
        # Data is still readable
        assert read_yaml(f) == {"hello": "world"}

    def test_creates_parent_dirs(self, tmp_path):
        f = tmp_path / "deep" / "nested" / "file.yaml"
        write_yaml(f, {"ok": True})
        assert read_yaml(f) == {"ok": True}

    def test_no_leftover_tmp_files(self, tmp_path):
        f = tmp_path / "clean.yaml"
        write_yaml(f, {"data": 1})
        tmp_files = list(tmp_path.glob(".clean.yaml.*.tmp"))
        assert tmp_files == []


# ── read_model / write_model ──────────────────────────────────────


class TestModelRoundTrip:
    def test_round_trip(self, tmp_path):
        f = tmp_path / "model.yaml"
        m = SampleModel(name="test", value=99)
        write_model(f, m)
        loaded = read_model(f, SampleModel)
        assert loaded is not None
        assert loaded.name == "test"
        assert loaded.value == 99

    def test_none_on_missing(self, tmp_path):
        assert read_model(tmp_path / "missing.yaml", SampleModel) is None

    def test_none_on_empty(self, tmp_path):
        f = tmp_path / "empty.yaml"
        f.write_text("")
        assert read_model(f, SampleModel) is None


# ── update_yaml ───────────────────────────────────────────────────


class TestUpdateYaml:
    def test_updates_existing(self, tmp_path):
        f = tmp_path / "upd.yaml"
        write_yaml(f, {"count": 1})

        def inc(cm):
            cm["count"] = cm["count"] + 1

        assert update_yaml(f, inc) is True
        assert read_yaml(f)["count"] == 2

    def test_comment_preservation(self, tmp_path):
        f = tmp_path / "commented.yaml"
        # Write a file with a header comment
        write_yaml(f, {"key": "original"}, header_comment="Keep this comment")

        def change(cm):
            cm["key"] = "modified"

        update_yaml(f, change)
        content = f.read_text()
        assert "Keep this comment" in content
        assert read_yaml(f)["key"] == "modified"

    def test_create_if_missing_true(self, tmp_path):
        f = tmp_path / "new.yaml"

        def init(cm):
            cm["created"] = True

        assert update_yaml(f, init, create_if_missing=True) is True
        assert read_yaml(f)["created"] is True

    def test_create_if_missing_false(self, tmp_path):
        f = tmp_path / "nope.yaml"
        assert update_yaml(f, lambda cm: None, create_if_missing=False) is False
        assert not f.exists()


# ── append_to_list / remove_from_list ─────────────────────────────


class TestListOperations:
    def test_append(self, tmp_path):
        f = tmp_path / "list.yaml"
        append_to_list(f, "items", {"name": "first"})
        append_to_list(f, "items", {"name": "second"})
        data = read_yaml(f)
        assert len(data["items"]) == 2
        assert data["items"][0]["name"] == "first"
        assert data["items"][1]["name"] == "second"

    def test_remove_by_predicate(self, tmp_path):
        f = tmp_path / "list.yaml"
        append_to_list(f, "items", {"id": 1, "name": "keep"})
        append_to_list(f, "items", {"id": 2, "name": "remove"})
        append_to_list(f, "items", {"id": 3, "name": "keep-too"})

        removed = remove_from_list(f, "items", lambda d: d.get("id") == 2)
        assert removed is True
        data = read_yaml(f)
        assert len(data["items"]) == 2
        assert all(item["name"] != "remove" for item in data["items"])

    def test_remove_returns_false_when_nothing_removed(self, tmp_path):
        f = tmp_path / "list.yaml"
        append_to_list(f, "items", {"id": 1})
        removed = remove_from_list(f, "items", lambda d: d.get("id") == 999)
        assert removed is False
