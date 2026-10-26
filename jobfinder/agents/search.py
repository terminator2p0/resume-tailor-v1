from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from config_loader import Settings

    from ..models import Job
    from ..protocols import LocalModel

import re

from ..common import skill_text, utcnow
from ..models import Job, parse_posted_at
from ..scrapers.sources import run_source
from .employer import EmployerVerifier

NEGATIVE_POLICY = [
    r"\b(?:no|without)\s+(?:visa\s+|immigration\s+|employment\s+)?sponsorship\s+(?:is\s+)?(?:available|provided|offered)",
    r"\b(?:cannot|can't|unable\s+to|will\s+not|won't|do\s+not|does\s+not|not\s+able\s+to)\s+(?:provide\s+|offer\s+|support\s+)?(?:visa\s+|immigration\s+|employment\s+)?sponsor(?:ship)?\b",
    r"\bsponsorship\s+(?:is\s+|will\s+be\s+)?(?:not\s+(?:available|provided|offered|supported)|unavailable)\b",
    r"\b(?:without|not\s+require|not\s+requiring)\s+(?:current\s+or\s+future\s+|future\s+|now\s+or\s+in\s+the\s+future\s+)?(?:visa\s+|employment\s+)?sponsorship\b",
    r"\b(?:no|not\s+(?:accepting|eligible)|cannot\s+(?:accept|hire))\s+(?:(?:candidates|applicants)\s+(?:on|with)\s+)?(?:h[ -]?1b|stem[ -]?opt|opt)\b",
    r"\b(?:h[ -]?1b|stem[ -]?opt|opt)\s+(?:candidates\s+|applicants\s+)?(?:are\s+|is\s+)?(?:not\s+(?:accepted|eligible|supported)|ineligible)\b",
]


def sponsorship_restrictions(text: str) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+|[\r\n]+", text)
    return [
        sentence.strip()
        for sentence in sentences
        if any(re.search(pattern, sentence, re.I) for pattern in NEGATIVE_POLICY)
    ]


def extract_skills(text: str, keywords: list[str] | None = None) -> list[str]:
    """Match configured terms; the keyword agent supplies open-ended terms later."""
    return [
        skill
        for skill in (keywords or [])
        if re.search(r"(?<!\w)" + re.escape(skill) + r"(?!\w)", text, re.I)
    ]


ANALYSIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "target_role_match": {"type": "boolean"},
        "location_eligible": {"type": "boolean"},
        "location_evidence": {"type": "string"},
        "employment_type": {
            "enum": [
                "full_time",
                "contract",
                "contract_to_hire",
                "part_time",
                "internship",
                "temporary",
                "unknown",
            ]
        },
        "work_arrangement": {"enum": ["remote", "hybrid", "onsite", "unknown"]},
        "sponsorship_status": {"enum": ["restricted", "available", "unknown"]},
        "sponsorship_evidence": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        "required_experience": {"type": "string"},
        "required_experience_min": {"type": ["integer", "null"], "minimum": 0},
        "required_experience_max": {"type": ["integer", "null"], "minimum": 0},
        "experience_evidence": {"type": "string"},
        "experience_level": {"type": "string"},
        "experience_level_match": {"type": "boolean"},
        "qualifications": {"type": "array", "items": {"type": "string"}},
    },
}
ANALYSIS_SCHEMA["required"] = list(ANALYSIS_SCHEMA["properties"])


def experience_filter(result: dict, description: str, config: dict) -> tuple[bool, str]:
    """Validate quoted experience bounds before applying the configured band.

    An open-ended 3+/4+/5+ requirement qualifies by its stated minimum. A
    closed range must fit entirely inside the configured band.
    """
    minimum, maximum = result["required_experience_min"], result["required_experience_max"]
    evidence = result["experience_evidence"].strip()
    if minimum is None and maximum is None:
        return (
            (True, "Experience not stated; retained for review.")
            if config["keep_unknown_experience"]
            else (False, "Unknown required experience excluded by configuration.")
        )
    # Some local models add quotation marks around an otherwise verbatim quote.
    if (
        evidence.casefold() not in description.casefold()
        and len(evidence) >= 2
        and (evidence[0], evidence[-1]) in {('"', '"'), ("'", "'"), ("“", "”")}
    ):
        evidence = evidence[1:-1].strip()
        result["experience_evidence"] = evidence
    if not evidence or evidence.casefold() not in description.casefold():
        return False, "Experience evidence was not found in the posting."
    # Check that the quoted text actually contains the model's year values.
    normalized = evidence.casefold()
    for number, word in enumerate(
        (
            "zero",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
        )
    ):
        normalized = re.sub(r"\b" + word + r"\b", str(number), normalized)
    normalized = re.sub(r"(\d+)\s*\(\s*\1\s*\)", r"\1", normalized)
    pairs = [
        (int(a), int(b) if b else None)
        for a, b in re.findall(
            r"\b(\d+)\s*(?:[-–—]\s*|to\s+)?(?:(\d+)\s*)?\+?\s*(?:years?|yrs?)\b", normalized
        )
    ]
    values = {value for pair in pairs for value in pair if value is not None}
    if any(value is not None and value not in values for value in (minimum, maximum)):
        return False, "Experience bounds were not supported by quoted year requirements."
    if (
        minimum is not None
        and maximum is not None
        and (minimum > maximum or (minimum, maximum) not in pairs)
    ):
        return False, "Experience range was not supported by the posting."
    lower, upper = config["minimum_required_experience"], config["maximum_required_experience"]
    if maximum is not None and maximum > upper:
        return False, f"Required experience is outside the configured {lower}–{upper} year range."
    if minimum is None:
        return (
            (True, "Minimum experience not stated; retained for review.")
            if config["keep_unknown_experience"]
            else (False, "Unknown required experience excluded by configuration.")
        )
    if minimum < lower or minimum > upper or maximum is not None and maximum > upper:
        return False, f"Required experience is outside the configured {lower}–{upper} year range."
    return True, "Required experience fits the configured range."


class SearchAgent:
    def __init__(self, settings: Settings, llm: LocalModel) -> None:
        self.settings, self.llm = settings, llm
        self.instructions = skill_text(settings.project_root, "job_scraper")
        self.employer_verifier = EmployerVerifier(settings, llm)

    def discover(self, progress: Any = print) -> tuple[list[Job], list[str]]:
        jobs, errors = [], []
        config = self.settings.data["search"]
        if not config["sources"]:
            raise ValueError(
                "No job sites enabled. Enable search.sites entries or use --jobs FILE."
            )
        import time

        for source in config["sources"]:
            interval = config["sites"][source]["request_interval_seconds"]
            for query in config["queries"]:
                for location in config["locations"]:
                    for recent in (
                        (True,)
                        if config["recent_share"] == 1
                        else (False,)
                        if config["recent_share"] == 0
                        else (True, False)
                    ):
                        if getattr(self, "_last_request", None) is not None:
                            time.sleep(max(0, interval - (time.monotonic() - self._last_request)))
                        progress(
                            f"Searching {source}: {query} in {location} ({'recent' if recent else 'any age'})"
                        )
                        candidates, diagnostics = run_source(
                            self.settings, source, query, recent, location=location
                        )
                        self._last_request = time.monotonic()
                        jobs.extend(candidates)
                        errors.extend(diagnostics)
        return jobs, errors

    def analyze(self, job: Job, resume_text: str) -> tuple[bool, str]:
        config = self.settings.data["search"]
        if any(
            term.casefold() in (job.job_title + "\n" + job.job_description).casefold()
            for term in config["exclusion_keywords"]
        ):
            return False, "Posting contains a configured exclusion keyword."
        restrictions = (
            sponsorship_restrictions(job.job_description) if config["require_sponsorship"] else []
        )
        if restrictions:
            job.sponsorship_status, job.sponsorship_evidence = "restricted", restrictions
            return False, "Explicit sponsorship/OPT restriction."
        expires = parse_posted_at(job.expires_at)
        if expires is not None and expires < utcnow():
            return False, "Posting has expired."
        prompt = (
            self.instructions
            + "\nAssess target_role_match and location_eligible against supplied preferences, including remote geographic restrictions. Quote exact location evidence. Classify experience_level and experience_level_match against the supplied levels; an empty level list accepts all levels. Never infer experience years from a title. For mandatory years, return min and max with verbatim experience_evidence: 3+ means min=3, max=null; 3-5 means min=3, max=5. Use the highest simultaneous minimum, or lowest qualifying alternative education route. Ignore preferred years. Unknown years means null bounds and empty evidence. Sponsorship available/restricted requires exact quotes; a general work-authorization requirement is unknown."
        )
        result = self.llm.generate(
            "job_parser",
            prompt,
            {
                "job_title": job.job_title,
                "company": job.company,
                "location": job.location,
                "description": job.job_description,
                "employment_type": job.employment_type,
                "work_arrangement": job.work_arrangement,
                "preferences": {
                    key: config[key]
                    for key in (
                        "job_titles",
                        "country",
                        "locations",
                        "experience_levels",
                        "work_arrangements",
                        "employment_types",
                    )
                },
            },
            ANALYSIS_SCHEMA,
        )
        job.analysis = result
        evidence = result["sponsorship_evidence"]
        if any(
            not quote.strip() or quote.lower() not in job.job_description.lower()
            for quote in evidence
        ):
            return False, "Parser sponsorship evidence was not found in the posting."
        if result["sponsorship_status"] != "unknown" and not evidence:
            return False, "Parser classified a policy without supporting evidence."
        job.sponsorship_status, job.sponsorship_evidence = result["sponsorship_status"], evidence
        job.employment_type, job.work_arrangement = (
            result["employment_type"],
            result["work_arrangement"],
        )
        job.required_experience, job.qualifications = (
            result["required_experience"],
            result["qualifications"],
        )
        job.skills = extract_skills(job.job_description, config["tracked_keywords"])
        experience_ok, experience_reason = experience_filter(
            result, job.job_description, self.settings.data["search"]
        )
        if not experience_ok:
            return False, experience_reason
        supported = {skill.lower() for skill in extract_skills(resume_text, job.skills)}
        job.matching_score = sum(skill.lower() in supported for skill in job.skills) / max(
            len(job.skills), 1
        )
        if not result["target_role_match"]:
            return False, "Role is outside configured job titles."
        if not result["location_eligible"]:
            return False, "Configured location eligibility was not established."
        location_evidence = result["location_evidence"].strip()
        normalize = lambda text: re.sub(r"\W+", " ", text.casefold()).strip()
        if not location_evidence or normalize(location_evidence) not in normalize(
            job.location + "\n" + job.job_description
        ):
            return False, "Location evidence was not found in the supplied posting."
        if config["experience_levels"] and not result["experience_level_match"]:
            return False, "Experience level is outside the configured selection."
        if job.work_arrangement == "unknown":
            if not config["keep_unknown_work_arrangement"]:
                return False, "Unknown work arrangement excluded by configuration."
        elif job.work_arrangement not in config["work_arrangements"]:
            return False, "Work arrangement is outside the configured selection."
        if job.employment_type == "unknown" and not config["keep_unknown_employment_type"]:
            return False, "Unknown employment type excluded by configuration."
        if config["require_sponsorship"] and job.sponsorship_status == "restricted":
            return False, "Explicit sponsorship/OPT restriction."
        if (
            job.employment_type != "unknown"
            and job.employment_type not in self.settings.data["search"]["employment_types"]
        ):
            return False, "Employment type is outside the configured selection."
        employer = (
            self.employer_verifier.verify(job)
            if config["verify_employer"]
            else {
                "policy_status": "unknown",
                "evidence": [],
                "sponsorship_unverified": True,
                "reason": "Employer verification disabled by configuration.",
            }
        )
        job.employer_verification = employer
        if employer["policy_status"] != "unknown":
            job.sponsorship_status = employer["policy_status"]
            job.sponsorship_evidence = employer["evidence"]
        if config["require_sponsorship"] and employer["policy_status"] == "restricted":
            return False, "Explicit sponsorship/OPT restriction on employer posting."
        if (
            config["require_sponsorship"]
            and job.sponsorship_status == "unknown"
            and not config["keep_unknown_sponsorship"]
        ):
            return False, "Unknown sponsorship excluded by configuration."
        return True, "Eligible for user review."
