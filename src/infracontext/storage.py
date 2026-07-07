"""YAML storage with comment preservation using ruamel.yaml."""

import logging
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


class StorageError(Exception):
    """Raised when a YAML file cannot be read or parsed."""


# Configure ruamel.yaml for round-trip (comment-preserving) mode.
# Used for writes (and for update_yaml, which must preserve comments).
_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.default_flow_style = False
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.width = 120

# Fast loader for pure reads. Reads never need comment preservation, so we use
# PyYAML's C-accelerated safe loader (~8x faster than ruamel's round-trip loader
# on typical node files) -- decisive for read-heavy commands (load_graph, list,
# find, doctor, syncs). Both loaders resolve YAML timestamps to datetime.date/
# datetime and share identical scalar type semantics, so the model layer is
# unaffected. CSafeLoader is C-backed; fall back to the pure-Python SafeLoader
# on the rare build that lacks the extension.
_SafeLoader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Serialize writes to ``path`` using a per-file advisory lock.

    Creates a sibling ``.<name>.lock`` file (mode 0600) and takes an exclusive
    ``flock`` on it. On platforms without ``fcntl`` (non-POSIX) the lock is a
    no-op -- correctness still holds for the common single-process case, and
    the atomic-rename writers below protect against partial reads.

    Shared across the YAML storage layer and the credential metadata index so
    both serialize their read-modify-write cycles the same way.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / f".{path.name}.lock"
    fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _atomic_dump(path: Path, data: CommentedMap | CommentedSeq) -> None:
    """Atomically replace target file with serialized YAML data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            _yaml.dump(data, f)
        os.replace(tmp_path, path)
    finally:
        with suppress(FileNotFoundError):
            tmp_path.unlink()


def _to_commented(data: dict | list) -> CommentedMap | CommentedSeq:
    """Convert a plain dict/list to ruamel.yaml commented structure."""
    if isinstance(data, dict):
        cm = CommentedMap()
        for k, v in data.items():
            if isinstance(v, dict | list):
                cm[k] = _to_commented(v)
            else:
                cm[k] = v
        return cm
    elif isinstance(data, list):
        cs = CommentedSeq()
        for item in data:
            if isinstance(item, dict | list):
                cs.append(_to_commented(item))
            else:
                cs.append(item)
        return cs
    return data


def read_yaml(path: Path) -> dict:
    """Read a YAML mapping from a file.

    Returns an empty dict if the file doesn't exist or is empty. A top-level
    node that isn't a mapping (e.g. a stray list, or a bare scalar from a
    hand-edit) is treated as empty with a warning rather than raising
    ``ValueError`` from ``dict(...)`` -- a single malformed file shouldn't
    abort an entire graph/list load.

    Raises:
        StorageError: If the file exists but contains malformed YAML.
    """
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.load(f, Loader=_SafeLoader)
    except yaml.YAMLError as e:
        raise StorageError(f"Failed to parse YAML in {path}: {e}") from e

    if data is None:
        return {}
    if not isinstance(data, dict):
        log.warning(
            "%s: top-level YAML is a %s, expected a mapping -- treating as empty",
            path,
            type(data).__name__,
        )
        return {}
    return data


def write_yaml(path: Path, data: dict, *, header_comment: str | None = None) -> None:
    """Write data to a YAML file with optional header comment."""
    cm = _to_commented(data)
    if header_comment and isinstance(cm, CommentedMap):
        cm.yaml_set_start_comment(header_comment)
    with file_lock(path):
        _atomic_dump(path, cm)


def _strip_extra_fields[T: BaseModel](data: dict, model_cls: type[T], path: Path) -> dict:
    """Remove fields not recognised by the model and log warnings.

    This handles schema drift gracefully: files written by an older (or newer)
    version of infracontext won't crash on load -- unknown fields are dropped
    with a warning so the user knows to update the file.
    """
    known_fields = set(model_cls.model_fields)
    extra_keys = set(data) - known_fields
    if not extra_keys:
        return data
    log.warning(
        "%s: dropping unknown fields %s -- file may need updating to current schema",
        path,
        sorted(extra_keys),
    )
    return {k: v for k, v in data.items() if k in known_fields}


def read_model[T: BaseModel](path: Path, model_cls: type[T]) -> T | None:
    """Read a YAML file and parse it into a Pydantic model.

    Unknown top-level fields are stripped with a warning rather than
    raising a ValidationError, so that files from older schema versions
    degrade gracefully.
    """
    data = read_yaml(path)
    if not data:
        return None
    # If the model forbids extras, pre-strip unknown fields to avoid a hard crash.
    model_extra = model_cls.model_config.get("extra", "ignore")
    if model_extra == "forbid":
        data = _strip_extra_fields(data, model_cls, path)
    try:
        return model_cls.model_validate(data)
    except ValidationError:
        # If validation still fails after stripping, let it propagate --
        # it's a real schema error, not just stale fields.
        raise


def write_model(path: Path, model: BaseModel, *, header_comment: str | None = None) -> None:
    """Write a Pydantic model to a YAML file."""
    data = model.model_dump(mode="json", exclude_none=True)
    write_yaml(path, data, header_comment=header_comment)


def update_yaml(
    path: Path,
    updater: Callable[[CommentedMap], None],
    *,
    create_if_missing: bool = False,
) -> bool:
    """Update a YAML file in-place, preserving comments.

    Args:
        path: Path to the YAML file
        updater: Function that modifies the CommentedMap in place
        create_if_missing: If True, create file with empty dict if missing

    Returns:
        True if file was updated, False if file didn't exist and create_if_missing is False
    """
    with file_lock(path):
        if not path.exists():
            if create_if_missing:
                cm = CommentedMap()
                updater(cm)
                _atomic_dump(path, cm)
                return True
            return False

        with path.open("r", encoding="utf-8") as f:
            cm = _yaml.load(f)
            if cm is None:
                cm = CommentedMap()

        updater(cm)
        _atomic_dump(path, cm)
        return True


def append_to_list(
    path: Path,
    key: str,
    item: dict,
    *,
    create_if_missing: bool = True,
) -> None:
    """Append an item to a list in a YAML file."""

    def _append(cm: CommentedMap) -> None:
        if key not in cm:
            cm[key] = CommentedSeq()
        cm[key].append(_to_commented(item))

    update_yaml(path, _append, create_if_missing=create_if_missing)


def remove_from_list(
    path: Path,
    key: str,
    predicate: Callable[[dict], bool],
) -> bool:
    """Remove items matching predicate from a list in a YAML file.

    Returns True if any items were removed.
    """
    removed = False

    def _remove(cm: CommentedMap) -> None:
        nonlocal removed
        if key not in cm:
            return
        original_len = len(cm[key])
        cm[key] = CommentedSeq([item for item in cm[key] if not predicate(dict(item))])
        removed = len(cm[key]) < original_len

    update_yaml(path, _remove)
    return removed
