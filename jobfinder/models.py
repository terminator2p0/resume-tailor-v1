from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from .common import canonical_url, digest, utcnow


def parse_posted_at(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (
            stamp.replace(tzinfo=timezone.utc)
            if stamp.tzinfo is None
            else stamp.astimezone(timezone.utc)
        )
    except ValueError:
        return None


@dataclass
class Job:
    job_title: str
    company: str
    location: str
    job_description: str
    application_url: str
    source: str = "import"
    source_posting_id: str = ""
    source_url: str = ""
    posted_at: str | None = None
    expires_at: str | None = None
    employment_type: str = "unknown"
    work_arrangement: str = "unknown"
    qualifications: list[str] = field(default_factory=list)
    sponsorship_status: str = "unknown"
    sponsorship_evidence: list[str] = field(default_factory=list)
    required_experience: str = "unknown"
    skills: list[str] = field(default_factory=list)
    matching_score: float = 0.0
    date_precision: str = "unknown"
    analysis: dict = field(default_factory=dict)
    employer_verification: dict = field(default_factory=dict)
    keyword_plan: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("job_title", "company", "location", "job_description", "application_url"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"A job needs a non-empty {name}.")
        self.application_url = canonical_url(self.application_url)
        if self.source_url:
            self.source_url = canonical_url(self.source_url)
        if len(self.job_description.strip()) < 120:
            raise ValueError("The job description is too short to tailor a resume reliably.")
        if self.posted_at and parse_posted_at(self.posted_at) is None:
            self.posted_at = None

    @property
    def posting_id(self) -> str:
        return digest(self.source + "|" + (self.source_posting_id or self.application_url))[:20]

    @property
    def identity_keys(self) -> set[str]:
        keys = {"url:" + self.application_url}
        if self.source_url:
            keys.add("url:" + self.source_url)
        if self.source_posting_id:
            keys.add(f"id:{self.source}:{self.source_posting_id}")
        # Collapse mirrors of the same company/title/location across boards.
        norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
        keys.add(
            "role:"
            + digest("|".join(norm(s) for s in (self.company, self.job_title, self.location)))
        )
        return keys

    def is_recent(self, days: int, now: datetime | None = None) -> bool:
        now = now or utcnow()
        stamp = parse_posted_at(self.posted_at)
        return stamp is not None and now - timedelta(days=days) <= stamp <= now

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "posting_id": self.posting_id,
            "identity_keys": sorted(self.identity_keys),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        fields = cls.__dataclass_fields__
        return cls(**{key: value for key, value in data.items() if key in fields})
