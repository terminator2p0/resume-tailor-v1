"""Discover safe DOCX targets without a candidate-specific paragraph map."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from docx import Document
from docx.text.paragraph import Paragraph

from .common import digest


class ResumeDocument:
    """Document facade with body and table paragraphs in stable XML order.

    Headers, footers, shapes and text boxes remain protected package content.
    """

    def __init__(self, path: Path) -> None:
        self._document = Document(path)
        self.element = self._document.element
        self.paragraphs = [
            Paragraph(node, self._document._body)
            for node in self.element.body.xpath(".//w:p")
            if not node.xpath("ancestor::w:txbxContent")
        ]


def normalized(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold()))


def safe_target(paragraph: Paragraph) -> bool:
    return bool(
        paragraph.text.strip()
        and paragraph._p.xpath(".//w:t")
        and not paragraph._p.xpath(
            ".//w:hyperlink | .//w:fldChar | .//w:instrText | .//w:br | .//w:r/w:tab | .//w:drawing"
        )
    )


def looks_like_heading(paragraph: Paragraph) -> bool:
    text = paragraph.text.strip()
    if not text or len(text.split()) > 14:
        return False
    style = (paragraph.style.name or "").casefold()
    runs = [run for run in paragraph.runs if run.text.strip()]
    return "heading" in style or text.isupper() or bool(runs and all(run.bold for run in runs))


def has_date(text: str) -> bool:
    return bool(re.search(r"\b(?:19|20)\d{2}\b|\b(?:present|current)\b", text, re.I))


def create_profile(base: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Map content conservatively; labels can be configured in any language.

    Identity, role/project headings, dates, unknown headings and protected
    sections are never targets. Facts are scoped to the current role/project.
    """
    document = ResumeDocument(base)
    aliases = {
        normalized(label): section
        for section, labels in config["section_aliases"].items()
        for label in labels
    }
    protected = {normalized(label) for label in config["protected_headings"]}
    targets: list[dict[str, Any]] = []
    contexts: dict[str, list[int]] = {"all": list(range(len(document.paragraphs)))}
    section: str | None = None
    context = "all"
    first_in_block = False
    found_section = False
    for index, paragraph in enumerate(document.paragraphs):
        text = paragraph.text.strip()
        if not text:
            continue
        label = normalized(text)
        if label in aliases:
            section = aliases[label]
            found_section = True
            context = f"{section}_{index}"
            contexts[context] = [index]
            first_in_block = section in {"experience_bullets", "project_descriptions"}
            continue
        # Skills categories may be bold, but an unrelated heading must stop
        # the section rather than making the next section's content editable.
        explicit_heading = "heading" in (paragraph.style.name or "").casefold()
        skill_list_heading = (
            section == "skills"
            and not explicit_heading
            and any(separator in text for separator in (":", ",", ";", "|"))
        )
        if label in protected or looks_like_heading(paragraph) and not skill_list_heading:
            if (
                section in {"experience_bullets", "project_descriptions"}
                and label not in protected
                and not (
                    "heading" in (paragraph.style.name or "").lower()
                    and paragraph.style.name.lower().endswith("1")
                )
            ):
                context = f"{section}_{index}"
                contexts[context] = [index]
                first_in_block = False
            else:
                section = None
            continue
        if section is None:
            # An unlabeled opening summary is common. Contact lines remain protected.
            if (
                not found_section
                and len(text.split()) >= 18
                and not has_date(text)
                and not re.search(r"@|https?://|\+?\d[\d ()-]{7,}", text)
            ):
                if safe_target(paragraph):
                    targets.append(
                        {
                            "paragraph_id": index,
                            "section": "summary",
                            "context": "all",
                            "text_hash": digest(paragraph.text),
                        }
                    )
            continue
        contexts[context].append(index)
        if section in {"experience_bullets", "project_descriptions"}:
            bullet = (
                bool(paragraph._p.xpath("./w:pPr/w:numPr"))
                or "list" in (paragraph.style.name or "").lower()
                or text.startswith(("•", "-", "▪", "●"))
            )
            # Preserve role names, employers, project names and date lines.
            if has_date(text) or first_in_block and not bullet:
                if has_date(text) and len(contexts[context]) > 2:
                    contexts[context].pop()
                    context = f"{section}_{index}"
                    contexts[context] = [index]
                first_in_block = False
                continue
            first_in_block = False
            if not bullet and len(text.split()) < 8:
                contexts[context].pop()
                context = f"{section}_{index}"
                contexts[context] = [index]
                continue
        if safe_target(paragraph):
            targets.append(
                {
                    "paragraph_id": index,
                    "section": section,
                    "context": context,
                    "text_hash": digest(paragraph.text),
                }
            )
    allowed = set(config["editable_sections"])
    targets = [entry for entry in targets if entry["section"] in allowed]
    if not targets:
        raise ValueError(
            "No safely editable resume text found. Use Word headings or configure resume.section_aliases for your section labels. Text boxes, fields and hyperlink paragraphs are protected. Run --inspect-resume to inspect the layout."
        )
    return {
        "version": 2,
        "base_sha256": digest(base.read_bytes()),
        "targets": targets,
        "contexts": contexts,
    }
