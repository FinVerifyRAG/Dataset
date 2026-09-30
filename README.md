# RegGuard Regulatory Scraper — RBI + SEBI

Automated, resumable, provenance-tracked downloader for RBI circulars and
SEBI circulars / master circulars, built to feed directly into **M1
(Temporal Knowledge Base)** of the RegGuard master plan.

This was built and verified against the live sites on **2026-09-30**.
Government websites change layout without notice — read the "Before a
large run" checklist below every time you pick this project back up after
a gap of more than a few weeks.

---

## 1. What this does

| Regulator | Source | Method | Why |
|---|---|---|---|
| RBI | [Index To RBI Circulars](https://www.rbi.org.in/Scripts/BS_CircularIndexDisplay.aspx) | Plain `requests` + BeautifulSoup, walking sequential document IDs | Listing page pagination is ASP.NET postback-based (brittle to reverse-engineer); IDs are stable, directly-addressable, and roughly chronological |
| SEBI | [Circulars](https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=1&ssid=7&smid=0) / [Master Circulars](https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=1&ssid=6&smid=0) | Playwright (headless Chromium) clicks "Next" through the listing; detail pages then fetched with plain `requests` | Listing pagination is JavaScript-driven; a real headless browser is more robust here than guessing hidden POST parameters |

Both scrapers:
- Check `robots.txt` before every request (`urllib.robotparser`).
- Rate-limit themselves (default: 2–3 seconds between requests to the same host).
- Identify themselves honestly via a custom `User-Agent` — **edit `USER_AGENT` in `scraper/common.py` to include your real institution/email before running this at scale.**
- Write a `manifest.json` per regulator/section recording `url`, `sha256`, `local_path`, `fetched_at`, `regulator`, `doc_type`, `title`, `issue_date`, `instrument_id` — this is the provenance record M1's `TemporalKnowledgeBase` expects.
- Are **resumable**: re-running skips any URL already in the manifest, so you can stop and restart freely.
- Write a flat CSV index (`rbi_index.csv`, `sebi_circulars_index.csv`, etc.) alongside the manifest for quick eyeballing in a spreadsheet.

---

## 2. Setup

```bash
python -m venv .venv
source .venv/bin/activate          # or .venv\Scripts\activate on Windows

pip install -r requirements.txt
playwright install chromium        # downloads the headless browser Playwright drives
```

Then open `scraper/common.py` and edit:

```python
USER_AGENT = (
    "RegGuardResearchBot/1.0 "
    "(+academic capstone project; contact: YOUR_EMAIL@YOUR_INSTITUTION.edu)"
)
```

Put your real contact email there. This matters more than it looks — if
anything about the scrape ever needs investigating on RBI/SEBI's side, an
honest, reachable User-Agent is the difference between "an academic
researcher we can email" and "an anonymous bot we should block."

---

## 3. Before a large run — verification checklist

**Do this every time**, and especially the first time:

1. **Check robots.txt by hand** in a browser:
   `https://www.rbi.org.in/robots.txt` and `https://www.sebi.gov.in/robots.txt`.
   The scrapers check this automatically at runtime too, but read it yourself
   once so you know what you're agreeing to.

2. **Run the RBI self-test**:
   ```bash
   python -m scraper.rbi_scraper --self-test
   ```
   This checks that the parser still correctly extracts the PDF link,
   instrument ID, date, and title from two known-good circular IDs. If it
   fails, RBI has changed their page layout — fix `parse_detail_page()` in
   `rbi_scraper.py` before doing anything else.

3. **Run both scrapers small first**:
   ```bash
   python run.py --rbi-max-docs 20 --sebi-max-pages 1
   ```
   Open the resulting CSVs and manifests. Do the titles, dates, and PDF
   links look right? Open two or three downloaded PDFs and confirm they're
   real circulars, not error pages saved with a `.pdf` extension.

4. **Only then scale up**:
   ```bash
   python run.py --rbi-max-docs 2000 --sebi-max-pages 40
   ```
   At ~2–3 seconds per document, a few thousand RBI circulars will take
   hours, not minutes — this is intentional (see §5).

---

## 4. Output layout

```
data/raw/
├── rbi/
│   ├── pdf/                     # downloaded circular PDFs
│   ├── manifest.json            # provenance ledger (url, sha256, ...)
│   └── rbi_index.csv            # flat spreadsheet view
└── sebi/
    ├── circulars/
    │   ├── pdf/
    │   ├── html_fallback/       # for the rare circular with no attached PDF
    │   ├── manifest.json
    │   └── sebi_circulars_index.csv
    └── master_circulars/
        ├── pdf/
        ├── manifest.json
        └── sebi_master_circulars_index.csv
```

Point M1's `TemporalKnowledgeBase.ingest_directory()` at each `pdf/` folder
(with the appropriate `regulator_hint`) once you're happy with a batch.

---

## 5. Legal / ethical notes — read this

- Both RBI and SEBI publish these documents specifically for public
  consumption; downloading them for academic research is materially
  different from, say, scraping content behind a login wall or paywall.
  That said, the following still apply:
- **Respect robots.txt.** Both scrapers do this automatically and will
  skip (and log) any URL robots.txt disallows. Don't disable this check.
- **Rate-limit yourself.** The defaults (2–3 seconds between requests) are
  deliberately conservative. These are public-service sites with real
  traffic from citizens, banks, and market participants — a scrape that
  finishes overnight instead of in ten minutes costs you nothing and
  avoids being the reason someone else's page loads slowly.
- **Identify yourself.** An honest `User-Agent` with a real contact email
  is a small courtesy that also protects you: if your IP does get
  rate-limited or blocked, having identified yourself as a specific
  academic project makes it easy to sort out.
- **Don't republish the raw corpus.** Store SHA-256 hashes and source URLs
  (the manifest already does this) rather than distributing the PDFs
  themselves if you share your dataset publicly — link to the manifest
  and let others re-fetch from the original source. This also protects you
  if a document is later withdrawn or corrected.
- **This is not legal advice.** If your institution has a research-ethics
  or data-governance process that covers web scraping, run this past them
  before a large-scale run — a few minutes now avoids a much bigger
  problem later, and shows up well in your final report's methodology
  section regardless.

---

## 6. Known limitations / things to extend

- **RBI Master Directions / Master Circulars** (as opposed to individual
  circulars) live at different URLs (`BS_ViewMasterDirections.aspx`,
  `BS_ViewMasterCirculardetails.aspx`) with a similar but not identical
  structure to the Index page this scraper targets. Not yet implemented —
  a natural next module, following the same ID-walking pattern if their
  detail pages are similarly addressable, otherwise a direct table scrape
  since master circular lists are typically short (tens, not thousands).
- **RBI ID-walking will occasionally hit non-circular content** (other RBI
  pages share the same `Id` numbering space) — these are silently skipped.
  If you see long runs of misses, re-run the self-test.
- **SEBI's "Next" link text-match** (`has_text="Next"`) is a Playwright
  locator matched on visible text, verified live on 2026-09-30. If SEBI
  redesigns their pagination control, this is the first thing to check.
- **No OCR fallback yet.** A handful of very old circulars (pre-2010,
  especially RBI) may be scanned images rather than text PDFs. `parser.py`
  in M1 is where OCR (per the master plan) should be wired in — this
  scraper just gets you the raw file.
- **This does not build the amendment/supersession graph** — that's M1's
  job once documents are ingested. This scraper's `manifest.json` gives
  you `issue_date` per document, which M1 needs as a starting point, but
  "supersedes"/"amends" extraction from the document *text* still happens
  downstream in M1's `supersession_graph.py`.
