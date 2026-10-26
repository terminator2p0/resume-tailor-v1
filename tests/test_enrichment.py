from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

from helpers import ProjectCase, analysis, job

from jobfinder.agents.employer import EmployerVerifier
from jobfinder.agents.keywords import KeywordAgent, contains_term
from jobfinder.agents.search import SearchAgent
from jobfinder.llm import ModelError


class KeywordTests(ProjectCase):
    def response(self, payload, term="Python", **overrides):
        line = next(row for row in payload["lines"] if term.casefold() in row["text"].casefold())
        return {
            "keywords": [
                {
                    "term": term,
                    "category": "technology",
                    "priority": "high",
                    "requirement": "required",
                    "line_id": line["id"],
                    "why": "Relevant requirement",
                    "placement": ["skills"],
                    **overrides,
                }
            ]
        }

    def test_open_ended_terms_exact_evidence_and_audits(self):
        candidate = job()
        candidate.job_description += " Familiarity with ExampleNovelTool is required."
        model = Mock()
        model.generate.side_effect = lambda role, instructions, payload, schema: self.response(
            payload, "ExampleNovelTool"
        )
        result = KeywordAgent(self.settings, model).analyze(candidate)
        self.assertEqual(candidate.skills, ["ExampleNovelTool"])
        self.assertIn("ExampleNovelTool", result["keywords"][0]["evidence"])
        self.assertTrue(Path(result["audit_path"]).is_file())
        self.assertTrue(list(self.settings.jobs_root.glob("*.json")))

    def test_invented_term_retries_and_fails(self):
        model = Mock()
        model.generate.return_value = {
            "keywords": [
                {
                    "term": "InventedTerm",
                    "category": "technology",
                    "priority": "high",
                    "requirement": "required",
                    "line_id": "board:1",
                    "why": "Test",
                    "placement": ["skills"],
                }
            ]
        }
        with self.assertRaises(ModelError):
            KeywordAgent(self.settings, model).analyze(job())
        self.assertGreater(model.generate.call_count, 1)

    def test_model_connection_failure_is_not_retried(self):
        model = Mock()
        model.generate.side_effect = ModelError("Disconnected")
        with self.assertRaises(ModelError):
            KeywordAgent(self.settings, model).analyze(job())
        self.assertEqual(model.generate.call_count, 1)

    def test_employer_evidence_takes_priority(self):
        candidate = job()
        candidate.employer_verification = {
            "description": "Python is required for maintaining backend APIs and automated service tests."
        }
        model = Mock()
        model.generate.side_effect = lambda role, instructions, payload, schema: self.response(
            payload
        )
        result = KeywordAgent(self.settings, model).analyze(candidate)
        self.assertEqual(result["keywords"][0]["source"], "employer")

    def test_compound_terms_do_not_match_only_vendor(self):
        self.assertFalse(contains_term("Uses ExampleCorp systems", "ExampleCorp FeatureTool"))
        self.assertTrue(
            contains_term("Uses ExampleCorp FeatureTool daily", "ExampleCorp FeatureTool")
        )
        self.assertFalse(contains_term("Ruby", "R"))


class EmployerPolicyTests(ProjectCase):
    def test_verified_employer_restriction_applies_only_when_requested(self):
        for required in [True, False]:
            self.raw["search"].update(require_sponsorship=required, verify_employer=True)
            candidate = job()
            record = {
                **candidate.to_dict(),
                "job_description": candidate.job_description + " We cannot sponsor visas.",
            }
            model = Mock()
            model.generate.return_value = analysis()
            with patch("jobfinder.agents.employer.company_api_job", return_value=[record]):
                accepted, reason = SearchAgent(self.load(), model).analyze(candidate, "Python")
            self.assertEqual(accepted, not required)
            self.assertEqual(candidate.employer_verification["policy_status"], "restricted")

    def test_fabricated_policy_is_unverified(self):
        candidate = job()
        model = Mock()
        model.generate.return_value = {"status": "available", "evidence": ["Invented promise"]}
        with patch("jobfinder.agents.employer.company_api_job", return_value=[candidate.to_dict()]):
            result = EmployerVerifier(self.settings, model).verify(candidate)
        self.assertTrue(result["sponsorship_unverified"])
        self.assertEqual(result["policy_status"], "unknown")
