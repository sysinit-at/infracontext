"""YAML storage with comment preservation using ruamel.yaml."""

import logging
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from types import UnionType
from typing import Union, get_args, get_origin

import yaml
from pydantic import BaseModel
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
# datetime. CSafeLoader is C-backed; fall back to the pure-Python SafeLoader
# on the rare build that lacks the extension.
_PyYamlSafeLoader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class _Yaml12SafeLoader(_PyYamlSafeLoader):  # type: ignore[misc, valid-type]
    """PyYAML safe loader with YAML 1.2 boolean semantics.

    The write path is ruamel.yaml in YAML 1.2 mode, where the plain words
    ``yes``/``no``/``on``/``off`` are ordinary strings and therefore emitted
    *unquoted*. PyYAML implements YAML 1.1, where those same words are
    booleans -- so a string ``"yes"`` written by us would flip to ``True`` on
    the next read. That silently corrupts attribute values and makes syncs
    see phantom changes on every run. Dropping the 1.1-only boolean forms
    from the resolver aligns both directions; ``true``/``false`` (the 1.2
    forms, and what ruamel emits for real booleans) still resolve as bools.

    Implicit resolvers are keyed by first character: removing the bool entry
    under y/Y/n/N/o/O kills yes/no/on/off resolution while t/T/f/F keep it.
    """

    yaml_implicit_resolvers = {
        first_char: [
            (tag, regexp)
            for tag, regexp in resolvers
            if tag != "tag:yaml.org,2002:bool" or first_char in "tTfF"
        ]
        for first_char, resolvers in _PyYamlSafeLoader.yaml_implicit_resolvers.items()
    }


_SafeLoader = _Yaml12SafeLoader


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


# A path to a stripped unknown field: container steps (field names, list
# indices, dict keys) leading to the model that owned it, plus key and value.
type _Trail = tuple[str | int, ...]
type StrippedField = tuple[_Trail, str, object]

# Attribute (set via object.__setattr__, so it lives in the instance __dict__
# without registering as a pydantic field) that carries unknown fields stripped
# by read_model on the model instance that owned them. write_model merges them
# back so read -> edit -> write never silently deletes fields written by a
# newer infracontext version. pydantic's __eq__ compares declared fields only,
# so the stash never affects model equality.
_UNKNOWN_FIELDS_ATTR = "_ic_unknown_fields"


def _unwrap_optional(annotation: object) -> object:
    """Reduce ``X | None`` to ``X``; leave everything else untouched."""
    if get_origin(annotation) in (Union, UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _is_model(annotation: object) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def strip_unknown_fields(
    data: dict,
    model_cls: type[BaseModel],
    *,
    source: Path | str,
    stripped: list[StrippedField] | None = None,
    warn: bool = True,
    _trail: _Trail = (),
) -> dict:
    """Return a copy of ``data`` without fields unknown to ``model_cls``.

    Recurses into nested models (``BaseModel`` fields, ``list[Model]``,
    ``dict[str, Model]``) so schema drift anywhere in the file degrades
    gracefully instead of hard-failing validation of ``extra="forbid"``
    models. Only levels whose model forbids extras are stripped. Each dropped
    field is logged (unless ``warn`` is False) and, when ``stripped`` is
    given, recorded as ``(trail, key, value)`` for round-trip preservation.
    """
    fields = model_cls.model_fields
    forbids_extra = model_cls.model_config.get("extra", "ignore") == "forbid"
    out: dict = {}
    for key, value in data.items():
        if key not in fields:
            if not forbids_extra:
                out[key] = value
                continue
            if warn:
                dotted = ".".join(str(part) for part in (*_trail, key))
                log.warning(
                    "%s: dropping unknown field '%s' -- file may be from a newer schema "
                    "(the value is preserved if the file is rewritten)",
                    source,
                    dotted,
                )
            if stripped is not None:
                stripped.append((_trail, key, value))
            continue

        annotation = _unwrap_optional(fields[key].annotation)
        origin = get_origin(annotation)
        if _is_model(annotation) and isinstance(value, dict):
            value = strip_unknown_fields(
                value, annotation, source=source, stripped=stripped, warn=warn, _trail=(*_trail, key)
            )
        elif origin is list and isinstance(value, list):
            args = get_args(annotation)
            item_cls = _unwrap_optional(args[0]) if args else None
            if _is_model(item_cls):
                value = [
                    strip_unknown_fields(
                        item, item_cls, source=source, stripped=stripped, warn=warn, _trail=(*_trail, key, i)
                    )
                    if isinstance(item, dict)
                    else item
                    for i, item in enumerate(value)
                ]
        elif origin is dict and isinstance(value, dict):
            args = get_args(annotation)
            value_cls = _unwrap_optional(args[1]) if len(args) == 2 else None
            if _is_model(value_cls):
                value = {
                    k: strip_unknown_fields(
                        v, value_cls, source=source, stripped=stripped, warn=warn, _trail=(*_trail, key, k)
                    )
                    if isinstance(v, dict)
                    else v
                    for k, v in value.items()
                }
        out[key] = value
    return out


def _attach_stripped_fields(model: BaseModel, stripped: list[StrippedField]) -> None:
    """Stash each stripped field on the nested model instance that owned it.

    Anchoring to the owning *instance* (rather than a positional path) means
    the stash follows the item through list mutations: removing or appending
    siblings, or filtering a list, keeps each surviving item's unknown fields
    with it. Replaced items drop their stash, which is the correct outcome.
    """
    for trail, key, value in stripped:
        target: object = model
        for step in trail:
            if isinstance(target, BaseModel):
                target = getattr(target, str(step), None)
            elif isinstance(target, list) and isinstance(step, int) and step < len(target):
                target = target[step]
            elif isinstance(target, dict):
                target = target.get(step)
            else:
                target = None
            if target is None:
                break
        if isinstance(target, BaseModel):
            extras = getattr(target, _UNKNOWN_FIELDS_ATTR, None)
            if extras is None:
                extras = {}
                object.__setattr__(target, _UNKNOWN_FIELDS_ATTR, extras)
            extras[key] = value


def _merge_unknown_fields(model: BaseModel, dumped: dict) -> None:
    """Merge stashed unknown fields back into a ``model_dump`` result.

    Walks the model tree and the dumped dict in parallel; both have identical
    shape (``exclude_none`` drops keys, never list items), so stashes land on
    the dump of exactly the instance that carries them.
    """
    extras = getattr(model, _UNKNOWN_FIELDS_ATTR, None)
    if extras:
        for key, value in extras.items():
            dumped.setdefault(key, value)
    for name in type(model).model_fields:
        value = getattr(model, name, None)
        sub = dumped.get(name)
        if isinstance(value, BaseModel) and isinstance(sub, dict):
            _merge_unknown_fields(value, sub)
        elif isinstance(value, list) and isinstance(sub, list) and len(value) == len(sub):
            for item, dumped_item in zip(value, sub, strict=True):
                if isinstance(item, BaseModel) and isinstance(dumped_item, dict):
                    _merge_unknown_fields(item, dumped_item)
        elif isinstance(value, dict) and isinstance(sub, dict):
            for k, item in value.items():
                dumped_item = sub.get(k)
                if isinstance(item, BaseModel) and isinstance(dumped_item, dict):
                    _merge_unknown_fields(item, dumped_item)


def read_model[T: BaseModel](path: Path, model_cls: type[T]) -> T | None:
    """Read a YAML file and parse it into a Pydantic model.

    Unknown fields (top-level and nested) are stripped with a warning rather
    than raising a ValidationError, so files written by a newer (or older)
    infracontext version degrade gracefully. Stripped fields are stashed on
    the model and restored by :func:`write_model`, so a read -> edit -> write
    cycle never deletes them. Validation errors other than unknown fields
    still propagate -- they're real schema errors, not drift.
    """
    data = read_yaml(path)
    if not data:
        return None
    stripped: list[StrippedField] = []
    data = strip_unknown_fields(data, model_cls, source=path, stripped=stripped)
    model = model_cls.model_validate(data)
    if stripped:
        _attach_stripped_fields(model, stripped)
    return model


def write_model(path: Path, model: BaseModel, *, header_comment: str | None = None) -> None:
    """Write a Pydantic model to a YAML file.

    Unknown fields stripped by :func:`read_model` are merged back into the
    output so they survive edit round-trips (see ``_UNKNOWN_FIELDS_ATTR``).
    """
    data = model.model_dump(mode="json", exclude_none=True)
    _merge_unknown_fields(model, data)
    write_yaml(path, data, header_comment=header_comment)


def update_yaml(
    path: Path,
    updater: Callable[[CommentedMap], object],
    *,
    create_if_missing: bool = False,
) -> bool:
    """Update a YAML file in-place, preserving comments.

    Args:
        path: Path to the YAML file
        updater: Function that modifies the CommentedMap in place. Returning
            ``False`` (exactly) vetoes the write: the file is left untouched
            — not even reformatted. Any other return value (including the
            usual ``None``) writes as before.
        create_if_missing: If True, create file with empty dict if missing

    Returns:
        True if file was updated, False if the file didn't exist (and
        ``create_if_missing`` is False) or the updater vetoed the write
    """
    with file_lock(path):
        if not path.exists():
            if create_if_missing:
                cm = CommentedMap()
                if updater(cm) is False:
                    return False
                _atomic_dump(path, cm)
                return True
            return False

        with path.open("r", encoding="utf-8") as f:
            cm = _yaml.load(f)
            if cm is None:
                cm = CommentedMap()

        if updater(cm) is False:
            return False
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
