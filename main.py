"""Local pipeline entry point; delivery behavior comes from validated configuration."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from config_loader import load_settings
from jobfinder.agents.emailer import EmailAgent
from jobfinder.agents.keywords import KeywordAgent
from jobfinder.agents.resume import ResumeAgent, inspect_resume
from jobfinder.agents.search import SearchAgent, extract_skills
from jobfinder.batch import tailor_batch
from jobfinder.common import atomic_json, skill_text, utcnow
from jobfinder.llm import OllamaClient
from jobfinder.models import Job
from jobfinder.state import StateStore


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Find jobs matching your preferences and tailor your resume with local Ollama models."
    )
    result.add_argument(
        "--config", type=Path, help="YAML/JSON settings (default: project config.yaml)."
    )
    mode = result.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="Validate setup and local models; do not scrape or email.",
    )
    mode.add_argument(
        "--inspect-resume",
        action="store_true",
        help="List resume paragraphs without editing the source.",
    )
    mode.add_argument(
        "--search-only",
        action="store_true",
        help="Fetch and save raw jobs without model inference or email.",
    )
    mode.add_argument(
        "--send-outbox", metavar="ID", help="Send an existing real-job preview digest."
    )
    mode.add_argument(
        "--resolve-outbox",
        metavar="ID",
        help="Reconcile uncertain delivery after checking your mail provider.",
    )
    inputs = result.add_mutually_exclusive_group()
    inputs.add_argument(
        "--jobs",
        type=Path,
        help="Use an imported local JSON array of postings instead of searching.",
    )
    inputs.add_argument(
        "--sample",
        action="store_true",
        help="Use explicitly fictitious demo postings; sending is disabled.",
    )
    delivery = result.add_mutually_exclusive_group()
    delivery.add_argument(
        "--send",
        action="store_true",
        help="Explicitly send the digest (requires email.enabled=true).",
    )
    delivery.add_argument(
        "--preview",
        action="store_true",
        help="Save a preview even when email is enabled; do not send.",
    )
    result.add_argument(
        "--delivery",
        choices=["sent", "not_sent"],
        help="User-confirmed status for --resolve-outbox.",
    )
    result.add_argument(
        "--limit", type=int, help="Reduce the configured batch size for a manual run."
    )
    return result


def validate_arguments(args: argparse.Namespace) -> None:
    """Reject conflicting delivery flags before loading secrets or contacting services."""
    if args.send_outbox or args.resolve_outbox:
        if args.preview or args.sample or args.jobs or args.send or args.limit is not None:
            raise ValueError(
                "Use --send-outbox or --resolve-outbox separately from --preview, --sample, --jobs, --send and --limit."
            )
    if args.sample and args.send:
        raise ValueError("Sample postings are for local previews only.")
    if args.delivery and not args.resolve_outbox or args.resolve_outbox and not args.delivery:
        raise ValueError("Use --resolve-outbox ID together with --delivery sent or not_sent.")
    if args.send and (args.check or args.inspect_resume or args.search_only):
        raise ValueError("--send applies only to a normal pipeline run.")


def run(args: argparse.Namespace) -> int:
    validate_arguments(args)
    settings = load_settings(args.config)
    if args.send and not settings.data["email"]["enabled"]:
        raise ValueError("--send requires email.enabled=true.")
    should_send = settings.data["email"]["enabled"] and not args.preview and not args.sample
    if args.limit is not None and not 1 <= args.limit <= settings.data["search"]["batch_size"]:
        raise ValueError("--limit must be between one and the configured batch size.")
    if args.inspect_resume:
        print(json.dumps(inspect_resume(settings.base_resume), indent=2, ensure_ascii=False))
        return 0
    for name in ("job_scraper", "job_keywords", "docx_tailorer", "email_notifier"):
        skill_text(settings.project_root, name)
    llm = OllamaClient(settings)
    if args.check:
        installed = llm.check_models()
        ResumeAgent(settings, llm)
        print("Configuration and detected resume layout: OK")
        print("Configuration file: " + str(settings.config_path or "provided settings"))
        print("SMTP secret variable: " + settings.data["email"]["password_environment_variable"])
        print("Installed local models: " + ", ".join(installed))
        limits = llm.limits("resume_tailorer")
        print(
            f"Resume tailoring limits: {limits['max_input_characters']:,} input characters, "
            f"{limits['num_ctx']:,} context tokens, {limits['timeout_seconds']:g} seconds per request."
        )
        print("SMTP password: " + ("configured" if settings.smtp_password else "not configured"))
        print(
            "Delivery: "
            + (
                "automatic when enabled; --preview suppresses sending"
                if settings.data["email"]["enabled"]
                else "local files only (email disabled)"
            )
        )
        return 0
    store = StateStore(settings.storage_root, settings.output_root)
    with store.locked():
        email = EmailAgent(settings, store)
        if args.send_outbox:
            manifest = email.send(args.send_outbox)
            print(f"SMTP accepted digest {manifest['digest_id']} ({len(manifest['jobs'])} jobs).")
            return 0
        if args.resolve_outbox:
            email.resolve(args.resolve_outbox, args.delivery == "sent")
            print("Delivery reconciliation recorded.")
            return 0
        if should_send and not args.search_only:
            email.password()  # Fail before scraping or tailoring if setup is incomplete.
        run_id = utcnow().strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
        settings.data["run_id"] = run_id
        report = {
            "run_id": run_id,
            "started_at": utcnow().isoformat(),
            "status": "running",
            "diagnostics": [],
            "candidate_decisions": [],
            "selected": [],
        }
        report_path = settings.logs_root / "runs" / f"{run_id}.json"
        atomic_json(report_path, report)
        try:
            if not args.search_only:
                llm.check_models()
                resume = ResumeAgent(settings, llm)
            search = SearchAgent(settings, llm)
            if args.jobs or args.sample:
                path = (
                    settings.resolve_path(str(args.jobs))
                    if args.jobs
                    else settings.project_root / "examples" / "sample_jobs.json"
                )
                records = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(records, list):
                    raise ValueError("Imported jobs must be a JSON array.")
                if args.sample:
                    for record in records:
                        record["posted_at"] = utcnow().isoformat()
                        record["source"] = "sample"
                jobs = [Job.from_dict(record) for record in records]
            else:
                jobs, diagnostics = search.discover()
                report["diagnostics"].extend(diagnostics)
            blocked, unique = store.blocked_keys(), []
            for job in jobs:
                atomic_json(settings.jobs_root / f"{job.posting_id}.json", job.to_dict())
                if job.identity_keys.intersection(blocked):
                    continue
                blocked.update(job.identity_keys)
                unique.append(job)
            report["raw_count"], report["unique_count"] = len(jobs), len(unique)
            if args.search_only:
                report["status"] = "search_complete"
                print(f"Saved {len(jobs)} raw postings; {len(unique)} are new unique candidates.")
                return 0
            supported = {
                skill.lower()
                for skill in extract_skills(
                    resume.context_text, settings.data["search"]["tracked_keywords"]
                )
            }
            # Prioritize recent candidates with supported skills before costly inference.
            unique.sort(
                key=lambda job: (
                    job.is_recent(settings.data["search"]["recent_days"]),
                    sum(
                        s.lower() in supported
                        for s in extract_skills(
                            job.job_description, settings.data["search"]["tracked_keywords"]
                        )
                    ),
                ),
                reverse=True,
            )
            analysis_limit = settings.data["search"]["max_candidates_to_analyze"]
            # Reserve analysis capacity for the unrestricted pool as well; a large
            # recent pool must not starve the configured any-age allocation.
            recent_pool = [
                job for job in unique if job.is_recent(settings.data["search"]["recent_days"])
            ]
            unrestricted_pool = [
                job for job in unique if not job.is_recent(settings.data["search"]["recent_days"])
            ]
            share = settings.data["search"]["recent_share"]
            if share == 0:
                candidates = unique[:analysis_limit]
            elif share == 1:
                candidates = recent_pool[:analysis_limit]
            else:
                unrestricted_limit = max(1, round(analysis_limit * (1 - share)))
                candidates = recent_pool[
                    : analysis_limit - min(len(unrestricted_pool), unrestricted_limit)
                ]
                candidates += unrestricted_pool[: analysis_limit - len(candidates)]
            eligible = []
            for index, job in enumerate(candidates, 1):
                print(
                    f"Checking {index}/{len(candidates)}: {job.job_title} at {job.company}",
                    flush=True,
                )
                try:
                    accepted, reason = search.analyze(job, resume.context_text)
                    if accepted:
                        eligible.append(job)
                    report["candidate_decisions"].append(
                        {"posting_id": job.posting_id, "accepted": accepted, "reason": reason}
                    )
                except Exception as exc:
                    report["diagnostics"].append(
                        f"Candidate {job.posting_id}: {type(exc).__name__}: {exc}"
                    )
                atomic_json(settings.jobs_root / f"{job.posting_id}.json", job.to_dict())
                atomic_json(report_path, report)
            eligible.sort(key=lambda job: job.matching_score, reverse=True)
            report["eligible_count"] = len(eligible)
            entries = tailor_batch(
                eligible,
                resume,
                store.load(),
                args.limit or settings.data["search"]["batch_size"],
                settings.data["search"]["recent_share"],
                settings.data["search"]["recent_days"],
                settings.data["search"].get("max_candidates_to_tailor", 10),
                report,
                on_progress=lambda: atomic_json(report_path, report),
                keyword_agent=KeywordAgent(settings, llm),
            )
            report["selected"] = [entry["posting_id"] for entry in entries]
            if not entries:
                report["status"] = "no_ready_jobs"
                print(
                    "No jobs with validated tailored resumes are ready. See the local run report for reasons."
                )
                return 2 if eligible or candidates and not report["candidate_decisions"] else 0
            manifest = email.prepare(entries, report["diagnostics"])
            report["digest_id"] = manifest["digest_id"]
            if should_send:
                email.send(manifest["digest_id"])
                report["status"] = "sent"
                print(f"SMTP accepted one digest containing {len(entries)} jobs.")
            else:
                report["status"] = "preview_ready"
                print(f"Preview ready: {Path(manifest['email_path']).with_suffix('.txt')}")
                print(f"Digest ID: {manifest['digest_id']}")
                if (
                    not args.preview
                    and not settings.data["email"]["enabled"]
                    and not manifest["sample"]
                ):
                    store.record_completed(manifest)
                    manifest["status"] = "local_complete"
                    atomic_json(Path(manifest["email_path"]).parent / "manifest.json", manifest)
                    report["status"] = "local_complete"
                if not manifest["sample"] and settings.data["email"]["enabled"]:
                    print(
                        f"Send this preview with: python main.py --send-outbox {manifest['digest_id']}"
                    )
            return 0
        except Exception as exc:
            report["status"], report["error"] = "failed", f"{type(exc).__name__}: {exc}"
            raise
        finally:
            report["model_statistics"] = llm.statistics
            report["finished_at"] = utcnow().isoformat()
            atomic_json(report_path, report)
            print(f"Run report: {report_path}")


def main() -> int:
    args = parser().parse_args()
    try:
        return run(args)
    except KeyboardInterrupt:
        print("Run interrupted. Local artifacts were retained.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
