"""Fill a digest from eligible jobs, replacing failed tailoring candidates."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .agents.keywords import KeywordAgent
    from .agents.resume import ResumeAgent
    from .models import Job

from .state import allocate


def tailor_batch(
    jobs: list[Job],
    resume: ResumeAgent,
    state: dict[str, Any],
    size: int,
    share: float,
    recent_days: int,
    max_attempts: int,
    report: dict[str, Any],
    on_progress: Callable[[], None] | None = None,
    now: datetime | None = None,
    keyword_agent: KeywordAgent | None = None,
) -> list[dict[str, Any]]:
    ready, failed = {}, set()
    attempts = report.setdefault("tailoring_attempts", [])
    attempted = 0
    while attempted < max_attempts:
        plan = allocate(
            [job for job in jobs if job.posting_id not in failed],
            state,
            size,
            share,
            recent_days,
            now,
        )
        pending = [(job, bucket) for job, bucket in plan if job.posting_id not in ready]
        if not pending:
            break
        job, bucket = pending[0]
        attempted += 1
        print(f"Tailoring resume {attempted}/{max_attempts}: {job.company}", flush=True)
        event = {"posting_id": job.posting_id, "company": job.company, "status": "running"}
        attempts.append(event)
        if on_progress:
            on_progress()
        try:
            if keyword_agent is not None:
                print(f"Planning job keywords: {job.company}", flush=True)
                keyword_agent.analyze(job)
                event["keyword_audit_path"] = job.keyword_plan["audit_path"]
            ready[job.posting_id] = resume.tailor(job)
            event["status"] = "validated"
            event["resume_path"] = ready[job.posting_id]["resume_path"]
        except Exception as exc:
            failed.add(job.posting_id)
            event.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            report["diagnostics"].append(f"Tailoring {job.posting_id}: {event['error']}")
            print(
                f"Skipping {job.company}: {type(exc).__name__}: {str(exc)[:350]}. Trying another eligible job.",
                flush=True,
            )
        if on_progress:
            on_progress()
    # Recompute using successful artifacts only. Old/undated jobs cannot fill
    # a missing recent slot, even when an earlier full-batch plan included them.
    completed = allocate(
        [job for job in jobs if job.posting_id in ready], state, size, share, recent_days, now
    )
    entries = [
        {**job.to_dict(), **ready[job.posting_id], "allocation_bucket": bucket}
        for job, bucket in completed
    ]
    if len(entries) < size:
        print(
            f"Ready: {len(entries)}/{size} jobs after {attempted} tailoring attempts.", flush=True
        )
    return entries
