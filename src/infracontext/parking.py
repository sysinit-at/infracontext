"""Park oversized query payloads on disk; explore them with bounded read ops.

Observability sources (Loki logs, CheckMK service lists, SOS findings) can
return payloads far larger than an LLM's context can absorb. Instead of
dumping them wholesale, the MCP server parks any per-source payload above a
byte threshold in a per-user scratch directory and returns a compact pointer.
The agent then pulls only the slices it needs through four bounded read
operations: :func:`schema_parked`, :func:`grep_parked`, :func:`slice_parked`,
and :func:`get_parked`. The pattern is borrowed from AURA's scratchpad
(github.com/mezmo/aura); the per-call extraction cap and the two-stage path
containment guard follow its design.

Scope: parking applies only on the MCP path (``ic mcp serve``). CLI ``--json``
output stays complete -- scripts piping to ``jq`` expect the full document.

Files are content-addressed (``<label>-<sha256[:12]>.json``), so re-running
the same query re-uses the same file, and pruned opportunistically after
``RETENTION_DAYS`` (reuse refreshes the mtime, so a pointer just handed out
never dangles). Payloads may contain secrets from logs, so the scratch
directory is created ``0o700`` and files ``0o600``. The scratch directory
lives outside the environment repo (``$IC_SCRATCH_DIR`` >
``$XDG_CACHE_HOME/infracontext/parked`` > ``~/.cache/infracontext/parked``)
so nothing needs gitignoring.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

PARK_THRESHOLD_ENV = "IC_PARK_THRESHOLD"
SCRATCH_DIR_ENV = "IC_SCRATCH_DIR"

# ~5k tokens of pretty-printed JSON -- AURA's default interception threshold.
DEFAULT_PARK_THRESHOLD_BYTES = 20_000
# Hard ceiling on what any single read op may return to the model.
MAX_EXTRACT_BYTES = 16_384
MAX_PATTERN_LEN = 512
MAX_GREP_MATCHES = 50
MAX_SLICE_LINES = 400
# Long string values produce long lines even in indent=2 JSON; cap each
# emitted line so one log line can't blow the extraction cap on its own.
MAX_LINE_CHARS = 500
# Regex searches scan at most this many chars per line (memory/normal-case
# bound) and the whole scan runs under an engine-enforced deadline (see
# grep_parked -- input bounds alone cannot stop exponential backtracking).
MAX_SEARCH_LINE_CHARS = 20_000
GREP_TIMEOUT_SECONDS = 5.0
RETENTION_DAYS = 7

_PREVIEW_KEYS = 20
_PREVIEW_STR = 80
_DEFAULT_STR_WINDOW = 4_000


class ParkingError(RuntimeError):
    """A parked-file operation could not complete (bad ref, bad args, too big)."""


# ── configuration ──────────────────────────────────────────────────


def park_threshold() -> int:
    """Byte threshold above which a payload is parked (env-overridable)."""
    raw = os.environ.get(PARK_THRESHOLD_ENV, "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_PARK_THRESHOLD_BYTES
    return value if value > 0 else DEFAULT_PARK_THRESHOLD_BYTES


def scratch_dir() -> Path:
    """Resolve (and create, user-only) the scratch directory for parked payloads."""
    if override := os.environ.get(SCRATCH_DIR_ENV):
        root = Path(override).expanduser()
    elif xdg := os.environ.get("XDG_CACHE_HOME"):
        root = Path(xdg).expanduser() / "infracontext" / "parked"
    else:
        root = Path.home() / ".cache" / "infracontext" / "parked"
    root.mkdir(parents=True, exist_ok=True)
    # Parked payloads may carry secrets from logs; keep the dir user-only.
    # Best-effort: an exotic filesystem without chmod must not break parking.
    with contextlib.suppress(OSError):
        root.chmod(0o700)
    return root


# ── parking ────────────────────────────────────────────────────────


def maybe_park(data: Any, *, label: str) -> Any:
    """Return ``data`` unchanged if small, else park it and return a pointer.

    The pointer is a dict marked with ``"_parked": True`` carrying the file
    reference, size, a shallow structure preview, and copy-pasteable usage
    hints for the read ops -- mirroring AURA's file pointer so the model knows
    exactly how to explore what it can't see.
    """
    payload = _serialize(data)
    size = len(payload.encode("utf-8"))
    if size <= park_threshold():
        return data

    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    name = f"{_sanitize_label(label)}-{digest}.json"
    try:
        path = scratch_dir() / name
        if path.exists():
            # Refresh the retention clock: a pointer we hand out now must not
            # be deleted by the next opportunistic prune just because the
            # content was first parked long ago.
            path.touch()
        else:
            _atomic_write(path, payload)
            _prune(path.parent)
    except OSError:
        # Parking is best-effort context hygiene: an unwritable scratch dir
        # must degrade to the old behavior (full payload), never fail triage.
        return data

    f = f'file="{name}"'
    return {
        "_parked": True,
        "file": name,
        "bytes": size,
        "lines": payload.count("\n") + 1,
        "note": (
            "Output too large for context; parked on disk. "
            "Pull only the slices you need with the parked_* tools."
        ),
        "preview": _outline(data, depth=2),
        "next": [
            f"parked_schema({f}) — structure outline",
            f'parked_grep({f}, pattern="error") — regex search with context lines',
            f"parked_slice({f}, start=1, end=50) — numbered line range",
            f'parked_get({f}, path="key.subkey[0]") — extract a nested value',
        ],
    }


def _serialize(data: Any) -> str:
    """Pretty-print a payload the way the CLI emits it (indent=2).

    The threshold is measured on this form because it is what would have
    landed in context; the same text is written to disk so grep/slice line
    numbers are stable and meaningful.
    """
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


def _sanitize_label(label: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", label.lower()).strip("-")
    return (cleaned or "payload")[:60]


def _atomic_write(path: Path, payload: str) -> None:
    """Write via temp file + rename so concurrent readers never see a torn file.

    Two ``ic mcp serve`` processes triaging the same node race on the same
    content-addressed name; both write identical bytes and ``os.replace`` is
    atomic on POSIX, so the last rename wins harmlessly. ``mkstemp`` creates
    the temp file ``0o600``, which the rename preserves.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _prune(root: Path) -> None:
    """Opportunistically delete parked files older than the retention window.

    Also sweeps ``*.tmp`` leftovers from crashed atomic writes.
    """
    cutoff = time.time() - RETENTION_DAYS * 24 * 3600
    for pattern in ("*.json", "*.tmp"):
        for f in root.glob(pattern):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:  # racing prune / permissions -- never fail a park
                pass


# ── containment guard ──────────────────────────────────────────────


def _resolve_parked(file: str) -> Path:
    """Resolve a file token strictly inside the scratch directory.

    Two-stage guard (after AURA's storage read guard): reject anything that
    is not a bare ``*.json`` filename lexically, then resolve symlinks and
    re-verify the real path still lives directly under the real scratch root.
    """
    if (
        not file
        or file != file.strip()
        or file.startswith(".")
        or "/" in file
        or "\\" in file
        or ".." in file
        or not file.endswith(".json")
        or any(ord(c) < 32 or ord(c) == 127 for c in file)
    ):
        raise ParkingError(
            f"Invalid parked-file reference {file!r}: expected a bare *.json "
            "filename as returned in a pointer's 'file' field."
        )
    try:
        root = scratch_dir().resolve()
        path = (root / file).resolve()
    except (OSError, ValueError) as err:  # unresolvable name (e.g. filesystem limits)
        raise ParkingError(f"Invalid parked-file reference {file!r}: {err}") from err
    if path.parent != root:
        raise ParkingError(f"Parked-file reference {file!r} escapes the scratch directory.")
    if not path.is_file():
        raise ParkingError(
            f"No parked file {file!r}. It may have been pruned "
            f"(retention: {RETENTION_DAYS} days) — re-run the original query."
        )
    return path


def _read_text(file: str) -> str:
    try:
        return _resolve_parked(file).read_text(encoding="utf-8")
    except OSError as err:
        raise ParkingError(f"Could not read parked file {file!r}: {err}") from err


def _load(file: str) -> Any:
    try:
        return json.loads(_read_text(file))
    except json.JSONDecodeError as err:
        raise ParkingError(f"Could not parse parked file {file!r}: {err}") from err


# ── read ops ───────────────────────────────────────────────────────


def schema_parked(file: str) -> dict:
    """Structure outline of a parked payload, deepest view that fits the cap."""
    text = _read_text(file)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        raise ParkingError(f"Could not parse parked file {file!r}: {err}") from err
    # Sizes describe the on-disk file -- the same numbers the pointer reported
    # and the same lines grep/slice address.
    base = {
        "file": file,
        "bytes": len(text.encode("utf-8")),
        "lines": text.count("\n") + 1,
    }
    for depth in range(5, 0, -1):
        result = {**base, "depth": depth, "schema": _outline(data, depth=depth)}
        if _encoded_size(result) <= MAX_EXTRACT_BYTES:
            return result
    # Even depth=1 is over the cap only for pathological key counts; the
    # outline itself caps keys per level, so this is effectively unreachable.
    return {**base, "depth": 0, "schema": _outline(data, depth=0)}


def _scan_lines(matcher: Any, lines: list[str], deadline: float) -> list[int]:
    """Return indices of lines matching ``matcher``, bounded by ``deadline``.

    ``matcher`` is a compiled ``regex`` pattern. Each per-line search gets the
    remaining budget as an engine-enforced timeout; ``TimeoutError``
    propagates to the caller. The deadline must live *inside* the engine:
    stdlib ``re`` holds the GIL for an entire uninterruptible match, so
    neither a watchdog thread nor a signal can cut short catastrophic
    backtracking on a single long line.
    """
    hits = []
    for i, line in enumerate(lines):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        if matcher.search(line, 0, min(len(line), MAX_SEARCH_LINE_CHARS), timeout=remaining):
            hits.append(i)
    return hits


def grep_parked(
    file: str, pattern: str, context: int = 2, max_matches: int = MAX_GREP_MATCHES
) -> dict:
    """Regex search over a parked file; bounded matches with context lines."""
    # mrab-regex, not stdlib re: its matching loop checks a caller-supplied
    # timeout, which is the only reliable defense against ReDoS (see
    # _scan_lines). Lazy import -- it ships with the 'mcp' extra, the only
    # install mode that reaches this code.
    try:
        import regex
    except ImportError:
        raise ParkingError(
            "parked_grep requires the 'regex' package (part of the 'mcp' extra: "
            "uv tool install '.[mcp]' / uv sync --extra mcp)."
        ) from None

    if not pattern:
        raise ParkingError("grep pattern must be non-empty.")
    if len(pattern) > MAX_PATTERN_LEN:
        raise ParkingError(f"grep pattern longer than {MAX_PATTERN_LEN} chars.")
    try:
        matcher = regex.compile(pattern)
    except regex.error as err:
        raise ParkingError(f"Invalid regex {pattern!r}: {err}") from err

    context = max(0, min(context, 10))
    max_matches = max(1, min(max_matches, MAX_GREP_MATCHES))
    lines = _read_text(file).splitlines()

    try:
        hits = _scan_lines(matcher, lines, time.monotonic() + GREP_TIMEOUT_SECONDS)
    except TimeoutError:
        raise ParkingError(
            f"Search for {pattern!r} timed out after {GREP_TIMEOUT_SECONDS:g}s — "
            "the regex engine aborted it (likely catastrophic backtracking). "
            "Simplify the pattern (avoid nested quantifiers) or use a literal string."
        ) from None

    matches = [
        {
            "line": i + 1,
            "excerpt": "\n".join(
                _numbered(lines, j) for j in range(max(0, i - context), min(len(lines), i + context + 1))
            ),
        }
        for i in hits[:max_matches]
    ]

    def _result(kept: list[dict]) -> dict:
        return {
            "file": file,
            "pattern": pattern,
            "total_matches": len(hits),
            "returned": len(kept),
            "truncated": len(kept) < len(hits),
            "matches": kept,
        }

    # Fit to the extraction cap by dropping trailing matches.
    while matches and _encoded_size(_result(matches)) > MAX_EXTRACT_BYTES:
        matches.pop()
    if not matches and hits:
        raise ParkingError(
            "Matches exist but even one excerpt exceeds the per-call cap; "
            "reduce context or use parked_slice on a narrower range."
        )
    return _result(matches)


def slice_parked(file: str, start: int, end: int) -> dict:
    """Numbered 1-based inclusive line range from a parked file, bounded."""
    if start < 1 or end < start:
        raise ParkingError(f"Invalid range {start}..{end}: need 1 <= start <= end.")
    lines = _read_text(file).splitlines()
    if start > len(lines):
        raise ParkingError(f"start={start} is past the end of the file ({len(lines)} lines).")

    # ``truncated`` must reflect what the caller asked for, so compare against
    # the request clamped only to the file -- not to the per-call line cap.
    requested_end = min(end, len(lines))
    end = min(requested_end, start + MAX_SLICE_LINES - 1)
    window = [_numbered(lines, i) for i in range(start - 1, end)]

    def _result(kept: list[str], actual_end: int) -> dict:
        return {
            "file": file,
            "start": start,
            "end": actual_end,
            "total_lines": len(lines),
            "truncated": actual_end < requested_end,
            "content": "\n".join(kept),
        }

    # Fit to the extraction cap by shrinking the window from the tail.
    while len(window) > 1 and _encoded_size(_result(window, start + len(window) - 1)) > MAX_EXTRACT_BYTES:
        window = window[: max(1, len(window) // 2)]
    return _result(window, start + len(window) - 1)


def get_parked(file: str, path: str, offset: int = 0, limit: int = 0) -> dict:
    """Extract a nested value from a parked JSON payload by dotted path.

    ``path`` supports dict keys and array indices: ``sources[2].data.logs``.
    String leaves are windowed by ``offset``/``limit`` characters and arrays
    by ``offset``/``limit`` elements; both windows shrink further if needed
    to honor the per-call cap (compare ``returned`` against ``length``).
    Dict/scalar values that exceed the cap fail with the value's structure so
    the caller can narrow the path.
    """
    data = _load(file)
    value = _walk(data, path)
    offset = max(0, offset)

    if isinstance(value, str):
        window = value[offset : offset + (limit or _DEFAULT_STR_WINDOW)]

        def _str_result(text: str) -> dict:
            return {
                "file": file,
                "path": path,
                "type": "str",
                "length": len(value),
                "offset": offset,
                "returned": len(text),
                "value": text,
            }

        # The cap applies here too: halve the window until the encoded result
        # fits (JSON escaping can inflate beyond the raw character count).
        while len(window) > 1 and _encoded_size(_str_result(window)) > MAX_EXTRACT_BYTES:
            window = window[: len(window) // 2]
        return _str_result(window)

    if isinstance(value, list):
        window = value[offset : offset + limit] if limit else value[offset:]

        def _list_result(items: list) -> dict:
            return {
                "file": file,
                "path": path,
                "type": "array",
                "length": len(value),
                "offset": offset,
                "returned": len(items),
                "value": items,
            }

        while len(window) > 1 and _encoded_size(_list_result(window)) > MAX_EXTRACT_BYTES:
            window = window[: max(1, len(window) // 2)]
        result = _list_result(window)
        if _encoded_size(result) <= MAX_EXTRACT_BYTES:
            return result
        raise ParkingError(
            f"Even a single element at {path!r} exceeds the "
            f"{MAX_EXTRACT_BYTES}-byte cap. Use a deeper path into one element."
        )

    result = {"file": file, "path": path, "type": type(value).__name__, "value": value}
    if _encoded_size(result) <= MAX_EXTRACT_BYTES:
        return result
    structure = json.dumps(_outline(value, depth=2), ensure_ascii=False, default=str)[:1_000]
    raise ParkingError(
        f"Value at {path!r} exceeds the {MAX_EXTRACT_BYTES}-byte cap. "
        f"Structure: {structure}. Narrow with a deeper path or use parked_grep."
    )


# ── helpers ────────────────────────────────────────────────────────


def _numbered(lines: list[str], index: int) -> str:
    text = lines[index]
    if len(text) > MAX_LINE_CHARS:
        text = text[:MAX_LINE_CHARS] + "…"
    return f"{index + 1}: {text}"


def _encoded_size(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))


def _outline(value: Any, depth: int) -> Any:
    """Compact recursive structure summary (keys, types, counts, samples)."""
    if isinstance(value, dict):
        if depth <= 0:
            return f"object({len(value)} keys)"
        out: dict[str, Any] = {}
        for i, (key, item) in enumerate(value.items()):
            if i >= _PREVIEW_KEYS:
                out["…"] = f"{len(value) - _PREVIEW_KEYS} more keys"
                break
            out[str(key)] = _outline(item, depth - 1)
        return out
    if isinstance(value, list):
        if not value:
            return "array(0)"
        if depth <= 0:
            return f"array({len(value)})"
        return {"__array__": len(value), "sample": _outline(value[0], depth - 1)}
    if isinstance(value, str):
        if len(value) <= _PREVIEW_STR:
            return value
        return f"str({len(value)}): {value[:_PREVIEW_STR]}…"
    return value


# Dict keys may contain anything except the path metacharacters themselves.
_PATH_TOKEN_RE = re.compile(r"\.?([^.\[\]]+)|\[(\d+)\]")


def _parse_path(path: str) -> list[str | int]:
    if not path or not path.strip():
        raise ParkingError("path must be non-empty, e.g. 'logs[0].line'.")
    tokens: list[str | int] = []
    pos = 0
    while pos < len(path):
        match = _PATH_TOKEN_RE.match(path, pos)
        if not match:
            raise ParkingError(
                f"Malformed path {path!r} at offset {pos}: expected 'key', '.key', or '[index]'."
            )
        tokens.append(int(match.group(2)) if match.group(2) is not None else match.group(1))
        pos = match.end()
    return tokens


def _walk(data: Any, path: str) -> Any:
    value = data
    consumed = ""
    for token in _parse_path(path):
        where = f"at {consumed!r}" if consumed else "at the top level"
        if isinstance(token, int):
            if not isinstance(value, list):
                raise ParkingError(f"Cannot index [{token}] {where}: value is {type(value).__name__}.")
            if not -len(value) <= token < len(value):
                raise ParkingError(f"Index [{token}] out of range {where}: array has {len(value)} element(s).")
            value = value[token]
            consumed += f"[{token}]"
        else:
            if not isinstance(value, dict):
                raise ParkingError(f"Cannot key {token!r} {where}: value is {type(value).__name__}.")
            if token not in value:
                available = ", ".join(list(value)[:20])
                raise ParkingError(f"No key {token!r} {where}. Available keys: {available}")
            value = value[token]
            consumed = f"{consumed}.{token}" if consumed else token
    return value
