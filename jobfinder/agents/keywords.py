"""Evidence-backed keyword planning before the resume model resume tailoring."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from config_loader import Settings

    from ..models import Job
    from ..protocols import LocalModel

import copy
import re
import textwrap
import uuid

from jsonschema import ValidationError, validate

from ..common import atomic_json, digest, skill_text, utcnow
from ..llm import ModelError

KEYWORD_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "keywords": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "term": {"type": "string", "minLength": 1},
                    "category": {"enum": ["technology", "competency", "domain"]},
                    "priority": {"enum": ["high", "medium", "low"]},
                    "requirement": {"enum": ["required", "preferred", "responsibility", "context"]},
                    "source": {"enum": ["board", "employer"]},
                    "evidence": {"type": "string", "minLength": 1},
                    "why": {"type": "string", "minLength": 1},
                    "placement": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": {
                            "enum": [
                                "summary",
                                "skills",
                                "experience_bullets",
                                "project_descriptions",
                            ]
                        },
                    },
                },
                "required": [
                    "term",
                    "category",
                    "priority",
                    "requirement",
                    "source",
                    "evidence",
                    "why",
                    "placement",
                ],
            },
        },
    },
    "required": ["keywords"],
}

# The model cites a line identifier instead of copying long evidence for each
# technology. The runner restores exact readable excerpts for the audit/handoff.
EXTRACTION_SCHEMA = copy.deepcopy(KEYWORD_SCHEMA)
wire_items = EXTRACTION_SCHEMA["properties"]["keywords"]
wire_items["minItems"] = 0  # Some sections contain only perks/legal boilerplate.
wire_fields = wire_items["items"]["properties"]
for name in ("evidence", "source"):
    wire_fields.pop(name)
wire_fields["line_id"] = {"type": "string", "minLength": 1}
wire_fields["why"]["maxLength"] = 120
wire_items["items"]["required"] = list(wire_fields)


def description_text(text: str) -> str:
    """Readable text with inline HTML removed; keep the raw posting untouched."""
    if re.search(r"</?(?:div|p|li|b|strong|span|br|h[1-6]|ul|a)\b", text, re.I):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(text, "html.parser")
        for node in soup(["script", "style", "nav", "footer"]):
            node.decompose()
        for node in soup.find_all(["p", "li", "h1", "h2", "h3", "br"]):
            if node.name == "br":
                node.replace_with("\n")
            else:
                node.append("\n")
        text = soup.get_text(" ", strip=False)
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def source_lines(sources: dict[str, str]) -> list[dict[str, str]]:
    lines, seen = [], set()
    # Prefer employer excerpts for repeated material. Keep distinct board lines.
    for source in ("employer", "board"):
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", sources[source]):
            for part in textwrap.wrap(
                sentence, width=600, break_long_words=False, break_on_hyphens=False
            ):
                key = " ".join(part.casefold().split())
                if key in seen:
                    continue
                seen.add(key)
                lines.append({"id": f"{source}:{len(lines) + 1}", "source": source, "text": part})
    return lines


def compact_plan(plan, include_reasons=True):
    """Avoid repeated evidence and local audit paths in the resume model's model input."""
    entries, evidence_lines = [], {}
    columns = ["term", "category", "priority", "requirement", "placement"] + (
        ["why"] if include_reasons else []
    )
    for item in plan.get("keywords", []):
        key = (item.get("source", ""), item.get("evidence", ""))
        line_id = next(
            (
                name
                for name, line in evidence_lines.items()
                if (line["source"], line["text"]) == key
            ),
            None,
        )
        if line_id is None:
            line_id = str(len(evidence_lines) + 1)
            evidence_lines[line_id] = {"source": key[0], "text": key[1]}
        entries.append([item[name] for name in columns] + [line_id])
    return (
        {
            "columns": columns + ["evidence_line"],
            "keywords": entries,
            "evidence_lines": evidence_lines,
        }
        if entries
        else {}
    )


def contains_term(text, term):
    """Exact phrase matching, with flexible whitespace and word boundaries."""
    pattern = r"\s+".join(re.escape(part) for part in term.split())
    return bool(pattern and re.search(r"(?<!\w)" + pattern + r"(?!\w)", text, re.I))


class KeywordAgent:
    def __init__(self, settings: Settings, llm: LocalModel) -> None:
        self.settings, self.llm = settings, llm
        self.instructions = skill_text(settings.project_root, "job_keywords")

    def analyze(self, job: Job) -> dict[str, Any]:
        sources = {
            "board": description_text(job.job_description),
            "employer": description_text(job.employer_verification.get("description", "")),
        }
        lines = source_lines(sources)
        path = self.settings.logs_root / "keywords" / job.posting_id / f"{uuid.uuid4().hex}.json"
        audit = {
            "posting_id": job.posting_id,
            "created_at": utcnow().isoformat(),
            "model": self.settings.data["llm"]["models"]["keyword_extractor"],
            "reasoning": self.settings.data["llm"]["reasoning"]["keyword_extractor"],
            "attempts": [],
            "status": "running",
            "readable_sources": sources,
            "source_lines": lines,
            "source_hashes": {key: digest(text) for key, text in sources.items()},
        }
        atomic_json(path, audit)
        job.keyword_plan = {}
        valid, rejected = {}, []

        def extract(section, depth=0):
            payload = {"job_title": job.job_title, "company": job.company, "lines": section}
            lookup = {line["id"]: line for line in section}
            for attempt in range(2):
                event = {"line_ids": list(lookup), "attempt": attempt + 1, "split_depth": depth}
                audit["attempts"].append(event)
                atomic_json(path, audit)
                try:
                    response = self.llm.generate(
                        "keyword_extractor", self.instructions, payload, EXTRACTION_SCHEMA
                    )
                    validate(response, EXTRACTION_SCHEMA)
                    event["response"] = response
                    accepted, bad = [], []
                    for item in response["keywords"]:
                        term = item["term"].strip()
                        line = lookup.get(item["line_id"])
                        if not line or not contains_term(line["text"], term):
                            # Recover a wrong citation only when the literal term
                            # is actually present elsewhere in this supplied section.
                            line = next(
                                (line for line in section if contains_term(line["text"], term)),
                                None,
                            )
                        if not term or len(term.split()) > 12 or line is None:
                            bad.append(
                                {
                                    "term": term,
                                    "line_id": item["line_id"],
                                    "reason": "Term not found in the supplied readable source lines.",
                                }
                            )
                            continue
                        accepted.append(
                            {
                                **{key: value for key, value in item.items() if key != "line_id"},
                                "term": term,
                                "source": line["source"],
                                "evidence": line["text"],
                            }
                        )
                    event["rejected_keywords"] = bad
                    if bad and not accepted:
                        raise ValueError(
                            "None of the proposed terms occur in the supplied source lines: "
                            + ", ".join(row["term"] for row in bad)
                        )
                    rejected.extend(bad)
                    for item in accepted:
                        key = " ".join(item["term"].casefold().split())
                        rank = lambda entry: (
                            {"high": 0, "medium": 1, "low": 2}[entry["priority"]],
                            {"required": 0, "responsibility": 1, "preferred": 2, "context": 3}[
                                entry["requirement"]
                            ],
                        )
                        if (
                            key not in valid
                            or (item["source"] == "employer" and valid[key]["source"] != "employer")
                            or (
                                item["source"] == valid[key]["source"]
                                and rank(item) < rank(valid[key])
                            )
                        ):
                            valid[key] = item
                    event["status"] = "validated"
                    atomic_json(path, audit)
                    return
                except (ValueError, ModelError, ValidationError) as exc:
                    event.update(status="retrying", error=str(exc))
                    atomic_json(path, audit)
                    if isinstance(exc, ModelError) and not (
                        exc.token_limited or exc.output_invalid
                    ):
                        # A disconnected/unavailable model cannot be repaired by
                        # issuing more requests for progressively smaller sections.
                        raise
                    if getattr(exc, "token_limited", False) and len(section) > 1:
                        break
                    if attempt == 0:
                        payload["correction_required"] = str(exc)
                        continue
                    if len(section) <= 1:
                        raise ModelError(str(exc)) from exc
                    break
            # All lines must complete: split the section instead of regenerating
            # the whole posting with a budget larger than the context window.
            middle = len(section) // 2
            print(
                f"Reducing keyword section to {middle} and {len(section) - middle} lines.",
                flush=True,
            )
            extract(section[:middle], depth + 1)
            extract(section[middle:], depth + 1)

        try:
            sections, current, size = [], [], 0
            for line in lines:
                if current and size + len(line["text"]) > 3000:
                    sections.append(current)
                    current, size = [], 0
                current.append(line)
                size += len(line["text"])
            if current:
                sections.append(current)
            for index, section in enumerate(sections, 1):
                print(f"Keyword section {index}/{len(sections)}: {job.company}", flush=True)
                extract(section)
            if not valid:
                raise ModelError("No evidence-backed material keywords were extracted.")
            keywords = sorted(
                valid.values(),
                key=lambda item: (
                    {"high": 0, "medium": 1, "low": 2}[item["priority"]],
                    {"required": 0, "responsibility": 1, "preferred": 2, "context": 3}[
                        item["requirement"]
                    ],
                ),
            )
            validate({"keywords": keywords}, KEYWORD_SCHEMA)
            job.keyword_plan = {
                "keywords": keywords,
                "audit_path": str(path),
                "model": audit["model"],
                "reasoning": audit["reasoning"],
                "rejected_keywords": rejected,
                "source_hashes": audit["source_hashes"],
                "evidence_basis": "readable_description",
            }
            job.skills = [item["term"] for item in keywords]
            audit.update(status="validated", result=job.keyword_plan)
            atomic_json(path, audit)
            atomic_json(self.settings.jobs_root / f"{job.posting_id}.json", job.to_dict())
            return job.keyword_plan
        except ModelError as exc:
            audit.update(status="failed", error=str(exc))
            atomic_json(path, audit)
            raise ModelError(f"Keyword planning failed: {exc}. Audit: {path}") from exc
