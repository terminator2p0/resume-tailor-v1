from __future__ import annotations

import json
import smtplib
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from helpers import ProjectCase, analysis, job

import main
from jobfinder.agents.emailer import DeliveryError, EmailAgent
from jobfinder.agents.employer import EmployerVerifier, application_links
from jobfinder.agents.resume import ResumeAgent
from jobfinder.agents.search import SearchAgent, experience_filter, sponsorship_restrictions
from jobfinder.batch import tailor_batch
from jobfinder.common import digest, utcnow
from jobfinder.llm import ModelError, OllamaClient
from jobfinder.scrapers.sources import board_worker
from jobfinder.state import StateStore, allocate


class SearchTests(ProjectCase):
    def analyze(self, **result):
        model = Mock()
        model.generate.return_value = analysis(**result)
        return SearchAgent(self.load(), model).analyze(job(), "Python")

    def test_generic_non_us_role_and_geography(self):
        self.assertTrue(self.analyze()[0])
        self.assertFalse(self.analyze(target_role_match=False)[0])
        self.assertFalse(self.analyze(location_eligible=False)[0])

    def test_arrangement_employment_and_level_filters(self):
        self.raw["search"].update(work_arrangements=["remote"], experience_levels=["entry level"])
        self.assertFalse(self.analyze()[0])
        self.assertFalse(self.analyze(work_arrangement="remote", employment_type="contract")[0])
        self.assertFalse(self.analyze(work_arrangement="remote", experience_level_match=False)[0])
        self.assertTrue(self.analyze(work_arrangement="remote")[0])

    def test_unknown_arrangements_can_be_rejected(self):
        self.raw["search"]["keep_unknown_work_arrangement"] = False
        self.assertFalse(self.analyze(work_arrangement="unknown")[0])

    def test_exclusions_reject_before_inference(self):
        self.raw["search"]["exclusion_keywords"] = ["product teams"]
        model = Mock()
        self.assertFalse(SearchAgent(self.load(), model).analyze(job(), "")[0])
        model.generate.assert_not_called()

    def test_sponsorship_filter_is_optional(self):
        candidate = job()
        candidate.job_description += " We cannot sponsor visas."
        for required, expected in [(False, True), (True, False)]:
            self.raw["search"]["require_sponsorship"] = required
            model = Mock()
            model.generate.return_value = analysis(
                sponsorship_status="restricted", sponsorship_evidence=["We cannot sponsor visas."]
            )
            self.assertEqual(SearchAgent(self.load(), model).analyze(candidate, "")[0], expected)

    def test_fabricated_evidence_does_not_pass(self):
        self.assertFalse(self.analyze(location_evidence="Invented location")[0])
        self.assertFalse(
            self.analyze(sponsorship_status="available", sponsorship_evidence=["Invented policy"])[
                0
            ]
        )

    def test_unknown_sponsorship_gate(self):
        self.raw["search"].update(require_sponsorship=True, keep_unknown_sponsorship=False)
        self.assertFalse(self.analyze()[0])

    def test_discovery_only_enabled_sites_and_configured_locations(self):
        self.raw["search"].update(
            job_titles=["Designer"],
            locations=["Paris", "Lyon"],
            sites={
                name: {"enabled": name == "linkedin", "request_interval_seconds": 0.0}
                for name in ["linkedin", "indeed", "glassdoor", "company_careers"]
            },
        )
        with patch("jobfinder.agents.search.run_source", return_value=([], [])) as source:
            SearchAgent(self.load(), None).discover(progress=lambda text: None)
        self.assertEqual(source.call_count, 2)
        self.assertEqual(source.call_args_list[0].args[1:4], ("linkedin", "Designer", True))
        self.assertEqual(source.call_args_list[1].kwargs["location"], "Lyon")

    def test_board_parameters_include_country_location_and_remote(self):
        import pandas as pd

        with patch("jobspy.scrape_jobs", return_value=pd.DataFrame()) as scrape:
            board_worker(
                {
                    "source": "indeed",
                    "query": "Designer",
                    "location": "Paris",
                    "country": "France",
                    "limit": 9,
                    "recent": True,
                    "recent_days": 2,
                    "remote_only": True,
                }
            )
        self.assertEqual(scrape.call_args.kwargs["country_indeed"], "France")
        self.assertEqual(scrape.call_args.kwargs["location"], "Paris")
        self.assertTrue(scrape.call_args.kwargs["is_remote"])

    def test_mandatory_year_ranges_and_quotes(self):
        config = {
            **self.settings.data["search"],
            "minimum_required_experience": 2,
            "maximum_required_experience": 4,
        }
        for minimum, maximum, quote, expected in [
            (2, None, "2+ years", True),
            (2, 4, "2–4 years", True),
            (2, 6, "2–6 years", False),
            (7, None, "7 years", False),
        ]:
            result = {
                "required_experience_min": minimum,
                "required_experience_max": maximum,
                "experience_evidence": quote,
            }
            self.assertEqual(experience_filter(result, quote, config)[0], expected)
        self.assertFalse(
            experience_filter(
                {
                    "required_experience_min": 2,
                    "required_experience_max": None,
                    "experience_evidence": "2 years",
                },
                "No years stated",
                config,
            )[0]
        )

    def test_restriction_negations(self):
        self.assertTrue(sponsorship_restrictions("We cannot offer visa sponsorship."))
        self.assertFalse(sponsorship_restrictions("Sponsorship is available."))
        self.assertFalse(sponsorship_restrictions("We do not discriminate based on visa status."))

    def test_employer_verification_fails_open_with_warning(self):
        with (
            patch("jobfinder.agents.employer.company_api_job", return_value=[]),
            patch("jobfinder.agents.employer.fetch_html_page", side_effect=OSError("Unavailable")),
        ):
            result = EmployerVerifier(self.settings, Mock()).verify(job())
        self.assertTrue(result["sponsorship_unverified"])
        self.assertEqual(result["policy_status"], "unknown")

    def test_only_explicit_application_links_followed(self):
        links = application_links(
            '<a href="/jobs/apply">Apply</a><a href="https://example.org/">Other</a>',
            "https://example.com/job",
        )
        self.assertEqual(links, ["https://example.com/jobs/apply"])


class NotificationTests(ProjectCase):
    def setUp(self):
        super().setUp()
        self.raw["email"].update(
            enabled=True,
            sender="sender@example.com",
            recipient="reader@example.com",
            host="smtp.example.com",
        )
        self.settings = self.load()
        object.__setattr__(self.settings, "smtp_password", "synthetic-test-secret")
        self.store = StateStore(self.settings.storage_root, self.settings.output_root)
        self.email = EmailAgent(self.settings, self.store)
        agent = ResumeAgent(self.settings, None)
        index = next(i for i, entry in agent.targets.items() if entry["section"] == "summary")
        path = self.settings.output_root / "tailored_resumes/example.docx"
        agent.apply(
            [
                {
                    "paragraph_id": index,
                    "replacement": "Software professional delivering reliable Python backend services for collaborative product teams.",
                }
            ],
            path,
        )
        self.entry = {
            **job().to_dict(),
            "resume_path": str(path),
            "resume_sha256": digest(path.read_bytes()),
            "matched_skills": ["Python"],
            "gaps": [],
            "edits": [],
            "allocation_bucket": "recent",
        }

    def test_prepared_digest_has_attachment_without_changing_state(self):
        from email import policy
        from email.parser import BytesParser

        box = self.email.prepare([self.entry], [])
        message = BytesParser(policy=policy.default).parsebytes(
            Path(box["email_path"]).read_bytes()
        )
        self.assertEqual(len(list(message.iter_attachments())), 1)
        self.assertFalse(self.store.load()["delivered"])
        self.assertIn(
            "SPONSORSHIP UNVERIFIED", Path(box["email_path"]).with_suffix(".txt").read_text()
        )
        self.assertNotIn("synthetic-test-secret", Path(box["email_path"]).read_text())

    def test_disabled_email_never_connects(self):
        self.settings.data["email"]["enabled"] = False
        with (
            patch("jobfinder.agents.emailer.smtplib.SMTP_SSL") as smtp,
            self.assertRaisesRegex(DeliveryError, "disabled"),
        ):
            self.email.send("anything")
        smtp.assert_not_called()

    def test_missing_password_explains_environment_key(self):
        object.__setattr__(self.settings, "smtp_password", "")
        with self.assertRaisesRegex(DeliveryError, "JOBFINDER_SMTP_PASSWORD"):
            self.email.password()

    def test_ssl_success_and_duplicate_send_blocked(self):
        box = self.email.prepare([self.entry], [])
        with patch("jobfinder.agents.emailer.smtplib.SMTP_SSL") as smtp:
            smtp.return_value.__enter__.return_value.sendmail.return_value = {}
            self.email.send(box["digest_id"])
            with self.assertRaises(DeliveryError):
                self.email.send(box["digest_id"])
        self.assertEqual(self.store.outboxes()[0]["status"], "sent")
        self.assertEqual(self.store.load()["allocation"]["recent"], 1)

    def test_starttls_with_custom_username(self):
        self.settings.data["email"].update(security="starttls", port=587, username="custom-login")
        box = self.email.prepare([self.entry], [])
        with patch("jobfinder.agents.emailer.smtplib.SMTP") as smtp:
            server = smtp.return_value.__enter__.return_value
            server.sendmail.return_value = {}
            self.email.send(box["digest_id"])
            server.starttls.assert_called_once()
            server.login.assert_called_once_with("custom-login", "synthetic-test-secret")

    def test_uncertain_delivery_blocks_retry_and_can_be_reconciled(self):
        box = self.email.prepare([self.entry], [])
        with patch("jobfinder.agents.emailer.smtplib.SMTP_SSL") as smtp:
            smtp.return_value.__enter__.return_value.sendmail.side_effect = (
                smtplib.SMTPServerDisconnected("test")
            )
            with self.assertRaises(DeliveryError):
                self.email.send(box["digest_id"])
        self.assertEqual(self.store.outboxes()[0]["status"], "uncertain")
        self.assertTrue(self.store.blocked_keys())
        self.email.resolve(box["digest_id"], False)
        self.assertFalse(self.store.blocked_keys())

    def test_authentication_failure_is_retryable(self):
        box = self.email.prepare([self.entry], [])
        with patch("jobfinder.agents.emailer.smtplib.SMTP_SSL") as smtp:
            smtp.return_value.__enter__.return_value.login.side_effect = (
                smtplib.SMTPAuthenticationError(535, b"bad password")
            )
            with self.assertRaises(DeliveryError):
                self.email.send(box["digest_id"])
        self.assertEqual(self.store.outboxes()[0]["status"], "failed")

    def test_sample_and_changed_attachment_cannot_send(self):
        self.entry["source"] = "sample"
        box = self.email.prepare([self.entry], [])
        with self.assertRaisesRegex(DeliveryError, "Sample"):
            self.email.send(box["digest_id"])
        Path(self.entry["resume_path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(DeliveryError, "changed"):
            self.email.prepare([self.entry], [])

    def test_custom_templates_and_invalid_placeholders(self):
        path = self.root / "subject.txt"
        path.write_text("Opportunities: $count for $titles")
        self.settings.data["email"]["subject_template"] = "subject.txt"
        box = self.email.prepare([self.entry], [])
        self.assertIn("Opportunities: 1", Path(box["email_path"]).read_text())
        path.write_text("$unknown")
        with self.assertRaisesRegex(DeliveryError, "subject_template"):
            self.email.prepare([self.entry], [])


class OrchestratorTests(ProjectCase):
    def test_automatic_email_preview_override_and_disabled_mode(self):
        for enabled, preview, expected in [(True, False, 1), (True, True, 0), (False, False, 0)]:
            self.raw["email"] = {
                "enabled": enabled,
                "sender": "sender@example.com",
                "recipient": "reader@example.com",
                "host": "smtp.example.com",
            }
            settings = self.load()
            settings.data["search"]["recent_share"] = 0.0
            args = main.parser().parse_args(["--preview"] if preview else [])
            entry = {**job().to_dict(), "allocation_bucket": "unrestricted"}
            with (
                patch("main.load_settings", return_value=settings),
                patch("main.OllamaClient") as llm,
                patch("main.ResumeAgent") as resume,
                patch("main.SearchAgent") as search,
                patch("main.EmailAgent") as email,
                patch("main.tailor_batch", return_value=[entry]),
            ):
                llm.return_value.statistics = []
                resume.return_value.context_text = "Python"
                search.return_value.discover.return_value = ([job()], [])
                search.return_value.analyze.return_value = (True, "Eligible")
                email.return_value.prepare.return_value = {
                    "digest_id": "test",
                    "email_path": str(settings.output_root / "digest.eml"),
                    "sample": False,
                    "jobs": [entry],
                    "message_id": "test",
                }
                self.assertEqual(main.run(args), 0)
                self.assertEqual(email.return_value.send.call_count, expected)
            self.assertTrue(list(settings.jobs_root.glob("*.json")))
            self.assertTrue(list(settings.logs_root.glob("runs/*.json")))

    def test_any_age_and_recent_shortage_allocation(self):
        state = {"allocation": {"recent": 0, "unrestricted": 0}}
        old = job(1)
        old.posted_at = (utcnow() - timedelta(days=40)).isoformat()
        self.assertEqual(len(allocate([old], state, 5, 0, 7)), 1)
        self.assertFalse(allocate([old], state, 5, 1, 7))
        self.assertEqual(len(allocate([job(), old], state, 5, 0.8, 7)), 1)

    def test_failed_tailoring_candidate_replaced(self):
        resume = Mock()
        resume.tailor.side_effect = [RuntimeError("test failure"), {"resume_path": "test"}]
        report = {"diagnostics": []}
        results = tailor_batch(
            [job(), job(1)],
            resume,
            {"allocation": {"recent": 0, "unrestricted": 0}},
            1,
            1,
            7,
            2,
            report,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(report["tailoring_attempts"][0]["status"], "failed")

    def test_local_models_implicit_latest_and_connection_error(self):
        client = OllamaClient(self.settings)
        client.config["models"] = {"job_parser": "my-local"}
        with patch.object(
            client, "_request", return_value={"models": [{"name": "my-local:latest"}]}
        ):
            client.check_models()
        with patch.object(client.opener, "open", side_effect=OSError("private detail")):
            with self.assertRaisesRegex(ModelError, "Check server"):
                client._request("/api/tags")

    def test_model_json_schema_correction_and_no_connection_retry(self):
        client = OllamaClient(self.settings)
        schema = {
            "type": "object",
            "properties": {"answer": {"type": "boolean"}},
            "required": ["answer"],
        }
        with patch.object(
            client,
            "_request",
            side_effect=[
                {"message": {"content": "bad"}},
                {"message": {"content": '{"answer": true}'}},
            ],
        ) as request:
            self.assertTrue(client.generate("job_parser", "Return JSON", {}, schema)["answer"])
            self.assertEqual(request.call_count, 2)
        with patch.object(client, "_request", side_effect=ModelError("Disconnected")) as request:
            with self.assertRaises(ModelError):
                client.generate("job_parser", "Return JSON", {}, schema)
            self.assertEqual(request.call_count, 1)


class EndToEndTests(ProjectCase):
    def test_real_pipeline_with_synthetic_models_and_imported_posting(self):
        settings = self.load()
        source_hash = digest(settings.base_resume.read_bytes())
        (self.root / "jobs.json").write_text(json.dumps([job().to_dict()]))

        class SyntheticModel:
            statistics = []

            def check_models(self):
                return ["example-local:latest"]

            def generate(self, role, instructions, payload, schema):
                if role == "job_parser":
                    return analysis()
                if role == "keyword_extractor":
                    line = next(row for row in payload["lines"] if "Python" in row["text"])
                    return {
                        "keywords": [
                            {
                                "term": "Python",
                                "category": "technology",
                                "priority": "high",
                                "requirement": "required",
                                "line_id": line["id"],
                                "why": "Core tool",
                                "placement": ["summary"],
                            }
                        ]
                    }
                if role == "resume_tailorer":
                    index = next(
                        row[0] for row in payload["editable_targets"] if row[1] == "summary"
                    )
                    return {
                        "edits": [
                            {
                                "paragraph_id": index,
                                "replacement": "Software professional delivering reliable Python backend services and automated testing for collaborative product teams.",
                            }
                        ]
                    }
                return {
                    "decisions": [
                        {
                            "paragraph_id": row["paragraph_id"],
                            "supported": True,
                            "reason": "Supported",
                        }
                        for row in payload["proposed_edits"]
                    ]
                }

        args = main.parser().parse_args(["--jobs", "jobs.json"])
        with (
            patch("main.load_settings", return_value=settings),
            patch("main.OllamaClient", return_value=SyntheticModel()),
            patch("jobfinder.agents.emailer.smtplib.SMTP_SSL") as smtp,
        ):
            self.assertEqual(main.run(args), 0)
            smtp.assert_not_called()
        self.assertEqual(source_hash, digest(settings.base_resume.read_bytes()))
        self.assertEqual(len(list(settings.output_root.rglob("*.docx"))), 1)
        self.assertEqual(len(list(settings.output_root.rglob("digest.eml"))), 1)
        self.assertEqual(len(list(settings.jobs_root.glob("*.json"))), 1)
        store = StateStore(settings.storage_root, settings.output_root)
        self.assertTrue(store.blocked_keys())
        self.assertEqual(store.outboxes()[0]["status"], "local_complete")
        for path in self.root.rglob("*.json"):
            self.assertNotIn("smtp_password", path.read_text())

    def test_search_only_import_uses_no_models_or_smtp(self):
        settings = self.load()
        (self.root / "jobs.json").write_text(json.dumps([job().to_dict()]))
        with (
            patch("main.load_settings", return_value=settings),
            patch("main.OllamaClient") as model,
            patch("main.EmailAgent") as email,
        ):
            model.return_value.statistics = []
            self.assertEqual(
                main.run(main.parser().parse_args(["--search-only", "--jobs", "jobs.json"])), 0
            )
            model.return_value.check_models.assert_not_called()
            email.return_value.send.assert_not_called()
        self.assertEqual(len(list(settings.jobs_root.glob("*.json"))), 1)
