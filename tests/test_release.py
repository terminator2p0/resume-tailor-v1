"""Public-release regressions, using disposable Git repositories and fake secrets."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import ProjectCase

import main
from config_loader import PROJECT_ROOT, ConfigurationError
from scripts.check_release import check_release


class DeliveryArgumentTests(unittest.TestCase):
    def test_conflicting_delivery_flags_never_load_credentials_or_send(self):
        for flags in [
            ["--send-outbox", "example", "--preview"],
            ["--send-outbox", "example", "--sample"],
            ["--send-outbox", "example", "--jobs", "jobs.json"],
            ["--resolve-outbox", "example", "--delivery", "sent", "--preview"],
            ["--sample", "--send"],
            ["--search-only", "--send"],
        ]:
            with (
                self.subTest(flags=flags),
                patch("main.load_settings") as loader,
                patch("main.EmailAgent") as email,
            ):
                with self.assertRaises(ValueError):
                    main.run(main.parser().parse_args(flags))
                loader.assert_not_called()
                email.assert_not_called()

    def test_standalone_delivery_and_preview_arguments_remain_supported(self):
        for flags in [
            ["--send-outbox", "example"],
            ["--preview"],
            ["--sample"],
            ["--resolve-outbox", "example", "--delivery", "not_sent"],
        ]:
            main.validate_arguments(main.parser().parse_args(flags))


class StartupReleaseTests(ProjectCase):
    def test_password_variable_requires_name_not_a_secret(self):
        self.raw["email"]["password_environment_variable"] = "abcdefghijklmnop"
        with self.assertRaises(ConfigurationError) as error:
            self.load()
        self.assertIn("environment variable NAME", str(error.exception))
        self.assertNotIn("abcdefghijklmnop", str(error.exception))

    def test_valid_custom_secret_name(self):
        self.raw["email"]["password_environment_variable"] = "CUSTOM_SMTP_SECRET"
        with patch.dict(os.environ, {"CUSTOM_SMTP_SECRET": "synthetic-secret-value"}):
            settings = self.load()
        self.assertEqual(settings.smtp_password, "synthetic-secret-value")

    def test_duplicate_yaml_and_json_keys_fail_without_echoing_values(self):
        for name, content in [
            ("duplicate.yaml", "email:\n  enabled: true\n  enabled: false\n"),
            ("duplicate.json", '{"email": {"enabled": true, "enabled": false}}'),
        ]:
            path = self.root / name
            path.write_text(content, encoding="utf-8")
            from config_loader import load_settings

            with self.assertRaisesRegex(ConfigurationError, "duplicate keys"):
                load_settings(path, project_root=self.root)

    def test_mailbox_separator_in_single_address_rejected(self):
        self.raw["email"]["recipient"] = "first,second@example.com"
        with self.assertRaises(ConfigurationError):
            self.load()


class PublicFileTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        shutil.copy(PROJECT_ROOT / ".gitignore", self.root / ".gitignore")
        for name in (
            "README.md",
            "REQUIREMENTS.md",
            "requirements.txt",
            "config.example.yaml",
            ".env.example",
        ):
            (self.root / name).write_text("Generic setup example\n", encoding="utf-8")

    def test_private_files_ignored_even_in_nested_custom_resume_directory(self):
        for name in (
            ".env",
            "config.yaml",
            "personal/resume.docx",
            "custom-output/digest.eml",
            "data/state.json",
        ):
            p = self.root / name
            p.parent.mkdir(exist_ok=True)
            p.write_text("# Private local content\n", encoding="utf-8")
        result = check_release(self.root)
        self.assertFalse(result.errors)
        self.assertNotIn("personal/resume.docx", result.files)
        self.assertIn("config.example.yaml", result.files)

    def test_force_added_private_file_is_rejected(self):
        (self.root / ".env").write_text("JOBFINDER_SMTP_PASSWORD=synthetic-secret-value\n")
        subprocess.run(["git", "add", "-f", ".env"], cwd=self.root, check=True, capture_output=True)
        result = check_release(self.root)
        self.assertTrue(result.errors)
        self.assertNotIn("synthetic-secret-value", "\n".join(result.errors))

    def test_known_local_secret_in_public_file_is_detected_without_printing_value(self):
        (self.root / ".env").write_text("JOBFINDER_SMTP_PASSWORD=synthetic-secret-value\n")
        (self.root / "accidental.txt").write_text("synthetic-secret-value")
        result = check_release(self.root)
        self.assertTrue(any("accidental.txt" in error for error in result.errors))
        self.assertNotIn("synthetic-secret-value", "\n".join(result.errors))

    def test_undecided_license_is_reported_without_adding_one(self):
        result = check_release(self.root)
        self.assertTrue(any("License is undecided" in note for note in result.notes))
        self.assertFalse((self.root / "LICENSE").exists())
