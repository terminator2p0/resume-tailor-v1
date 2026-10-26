from __future__ import annotations

import math
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

import portalocker

if TYPE_CHECKING:
    from datetime import datetime

    from .models import Job

from .common import atomic_json, read_json, utcnow


class StateStore:
    def __init__(self, root: Path, output_root: Path) -> None:
        self.output_root = output_root
        (output_root / "outbox").mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = root
        for directory in ("jobs",):
            (root / directory).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / "state.json"

    @contextmanager
    def locked(self) -> Iterator[StateStore]:
        with (self.root / ".runner.lock").open("a+") as handle:
            try:
                portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
            except portalocker.exceptions.LockException as exc:
                raise RuntimeError("Another job-finder run is already active.") from exc
            try:
                yield self
            finally:
                portalocker.unlock(handle)

    def load(self) -> dict:
        if not self.path.exists():
            return {
                "version": 1,
                "delivered": {},
                "allocation": {"recent": 0, "unrestricted": 0},
                "digests": {},
            }
        data = read_json(self.path)
        if data.get("version") != 1 or not all(
            k in data for k in ("delivered", "allocation", "digests")
        ):
            raise ValueError("Processing state is unsupported or incomplete; it was not reset.")
        return data

    def outboxes(self) -> list[dict]:
        return [
            read_json(path)
            for path in sorted((self.output_root / "outbox").glob("*/manifest.json"))
        ]

    def blocked_keys(self) -> set[str]:
        state = self.load()
        keys = {key for entry in state["delivered"].values() for key in entry["identity_keys"]}
        for box in self.outboxes():
            if box["status"] in {"sending", "uncertain", "sent"}:
                keys.update(key for entry in box["jobs"] for key in entry["identity_keys"])
        return keys

    def record_sent(self, manifest: dict) -> None:
        self.record_completed(manifest, delivery="smtp")

    def record_completed(self, manifest: dict, *, delivery: str = "local") -> None:
        state = self.load()
        if manifest["digest_id"] in state["digests"]:
            return
        for entry in manifest["jobs"]:
            state["delivered"][entry["posting_id"]] = {
                "identity_keys": entry["identity_keys"],
                "digest_id": manifest["digest_id"],
                "completed_at": utcnow().isoformat(),
                "delivery": delivery,
                "application_url": entry["application_url"],
            }
            state["allocation"][entry["allocation_bucket"]] += 1
        state["digests"][manifest["digest_id"]] = {
            "message_id": manifest["message_id"],
            "count": len(manifest["jobs"]),
            "delivery": delivery,
        }
        atomic_json(self.path, state)


def allocate(
    jobs: list[Job],
    state: dict[str, Any],
    size: int,
    share: float,
    recent_days: int,
    now: datetime | None = None,
) -> list[tuple[Job, str]]:
    """Carry the rounding error across successful deliveries; never old-job backfill.

    If there are no recent jobs, return nothing. Otherwise the unrestricted quota
    is based on the jobs actually available, so a shortage cannot produce an
    unrestricted-only batch. The configured fractional target is best-effort under shortages.
    """
    if size < 1:
        return []
    recent = [job for job in jobs if job.is_recent(recent_days, now)]
    other = [job for job in jobs if not job.is_recent(recent_days, now)]
    if share == 0:
        return [(job, "unrestricted") for job in jobs[:size]]
    if not recent:
        return []
    delivered = sum(state["allocation"].values())
    previous_recent = state["allocation"]["recent"]
    recent_quota = max(1, min(size, math.floor((delivered + size) * share + 0.5) - previous_recent))
    chosen_recent = recent[:recent_quota]
    # Reduce the unrestricted allowance if the recent quota cannot be filled.
    effective_size = len(chosen_recent) + (
        size - recent_quota if len(chosen_recent) == recent_quota else 0
    )
    flexible_quota = max(0, effective_size - len(chosen_recent))
    # Any-age slots prefer other jobs; remaining recent jobs can also fill them.
    flexible = (other + recent[recent_quota:])[:flexible_quota]
    return [(job, "recent") for job in chosen_recent] + [(job, "unrestricted") for job in flexible]
