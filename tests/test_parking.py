"""Tests for oversized-output parking (``infracontext.parking``).

Every test isolates the scratch directory via ``IC_SCRATCH_DIR`` so nothing
touches the real per-user cache, and pins the threshold via
``IC_PARK_THRESHOLD`` where size behavior matters.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from infracontext.parking import (
    DEFAULT_PARK_THRESHOLD_BYTES,
    MAX_EXTRACT_BYTES,
    ParkingError,
    get_parked,
    grep_parked,
    maybe_park,
    park_threshold,
    schema_parked,
    scratch_dir,
    slice_parked,
)


@pytest.fixture(autouse=True)
def isolated_scratch(tmp_path, monkeypatch):
    scratch = tmp_path / "parked"
    monkeypatch.setenv("IC_SCRATCH_DIR", str(scratch))
    return scratch


@pytest.fixture
def low_threshold(monkeypatch):
    monkeypatch.setenv("IC_PARK_THRESHOLD", "100")


def _park(data, label="test"):
    pointer = maybe_park(data, label=label)
    assert isinstance(pointer, dict) and pointer.get("_parked") is True
    return pointer


def _big_logs(n=50):
    return {"logs": [{"timestamp": f"2026-07-15T10:{i:02d}", "line": f"error in worker {i}: " + "x" * 40} for i in range(n)]}


class TestThresholdAndConfig:
    def test_default_threshold(self, monkeypatch):
        monkeypatch.delenv("IC_PARK_THRESHOLD", raising=False)
        assert park_threshold() == DEFAULT_PARK_THRESHOLD_BYTES

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("IC_PARK_THRESHOLD", "1234")
        assert park_threshold() == 1234

    def test_invalid_or_nonpositive_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("IC_PARK_THRESHOLD", "banana")
        assert park_threshold() == DEFAULT_PARK_THRESHOLD_BYTES
        monkeypatch.setenv("IC_PARK_THRESHOLD", "0")
        assert park_threshold() == DEFAULT_PARK_THRESHOLD_BYTES

    def test_scratch_dir_env_override(self, isolated_scratch):
        assert scratch_dir() == isolated_scratch
        assert isolated_scratch.is_dir()


class TestMaybePark:
    def test_small_payload_passes_through(self):
        data = {"summary": {"ok": 3}}
        assert maybe_park(data, label="small") is data

    def test_large_payload_parked(self, low_threshold, isolated_scratch):
        data = _big_logs()
        pointer = _park(data)

        assert pointer["file"].startswith("test-")
        assert pointer["file"].endswith(".json")
        assert pointer["bytes"] > 100
        assert pointer["lines"] > 1
        assert "preview" in pointer and "next" in pointer
        # Every usage hint names the actual file so it is copy-pasteable.
        assert all(pointer["file"] in hint for hint in pointer["next"])

        parked_file = isolated_scratch / pointer["file"]
        assert json.loads(parked_file.read_text(encoding="utf-8")) == data

    def test_content_addressed_and_idempotent(self, low_threshold, isolated_scratch):
        data = _big_logs()
        first = _park(data)
        second = _park(data)
        assert first["file"] == second["file"]
        assert len(list(isolated_scratch.glob("*.json"))) == 1

    def test_label_sanitized(self, low_threshold):
        pointer = _park(_big_logs(), label="vm:web-01/Loki (recent)")
        name = pointer["file"]
        assert "/" not in name and ":" not in name and " " not in name

    def test_unwritable_scratch_degrades_to_passthrough(self, low_threshold, tmp_path, monkeypatch):
        # A *file* where the scratch dir should be makes mkdir fail; parking
        # must fall back to returning the payload rather than failing triage.
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("", encoding="utf-8")
        monkeypatch.setenv("IC_SCRATCH_DIR", str(blocker))
        data = _big_logs()
        assert maybe_park(data, label="x") is data

    def test_old_files_pruned_on_park(self, low_threshold, isolated_scratch):
        isolated_scratch.mkdir(parents=True, exist_ok=True)
        old = time.time() - 8 * 24 * 3600
        stale = isolated_scratch / "stale-000000000000.json"
        stale.write_text("{}", encoding="utf-8")
        leftover = isolated_scratch / "tmpcrashed.tmp"  # crashed atomic write
        leftover.write_text("", encoding="utf-8")
        for f in (stale, leftover):
            os.utime(f, (old, old))

        _park(_big_logs(), label="fresh")
        assert not stale.exists()
        assert not leftover.exists()

    def test_reuse_refreshes_mtime_so_prune_spares_it(self, low_threshold, isolated_scratch):
        # A pointer handed out seconds ago must never dangle: re-parking
        # existing content refreshes the retention clock before the next
        # opportunistic prune runs.
        data = _big_logs()
        first = _park(data)
        parked_file = isolated_scratch / first["file"]
        old = time.time() - 8 * 24 * 3600
        os.utime(parked_file, (old, old))

        second = _park(data)  # reuse branch
        assert second["file"] == first["file"]
        _park({"other": [{"x": "y" * 60} for _ in range(20)]}, label="other")  # triggers prune
        assert parked_file.exists()

    def test_scratch_permissions_user_only(self, low_threshold, isolated_scratch):
        # Parked payloads may contain secrets from logs.
        pointer = _park(_big_logs())
        assert isolated_scratch.stat().st_mode & 0o777 == 0o700
        assert (isolated_scratch / pointer["file"]).stat().st_mode & 0o777 == 0o600


class TestContainmentGuard:
    @pytest.mark.parametrize(
        "ref",
        [
            "../escape.json",
            "a/b.json",
            "..\\win.json",
            ".hidden.json",
            "",
            " padded.json ",
            "/etc/passwd",
            "no-json-suffix",
            "nul\x00byte.json",
        ],
    )
    def test_rejects_non_bare_references(self, ref):
        with pytest.raises(ParkingError):
            schema_parked(ref)

    def test_missing_file_mentions_pruning(self):
        with pytest.raises(ParkingError) as exc:
            schema_parked("gone-abcdef123456.json")
        assert "pruned" in str(exc.value)

    def test_symlink_escape_rejected(self, isolated_scratch, tmp_path):
        outside = tmp_path / "outside.json"
        outside.write_text('{"secret": true}', encoding="utf-8")
        isolated_scratch.mkdir(parents=True, exist_ok=True)
        link = isolated_scratch / "link.json"
        link.symlink_to(outside)
        with pytest.raises(ParkingError):
            schema_parked("link.json")


class TestReadOps:
    @pytest.fixture
    def parked(self, low_threshold):
        return _park(_big_logs())["file"]

    def test_schema_outline(self, parked):
        result = schema_parked(parked)
        assert result["file"] == parked
        assert result["lines"] > 1
        assert result["schema"]["logs"]["__array__"] == 50
        assert "timestamp" in result["schema"]["logs"]["sample"]

    def test_grep_matches_with_context(self, parked):
        result = grep_parked(parked, r"worker 7:", context=1)
        assert result["total_matches"] == 1
        assert result["truncated"] is False
        match = result["matches"][0]
        # 1-based line number, context line above and below included.
        assert f"{match['line']}:" in match["excerpt"]
        assert match["excerpt"].count("\n") == 2

    def test_grep_caps_matches(self, parked):
        result = grep_parked(parked, "error", max_matches=5)
        assert result["returned"] == 5
        assert result["total_matches"] == 50
        assert result["truncated"] is True

    def test_grep_rejects_bad_regex_and_empty_pattern(self, parked):
        with pytest.raises(ParkingError):
            grep_parked(parked, "(unclosed")
        with pytest.raises(ParkingError):
            grep_parked(parked, "")

    def test_slice_returns_numbered_range(self, parked):
        result = slice_parked(parked, 1, 3)
        assert result["start"] == 1 and result["end"] == 3
        lines = result["content"].splitlines()
        assert lines[0].startswith("1: ")
        assert lines[2].startswith("3: ")

    def test_slice_clamps_to_file_end(self, parked):
        total = slice_parked(parked, 1, 1)["total_lines"]
        result = slice_parked(parked, total, total + 100)
        assert result["end"] == total

    def test_slice_line_cap_sets_truncated(self, low_threshold):
        # Requesting more than MAX_SLICE_LINES must be flagged, not silently
        # clamped -- agents rely on `truncated` to know they got less.
        parked = _park({"logs": [{"line": f"l{i}"} for i in range(300)]})["file"]
        total = slice_parked(parked, 1, 1)["total_lines"]
        assert total > 400
        result = slice_parked(parked, 1, total)
        assert result["end"] == 400
        assert result["truncated"] is True

    def test_grep_redos_pattern_aborted_by_engine_timeout(self, low_threshold, monkeypatch):
        # A genuinely catastrophic pattern against a long line: the regex
        # engine's internal deadline must abort it. (Stdlib re would hold the
        # GIL until the heat death of the universe here -- that is why the
        # implementation uses mrab-regex.)
        import infracontext.parking as parking_mod

        monkeypatch.setattr(parking_mod, "GREP_TIMEOUT_SECONDS", 0.2)
        parked = _park({"blob": "a" * 5_000})["file"]
        start = time.monotonic()
        with pytest.raises(ParkingError) as exc:
            grep_parked(parked, r"(a+)+c")
        assert "timed out" in str(exc.value)
        # The abort must come from the engine deadline, not from finishing the
        # exponential scan; allow generous slack for slow CI.
        assert time.monotonic() - start < 2.0

    def test_slice_rejects_bad_ranges(self, parked):
        with pytest.raises(ParkingError):
            slice_parked(parked, 0, 5)
        with pytest.raises(ParkingError):
            slice_parked(parked, 5, 4)
        with pytest.raises(ParkingError):
            slice_parked(parked, 10_000, 10_001)

    def test_get_walks_keys_and_indices(self, parked):
        result = get_parked(parked, "logs[3].line")
        assert result["type"] == "str"
        assert "worker 3" in result["value"]

    def test_get_windows_arrays(self, parked):
        result = get_parked(parked, "logs", offset=10, limit=2)
        assert result["length"] == 50
        assert result["returned"] == 2
        assert result["value"][0]["line"].startswith("error in worker 10")

    def test_get_missing_key_lists_available(self, parked):
        with pytest.raises(ParkingError) as exc:
            get_parked(parked, "nope")
        assert "logs" in str(exc.value)

    def test_get_index_out_of_range(self, parked):
        with pytest.raises(ParkingError) as exc:
            get_parked(parked, "logs[99]")
        assert "50" in str(exc.value)

    def test_get_malformed_path(self, parked):
        with pytest.raises(ParkingError):
            get_parked(parked, "logs[abc]")

    def test_get_windows_large_strings(self, low_threshold):
        parked = _park({"blob": "a" * 10_000})["file"]
        result = get_parked(parked, "blob", offset=100, limit=50)
        assert result["length"] == 10_000
        assert result["returned"] == 50
        assert result["value"] == "a" * 50

    def test_schema_sizes_match_pointer_and_disk(self, low_threshold, isolated_scratch):
        # One file, one set of numbers: pointer, schema, and disk must agree
        # so agents can budget slice/grep calls against real sizes.
        pointer = _park(_big_logs())
        schema = schema_parked(pointer["file"])
        on_disk = (isolated_scratch / pointer["file"]).stat().st_size
        assert schema["bytes"] == pointer["bytes"] == on_disk
        assert schema["lines"] == pointer["lines"]


class TestExtractionCap:
    def test_oversized_dict_value_raises_with_structure(self, low_threshold):
        big = {"wrap": {f"key{i}": "v" * 200 for i in range(200)}}
        parked = _park(big)["file"]
        with pytest.raises(ParkingError) as exc:
            get_parked(parked, "wrap")
        assert str(MAX_EXTRACT_BYTES) in str(exc.value)

    def test_get_string_window_honors_cap(self, low_threshold):
        # A huge explicit limit must not bypass the per-call cap.
        parked = _park({"blob": "x" * 100_000})["file"]
        result = get_parked(parked, "blob", limit=100_000)
        assert len(json.dumps(result).encode()) <= MAX_EXTRACT_BYTES
        assert 0 < result["returned"] < result["length"]

    def test_get_array_window_shrinks_to_fit(self, low_threshold):
        big = {"items": [{"payload": "v" * 500} for _ in range(100)]}
        parked = _park(big)["file"]
        result = get_parked(parked, "items")
        assert len(json.dumps(result).encode()) <= MAX_EXTRACT_BYTES
        assert 0 < result["returned"] < result["length"]

    def test_get_single_oversized_element_advises_deeper_path(self, low_threshold):
        big = {"items": [{"payload": "v" * 20_000}]}
        parked = _park(big)["file"]
        with pytest.raises(ParkingError) as exc:
            get_parked(parked, "items")
        assert "deeper path" in str(exc.value)

    def test_grep_result_fits_cap(self, low_threshold):
        # Many matches with wide context must still fit the per-call cap.
        big = {"logs": [{"line": "error " + "x" * 400} for _ in range(200)]}
        parked = _park(big)["file"]
        result = grep_parked(parked, "error", context=10, max_matches=50)
        assert len(json.dumps(result).encode()) <= MAX_EXTRACT_BYTES
        assert result["returned"] >= 1

    def test_slice_result_fits_cap(self, low_threshold):
        big = {"logs": [{"line": "y" * 490} for _ in range(400)]}
        parked = _park(big)["file"]
        result = slice_parked(parked, 1, 400)
        assert len(json.dumps(result).encode()) <= MAX_EXTRACT_BYTES
        assert result["end"] < 400
