"""
sebi_scraper.py — Scrapes SEBI circulars / master circulars.

VERIFIED against the live site on 2026-09-30:
  Circulars listing:        https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=1&ssid=7&smid=0
  Master Circulars listing: https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=1&ssid=6&smid=0
  Detail pages:             static HTML at
      https://www.sebi.gov.in/legal/circulars/<mon-yyyy>/<slug>_<id>.html
  Table shows "1 to 25 of N records" with Next/Last links.

Why Playwright instead of plain requests for this one
-------------------------------------------------------
Unlike RBI's Index page, SEBI's listing pagination ("Next", page numbers)
is wired to JavaScript (`searchFormNewsList('n', ...)`) that we could not
verify translates to a simple, stable GET/POST parameter without a browser
to observe the actual network call — and guessing wrong silently returns
page 1 over and over, which is a worse failure mode than being explicit
about needing a real browser. Playwright (headless Chromium) clicks
"Next" exactly like a person would, which is slower per-page but immune to
that whole class of guesswork, and far more likely to keep working after
a future site redesign that only changes the JS internals.

The individual circular/master-circular DETAIL pages, once you have their
URL, are plain static HTML — those are fetched with plain `requests`
(faster, no browser needed) exactly like the RBI scraper.

Setup
-----
    pip install playwright beautifulsoup4 requests
    playwright install chromium

Usage
-----
    python -m scraper.sebi_scraper --section circulars --max-pages 5
    python -m scraper.sebi_scraper --section master_circulars --max-pages 3

IMPORTANT — verify before a large run
--------------------------------------
1. Run with --max-pages 1 first and inspect the output CSV/manifest.
2. Check robots.txt manually once: https://www.sebi.gov.in/robots.txt
3. If SEBI's listing page markup has changed, update ROW_SELECTOR /
   the table-parsing logic in `parse_listing_html()` below — the
   overall Playwright-driven pagination loop should not need to change.
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
    ManifestEntry,
    RateLimiter,
    RobotsChecker,
    download_pdf,
    make_session,
    safe_filename,
    sha256_bytes,
)

SECTION_URLS = {
    "circulars": "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=1&ssid=7&smid=0",
    "master_circulars": "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=1&ssid=6&smid=0",
}


@dataclass
class SebiListingRow:
    date_str: str
    title: str
    detail_url: str


def parse_listing_html(html: str) -> list[SebiListingRow]:
    """Parse one rendered listing page (after Playwright has loaded it) into
    (date, title, detail_url) rows. The listing is a plain <table> with
    two columns: Date, Title (title text is itself the link).
    """
    soup = BeautifulSoup(html, "html.parser")
    rows: list[SebiListingRow] = []

    table = soup.find("table")
    if table is None:
        return rows

    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        date_str = cells[0].get_text(strip=True)
        a = cells[1].find("a", href=True)
        if not a:
            continue
        title = a.get_text(strip=True)
        href = a["href"]
        if href.startswith("/"):
            href = "https://www.sebi.gov.in" + href
        if not href.startswith("http"):
            continue
        rows.append(SebiListingRow(date_str=date_str, title=title, detail_url=href))

    return rows


def extract_pdf_and_text_from_detail(html: str, page_url: str):
    """From a SEBI circular detail page, find the attached PDF link if one
    exists, and always return the page's visible text as a fallback source
    (some circulars are rendered as HTML directly with no separate PDF).
    """
    soup = BeautifulSoup(html, "html.parser")
    pdf_url = None

    # 1. Direct <a> tag with .pdf
    pdf_a = soup.find("a", href=re.compile(r"\.pdf($|\?)", re.IGNORECASE))
    if pdf_a and pdf_a.get("href"):
        href = pdf_a["href"].strip()
        if href.startswith("/"):
            href = "https://www.sebi.gov.in" + href
        if href.startswith("http"):
            pdf_url = href

    # 2. Check iframe or embed src (SEBI uses PDF.js viewer: web/?file=https://www.sebi.gov.in/sebi_data/attachdocs/...)
    if not pdf_url:
        for tag in soup.find_all(["iframe", "embed", "object"]):
            src = tag.get("src") or tag.get("data") or ""
            m = re.search(r"file=(https?://[^\s&\"'<>]+\.pdf)", src, re.IGNORECASE)
            if m:
                pdf_url = m.group(1)
                break
            elif ".pdf" in src.lower():
                if src.startswith("http"):
                    pdf_url = src
                elif src.startswith("/"):
                    pdf_url = "https://www.sebi.gov.in" + src
                break

    # 3. Direct regex match for SEBI attachdocs PDF URLs in full HTML
    if not pdf_url:
        m = re.search(r"https?://www\.sebi\.gov\.in/sebi_data/attachdocs/[^\s\"'<>]+\.pdf", html, re.IGNORECASE)
        if m:
            pdf_url = m.group(0)

    # 4. Fallback search for any .pdf URL within sebi.gov.in in the markup
    if not pdf_url:
        m = re.search(r"https?://[^\s\"'<>]+\.pdf", html, re.IGNORECASE)
        if m:
            pdf_url = m.group(0)

    text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    return pdf_url, text


def scrape_sebi(
    section: str,
    out_dir: Path,
    max_pages: int = 5,
    sleep_between_pages: float = 3.0,
    sleep_between_docs: float = 2.0,
) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise SystemExit(
            "Playwright is required for the SEBI scraper "
            "(pip install playwright && playwright install chromium)"
        ) from e

    if section not in SECTION_URLS:
        raise ValueError(f"section must be one of {list(SECTION_URLS)}")
    start_url = SECTION_URLS[section]

    out_dir.mkdir(parents=True, exist_ok=True)
    out_pdf_dir = out_dir / "pdf"
    out_html_dir = out_dir / "html_fallback"
    manifest = Manifest(out_dir / "manifest.json")
    csv_path = out_dir / f"sebi_{section}_index.csv"

    write_header = not csv_path.exists()
    csv_file = csv_path.open("a", newline="", encoding="utf-8")
    writer = csv.writer(csv_file)
    if write_header:
        writer.writerow(["date", "title", "detail_url", "pdf_url", "local_path"])

    session = make_session()
    limiter = RateLimiter(min_delay=sleep_between_docs)
    robots = RobotsChecker()

    if not robots.allowed(start_url):
        raise SystemExit(f"robots.txt disallows {start_url} — stopping.")

    all_rows: list[SebiListingRow] = []

    print(f"[playwright] launching headless browser for section={section}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (RegGuardResearchBot/1.0; academic capstone project)"
        ))
        page.goto(start_url, timeout=60_000)
        page.wait_for_selector("table", timeout=30_000)

        for page_num in range(1, max_pages + 1):
            html = page.content()
            rows = parse_listing_html(html)
            print(f"[listing] page {page_num}: {len(rows)} rows found")
            all_rows.extend(rows)

            if page_num == max_pages:
                break

            # Click the "Next" link exactly as a user would. SEBI's listing
            # exposes a link whose visible text is "Next **" (verified in the
            # live page on 2026-09-30). If this selector stops matching,
            # SEBI has changed its pagination markup — update the text match
            # below after re-inspecting the page in a real browser.
            next_link = page.locator("a", has_text="Next")
            if next_link.count() == 0:
                print("[listing] no 'Next' link found — stopping pagination.")
                break

            time.sleep(sleep_between_pages)  # be polite between page loads
            next_link.first.click()
            page.wait_for_timeout(2000)  # let the AJAX/postback settle
            page.wait_for_selector("table", timeout=30_000)

        browser.close()

    print(f"[listing] total rows collected: {len(all_rows)}")

    for row in all_rows:
        if manifest.has(row.detail_url):
            continue
        if not robots.allowed(row.detail_url):
            print(f"[skip] robots.txt disallows {row.detail_url}")
            continue

        limiter.wait("www.sebi.gov.in")
        try:
            resp = session.get(row.detail_url, timeout=30)
        except Exception as e:
            print(f"[warn] failed to fetch {row.detail_url}: {e}")
            continue
        if resp.status_code != 200:
            print(f"[warn] HTTP {resp.status_code} for {row.detail_url}")
            continue

        pdf_url, page_text = extract_pdf_and_text_from_detail(resp.text, row.detail_url)

        local_path = None
        if pdf_url:
            local_path = download_pdf(
                session=session,
                limiter=limiter,
                robots=robots,
                manifest=manifest,
                pdf_url=pdf_url,
                out_dir=out_pdf_dir,
                regulator="SEBI",
                doc_type=("master_circular" if section == "master_circulars"
                           else "circular"),
                source_listing_url=row.detail_url,
                title=row.title,
                issue_date=row.date_str,
            )
        else:
            # No separate PDF — persist the rendered page text so M1's
            # parser still has something to ingest, and record it in the
            # manifest the same way a PDF would be, for provenance.
            out_html_dir.mkdir(parents=True, exist_ok=True)
            stem = safe_filename(row.title)
            local_path = out_html_dir / f"{stem}.txt"
            local_path.write_text(page_text, encoding="utf-8")
            manifest.add(ManifestEntry(
                url=row.detail_url,
                local_path=str(local_path),
                sha256=sha256_bytes(page_text.encode("utf-8")),
                regulator="SEBI",
                doc_type=("master_circular" if section == "master_circulars"
                           else "circular") + "_html_fallback",
                fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                http_status=resp.status_code,
                source_listing_url=row.detail_url,
                title=row.title,
                issue_date=row.date_str,
            ))

        writer.writerow([
            row.date_str, row.title, row.detail_url, pdf_url,
            str(local_path) if local_path else "(skipped)",
        ])
        csv_file.flush()

    csv_file.close()
    print(f"[done] section={section}: manifest now has {len(manifest)} total entries.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape SEBI circulars.")
    parser.add_argument("--section", choices=list(SECTION_URLS), default="circulars")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--sleep-pages", type=float, default=3.0)
    parser.add_argument("--sleep-docs", type=float, default=2.0)
    args = parser.parse_args()

    out_dir = args.out or Path("data/raw/sebi") / args.section
    scrape_sebi(
        section=args.section,
        out_dir=out_dir,
        max_pages=args.max_pages,
        sleep_between_pages=args.sleep_pages,
        sleep_between_docs=args.sleep_docs,
    )
