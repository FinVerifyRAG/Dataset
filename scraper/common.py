"""
common.py — Shared infrastructure for the RBI / SEBI regulatory scraper.

Implements the parts of Master Plan M1 ("downloader with URL + SHA-256
provenance; respect site terms/robots") that both scrapers need:

  - a polite HTTP session (custom User-Agent, retries, backoff)
  - a robots.txt checker (per-domain, cached)
  - a token-bucket-style rate limiter
  - a resumable manifest (JSON) recording url, sha256, local path,
    fetch timestamp, http status, regulator, doc_type
  - a safe-filename helper

Nothing here is regulator-specific. rbi_scraper.py and sebi_scraper.py
both import from this module.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.robotparser as robotparser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Identify yourself honestly. Government sites log User-Agents; an academic,
# identifiable UA is both more ethical and less likely to get silently
# rate-limited/blocked than pretending to be a browser.
# Browser-standard User Agent to avoid WAF / bot-challenge false-positives
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# Minimum seconds between requests to the *same* host. Keep this polite —
# these are public-interest government sites serving many users.
MIN_DELAY_SECONDS = 2.0

MANIFEST_FILENAME = "manifest.json"


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Enforces a minimum delay between successive requests to one host."""

    def __init__(self, min_delay: float = MIN_DELAY_SECONDS):
        self.min_delay = min_delay
        self._last_call: dict[str, float] = {}

    def wait(self, host: str) -> None:
        now = time.monotonic()
        last = self._last_call.get(host, 0.0)
        elapsed = now - last
        if elapsed < self.min_delay:
            time.sleep(self.min_delay - elapsed)
        self._last_call[host] = time.monotonic()


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------

class RobotsChecker:
    """Caches and checks robots.txt per domain using Python's robotparser."""

    def __init__(self, user_agent: str = USER_AGENT):
        self.user_agent = user_agent
        self._parsers: dict[str, robotparser.RobotFileParser] = {}

    def _get_parser(self, url: str) -> robotparser.RobotFileParser:
        parsed = urlparse(url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        if root not in self._parsers:
            rp = robotparser.RobotFileParser()
            rp.set_url(root + "/robots.txt")
            try:
                rp.read()
            except Exception:
                print(f"[robots] could not fetch robots.txt for {root}; "
                      f"proceeding cautiously")
            self._parsers[root] = rp
        return self._parsers[root]

    def allowed(self, url: str) -> bool:
        try:
            rp = self._get_parser(url)
            return rp.can_fetch(self.user_agent, url)
        except Exception:
            return True


# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------

def make_session(referer: Optional[str] = None) -> requests.Session:
    """A requests.Session with retries/backoff and realistic browser headers."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,application/pdf,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    })
    if referer:
        session.headers["Referer"] = referer
    retry = Retry(
        total=5,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ---------------------------------------------------------------------------
# Manifest (provenance record — matches Master Plan §M1: "URL + SHA-256")
# ---------------------------------------------------------------------------

@dataclass
class ManifestEntry:
    url: str
    local_path: str
    sha256: str
    regulator: str
    doc_type: str
    fetched_at: str
    http_status: int
    source_listing_url: Optional[str] = None
    title: Optional[str] = None
    issue_date: Optional[str] = None
    instrument_id: Optional[str] = None
    extra: dict = field(default_factory=dict)


class Manifest:
    """A JSON-backed, resumable download ledger.

    Keyed by URL, so re-running the scraper skips files already recorded —
    this makes the whole pipeline safe to stop and restart, which matters
    for a scrape that may run for hours against a rate-limited target.
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._entries: dict[str, dict] = {}
        if self.path.exists():
            try:
                self._entries = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self._entries = {}

    def has(self, url: str) -> bool:
        return url in self._entries

    def add(self, entry: ManifestEntry) -> None:
        self._entries[entry.url] = entry.__dict__
        self._save()

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._entries, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def __len__(self) -> int:
        return len(self._entries)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_filename(s: str, max_len: int = 150) -> str:
    """Turn an arbitrary title/id into a filesystem-safe filename stem."""
    s = re.sub(r"[^\w\-. ]+", "_", s).strip().strip(".")
    s = re.sub(r"\s+", "_", s)
    return s[:max_len] if s else "untitled"


def download_pdf(
    session: requests.Session,
    limiter: RateLimiter,
    robots: RobotsChecker,
    manifest: Manifest,
    pdf_url: str,
    out_dir: Path,
    regulator: str,
    doc_type: str,
    source_listing_url: Optional[str] = None,
    title: Optional[str] = None,
    issue_date: Optional[str] = None,
    instrument_id: Optional[str] = None,
) -> Optional[Path]:
    """Download one PDF, verify that it is genuine binary PDF data, hash it, and record it in the manifest.

    Returns the local path, or None if skipped (already present / disallowed / failed / WAF challenge).
    """
    if manifest.has(pdf_url):
        return None  # already fetched in a previous run

    if not robots.allowed(pdf_url):
        print(f"[skip] robots.txt disallows: {pdf_url}")
        return None

    host = urlparse(pdf_url).netloc
    limiter.wait(host)

    # Set appropriate Referer based on regulator
    req_headers = {}
    if "rbi.org.in" in pdf_url.lower():
        req_headers["Referer"] = source_listing_url or "https://www.rbi.org.in/"
    elif "sebi.gov.in" in pdf_url.lower():
        req_headers["Referer"] = source_listing_url or "https://www.sebi.gov.in/"

    try:
        resp = session.get(pdf_url, headers=req_headers, timeout=60)
    except Exception as e:
        print(f"[warn] Failed connection to {pdf_url}: {e}")
        return None

    if resp.status_code != 200 or not resp.content:
        print(f"[warn] HTTP {resp.status_code} for {pdf_url}")
        return None

    # CRITICAL VALIDATION: Verify binary PDF magic bytes (%PDF)
    # If the server returned HTML (WAF block / JS challenge / error page), do not save as PDF!
    if not resp.content.startswith(b"%PDF"):
        if b"<!DOCTYPE" in resp.content[:200] or b"<html" in resp.content[:200].lower():
            print(f"[warn] Download from {pdf_url} returned HTML challenge instead of PDF. Skipping.")
        else:
            print(f"[warn] Download from {pdf_url} did not have valid PDF magic bytes. Skipping.")
        return None

    digest = sha256_bytes(resp.content)
    stem = safe_filename(instrument_id or title or digest[:12])
    out_dir.mkdir(parents=True, exist_ok=True)
    local_path = out_dir / f"{stem}__{digest[:8]}.pdf"
    local_path.write_bytes(resp.content)

    manifest.add(ManifestEntry(
        url=pdf_url,
        local_path=str(local_path),
        sha256=digest,
        regulator=regulator,
        doc_type=doc_type,
        fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        http_status=resp.status_code,
        source_listing_url=source_listing_url,
        title=title,
        issue_date=issue_date,
        instrument_id=instrument_id,
    ))
    return local_path
