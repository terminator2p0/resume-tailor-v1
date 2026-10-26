from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path.name}.")
    return value


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")[:70] or "Company"


def canonical_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise ValueError("A job link must be a public HTTP(S) URL without embedded credentials.")
    # Indeed sometimes exposes an advertising redirect rather than the employer URL.
    if parts.hostname.lower() == "jsv3.recruitics.com":
        employer = dict(parse_qsl(parts.query)).get("rx_url")
        if employer and urlsplit(employer).hostname != parts.hostname:
            return canonical_url(employer)
    tracking = {"trk", "trackingid", "ref", "refid", "source", "from", "fbclid", "gclid"}
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in tracking
    )
    host = parts.hostname.lower()
    if parts.port and parts.port not in {80, 443}:
        host += f":{parts.port}"
    return urlunsplit(
        (parts.scheme.lower(), host, parts.path.rstrip("/") or "/", urlencode(query), "")
    )


def skill_text(project_root: Path, name: str) -> str:
    path = project_root / "skills" / f"{name}_skills.md"
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"Agent instructions are empty: {path.name}")
    return text
