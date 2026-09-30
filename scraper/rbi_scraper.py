"""
rbi_scraper.py — Scrapes RBI circulars from the "Index To RBI Circulars" tool.

VERIFIED against the live site on 2026-09-30:
  Listing page:  https://www.rbi.org.in/Scripts/BS_CircularIndexDisplay.aspx
  Detail page:   https://www.rbi.org.in/Scripts/BS_CircularIndexDisplay.aspx?Id=<N>
  PDF link:      embedded in the detail page, hosted on rbidocs.rbi.org.in

Strategy — ID-range walking, not pagination-clicking
-----------------------------------------------------
The listing page's month/year filters are classic ASP.NET WebForms
postbacks (__doPostBack), which are brittle to reverse-engineer and change
across ASP.NET versions. Fortunately, every circular's detail page is
addressable directly by a small sequential integer `Id`, e.g.:

    ...BS_CircularIndexDisplay.aspx?Id=13693

These IDs appear to increment roughly in issue order across ALL of RBI's
"BS_...aspx?Id=" content (not just circulars), so this scraper:

  1. Fetches the default listing page (no Id) to find the CURRENT highest
     Id in use (bootstrap).
  2. Walks Id values downward from there, one HTTP request per Id.
  3. For each Id, checks whether the page is actually a circular detail
     page (has a PDF link + a subject heading) and, if so, extracts:
       - circular number / instrument_id
       - issue date
       - subject / title
       - the PDF url
       - the inline HTML text (RBI conveniently also renders the full
         circular text inline — useful as a fallback if PDF parsing
         later fails, and a hashable indicator of edits)
  4. Downloads the PDF, hashes it, and appends a row to the manifest.

This is slower than true pagination (one request per document, many of
which will be non-circular content and get skipped) but is far more robust
to site redesigns than guessing postback targets, and it naturally supports
resuming: stop the process any time, and re-running skips Ids already in
the manifest.

IMPORTANT — verify before a large run
--------------------------------------
Government sites change layout over time. Before scraping thousands of Ids:
  1. Run `python -m scraper.rbi_scraper --self-test` (see bottom of file)
     to confirm the parser still extracts fields correctly from a handful
     of known-good Ids.
  2. Check robots.txt manually once: https://www.rbi.org.in/robots.txt
  3. Start with a small --max-docs value and inspect the output CSV/manifest
     before committing to an overnight run.
"""

from __future__ import annotations

import argparse
import csv
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

from .common import (
    Manifest,
    RateLimiter,
    RobotsChecker,
    download_pdf,
    make_session,
)

BASE = "https://www.rbi.org.in/Scripts/BS_CircularIndexDisplay.aspx"


@dataclass
class RbiCircular:
    id_: int
    instrument_id: Optional[str]
    issue_date: Optional[str]
    title: Optional[str]
    pdf_url: Optional[str]
    inline_text: Optional[str]


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def fetch_default_listing_max_id(session) -> int:
    """Bootstrap: read the default (latest) listing page and return the
    highest `Id` referenced in it, to know where to start walking down from.
    """
    resp = session.get(BASE, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    ids = []
    for a in soup.find_all("a", href=True):
        m = re.search(r"[?&]Id=(\d+)", a["href"])
        if m:
            ids.append(int(m.group(1)))
    if not ids:
        raise RuntimeError(
            "Could not find any circular Id on the default listing page — "
            "the site layout may have changed. Inspect BASE manually."
        )
    return max(ids)


def parse_detail_page(html: str, id_: int) -> Optional[RbiCircular]:
    """Parse one BS_CircularIndexDisplay.aspx?Id=<id_> page.

    Returns None if this Id does not correspond to a circular detail page
    (e.g. it 404s, redirects to something else, or has no PDF link — some
    Ids in this numbering space belong to unrelated RBI content).
    """
    soup = BeautifulSoup(html, "html.parser")

    # The PDF link is an <a> tag whose href points at rbidocs.rbi.org.in
    # and ends in .PDF (case-insensitive on this site).
    pdf_a = soup.find(
        "a",
        href=re.compile(r"rbidocs\.rbi\.org\.in.*\.pdf", re.IGNORECASE),
    )
    if pdf_a is None:
        return None
    pdf_url = pdf_a["href"]

    # The main content table holds: bold title line, then the full circular
    # text (which itself starts with "RBI/20XX-XX/NNN ... <date>").
    # We pull the page's visible text and use light regex on the header
    # block rather than depending on exact table/row nesting, which is
    # fragile on ASP.NET-rendered markup.
    page_text = _clean(soup.get_text(" "))

    title_tag = soup.find("strong") or soup.find("b")
    title = _clean(title_tag.get_text()) if title_tag else None

    # Date pattern like "September 02, 2026" appearing near the top.
    # Matched first because the instrument-id extraction below uses its
    # position as a right boundary (the circular number + series text runs
    # right up to the issue date, and may itself contain periods, e.g.
    # "A.P. (DIR Series) Circular No. 20", so we can't just stop at the
    # first '.').
    date_match = re.search(
        r"(January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+\d{1,2},\s*\d{4}",
        page_text,
    )
    issue_date = date_match.group(0) if date_match else None

    # Circular number pattern, e.g. "RBI/2026-27/251" or the fuller
    # "RBI/2026-2027/251 A.P. (DIR Series) Circular No. 20" — captured up to
    # (but not including) the issue date found above, when present.
    instrument_start = re.search(r"RBI/20\d{2}-\d{2,4}/\d+", page_text)
    instrument_id = None
    if instrument_start:
        end = date_match.start() if date_match and date_match.start() > instrument_start.start() else instrument_start.end() + 60
        instrument_id = _clean(page_text[instrument_start.start():end])

    return RbiCircular(
        id_=id_,
        instrument_id=instrument_id,
        issue_date=issue_date,
        title=title,
        pdf_url=pdf_url,
        inline_text=page_text,
    )


def scrape_rbi(
    out_dir: Path,
    max_docs: int = 200,
    start_id: Optional[int] = None,
    min_id: int = 1,
    sleep_between: float = 2.0,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_pdf_dir = out_dir / "pdf"
    manifest = Manifest(out_dir / "manifest.json")
    csv_path = out_dir / "rbi_index.csv"

    session = make_session()
    limiter = RateLimiter(min_delay=sleep_between)
    robots = RobotsChecker()

    if start_id is None:
        print("[bootstrap] fetching default listing to find latest Id ...")
        start_id = fetch_default_listing_max_id(session)
        print(f"[bootstrap] latest Id observed: {start_id}")

    write_header = not csv_path.exists()
    csv_file = csv_path.open("a", newline="", encoding="utf-8")
    writer = csv.writer(csv_file)
    if write_header:
        writer.writerow(
            ["id", "instrument_id", "issue_date", "title", "pdf_url", "local_pdf_path"]
        )

    found = 0
    id_ = start_id
    misses_in_a_row = 0

    while id_ >= min_id and found < max_docs:
        detail_url = f"{BASE}?Id={id_}"

        if not robots.allowed(detail_url):
            print(f"[skip] robots.txt disallows {detail_url}")
            id_ -= 1
            continue

        limiter.wait("www.rbi.org.in")
        try:
            resp = session.get(detail_url, timeout=30)
        except Exception as e:
            print(f"[warn] request failed for Id={id_}: {e}")
            id_ -= 1
            continue

        if resp.status_code != 200:
            misses_in_a_row += 1
            id_ -= 1
            continue

        circular = parse_detail_page(resp.text, id_)
        if circular is None:
            # This Id belongs to non-circular content — normal and expected
            # occasionally; only warn if we get a long unbroken run of them,
            # which usually means the page structure has changed.
            misses_in_a_row += 1
            if misses_in_a_row and misses_in_a_row % 25 == 0:
                print(
                    f"[warn] {misses_in_a_row} consecutive non-circular Ids "
                    f"around Id={id_} — verify the parser still matches the "
                    f"live page structure."
                )
            id_ -= 1
            continue

        misses_in_a_row = 0

        local_path = download_pdf(
            session=session,
            limiter=limiter,
            robots=robots,
            manifest=manifest,
            pdf_url=circular.pdf_url,
            out_dir=out_pdf_dir,
            regulator="RBI",
            doc_type="circular",
            source_listing_url=detail_url,
            title=circular.title,
            issue_date=circular.issue_date,
            instrument_id=circular.instrument_id,
        )

        writer.writerow([
            circular.id_,
            circular.instrument_id,
            circular.issue_date,
            circular.title,
            circular.pdf_url,
            str(local_path) if local_path else "(already fetched or skipped)",
        ])
        csv_file.flush()

        found += 1
        if found % 20 == 0:
            print(f"[progress] {found}/{max_docs} circulars collected "
                  f"(currently at Id={id_})")

        id_ -= 1

    csv_file.close()
    print(f"[done] collected {found} RBI circulars. "
          f"Manifest has {len(manifest)} total entries.")


def self_test() -> None:
    """Sanity-check the parser against a few Ids verified by hand on
    2026-09-30. If this fails, the site layout has changed — fix
    parse_detail_page() before running a full scrape.
    """
    session = make_session()
    known_good_ids = [13693, 13712]
    for id_ in known_good_ids:
        resp = session.get(f"{BASE}?Id={id_}", timeout=30)
        c = parse_detail_page(resp.text, id_)
        assert c is not None, f"Id={id_} failed to parse as a circular"
        assert c.pdf_url and c.pdf_url.lower().endswith(".pdf"), (
            f"Id={id_}: no valid PDF url extracted"
        )
        print(f"[self-test OK] Id={id_}: {c.instrument_id} | {c.issue_date} "
              f"| {c.title}")
    print("[self-test] all checks passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape RBI circulars.")
    parser.add_argument("--out", type=Path, default=Path("data/raw/rbi"))
    parser.add_argument("--max-docs", type=int, default=200)
    parser.add_argument("--start-id", type=int, default=None)
    parser.add_argument("--min-id", type=int, default=1)
    parser.add_argument("--sleep", type=float, default=2.0)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        self_test()
    else:
        scrape_rbi(
            out_dir=args.out,
            max_docs=args.max_docs,
            start_id=args.start_id,
            min_id=args.min_id,
            sleep_between=args.sleep,
        )
