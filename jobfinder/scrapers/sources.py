"""Bounded job-board workers and public company JobPosting extraction."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from config_loader import Settings

import contextlib
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from html import unescape
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from ..common import canonical_url
from ..models import Job


def public_url(url: str) -> str:
    from urllib.parse import urlsplit

    url = canonical_url(url)
    host = urlsplit(url).hostname
    addresses = socket.getaddrinfo(host, None)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Job pages must resolve to public internet addresses.")
    return url


class PublicRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, public_url(newurl))


def fetch_html(url: str) -> str:
    return fetch_html_page(url)[0]


def fetch_html_page(url: str) -> tuple[str, str]:
    """Return HTML and the final public URL after validated redirects."""
    import certifi

    request = Request(
        public_url(url), headers={"User-Agent": "LocalJobFinder/1.0", "Accept": "text/html"}
    )
    context = ssl.create_default_context(cafile=certifi.where())
    with build_opener(ProxyHandler({}), PublicRedirects(), HTTPSHandler(context=context)).open(
        request, timeout=20
    ) as response:
        data = response.read(3_000_001)
        if len(data) > 3_000_000:
            raise ValueError("Job page exceeds the maximum fetch size.")
        if "html" not in response.headers.get("Content-Type", "").lower():
            raise ValueError("The job page is not HTML.")
        return data.decode(
            response.headers.get_content_charset() or "utf-8", errors="replace"
        ), canonical_url(response.geturl())


def fetch_json(url: str) -> Any:
    import certifi

    request = Request(
        public_url(url), headers={"User-Agent": "LocalJobFinder/1.0", "Accept": "application/json"}
    )
    context = ssl.create_default_context(cafile=certifi.where())
    with build_opener(ProxyHandler({}), PublicRedirects(), HTTPSHandler(context=context)).open(
        request, timeout=20
    ) as response:
        data = response.read(3_000_001)
        if len(data) > 3_000_000:
            raise ValueError("Company API response exceeds the fetch limit.")
        return json.loads(data)


def company_api_job(url: str) -> list[dict]:
    """Use documented, unauthenticated ATS GET endpoints for a discovered job URL."""
    parts = urlsplit(canonical_url(url))
    path = parts.path.strip("/").split("/")
    if (
        parts.hostname in {"boards.greenhouse.io", "job-boards.greenhouse.io"}
        and len(path) >= 3
        and path[1] == "jobs"
        and path[2].isdigit()
    ):
        data = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{path[0]}/jobs/{path[2]}")
        return [
            {
                "job_title": data["title"],
                "company": data["company_name"],
                "location": data["location"]["name"],
                "job_description": html_text(unescape(data["content"])),
                "application_url": data["absolute_url"],
                "source_url": url,
                "source": "company_careers",
                "source_posting_id": str(data["id"]),
                "posted_at": data.get("first_published"),
                "expires_at": data.get("application_deadline"),
                "date_precision": "timestamp",
            }
        ]
    if (
        parts.hostname in {"jobs.lever.co", "jobs.eu.lever.co"}
        and len(path) >= 2
        and re.fullmatch(r"[a-f0-9-]{36}", path[1])
    ):
        api_host = "api.eu.lever.co" if parts.hostname == "jobs.eu.lever.co" else "api.lever.co"
        data = fetch_json(f"https://{api_host}/v0/postings/{path[0]}/{path[1]}")
        description = data.get("descriptionPlain", "") + "\n" + data.get("additionalPlain", "")
        for section in data.get("lists", []):
            description += (
                "\n" + section.get("text", "") + "\n" + html_text(section.get("content", ""))
            )
        created = data.get("createdAt")
        posted = (
            datetime.fromtimestamp(created / 1000, timezone.utc).isoformat()
            if isinstance(created, (int, float))
            else None
        )
        return [
            {
                "job_title": data["text"],
                "company": path[0],
                "location": data.get("categories", {}).get("location", "unknown"),
                "employment_type": data.get("categories", {}).get("commitment", "unknown"),
                "job_description": description,
                "application_url": data.get("hostedUrl", url),
                "source_url": url,
                "source": "company_careers",
                "source_posting_id": data["id"],
                "posted_at": posted,
                "date_precision": "timestamp" if posted else "unknown",
            }
        ]
    return []


def html_text(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style", "nav", "footer"]):
        element.decompose()
    return soup.get_text("\n", strip=True)


def extract_company_jobs(html: str, url: str) -> list[dict]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    nodes = []

    def walk(value):
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            kind = value.get("@type", "")
            if kind == "JobPosting" or isinstance(kind, list) and "JobPosting" in kind:
                nodes.append(value)
            for key, item in value.items():
                if isinstance(item, (dict, list)):
                    walk(item)

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError):
            continue
    results = []
    for node in nodes:
        organization = node.get("hiringOrganization") or {}
        locations = node.get("jobLocation") or node.get("applicantLocationRequirements") or []
        if isinstance(locations, dict):
            locations = [locations]
        location_strings = []
        for place in locations:
            if not isinstance(place, dict):
                continue
            address = place.get("address", place)
            if not isinstance(address, dict):
                continue
            country = address.get("addressCountry", "")
            if isinstance(country, dict):
                country = country.get("name", "")
            parts = [
                address.get("addressLocality"),
                address.get("addressRegion"),
                country,
                place.get("name"),
            ]
            location_strings.append(", ".join(str(p) for p in parts if p))
        identifier = node.get("identifier") or {}
        if isinstance(identifier, dict):
            identifier = identifier.get("value", "")
        employment = node.get("employmentType", "unknown")
        if isinstance(employment, list):
            employment = ", ".join(employment)
        results.append(
            {
                "job_title": node.get("title", ""),
                "company": organization.get("name", "")
                if isinstance(organization, dict)
                else str(organization),
                "location": "; ".join(location_strings) or "unknown",
                "job_description": html_text(node.get("description", "")),
                "application_url": node.get("url") or url,
                "source_url": url,
                "source": "company_careers",
                "source_posting_id": str(identifier),
                "posted_at": node.get("datePosted"),
                "expires_at": node.get("validThrough"),
                "employment_type": employment.lower(),
                "work_arrangement": "remote"
                if node.get("jobLocationType") == "TELECOMMUTE"
                else "unknown",
                "date_precision": "date"
                if len(str(node.get("datePosted", ""))) == 10
                else "timestamp",
            }
        )
    return results


def board_worker(payload: dict) -> list[dict]:
    import pandas as pd
    from jobspy import scrape_jobs

    parameters = dict(
        site_name=[payload["source"]],
        search_term=payload["query"],
        location=payload["location"],
        country_indeed=payload["country"],
        results_wanted=payload["limit"],
        fetch_description=True,
        description_format="plain",
        verbose=0,
    )
    if payload.get("remote_only"):
        parameters["is_remote"] = True
    if payload["recent"]:
        parameters["hours_old"] = payload["recent_days"] * 24
    frame = scrape_jobs(**parameters)
    results = []

    def value(row, key, default=""):
        result = row.get(key)
        if result is None or not isinstance(result, (list, dict)) and pd.isna(result):
            return default
        if isinstance(result, (date, datetime)):
            return result.isoformat()
        return str(result)

    for _, row in frame.iterrows():
        results.append(
            {
                "job_title": value(row, "title"),
                "company": value(row, "company"),
                "location": value(row, "location", "unknown"),
                "job_description": value(row, "description"),
                "application_url": value(row, "job_url_direct") or value(row, "job_url"),
                "source_url": value(row, "job_url"),
                "source": payload["source"],
                "source_posting_id": value(row, "id"),
                "posted_at": value(row, "date_posted", None),
                "date_precision": "date",
                "employment_type": value(row, "job_type", "unknown"),
                "work_arrangement": "remote"
                if value(row, "is_remote").lower() == "true"
                else "unknown",
            }
        )
    return results


def company_worker(payload: dict) -> list[dict]:
    from ddgs import DDGS

    urls = list(payload.get("company_urls", []))
    deadline = time.monotonic() + payload.get("budget_seconds", 90) - 3
    for site in payload.get("company_domains", []):
        if time.monotonic() + 9 >= deadline:
            break
        query = f"site:{site} {payload['query']} {payload['location']}"
        time.sleep(payload.get("request_interval_seconds", 0))
        try:
            # Search engine indexing timestamps are not posting dates. Keep the
            # discovery broad and verify actual datePosted/first_published later.
            found = DDGS(timeout=8).text(
                query,
                region=payload.get("discovery_region", "wt-wt"),
                max_results=3,
                backend="duckduckgo",
            )
            for result in found:
                if result.get("href"):
                    urls.append(result["href"])
        except Exception as exc:
            print(f"Company discovery skipped {site} ({type(exc).__name__}).", file=sys.stderr)
    results = []
    urls.extend(payload.get("company_seed_urls", []))
    for position, url in enumerate(list(dict.fromkeys(urls))[: payload["limit"]]):
        if position:
            time.sleep(payload.get("request_interval_seconds", 0))
        if time.monotonic() + 21 >= deadline:
            print("Company search budget reached; returning completed postings.", file=sys.stderr)
            break
        try:
            extracted = company_api_job(url)
            if not extracted:
                extracted = extract_company_jobs(fetch_html(url), url)
            results.extend(extracted)
            if not extracted:
                print(f"No structured job posting found: {url}", file=sys.stderr)
        except Exception as exc:
            print(f"Company page skipped ({type(exc).__name__}): {url}", file=sys.stderr)
    return results


def run_source(
    settings: Settings, source: str, query: str, recent: bool, *, location: str
) -> tuple[list[Job], list[str]]:
    search = settings.data["search"]
    payload = dict(
        source=source,
        query=query,
        recent=recent,
        location=location,
        country=search["country"],
        remote_only=search["work_arrangements"] == ["remote"],
        company_domains=search["company_domains"],
        discovery_region=search["discovery_region"],
        request_interval_seconds=search["sites"][source]["request_interval_seconds"],
        recent_days=search["recent_days"],
        limit=search["results_per_source"],
        company_urls=search["company_urls"],
        company_seed_urls=search.get("company_seed_urls", []),
        budget_seconds=search["source_timeout_seconds"],
    )
    # Credentials loaded into the main process must never reach scraper subprocesses.
    environment = {
        k: v
        for k, v in os.environ.items()
        if k in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SYSTEMROOT"}
    }
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "jobfinder.scrapers.sources"],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=settings.project_root,
            env=environment,
            timeout=search["source_timeout_seconds"],
        )
    except subprocess.TimeoutExpired:
        return [], [f"{source}: search timed out ({query}, {'recent' if recent else 'any age'})."]
    if completed.returncode:
        return [], [f"{source}: search failed: {completed.stderr[-1000:].strip()}"]
    jobs, errors = [], []
    try:
        records = json.loads(completed.stdout)
        if not isinstance(records, list):
            raise ValueError("Source response must be a list.")
    except ValueError:
        return [], [f"{source}: scraper returned an invalid response."]
    for record in records:
        try:
            jobs.append(Job.from_dict(record))
        except (ValueError, TypeError) as exc:
            errors.append(f"{source}: incomplete job skipped ({exc}).")
    if completed.stderr.strip():
        errors.append(f"{source}: diagnostics: {completed.stderr[-1000:].strip()}")
    if not jobs:
        errors.append(
            f"{source}: no usable postings found for {query} ({'recent' if recent else 'any age'})."
        )
    return jobs, errors


if __name__ == "__main__":
    try:
        payload = json.load(sys.stdin)
        with contextlib.redirect_stdout(sys.stderr):
            result = (
                company_worker(payload)
                if payload["source"] == "company_careers"
                else board_worker(payload)
            )
        json.dump(result, sys.stdout, allow_nan=False)
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
