from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config_loader import Settings

    from ..state import StateStore

import re
import smtplib
import ssl
import uuid
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, make_msgid
from pathlib import Path
from string import Template

from ..common import atomic_json, digest, read_json, skill_text, utcnow
from ..models import parse_posted_at


class DeliveryError(RuntimeError):
    pass


class EmailAgent:
    def __init__(self, settings: Settings, store: StateStore) -> None:
        self.settings, self.store = settings, store
        self.instructions = skill_text(settings.project_root, "email_notifier")
        self.config = settings.data["email"]

    def password(self) -> str:
        if not self.config["enabled"]:
            raise DeliveryError("Email is disabled. Set email.enabled=true to send notifications.")
        if not self.settings.smtp_password:
            raise DeliveryError(
                f"SMTP password is missing. Set {self.config['password_environment_variable']} in .env or your environment."
            )
        return self.settings.smtp_password

    def render_template(self, field: str, default: str, values: dict[str, str]) -> str:
        configured = self.config.get(field)
        text = (
            self.settings.resolve_path(configured).read_text(encoding="utf-8")
            if configured
            else default
        )
        try:
            return Template(text).substitute(values)
        except (KeyError, ValueError) as exc:
            raise DeliveryError(
                f"Invalid email.{field}: use only documented $placeholders; escape literal dollars as $$."
            ) from exc

    def prepare(self, entries: list[dict], diagnostics: list[str]) -> dict:
        if not entries or len(entries) > self.settings.data["search"]["batch_size"]:
            raise DeliveryError(
                "A digest requires one to the configured batch_size validated jobs."
            )
        identifier = utcnow().strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
        directory = self.store.output_root / "outbox" / identifier
        directory.mkdir(mode=0o700)
        message = EmailMessage(policy=policy.SMTP)
        if self.config["sender"]:
            message["From"] = self.config["sender"]
        if self.config["recipient"]:
            message["To"] = self.config["recipient"]
        template_values = {
            "count": str(len(entries)),
            "date": utcnow().astimezone(self.settings.timezone).strftime("%b %d, %Y"),
            "titles": ", ".join(self.settings.data["search"]["job_titles"]),
        }
        subject = self.render_template(
            "subject_template", "Job matches: $count | $date", template_values
        ).strip()
        if "\r" in subject or "\n" in subject or not subject:
            raise DeliveryError("Email subject must be a single non-empty line.")
        message["Subject"] = subject
        message["Date"] = format_datetime(utcnow())
        message["Message-ID"] = make_msgid(domain="local-job-finder.invalid")
        sample = any(entry["source"] == "sample" for entry in entries)
        lines = [
            "SAMPLE PREVIEW — fictitious job postings. This digest cannot be sent."
            if sample
            else self.render_template("intro_template", "Your job matches", template_values),
            "",
        ]
        for index, entry in enumerate(entries, 1):
            path = Path(entry["resume_path"]).resolve()
            if (
                not path.is_relative_to(self.settings.output_root / "tailored_resumes")
                or path == self.settings.base_resume
            ):
                raise DeliveryError("Attachment is outside the tailored-resume directory.")
            if (
                not path.is_file()
                or path.suffix.lower() != ".docx"
                or digest(path.read_bytes()) != entry["resume_sha256"]
            ):
                raise DeliveryError("Resume attachment is missing or changed since validation.")
            posted = parse_posted_at(entry["posted_at"])
            posted_label = "Not stated"
            if posted:
                stamp = (
                    posted
                    if entry.get("date_precision") == "date"
                    else posted.astimezone(self.settings.timezone)
                )
                posted_label = stamp.strftime("%b %d, %Y")
            employment = {
                "full_time": "Full-time",
                "contract": "Contract",
                "contract_to_hire": "Contract-to-hire",
                "unknown": "Not stated",
            }.get(entry["employment_type"], entry["employment_type"])
            arrangement = {
                "remote": "Remote",
                "hybrid": "Hybrid",
                "onsite": "On-site",
                "unknown": "Not stated",
            }.get(entry["work_arrangement"], entry["work_arrangement"])
            section_names = {
                "summary": "Summary",
                "skills": "Technical skills",
                "experience_bullets": "Work experience",
                "project_descriptions": "Projects",
            }
            changed_sections = list(
                dict.fromkeys(
                    section_names[e["section"]]
                    for e in entry["edits"]
                    if e.get("section") in section_names
                )
            )
            employer = entry.get("employer_verification", {})
            unverified = employer.get("sponsorship_unverified", True)
            verification_lines = [
                "Employer sponsorship check: "
                + (
                    "SPONSORSHIP UNVERIFIED — "
                    + employer.get("reason", "Employer posting was not checked.")
                    if unverified
                    else "Explicit policy verified on the employer posting."
                ),
                *(["Employer posting checked: " + employer["url"]] if employer.get("url") else []),
            ]
            details = [
                f"{index}. {entry['job_title']} — {entry['company']}",
                f"Location: {entry['location']} | {arrangement} | {employment}",
                f"Required experience: {entry['required_experience']}",
                f"Posted: {posted_label}",
                f"Sponsorship/OPT policy: {entry['sponsorship_status']}",
                "Policy evidence: "
                + ("; ".join(entry["sponsorship_evidence"]) or "Not stated in the posting."),
                *verification_lines,
                *(
                    [
                        "Priority job keywords: "
                        + ", ".join(
                            item["term"]
                            for item in entry.get("keyword_plan", {}).get("keywords", [])
                            if item["priority"] == "high"
                        )
                    ]
                    if entry.get("keyword_plan")
                    else []
                ),
                "Matched skills: "
                + (", ".join(entry["matched_skills"]) or "See resume and posting."),
                (
                    "Skills not present in the tailored resume: "
                    if entry.get("skill_match_basis") == "updated_resume"
                    else "Skills not evidenced in the base resume: "
                )
                + (", ".join(entry["gaps"]) or "None identified in the tracked keyword list."),
                "Apply: "
                + (
                    employer["url"]
                    if employer.get("page_status") == "verified"
                    else entry["application_url"]
                ),
                f"Resume: {path.name}",
                *(
                    [
                        f"Resume factual feedback: {len(entry.get('flagged_paragraph_ids', entry.get('factual_review_warnings', [])))} flagged edits applied; review claims before applying."
                    ]
                    if entry.get("manual_fact_review_required")
                    else []
                ),
                *(
                    ["Tailored sections: " + ", ".join(changed_sections)]
                    if changed_sections
                    else []
                ),
                "",
            ]
            if self.config.get("job_template"):
                details = [
                    self.render_template(
                        "job_template",
                        "",
                        {
                            **template_values,
                            "index": str(index),
                            "job_title": entry["job_title"],
                            "company": entry["company"],
                            "location": entry["location"],
                            "work_arrangement": arrangement,
                            "employment_type": employment,
                            "required_experience": entry["required_experience"],
                            "posted": posted_label,
                            "application_url": entry["application_url"],
                            "resume_filename": path.name,
                            "matched_skills": ", ".join(entry["matched_skills"]),
                            "gaps": ", ".join(entry["gaps"]),
                            "sponsorship_status": entry["sponsorship_status"],
                            "verification_note": " ".join(verification_lines),
                            "factual_review_note": "Review flagged claims before applying."
                            if entry.get("manual_fact_review_required")
                            else "Strict factual review completed; check the resume before use.",
                        },
                    ),
                    *verification_lines,
                    "Review claims before applying."
                    if entry.get("manual_fact_review_required")
                    else "",
                    "",
                ]
            lines.extend(details)
            message.add_attachment(
                path.read_bytes(),
                maintype="application",
                subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
                filename=path.name,
            )
        if diagnostics:
            lines.extend(
                [
                    f"Search notes: {len(diagnostics)} source or candidate diagnostics were logged locally.",
                    "",
                ]
            )
        lines.append(
            self.render_template(
                "footer_template",
                "Please review each posting and resume before deciding whether to apply.",
                template_values,
            )
        )
        # A multipart message already exists after attachments: insert the text body.
        body = EmailMessage()
        body.set_content("\n".join(lines))
        message.get_payload().insert(0, body)
        raw = message.as_bytes()
        if len(raw) > self.config["max_message_bytes"]:
            raise DeliveryError("Digest exceeds the configured email size limit.")
        email_path = directory / "digest.eml"
        email_path.write_bytes(raw)
        email_path.chmod(0o600)
        (directory / "digest.txt").write_text("\n".join(lines), encoding="utf-8")
        manifest = {
            "digest_id": identifier,
            "message_id": str(message["Message-ID"]),
            "status": "prepared",
            "created_at": utcnow().isoformat(),
            "jobs": entries,
            "email_path": str(email_path),
            "email_sha256": digest(raw),
            "sample": sample,
        }
        atomic_json(directory / "manifest.json", manifest)
        return manifest

    def _path(self, identifier: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
            raise DeliveryError("Invalid outbox identifier.")
        path = self.store.output_root / "outbox" / identifier / "manifest.json"
        if not path.is_file():
            raise DeliveryError("The requested outbox does not exist.")
        return path

    def send(self, identifier: str) -> dict:
        self.password()  # Disabled email must never reach SMTP.
        path = self._path(identifier)
        manifest = read_json(path)
        if manifest["sample"]:
            raise DeliveryError("Sample digests cannot be sent.")
        if manifest["status"] not in {"prepared", "failed"}:
            raise DeliveryError(
                f"Digest status is {manifest['status']}; reconcile uncertain delivery before retrying."
            )
        blocked = self.store.blocked_keys()
        if any(blocked.intersection(entry["identity_keys"]) for entry in manifest["jobs"]):
            raise DeliveryError("A digest member was already delivered or has uncertain delivery.")
        email_path = Path(manifest["email_path"]).resolve()
        if email_path.parent != path.parent.resolve():
            raise DeliveryError("Outbox email path is invalid.")
        raw = email_path.read_bytes()
        if digest(raw) != manifest["email_sha256"]:
            raise DeliveryError("Prepared email changed after validation.")
        message = BytesParser(policy=policy.default).parsebytes(raw)
        if (
            str(message["From"]) != self.config["sender"]
            or str(message["To"]) != self.config["recipient"]
            or message["Cc"]
            or message["Bcc"]
        ):
            raise DeliveryError("Prepared email recipients do not match configuration.")
        password = self.password()
        manifest["status"] = "sending"
        atomic_json(path, manifest)
        transmission_started, accepted = False, False
        try:
            import certifi

            context = ssl.create_default_context(cafile=certifi.where())
            if self.config["security"] == "ssl":
                connection = smtplib.SMTP_SSL(
                    self.config["host"],
                    self.config["port"],
                    timeout=self.config["timeout_seconds"],
                    context=context,
                )
            else:
                connection = smtplib.SMTP(
                    self.config["host"], self.config["port"], timeout=self.config["timeout_seconds"]
                )
            with connection as server:
                if self.config["security"] == "starttls":
                    server.ehlo()
                    server.starttls(context=context)
                    server.ehlo()
                server.login(self.config["username"] or self.config["sender"], password)
                transmission_started = True
                refused = server.sendmail(self.config["sender"], [self.config["recipient"]], raw)
                if refused:
                    raise smtplib.SMTPRecipientsRefused(refused)
                accepted = True
                # Persist immediately after SMTP acceptance, before QUIT can fail.
                self.store.record_sent(manifest)
                manifest["status"], manifest["accepted_at"] = "sent", utcnow().isoformat()
                atomic_json(path, manifest)
        except Exception as exc:
            if accepted:
                # A QUIT disconnect does not invalidate SMTP acceptance. A failed
                # state write does: leave sending/uncertain for recovery.
                saved = read_json(path)
                if saved["status"] == "sent":
                    return saved
                manifest["status"] = "uncertain"
            elif (
                isinstance(exc, (smtplib.SMTPResponseException, smtplib.SMTPRecipientsRefused))
                or not transmission_started
            ):
                manifest["status"] = "failed"
            else:
                manifest["status"] = "uncertain"
            manifest["error"] = type(exc).__name__
            atomic_json(path, manifest)
            raise DeliveryError(
                f"Digest delivery {manifest['status']} ({type(exc).__name__}). Outbox retained."
            ) from exc
        return manifest

    def resolve(self, identifier: str, sent: bool) -> dict:
        path = self._path(identifier)
        manifest = read_json(path)
        if manifest["status"] not in {"sending", "uncertain"}:
            raise DeliveryError("Only sending/uncertain outboxes need reconciliation.")
        if sent:
            self.store.record_sent(manifest)
        manifest["status"] = "sent" if sent else "failed"
        manifest["resolved_at"] = utcnow().isoformat()
        atomic_json(path, manifest)
        return manifest
