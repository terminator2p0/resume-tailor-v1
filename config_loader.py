"""Validated local configuration. Environment > .env > YAML/JSON > defaults."""

from __future__ import annotations

import copy
import ipaddress
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import yaml
from dotenv import dotenv_values
from pydantic import ValidationError

from jobfinder.config.models import AppConfig

PROJECT_ROOT = Path(__file__).resolve().parent


class ConfigurationError(ValueError):
    """Configuration cannot be used; messages never contain secret values."""


def validate_local_llm_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ConfigurationError("llm.base_url must be an HTTP(S) loopback URL.")
    if (
        parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
    ):
        raise ConfigurationError("llm.base_url must contain only the local server origin.")
    if parts.hostname != "localhost":
        try:
            local = ipaddress.ip_address(parts.hostname).is_loopback
        except ValueError:
            local = False
        if not local:
            raise ConfigurationError("llm.base_url must use localhost or a loopback IP.")
    try:
        _ = parts.port
    except ValueError as exc:
        raise ConfigurationError("llm.base_url has an invalid port.") from exc
    return url.rstrip("/")


@dataclass(frozen=True)
class Settings:
    model: AppConfig
    project_root: Path
    config_path: Path | None = None
    smtp_password: str = field(default="", repr=False)

    @property
    def data(self) -> dict[str, Any]:
        """Validated runtime view consumed by the pipeline."""
        return self._runtime

    def __post_init__(self) -> None:
        data = self.model.model_dump(exclude_none=True)
        search = data["search"]
        search["sources"] = [name for name, site in search["sites"].items() if site["enabled"]]
        search["queries"] = search["job_titles"] + search["additional_queries"]
        data["resume"]["preserve_facts"] = data["resume"]["factual_review_mode"] == "strict"
        object.__setattr__(self, "_runtime", data)

    @property
    def base_resume(self) -> Path:
        return self.resolve_path(self.data["resume"]["base_path"])

    @property
    def storage_root(self) -> Path:
        return self.resolve_path(self.data["paths"]["data"])

    @property
    def jobs_root(self) -> Path:
        return self.storage_root / "jobs" / self.data.get("run_id", "standalone")

    @property
    def output_root(self) -> Path:
        return self.resolve_path(self.data["paths"]["output"])

    @property
    def logs_root(self) -> Path:
        return self.resolve_path(self.data["paths"]["logs"])

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.data["timezone"])

    def resolve_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        return (self.project_root / path).resolve() if not path.is_absolute() else path.resolve()


def unique_mapping(pairs: list[tuple[Any, Any]]) -> dict[Any, Any]:
    """Reject repeated settings instead of silently replacing earlier values."""
    result: dict[Any, Any] = {}
    for key, value in pairs:
        try:
            if key in result:
                raise ValueError("Duplicate configuration keys are not allowed.")
            result[key] = value
        except TypeError:
            raise ValueError("Configuration keys must be scalar field names.") from None
    return result


class UniqueSafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects ambiguous duplicate keys."""


def construct_unique_mapping(loader: UniqueSafeLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    return unique_mapping(loader.construct_pairs(node, deep=True))


UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_unique_mapping
)


def load_settings(
    path: str | Path | None = None,
    *,
    project_root: Path | None = None,
    validate_resume: bool = True,
) -> Settings:
    root = (project_root or PROJECT_ROOT).resolve()
    config_path = Path(path or os.environ.get("JOBFINDER_CONFIG", "config.yaml"))
    if not config_path.is_absolute():
        config_path = root / config_path
    try:
        text = config_path.read_text(encoding="utf-8")
        if config_path.suffix.lower() == ".json":
            raw = json.loads(text, object_pairs_hook=unique_mapping)
        elif config_path.suffix.lower() in {".yaml", ".yml"}:
            raw = yaml.load(text, Loader=UniqueSafeLoader)
        else:
            raise ConfigurationError("Use a .yaml, .yml or .json configuration file.")
    except FileNotFoundError as exc:
        raise ConfigurationError(
            f"Configuration not found: {config_path}. Copy config.example.yaml to config.yaml and edit it."
        ) from exc
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ConfigurationError(
            f"Cannot parse {config_path.name}; check file permissions, YAML/JSON syntax and duplicate keys."
        ) from exc
    if not isinstance(raw, dict):
        raise ConfigurationError("Configuration must be a YAML mapping or JSON object.")
    raw = copy.deepcopy(raw)
    environment = {**dotenv_values(root / ".env"), **os.environ}
    for key, value in environment.items():
        if not key.startswith("JOBFINDER__") or value is None:
            continue
        parts = key.removeprefix("JOBFINDER__").lower().split("__")
        target = raw
        try:
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = value
            target[parts[-1]] = parsed
        except (AttributeError, TypeError) as exc:
            raise ConfigurationError(f"Invalid environment override structure: {key}.") from exc
    try:
        model = AppConfig.model_validate(raw)
    except ValidationError as exc:
        errors = [
            f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
            for item in exc.errors(include_input=False, include_url=False)
        ]
        raise ConfigurationError("Invalid configuration:\n  " + "\n  ".join(errors)) from None
    validate_local_llm_url(model.llm.base_url)
    password = environment.get(model.email.password_environment_variable, "") or ""
    settings = Settings(
        model=model, project_root=root, config_path=config_path.resolve(), smtp_password=password
    )
    roots = [settings.storage_root, settings.output_root, settings.logs_root]
    if any(folder == root for folder in roots) or any(
        b.is_relative_to(a) or a.is_relative_to(b)
        for i, a in enumerate(roots)
        for b in roots[i + 1 :]
    ):
        raise ConfigurationError(
            "paths.data, paths.output and paths.logs must be distinct, non-nested directories outside the project root itself."
        )
    if any(settings.base_resume.is_relative_to(folder) for folder in roots):
        raise ConfigurationError("Keep resume.base_path outside data, output and logs directories.")
    if validate_resume and (
        not settings.base_resume.is_file() or settings.base_resume.suffix.lower() != ".docx"
    ):
        raise ConfigurationError(
            f"resume.base_path: DOCX file not found at {settings.base_resume}. Place your resume in templates/ and update the configuration."
        )
    if validate_resume:
        from zipfile import BadZipFile, ZipFile

        try:
            with ZipFile(settings.base_resume) as archive:
                if "word/document.xml" not in archive.namelist():
                    raise BadZipFile()
        except (BadZipFile, OSError):
            raise ConfigurationError(
                "resume.base_path is not a valid Word DOCX package. Open and save it as .docx in a Word-compatible editor."
            ) from None
    # Fail on template typos before spending time scraping or running models.
    from string import Template

    common_placeholders = {key: "example" for key in ("count", "date", "titles")}
    job_placeholders = {
        key: "example"
        for key in (
            "index",
            "job_title",
            "company",
            "location",
            "work_arrangement",
            "employment_type",
            "required_experience",
            "posted",
            "application_url",
            "resume_filename",
            "matched_skills",
            "gaps",
            "sponsorship_status",
            "verification_note",
            "factual_review_note",
        )
    }
    for template_field in ("subject_template", "intro_template", "job_template", "footer_template"):
        template = getattr(model.email, template_field)
        if template:
            template_path = settings.resolve_path(template)
            if not template_path.is_file():
                raise ConfigurationError(
                    f"email.{template_field}: template file not found at {template_path}."
                )
            try:
                rendered = Template(template_path.read_text(encoding="utf-8")).substitute(
                    {
                        **common_placeholders,
                        **(job_placeholders if template_field == "job_template" else {}),
                    }
                )
                if template_field == "subject_template" and (
                    not rendered.strip() or "\n" in rendered.strip() or "\r" in rendered.strip()
                ):
                    raise ValueError()
            except (OSError, UnicodeError, KeyError, ValueError):
                raise ConfigurationError(
                    f"email.{template_field}: invalid template; use the documented $placeholders and a single-line subject."
                ) from None
    return settings
