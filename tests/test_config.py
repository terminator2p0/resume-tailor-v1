from __future__ import annotations

import copy
import json
import os
from unittest.mock import patch

import yaml
from helpers import ProjectCase

from config_loader import PROJECT_ROOT, ConfigurationError, load_settings, validate_local_llm_url


class ConfigTests(ProjectCase):
    def test_generic_example_validates_without_private_resume(self):
        settings = load_settings(PROJECT_ROOT / "config.example.yaml", validate_resume=False)
        self.assertEqual(settings.data["resume"]["factual_review_mode"], "strict")
        self.assertFalse(settings.data["email"]["enabled"])

    def test_yaml_and_json_are_equivalent(self):
        path = self.root / "custom.yaml"
        path.write_text(yaml.safe_dump(self.raw))
        self.assertEqual(load_settings(path, project_root=self.root).data, self.settings.data)

    def test_missing_config_explains_copy_step(self):
        with self.assertRaisesRegex(ConfigurationError, "Copy config.example.yaml"):
            load_settings(self.root / "absent.yaml")

    def test_missing_required_keys_name_field(self):
        del self.raw["search"]["job_titles"]
        with self.assertRaisesRegex(ConfigurationError, "search.job_titles"):
            self.load()

    def test_unknown_fields_rejected(self):
        self.raw["search"]["job_titels"] = ["typo"]
        with self.assertRaisesRegex(ConfigurationError, "job_titels"):
            self.load()

    def test_strict_boolean_and_integer_validation(self):
        for value in ["false", 1]:
            self.raw["email"]["enabled"] = value
            with self.assertRaises(ConfigurationError):
                self.load()
        self.raw["email"]["enabled"] = False
        self.raw["search"]["batch_size"] = True
        with self.assertRaises(ConfigurationError):
            self.load()

    def test_bounds_and_candidate_limits(self):
        for changes in [
            {"minimum_required_experience": 6, "maximum_required_experience": 2},
            {"batch_size": 20, "max_candidates_to_tailor": 10},
            {"recent_share": 1.1},
            {"recent_share": float("nan")},
        ]:
            original = copy.deepcopy(self.raw)
            self.raw["search"].update(changes)
            with self.assertRaises(ConfigurationError):
                self.load()
            self.raw = original

    def test_missing_resume_path(self):
        self.raw["resume"]["base_path"] = "missing.docx"
        with self.assertRaisesRegex(ConfigurationError, "resume.base_path"):
            self.load()

    def test_paths_relative_to_project_not_current_directory(self):
        with patch("os.getcwd", return_value="/"):
            self.assertEqual(self.load().base_resume, (self.root / "templates/base.docx").resolve())

    def test_overlapping_outputs_and_source_rejected(self):
        for paths in [{"data": "data", "logs": "data/logs"}, {"data": "."}, {"data": "templates"}]:
            self.raw["paths"] = paths
            with self.assertRaises(ConfigurationError):
                self.load()

    def test_environment_overrides_dotenv_and_file(self):
        (self.root / ".env").write_text(
            "JOBFINDER__SEARCH__BATCH_SIZE=3\nJOBFINDER_SMTP_PASSWORD=file-secret\n"
        )
        with patch.dict(
            os.environ,
            {"JOBFINDER__SEARCH__BATCH_SIZE": "4", "JOBFINDER_SMTP_PASSWORD": "env-secret"},
        ):
            settings = self.load()
        self.assertEqual(settings.data["search"]["batch_size"], 4)
        self.assertEqual(settings.smtp_password, "env-secret")
        self.assertNotIn("env-secret", repr(settings))
        self.assertNotIn("env-secret", json.dumps(settings.data))

    def test_environment_lists_and_modes(self):
        with patch.dict(
            os.environ,
            {
                "JOBFINDER__SEARCH__JOB_TITLES": '["Designer"]',
                "JOBFINDER__RESUME__FACTUAL_REVIEW_MODE": "advisory",
            },
        ):
            settings = self.load()
        self.assertEqual(settings.data["search"]["job_titles"], ["Designer"])
        self.assertFalse(settings.data["resume"]["preserve_facts"])

    def test_enabled_email_requires_provider_fields(self):
        self.raw["email"]["enabled"] = True
        with self.assertRaisesRegex(ConfigurationError, "sender, recipient and host"):
            self.load()

    def test_header_injection_and_extra_recipient_rejected(self):
        for address in [
            "person@example.com\nBcc: hidden@example.com",
            "a@example.com,b@example.com",
        ]:
            self.raw["email"]["recipient"] = address
            with self.assertRaises(ConfigurationError):
                self.load()

    def test_template_path_error(self):
        self.raw["email"]["subject_template"] = "missing.txt"
        with self.assertRaisesRegex(ConfigurationError, "subject_template"):
            self.load()

    def test_unknown_timezone_and_remote_llm_rejected(self):
        self.raw["timezone"] = "No/SuchZone"
        with self.assertRaises(ConfigurationError):
            self.load()
        for url in [
            "https://example.com",
            "http://localhost:11434/api",
            "http://user:pass@localhost:11434",
            "http://localhost:bad",
        ]:
            with self.assertRaises(ConfigurationError):
                validate_local_llm_url(url)
        self.assertEqual(validate_local_llm_url("http://[::1]:11434/"), "http://[::1]:11434")

    def test_site_toggles_and_disabled_import_mode(self):
        self.raw["search"]["sites"] = {
            name: {"enabled": False}
            for name in ["linkedin", "indeed", "glassdoor", "company_careers"]
        }
        self.assertEqual(self.load().data["search"]["sources"], [])

    def test_cloud_tag_rejected(self):
        self.raw["llm"]["models"]["resume_tailorer"] = "model:cloud"
        with self.assertRaises(ConfigurationError):
            self.load()

    def test_invalid_docx_package_has_clear_error(self):
        self.settings.base_resume.write_text("not a DOCX")
        with self.assertRaisesRegex(ConfigurationError, "valid Word DOCX"):
            self.load()

    def test_template_placeholders_validated_at_startup(self):
        (self.root / "subject.txt").write_text("$unsupported")
        self.raw["email"]["subject_template"] = "subject.txt"
        with self.assertRaisesRegex(ConfigurationError, "subject_template"):
            self.load()
