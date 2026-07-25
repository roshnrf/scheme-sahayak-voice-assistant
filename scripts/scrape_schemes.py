"""Scrape 40-60 central government schemes from myscheme.gov.in in English, Hindi, and Malayalam.

myscheme.gov.in's own hi/ml translation coverage turned out to be inconsistent per-scheme:
some schemes have genuinely translated body content, others translate only the section
header labels and leave the body in English. So this uses native site content where it's
genuinely present, and falls back to Sarvam Mayura machine translation (flagged via
translated/mt_fields) for whatever the site doesn't have - see translate_fallback.py.

Resumable: slugs already present in data/schemes.json are skipped on rerun.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from translate_fallback import fill_translation_gaps

BASE = "https://www.myscheme.gov.in"
DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "schemes.json"

TARGET_TOTAL = 50
PER_MINISTRY_CAP = 3
MAX_PAGES = 15

SECTIONS = {
    "details": "details",
    "benefits": "benefits",
    "eligibility": "eligibility",
    "application_process": "application-process",
    "documents_required": "documents-required",
}


def load_existing() -> list[dict]:
    if DATA_PATH.exists():
        return json.loads(DATA_PATH.read_text(encoding="utf-8"))
    return []


def save(records: list[dict]) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_cards(page) -> list[tuple[str, str]]:
    """Return list of (slug, ministry) for the current results page."""
    return page.evaluate(
        """() => {
            const links = Array.from(document.querySelectorAll('a[href^="/schemes/"]'));
            return links.map(a => {
                const container = a.closest('div').parentElement;
                const h2s = container ? container.querySelectorAll('h2') : [];
                return [a.getAttribute('href').split('/schemes/')[1], h2s.length > 1 ? h2s[1].textContent.trim() : ''];
            });
        }"""
    )


def _goto_page(page, page_num: int) -> bool:
    """Click the pagination item for page_num, scoped to the pagination <ul>. Returns False if not found."""
    found = page.evaluate(
        """(n) => {
            const all = Array.from(document.querySelectorAll('li'));
            const li = all.find(e => e.textContent.trim() === String(n) && e.closest('ul'));
            if (!li) return false;
            li.scrollIntoView();
            li.click();
            return true;
        }""",
        page_num,
    )
    return bool(found)


def collect_slugs(page) -> dict[str, str]:
    """Filter to Central Schemes, paginate, diversify by ministry (category filter panel is a
    hidden mobile-only drawer on this site - not usable from a headless desktop context)."""
    page.goto(f"{BASE}/search", wait_until="networkidle")
    page.get_by_text("Central Schemes", exact=True).first.click(force=True)
    page.wait_for_timeout(1500)

    picked: dict[str, str] = {}
    per_ministry: dict[str, int] = {}

    for page_num in range(1, MAX_PAGES + 1):
        if len(picked) >= TARGET_TOTAL:
            break
        if page_num > 1:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(500)
            if not _goto_page(page, page_num):
                print(f"  [info] no page {page_num} control found, stopping pagination")
                break
            page.wait_for_timeout(1500)

        cards = _read_cards(page)
        added = 0
        for slug, ministry in cards:
            if slug in picked:
                continue
            if per_ministry.get(ministry, 0) >= PER_MINISTRY_CAP:
                continue
            picked[slug] = ministry
            per_ministry[ministry] = per_ministry.get(ministry, 0) + 1
            added += 1
        print(f"  page {page_num}: +{added} (total {len(picked)})")

    return picked


def extract_lang(page, slug: str, locale_prefix: str) -> dict[str, str]:
    prefix = f"/{locale_prefix}" if locale_prefix else ""
    page.goto(f"{BASE}{prefix}/schemes/{slug}", wait_until="networkidle")
    page.wait_for_timeout(800)

    def text_of(section_id: str) -> str:
        loc = page.locator(f"#{section_id}")
        if not loc.count():
            return ""
        text = loc.first.inner_text().replace("﻿", "").strip()
        # first line is always the section's own heading (e.g. "Details") - drop it
        _, _, rest = text.partition("\n")
        return (rest or text).strip()

    header = page.evaluate(
        """() => {
            const h1 = Array.from(document.querySelectorAll('h1')).find(h => h.textContent.trim().length > 0);
            if (!h1) return {name: '', ministry: ''};
            const h3 = h1.parentElement ? h1.parentElement.querySelector('h3') : null;
            return {name: h1.textContent.trim(), ministry: h3 ? h3.textContent.trim() : ''};
        }"""
    )

    return {
        "name": header["name"],
        "ministry": header["ministry"],
        **{field: text_of(section_id) for field, section_id in SECTIONS.items()},
    }


def scrape_scheme(page, slug: str, ministry_hint: str) -> dict:
    en = extract_lang(page, slug, "")
    if not en["details"]:
        raise ValueError("empty English details - bad scrape, not spending translation credits on it")

    record = {
        "slug": slug,
        "ministry_hint": ministry_hint,
        "source_url": f"{BASE}/schemes/{slug}",
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "en": en,
        "hi": extract_lang(page, slug, "hi"),
        "ml": extract_lang(page, slug, "ml"),
    }
    # only spend Sarvam credits translating once we know the scrape itself is good
    fill_translation_gaps(record)
    return record


def main() -> None:
    load_dotenv()
    records = load_existing()
    done_slugs = {r["slug"] for r in records}
    print(f"Already scraped: {len(done_slugs)}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1200})

        print("Collecting scheme slugs, diversified by ministry...")
        picked = collect_slugs(page)
        print(f"Total distinct slugs picked: {len(picked)}")

        for slug, ministry_hint in picked.items():
            if slug in done_slugs:
                continue
            print(f"Scraping {slug} ({ministry_hint})...")

            record = None
            for attempt in (1, 2):
                try:
                    record = scrape_scheme(page, slug, ministry_hint)
                    break
                except Exception as exc:
                    print(f"  [attempt {attempt}] {slug}: {exc}")
                    if "insufficient_quota" in str(exc) or "No credits available" in str(exc):
                        print("Sarvam account is out of credits. Stopping - top up and rerun "
                              "(already-scraped schemes will be skipped automatically).")
                        save(records)
                        return
                    page.wait_for_timeout(1500)

            if record is None:
                print(f"  [skip] {slug}: failed after retry")
                continue

            records.append(record)
            done_slugs.add(slug)
            save(records)
            print(f"  saved ({len(records)} total)")

        browser.close()

    print(f"Done. {len(records)} schemes in {DATA_PATH}")


if __name__ == "__main__":
    main()
