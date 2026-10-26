"""Strict, discoverable application settings; unknown fields are errors."""

from __future__ import annotations

from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Text = Annotated[str, Field(min_length=1)]
Section = Literal["summary", "skills", "experience_bullets", "project_descriptions"]


class ConfigModel(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", str_strip_whitespace=True, validate_default=True
    )


class SiteConfig(ConfigModel):
    enabled: bool = True
    request_interval_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 3.0


class SitesConfig(ConfigModel):
    linkedin: SiteConfig = Field(default_factory=SiteConfig)
    indeed: SiteConfig = Field(default_factory=SiteConfig)
    glassdoor: SiteConfig = Field(default_factory=SiteConfig)
    company_careers: SiteConfig = Field(default_factory=SiteConfig)


class SearchConfig(ConfigModel):
    job_titles: Annotated[list[Text], Field(min_length=1)]
    country: Text
    locations: Annotated[list[Text], Field(min_length=1)]
    experience_levels: list[Text] = Field(default_factory=list)
    exclusion_keywords: list[Text] = Field(default_factory=list)
    tracked_keywords: list[Text] = Field(default_factory=list)
    additional_queries: list[Text] = Field(default_factory=list)
    work_arrangements: Annotated[
        list[Literal["remote", "hybrid", "onsite"]], Field(min_length=1)
    ] = ["remote", "hybrid", "onsite"]
    employment_types: Annotated[
        list[
            Literal[
                "full_time", "contract", "contract_to_hire", "part_time", "internship", "temporary"
            ]
        ],
        Field(min_length=1),
    ] = ["full_time"]
    keep_unknown_work_arrangement: bool = True
    keep_unknown_employment_type: bool = True
    keep_unknown_experience: bool = True
    minimum_required_experience: Annotated[int, Field(ge=0)] = 0
    maximum_required_experience: Annotated[int, Field(ge=0)] = 50
    require_sponsorship: bool = False
    keep_unknown_sponsorship: bool = True
    verify_employer: bool = True
    batch_size: PositiveInt = 5
    recent_days: PositiveInt = 7
    recent_share: Annotated[float, Field(ge=0, le=1)] = 1.0
    results_per_source: PositiveInt = 20
    source_timeout_seconds: PositiveInt = 90
    max_candidates_to_analyze: PositiveInt = 25
    max_candidates_to_tailor: PositiveInt = 10
    sites: SitesConfig = Field(default_factory=SitesConfig)
    company_urls: list[Text] = Field(default_factory=list)
    company_domains: list[Text] = Field(
        default_factory=lambda: [
            "jobs.lever.co",
            "boards.greenhouse.io",
            "job-boards.greenhouse.io",
            "myworkdayjobs.com",
        ]
    )
    discovery_region: Text = "wt-wt"

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.minimum_required_experience > self.maximum_required_experience:
            raise ValueError("minimum_required_experience must be <= maximum_required_experience")
        if min(self.max_candidates_to_analyze, self.max_candidates_to_tailor) < self.batch_size:
            raise ValueError("candidate limits must be at least batch_size")
        # All sites may be disabled for imported-job execution.
        from urllib.parse import urlsplit

        for url in self.company_urls:
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("company_urls must contain HTTP(S) URLs without credentials")
        import re

        if any(
            not re.fullmatch(r"[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", domain)
            for domain in self.company_domains
        ):
            raise ValueError("company_domains must contain plain domain names")
        return self


class ModelsConfig(ConfigModel):
    job_parser: Text
    keyword_extractor: Text
    resume_tailorer: Text
    resume_reviewer: Text

    @field_validator("*")
    @classmethod
    def local_tag(cls, value: str) -> str:
        if "cloud" in value.lower():
            raise ValueError("cloud-backed models are unsupported; use a downloaded local model")
        return value


class ReasoningConfig(ConfigModel):
    job_parser: Literal["low", "medium", "high"] = "low"
    keyword_extractor: Literal["low", "medium", "high"] = "medium"
    resume_tailorer: Literal["low", "medium", "high"] = "medium"
    resume_reviewer: Literal["low", "medium", "high"] = "low"


class AgentLimits(ConfigModel):
    timeout_seconds: PositiveFloat | None = None
    num_ctx: PositiveInt | None = None
    max_input_characters: PositiveInt | None = None


class LLMConfig(ConfigModel):
    base_url: Text = "http://localhost:11434"
    models: ModelsConfig
    reasoning: ReasoningConfig = Field(default_factory=ReasoningConfig)
    timeout_seconds: PositiveFloat = 600.0
    keep_alive: Text = "0"
    num_ctx: Annotated[int, Field(ge=4096)] = 16384
    num_predict: PositiveInt = 4096
    max_input_characters: PositiveInt = 50000
    retries: Annotated[int, Field(ge=0, le=2)] = 1
    agent_limits: dict[
        Literal["job_parser", "keyword_extractor", "resume_tailorer", "resume_reviewer"],
        AgentLimits,
    ] = Field(default_factory=dict)


class ResumeConfig(ConfigModel):
    base_path: Text
    factual_review_mode: Literal["strict", "advisory"] = "strict"
    source_context_scope: Literal["role", "full_resume"] = "role"
    editable_sections: Annotated[list[Section], Field(min_length=1)] = [
        "summary",
        "skills",
        "experience_bullets",
        "project_descriptions",
    ]
    wording_strategy: Literal["blend_job_language", "prefer_verbatim_job_lines", "rephrase"] = (
        "blend_job_language"
    )
    save_updated_skills: bool = False
    # Labels, not paragraph positions. Extend these for another language or layout.
    section_aliases: dict[Section, list[Text]] = Field(
        default_factory=lambda: {
            "summary": ["summary", "professional summary", "profile", "objective", "about me"],
            "skills": ["skills", "technical skills", "core competencies", "expertise"],
            "experience_bullets": [
                "experience",
                "work experience",
                "professional experience",
                "employment history",
            ],
            "project_descriptions": ["projects", "personal projects", "selected projects"],
        }
    )
    protected_headings: list[Text] = Field(
        default_factory=lambda: [
            "education",
            "certifications",
            "certificates",
            "awards",
            "publications",
            "languages",
            "interests",
            "references",
        ]
    )


class EmailConfig(ConfigModel):
    enabled: bool = False
    sender: str = ""
    recipient: str = ""
    username: str = ""
    host: str = ""
    port: Annotated[int, Field(ge=1, le=65535)] = 465
    security: Literal["ssl", "starttls"] = "ssl"
    password_environment_variable: Text = "JOBFINDER_SMTP_PASSWORD"
    timeout_seconds: PositiveFloat = 30.0
    max_message_bytes: PositiveInt = 20000000
    subject_template: str | None = None
    intro_template: str | None = None
    job_template: str | None = None
    footer_template: str | None = None

    @field_validator("password_environment_variable")
    @classmethod
    def valid_password_variable(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", value):
            raise ValueError(
                "use an uppercase environment variable NAME such as JOBFINDER_SMTP_PASSWORD; put the password value in .env"
            )
        return value

    @model_validator(mode="after")
    def valid_email(self) -> Self:
        import re

        for field in ("sender", "recipient"):
            value = getattr(self, field)
            if value and not re.fullmatch(r"[^,;:\s<>@]+@[^,;:\s<>@]+\.[^,;:\s<>@]+", value):
                raise ValueError(f"{field} must be a single email address")
        if any(
            "\n" in value or "\r" in value
            for value in (self.host, self.username, self.password_environment_variable)
        ):
            raise ValueError("SMTP settings cannot contain line breaks")
        if self.enabled and not all((self.sender, self.recipient, self.host)):
            raise ValueError("enabled email requires sender, recipient and host")
        return self


class PathsConfig(ConfigModel):
    data: Text = "data"
    output: Text = "output"
    logs: Text = "logs"


class AppConfig(ConfigModel):
    search: SearchConfig
    llm: LLMConfig
    resume: ResumeConfig
    email: EmailConfig = Field(default_factory=EmailConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    timezone: Text = "UTC"

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError("use an IANA timezone, such as UTC or Europe/London") from None
        return value
