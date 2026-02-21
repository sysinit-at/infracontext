"""YAML storage with comment preservation using ruamel.yaml."""

import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from pydantic import BaseModel
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

# Configure ruamel.yaml for round-trip (comment-preserving) mode
_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.default_flow_style = False
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.width = 120


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Serialize writes using a per-file lock."""
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
    """Read a YAML file, returning empty dict if file doesn't exist."""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = _yaml.load(f)
        return dict(data) if data else {}


def write_yaml(path: Path, data: dict, *, header_comment: str | None = None) -> None:
    """Write data to a YAML file with optional header comment."""
    cm = _to_commented(data)
    if header_comment and isinstance(cm, CommentedMap):
        cm.yaml_set_start_comment(header_comment)
    with _file_lock(path):
        _atomic_dump(path, cm)


def read_model[T: BaseModel](path: Path, model_cls: type[T]) -> T | None:
    """Read a YAML file and parse it into a Pydantic model."""
    data = read_yaml(path)
    if not data:
        return None
    return model_cls.model_validate(data)


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
    with _file_lock(path):
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
