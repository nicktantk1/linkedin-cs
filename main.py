"""
LinkedIn Job Scraper with Telegram Notifications
-------------------------------------------------
Scrapes a LinkedIn job search URL, detects new listings,
stores seen jobs in SQLite, and sends Telegram alerts.

Setup:
  pip install playwright httpx
  playwright install chromium

Usage:
  python scraper.py                  # run once
  python scraper.py --reset          # clear the DB and start fresh

Schedule with cron (every 15 min):
  */15 * * * * /usr/bin/python3 /path/to/scraper.py >> /path/to/scraper.log 2>&1
"""

import argparse
import asyncio
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional
import os
import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Configuration — edit these values
# ---------------------------------------------------------------------------
load_dotenv()

LINKEDIN_URL = "https://www.linkedin.com/jobs/search/?keywords=marketing&f_TPR=r3600&geoId=102454443"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

DB_PATH = Path(__file__).parent / "seen_jobs.db"

PAGE_TIMEOUT_MS = 30_000
MIN_DELAY = 2.0
MAX_DELAY = 5.0

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def extract_job_id(*values: Optional[str]) -> str:
    """Extract a LinkedIn job id from plain id strings, entity urns, or URLs."""
    patterns = [
        r"urn:li:jobPosting:(\d+)",
        r"/jobs/view/(?:[^/?]+-)?(\d+)",
        r"\b(\d{7,})\b",
    ]

    for value in values:
        if not value:
            continue
        text = str(value).strip()
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1)
    return ""


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_jobs (
            job_id      TEXT PRIMARY KEY,
            sent_at     TEXT
        )
    """)
    conn.commit()
    return conn


def get_seen_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT job_id FROM seen_jobs").fetchall()
    return {r[0] for r in rows}


def get_seen_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM seen_jobs").fetchone()
    return int(row[0]) if row else 0


def is_new_job(conn: sqlite3.Connection, job_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM seen_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return row is None


def mark_jobs_sent(conn: sqlite3.Connection, job_ids: list[str]) -> None:
    now = datetime.utcnow().isoformat()
    conn.executemany(
        """
        INSERT OR REPLACE INTO seen_jobs (job_id, sent_at)
        VALUES (?, ?)
        """,
        [(job_id, now) for job_id in job_ids],
    )
    conn.commit()


def log_runtime_context(reset: bool) -> None:
    """Log execution context useful for cron and persistence debugging."""
    log.info(
        "Run context | reset=%s cwd=%s db_path=%s db_exists=%s github_run_id=%s github_run_attempt=%s",
        reset,
        Path.cwd(),
        DB_PATH.resolve(),
        DB_PATH.exists(),
        os.getenv("GITHUB_RUN_ID", "local"),
        os.getenv("GITHUB_RUN_ATTEMPT", "n/a"),
    )


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

async def send_telegram(jobs: list[dict]) -> None:
    """Send one Telegram message per new job."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    async with httpx.AsyncClient(timeout=10) as client:
        for job in jobs:
            job_url = f"https://www.linkedin.com/jobs/view/{job['job_id']}/"
            bullet_points = job.get("bullet_points", [])
            bullets_text = "\n".join(
                f"• {escape_md(truncate_text(item, 260))}" for item in bullet_points
            ) or "• N/A"

            text = (
                f"*{escape_md(truncate_text(job['title'], 160))}*\n"
                f"🏢 Company: {escape_md(truncate_text(job['company'], 120))}\n"
                f"\n*📌 Job Highlights:*\n{bullets_text}\n"
                f"\n[LinkedIn]({job_url})"
            )
            text = truncate_text(text, 3500)
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "MarkdownV2",
                "disable_web_page_preview": False,
            }
            max_attempts = 2
            for attempt in range(max_attempts):
                try:
                    r = await client.post(url, json=payload)
                    if r.status_code == 429 and attempt < (max_attempts - 1):
                        retry_after = 2
                        try:
                            retry_after = int(r.json().get("parameters", {}).get("retry_after", 2))
                        except Exception:
                            pass
                        log.warning(
                            f"Telegram rate-limited; retrying in {retry_after}s for job {job['job_id']}"
                        )
                        await asyncio.sleep(retry_after)
                        continue

                    r.raise_for_status()
                    log.info(f"Telegram ✓  {job['title']} @ {job['company']}")
                    break
                except Exception as exc:
                    if attempt == (max_attempts - 1):
                        log.error(f"Telegram send failed: {exc}")

            # Small pause between sends reduces burst-related 429 responses.
            await asyncio.sleep(0.25)


def escape_md(text: str) -> str:
    """Escape special chars for Telegram MarkdownV2."""
    special = r"_*[]()~`>#+-=|{}.!"
    return re.sub(f"([{re.escape(special)}])", r"\\\1", str(text))


def truncate_text(text: str, max_len: int) -> str:
    """Trim text to fit limits while preserving readability."""
    text = text.strip()
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return text[: max_len - 3].rstrip() + "..."


async def fetch_job_details(job_ids: list[str]) -> list[dict]:
    """Fetch full job details for each new job id and extract all description <li> bullets."""
    if not job_ids:
        return []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = await context.new_page()

        jobs: list[dict] = []
        for job_id in job_ids:
            job_url = f"https://www.linkedin.com/jobs/view/{job_id}/"
            try:
                await page.goto(job_url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
                await page.wait_for_timeout(1000)
            except Exception as exc:
                log.warning(f"Failed loading job detail page for {job_id}: {exc}")
                continue

            title_el = await page.query_selector(
                (
                    "h1.top-card-layout__title, "
                    "h1.t-24, "
                    "h1, "
                    "._6b06b22f._9c3c3006.b2efed5c._47af83a7._2327f28f.aa661bbd._62051d4a._112a7898._9ebd600b"
                )
            )
            company_el = await page.query_selector(
                (
                    "a.topcard__org-name-link, "
                    ".topcard__flavor-row a, "
                    ".top-card-layout__card .topcard__flavor, "
                    "a._112a7898._727ac2a2._38a26304.d4697dfb"
                )
            )
            title = (await title_el.inner_text()).strip() if title_el else "Unknown Title"
            company = (await company_el.inner_text()).strip() if company_el else "Unknown Company"

            selectors = [
                ".show-more-less-html__markup li",
                ".description__text li",
                ".jobs-description__content li",
                ".jobs-box__html-content li",
            ]
            bullets: list[str] = []

            for selector in selectors:
                elements = await page.query_selector_all(selector)
                for element in elements:
                    text = (await element.inner_text()).strip()
                    text = re.sub(r"\s+", " ", text)
                    if len(text) < 25:
                        continue
                    if text in bullets:
                        continue
                    bullets.append(text)
            jobs.append({
                "job_id": job_id,
                "title": title,
                "company": company,
                "bullet_points": bullets,
            })

        await browser.close()

    return jobs


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

async def scrape_jobs(url: str) -> list[str]:
    """
    Launch a headless Chromium browser, load the LinkedIn jobs page,
    and extract job cards from the DOM.
    """
    job_ids: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )

        page = await context.new_page()

        log.info(f"Loading: {url}")
        try:
            await page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            await page.screenshot(path="debug.png")
        except Exception as exc:
            log.error(f"Page load failed: {exc}")
            await browser.close()
            return job_ids

        # Wait for either known card containers or canonical job links.
        try:
            await page.wait_for_selector(
                (
                    "li[data-occludable-job-id], "
                    "div[data-job-id], "
                    "li .base-card, "
                    "a[href*='/jobs/view/']"
                ),
                timeout=PAGE_TIMEOUT_MS,
            )
        except Exception:
            log.warning("Timed out waiting for job cards — page may require login.")
            # Dump the page title so we can see what happened
            log.warning(f"Page title: {await page.title()}")
            await browser.close()
            return job_ids

        # Extract only job ids from card elements.
        job_cards = await page.query_selector_all(
            "li[data-occludable-job-id], div[data-job-id], li .base-card"
        )
        log.info(f"Found {len(job_cards)} card candidate(s) on the page.")

        for card in job_cards:
            # job_id from the attribute
            raw_occludable = await card.get_attribute("data-occludable-job-id")
            raw_job_id = await card.get_attribute("data-job-id")
            raw_urn = await card.get_attribute("data-entity-urn")
            job_id = extract_job_id(raw_occludable, raw_job_id, raw_urn)

            if not job_id:
                # Fall back to parsing the id from a jobs/view link when data attributes are missing.
                link_el = await card.query_selector("a[href*='/jobs/view/']")
                href = (await link_el.get_attribute("href")) if link_el else ""
                job_id = extract_job_id(href)

            if not job_id:
                continue

            job_ids.append(job_id)

    # Preserve order while removing duplicates.
    return list(dict.fromkeys(job_ids))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(reset: bool = False) -> None:
    log_runtime_context(reset)
    conn = init_db(DB_PATH)

    if reset:
        conn.execute("DELETE FROM seen_jobs")
        conn.commit()
        log.info("Database cleared.")

    seen_ids = get_seen_ids(conn)
    log.info(
        "Already sent %s job(s) in DB (row_count=%s).",
        len(seen_ids),
        get_seen_count(conn),
    )

    scraped_ids = await scrape_jobs(LINKEDIN_URL)
    log.info(f"Scraped {len(scraped_ids)} job id(s) from LinkedIn.")
    if scraped_ids:
        log.info("Scraped IDs sample: %s", scraped_ids[:10])

    new_job_ids = [job_id for job_id in scraped_ids if is_new_job(conn, job_id)]
    log.info(f"{len(new_job_ids)} new job id(s) found.")
    if new_job_ids:
        log.info("New IDs sample: %s", new_job_ids[:10])

    if new_job_ids:
        new_jobs = await fetch_job_details(new_job_ids)
        await send_telegram(new_jobs)
        mark_jobs_sent(conn, [j["job_id"] for j in new_jobs])
        log.info("After mark sent, DB row_count=%s.", get_seen_count(conn))
        log.info("Done — notifications sent.")
    else:
        log.info("No new jobs — nothing to do.")

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LinkedIn job scraper + Telegram notifier")
    parser.add_argument("--reset", action="store_true", help="Clear the seen-jobs DB before running")
    args = parser.parse_args()
    asyncio.run(main(reset=args.reset))