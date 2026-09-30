"""
run.py — single entry point for scraping both regulators.

This wraps rbi_scraper.py and sebi_scraper.py so you can run one command
to build (or extend) your data/raw/ corpus, matching the layout M1's
TemporalKnowledgeBase.ingest_directory() expects:

    data/raw/rbi/pdf/*.pdf
    data/raw/rbi/manifest.json
    data/raw/sebi/circulars/pdf/*.pdf
    data/raw/sebi/circulars/manifest.json
    data/raw/sebi/master_circulars/pdf/*.pdf
    data/raw/sebi/master_circulars/manifest.json

Examples
--------
# Small first run to sanity-check everything (recommended before scaling up)
python run.py --rbi-max-docs 20 --sebi-max-pages 1

# A larger backfill run once you've checked the output
python run.py --rbi-max-docs 2000 --sebi-max-pages 40

# Only one regulator
python run.py --only rbi --rbi-max-docs 500
python run.py --only sebi --sebi-max-pages 20
"""

from __future__ import annotations

import argparse
from pathlib import Path

from scraper.rbi_scraper import scrape_rbi
from scraper.sebi_scraper import scrape_sebi


def main():
    ap = argparse.ArgumentParser(description="Scrape RBI + SEBI regulatory documents.")
    ap.add_argument("--out-root", type=Path, default=Path("data/raw"))
    ap.add_argument("--only", choices=["rbi", "sebi", "both"], default="both")

    ap.add_argument("--rbi-max-docs", type=int, default=200)
    ap.add_argument("--rbi-start-id", type=int, default=None)
    ap.add_argument("--rbi-sleep", type=float, default=2.0)

    ap.add_argument("--sebi-sections", nargs="+",
                     default=["circulars", "master_circulars"])
    ap.add_argument("--sebi-max-pages", type=int, default=5)
    ap.add_argument("--sebi-sleep-pages", type=float, default=3.0)
    ap.add_argument("--sebi-sleep-docs", type=float, default=2.0)

    args = ap.parse_args()

    if args.only in ("rbi", "both"):
        print("=" * 70)
        print("Scraping RBI ...")
        print("=" * 70)
        scrape_rbi(
            out_dir=args.out_root / "rbi",
            max_docs=args.rbi_max_docs,
            start_id=args.rbi_start_id,
            sleep_between=args.rbi_sleep,
        )

    if args.only in ("sebi", "both"):
        for section in args.sebi_sections:
            print("=" * 70)
            print(f"Scraping SEBI / {section} ...")
            print("=" * 70)
            scrape_sebi(
                section=section,
                out_dir=args.out_root / "sebi" / section,
                max_pages=args.sebi_max_pages,
                sleep_between_pages=args.sebi_sleep_pages,
                sleep_between_docs=args.sebi_sleep_docs,
            )

    print("\nAll done. Next step: point M1's TemporalKnowledgeBase.ingest_directory()"
          f" at {args.out_root}/<regulator>/**/pdf/ to build chunks.parquet.")


if __name__ == "__main__":
    main()
