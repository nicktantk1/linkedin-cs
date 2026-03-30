"""
LinkedIn Job Scraper with Telegram Notifications
-------------------------------------------------
pip install playwright httpx beautifulsoup4 python-dotenv
playwright install chromium
"""

import argparse
import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv()

SEARCH_KEYWORDS = [
    "marketing coordinator",
    "communications coordinator",
    "corporate communications",
    "business development associate",
    "partnerships coordinator",
    "programme coordinator",
    "project coordinator",
    "talent acquisition coordinator",
    "recruiter associate",
]

GEO_ID      = "102454443"   # Singapore
TIME_RANGE  = "r3600"       # posted in the last hour
EXP_LEVEL   = "1,2"         # internship + entry level

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

DB_PATH         = Path(__file__).parent / "seen_jobs.db"
PAGE_TIMEOUT_MS = 30_000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_job_id(*values: Optional[str]) -> str:
    patterns = [
        r"urn:li:jobPosting:(\d+)",
        r"/jobs/view/(?:[^/?]+-)?(\d+)",
        r"\b(\d{7,})\b",
    ]
    for value in values:
        if not value:
            continue
        for pattern in patterns:
            match = re.search(pattern, str(value))
            if match:
                return match.group(1)
    return ""


def escape_md(text: str) -> str:
    return re.sub(r"([_*\[\]()~`>#+=|{}.!-])", r"\\\1", str(text))


def truncate(text: str, max_len: int) -> str:
    text = text.strip()
    return text if len(text) <= max_len else text[: max_len - 3].rstrip() + "..."

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_jobs (
            job_id  TEXT PRIMARY KEY,
            sent_at TEXT
        )
    """)
    conn.commit()
    return conn


def is_new(conn: sqlite3.Connection, job_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM seen_jobs WHERE job_id = ?", (job_id,)
    ).fetchone() is None


def mark_sent(conn: sqlite3.Connection, job_ids: list) -> None:
    now = datetime.utcnow().isoformat()
    conn.executemany(
        "INSERT OR REPLACE INTO seen_jobs (job_id, sent_at) VALUES (?, ?)",
        [(jid, now) for jid in job_ids],
    )
    conn.commit()

# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

async def scrape_jobs(keyword: str) -> list:
    api_url = (
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?"
        + urlencode({
            "keywords": keyword,
            "geoId":    GEO_ID,
            "f_TPR":    TIME_RANGE,
            "f_E":      EXP_LEVEL
        })
    )

    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=15) as client:
            r = await client.get(api_url)
            r.raise_for_status()
    except Exception as exc:
        log.error(f"Failed fetching '{keyword}': {exc}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    job_ids = []
    for card in soup.select("li"):
        div = card.find("div", {"data-entity-urn": True})
        if div:
            job_id = extract_job_id(div["data-entity-urn"])
            if job_id:
                job_ids.append(job_id)

    return job_ids

# ---------------------------------------------------------------------------
# Job detail fetcher
# ---------------------------------------------------------------------------

async def fetch_job_details(job_ids: list) -> list:
    if not job_ids:
        return []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = await context.new_page()
        jobs = []

        for job_id in job_ids:
            job_url = f"https://www.linkedin.com/jobs/view/{job_id}/"
            try:
                await page.goto(job_url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
                await page.wait_for_timeout(1000)
            except Exception as exc:
                log.warning(f"Could not load job {job_id}: {exc}")
                continue

            title_el   = await page.query_selector("h1.top-card-layout__title, h1.t-24, h1")
            company_el = await page.query_selector("a.topcard__org-name-link, .topcard__flavor-row a")

            title   = (await title_el.inner_text()).strip()   if title_el   else "Unknown Title"
            company = (await company_el.inner_text()).strip() if company_el else "Unknown Company"

            bullets = []
            for selector in [
                ".show-more-less-html__markup li",
                ".description__text li",
                ".jobs-description__content li",
            ]:
                for el in await page.query_selector_all(selector):
                    text = re.sub(r"\s+", " ", (await el.inner_text()).strip())
                    if len(text) >= 25 and text not in bullets:
                        bullets.append(text)

            jobs.append({"job_id": job_id, "title": title, "company": company, "bullets": bullets})

        await browser.close()
    return jobs

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

async def send_telegram(jobs: list) -> None:
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    async with httpx.AsyncClient(timeout=10) as client:
        for job in jobs:
            job_url = f"https://www.linkedin.com/jobs/view/{job['job_id']}/"
            bullets = "\n".join(
                f"• {escape_md(truncate(b, 260))}" for b in job["bullets"]
            ) or "• N/A"

            text = truncate(
                f"*{escape_md(truncate(job['title'], 160))}*\n"
                f"🏢 {escape_md(truncate(job['company'], 120))}\n\n"
                f"*📌 Highlights:*\n{bullets}\n\n"
                f"[View on LinkedIn]({job_url})",
                3500,
            )

            for attempt in range(2):
                try:
                    r = await client.post(api_url, json={
                        "chat_id":    TELEGRAM_CHAT_ID,
                        "text":       text,
                        "parse_mode": "MarkdownV2",
                    })
                    if r.status_code == 429 and attempt == 0:
                        wait = r.json().get("parameters", {}).get("retry_after", 2)
                        await asyncio.sleep(int(wait))
                        continue
                    r.raise_for_status()
                    log.info(f"Sent: {job['title']} @ {job['company']}")
                    break
                except Exception as exc:
                    if attempt == 1:
                        log.error(f"Telegram failed for {job['job_id']}: {exc}")

            await asyncio.sleep(0.25)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(reset: bool = False) -> None:
    conn = init_db()

    if reset:
        conn.execute("DELETE FROM seen_jobs")
        conn.commit()
        log.info("DB reset.")

    all_ids = []
    for keyword in SEARCH_KEYWORDS:
        ids = await scrape_jobs(keyword)
        log.info(f"[{len(ids):>2}] {keyword}")
        all_ids.extend(ids)

    scraped_ids = list(dict.fromkeys(all_ids))
    new_ids = [jid for jid in scraped_ids if is_new(conn, jid)]
    log.info(f"{len(new_ids)} new job(s) out of {len(scraped_ids)} scraped.")

    if new_ids:
        jobs = await fetch_job_details(new_ids)
        await send_telegram(jobs)
        mark_sent(conn, [j["job_id"] for j in jobs])

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="Clear the seen-jobs DB")
    args = parser.parse_args()
    asyncio.run(main(reset=args.reset))