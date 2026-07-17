"""Per-run sync records and derived node presence classification.

Freshness model ("stale beats deleted"): every source sync appends one small
committed YAML record under ``.infracontext/runs/``. Presence of
source-managed nodes is *derived* from the recent records at read time --
nodes never carry per-sync timestamps, so a sync of an unchanged environment
produces no node-YAML diffs (the run record is the only new file).

Records are informational history: a record's node lists describe what the
source *reported*, even when the sync guard prevented any node writes
(failed/partial/empty runs). Only successful, non-empty runs advance the
presence classification; everything else is recorded but ignored.
"""

import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from infracontext import __version__
from infracontext.paths import EnvironmentPaths
from infracontext.storage import read_model, write_model

log = logging.getLogger(__name__)

RUNS_DIR_NAME = "runs"

# Most recent records kept per (project, source) pair; older ones are pruned.
KEEP_RUNS_PER_SOURCE = 20

# A node absent from up to this many consecutive successful runs is
# "possibly-missing" (grace window); beyond it, "missing".
REMOVAL_GRACE_SYNCS = 3


class Presence(StrEnum):
    """Derived presence of a source-managed node."""

    PRESENT = "present"
    POSSIBLY_MISSING = "possibly-missing"
    MISSING = "missing"


class RunRecord(BaseModel):
    """One sync run of one source, as committed to .infracontext/runs/."""

    timestamp: str = Field(..., description="UTC time of the run (ISO 8601)")
    ic_version: str = Field(default="", description="infracontext version that ran the sync")
    source: str = Field(..., description="Source name (e.g. 'ssh-config', 'proxmox-prod')")
    project: str = Field(..., description="Project the sync ran against")
    status: str = Field(..., description="Sync status: success | partial | failed")
    created: list[str] = Field(default_factory=list, description="Node IDs created by this run")
    updated: list[str] = Field(default_factory=list, description="Node IDs updated by this run")
    confirmed_unchanged: list[str] = Field(
        default_factory=list,
        description="Node IDs the source reported but whose YAML needed no change",
    )

    model_config = {"extra": "forbid"}

    @property
    def seen_node_ids(self) -> frozenset[str]:
        """All node IDs the source reported in this run."""
        return frozenset([*self.created, *self.updated, *self.confirmed_unchanged])

    @property
    def counts_for_presence(self) -> bool:
        """Whether this run advances presence classification.

        Only successful, non-empty runs count: a failed/partial sync saw an
        incomplete picture, and an empty result (zero nodes reported) is far
        more likely a broken source than a vanished fleet.
        """
        return self.status == "success" and bool(self.seen_node_ids)


@dataclass(frozen=True)
class NodePresence:
    """Presence classification for one node, with the evidence count."""

    presence: Presence
    consecutive_misses: int


def runs_dir(environment: EnvironmentPaths) -> Path:
    """Directory holding run records (``.infracontext/runs/``)."""
    return environment.infracontext_dir / RUNS_DIR_NAME


def _timestamp_slug(timestamp: str) -> str:
    """Filesystem-safe compact form of an ISO timestamp (kept sortable)."""
    return timestamp.replace(":", "").replace("-", "")


def write_run_record(
    environment: EnvironmentPaths,
    *,
    project: str,
    source: str,
    status: str,
    created: Iterable[str] = (),
    updated: Iterable[str] = (),
    confirmed_unchanged: Iterable[str] = (),
    timestamp: str | None = None,
) -> Path:
    """Write one run record and prune old ones for the same (project, source).

    Returns the path of the written record. ``timestamp`` (ISO 8601 UTC)
    defaults to now; tests pass explicit values for determinism.
    """
    ts = timestamp or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    record = RunRecord(
        timestamp=ts,
        ic_version=__version__,
        source=source,
        project=project,
        status=str(status),
        created=sorted(created),
        updated=sorted(updated),
        confirmed_unchanged=sorted(confirmed_unchanged),
    )

    directory = runs_dir(environment)
    directory.mkdir(parents=True, exist_ok=True)
    base = f"{_timestamp_slug(ts)}-{source}"
    path = directory / f"{base}.yaml"
    # Same-second collision: '~' sorts after any letter, so suffixed names
    # stay newest-last under the (timestamp, filename) ordering.
    suffix = 1
    while path.exists():
        suffix += 1
        path = directory / f"{base}.~{suffix:02d}.yaml"
    write_model(path, record)

    _prune_run_records(environment, project=project, source=source)
    return path


def _load_records_with_paths(
    environment: EnvironmentPaths, *, project: str, source: str
) -> list[tuple[Path, RunRecord]]:
    """All parseable records for (project, source), oldest first."""
    directory = runs_dir(environment)
    if not directory.is_dir():
        return []
    found: list[tuple[Path, RunRecord]] = []
    for path in directory.glob("*.yaml"):
        try:
            record = read_model(path, RunRecord)
        except Exception as e:
            log.warning("Skipping unreadable run record %s: %s", path, e)
            continue
        if record is None:
            continue
        if record.project == project and record.source == source:
            found.append((path, record))
    found.sort(key=lambda item: (item[1].timestamp, item[0].name))
    return found


def _prune_run_records(environment: EnvironmentPaths, *, project: str, source: str) -> None:
    """Delete all but the newest KEEP_RUNS_PER_SOURCE records for the source."""
    records = _load_records_with_paths(environment, project=project, source=source)
    for path, _ in records[: max(0, len(records) - KEEP_RUNS_PER_SOURCE)]:
        try:
            path.unlink()
        except OSError as e:  # pragma: no cover - fs race/permissions
            log.warning("Could not prune run record %s: %s", path, e)


def load_run_records(environment: EnvironmentPaths, *, project: str, source: str) -> list[RunRecord]:
    """Run records for (project, source), newest first."""
    return [record for _, record in reversed(_load_records_with_paths(environment, project=project, source=source))]


def classify_presence(
    node_ids: Iterable[str],
    records: Sequence[RunRecord],
) -> dict[str, NodePresence]:
    """Classify nodes against a source's run history (``records`` newest first).

    For each node: ``present`` when seen in the latest counting run,
    ``possibly-missing`` when absent from 1..REMOVAL_GRACE_SYNCS consecutive
    counting runs, ``missing`` beyond that. Failed, partial, and empty runs
    never count (see :meth:`RunRecord.counts_for_presence`). Returns an empty
    dict when no counting runs exist -- without a successful observation there
    is no evidence to classify against.
    """
    seen_sets = [record.seen_node_ids for record in records if record.counts_for_presence]
    if not seen_sets:
        return {}

    result: dict[str, NodePresence] = {}
    for node_id in node_ids:
        misses = 0
        for seen in seen_sets:
            if node_id in seen:
                break
            misses += 1
        if misses == 0:
            presence = Presence.PRESENT
        elif misses <= REMOVAL_GRACE_SYNCS:
            presence = Presence.POSSIBLY_MISSING
        else:
            presence = Presence.MISSING
        result[node_id] = NodePresence(presence=presence, consecutive_misses=misses)
    return result
