"""Check the files Git would publish, without printing private values.

Run from any folder: python /path/to/repository/scripts/check_release.py
This is a release check, not a replacement for a dedicated secret scanner.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import dotenv_values


@dataclass
class ReleaseCheck:
    files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def check_release(root: Path) -> ReleaseCheck:
    result = ReleaseCheck()
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    result.files = sorted(set(filter(None, os.fsdecode(completed.stdout).split("\0"))))
    # Compare actual local values in memory. Never display them or send them anywhere.
    private_values: set[bytes] = set()
    environment = dict(os.environ)
    env_path = root / ".env"
    if env_path.is_file():
        local_environment = dotenv_values(env_path)
        environment = {**local_environment, **environment}
        for key, value in local_environment.items():
            if (
                value
                and len(value) >= 8
                and any(part in key.upper() for part in ("PASSWORD", "SECRET", "TOKEN", "API_KEY"))
            ):
                private_values.add(value.encode())
    config_path = root / "config.yaml"
    if config_path.is_file():
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if isinstance(config, dict) and isinstance(config.get("email"), dict):
            secret_name = config["email"].get(
                "password_environment_variable", "JOBFINDER_SMTP_PASSWORD"
            )
            secret = environment.get(secret_name) if isinstance(secret_name, str) else None
            if secret and len(secret) >= 8:
                private_values.add(secret.encode())
            for key in ("sender", "recipient", "username"):
                value = config["email"].get(key)
                if isinstance(value, str) and value and not value.endswith("@example.com"):
                    private_values.add(value.encode())
    home = str(Path.home())
    if len(home) > 3:
        private_values.add(home.encode())
    for name in result.files:
        path = root / name
        if path.is_symlink():
            result.errors.append(f"{name}: symbolic links must be reviewed before publishing.")
            continue
        if not path.is_file():
            result.errors.append(f"{name}: indexed file is missing from the working directory.")
            continue
        if (
            path.name.startswith(".env")
            and path.name != ".env.example"
            or path.name in {"config.yaml", "config.yml", "config.json"}
            or path.suffix.lower() in {".docx", ".eml"}
            or Path(name).parts[0] in {"data", "logs", "output", ".venv"}
        ):
            result.errors.append(f"{name}: private/runtime file would be published.")
        data = path.read_bytes()
        if any(value in data for value in private_values):
            result.errors.append(
                f"{name}: contains a local secret, personal address or home path (value hidden)."
            )
        if any(
            marker in data
            for marker in (
                b"-----BEGIN " + b"PRIVATE KEY-----",
                b"-----BEGIN " + b"OPENSSH PRIVATE KEY-----",
            )
        ):
            result.errors.append(f"{name}: contains a private-key marker.")
    for name in (
        "README.md",
        "REQUIREMENTS.md",
        "requirements.txt",
        "config.example.yaml",
        ".env.example",
    ):
        if name not in result.files:
            result.errors.append(f"{name}: required public setup file is missing or ignored.")
    if not (root / "LICENSE").is_file():
        result.notes.append(
            "License is undecided; do not describe this repository as open source until usage terms are chosen."
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        result = check_release(args.root)
    except (OSError, subprocess.CalledProcessError, ValueError, yaml.YAMLError):
        print(
            "Release check failed: verify the repository path, Git installation and local configuration syntax."
        )
        return 1
    print(
        f"Checked {len(result.files)} prospective public files; private values are never printed."
    )
    for note in result.notes:
        print("NOTE: " + note)
    for error in result.errors:
        print("ERROR: " + error)
    if not result.errors:
        print("Public-file checks passed.")
    return int(bool(result.errors))


if __name__ == "__main__":
    raise SystemExit(main())
