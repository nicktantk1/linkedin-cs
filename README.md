# LinkedIn Job Scraper (Telegram Alerts)

## Purpose
This application monitors recent LinkedIn job postings for selected keywords, enriches each posting with job and company details, filters by company follower count, and sends matching results to Telegram.

## main.py fields
Core configuration and data fields used in `main.py`:

- `SEARCH_KEYWORDS`: keywords to query on LinkedIn
- `GEO_ID`: location filter (currently Singapore)
- `TIME_RANGE`: recency filter for postings
- `EXP_LEVEL`: experience-level filter
- `MIN_FOLLOWERS`: minimum company follower threshold
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`: Telegram delivery credentials
- `DB_PATH`: SQLite file used to track already-sent jobs
- `PAGE_TIMEOUT_MS`: page load timeout for Playwright

Job record fields collected per posting:

- `job_id`
- `title`
- `company`
- `bullets` (highlights/responsibilities extracted from list items)
- `followers` (from company page)
- `company_size` (employees range/text from company page)

## Workflow
1. For each keyword in `SEARCH_KEYWORDS`, the app calls LinkedIn's guest jobs endpoint and collects all matching job links/entries.
2. From each job entry, it extracts all job IDs.
3. For each job ID, it opens the job page and extracts:
   - job title
   - company name
   - bullet-point highlights from the description
   - company page URL
4. It then visits the company page and extracts:
   - follower count
   - employees/company size text
5. Jobs are filtered by `MIN_FOLLOWERS` and deduplicated using `seen_jobs.db`.
6. Remaining jobs are sent to Telegram, then marked as sent in SQLite.

## Run
Install dependencies:

```bash
pip install -r requirements.txt
playwright install chromium
```

Run scraper:

```bash
python main.py
```

Optional reset of seen-jobs database:

```bash
python main.py --reset
```
