"""
Adapter for remoteok.com — a public, unauthenticated JSON API returning the
full listing feed (including description) in one call, no browser needed.
First array element is a legal/metadata banner, not a job — skip it.
"""
import html
import httpx
from bs4 import BeautifulSoup

_API_URL = "https://remoteok.com/api"
_HEADERS = {"User-Agent": "Mozilla/5.0 (JobHunter/1.0)"}


def _location_str(job: dict) -> str | None:
    loc = (job.get("location") or "").strip()
    return loc or None


async def scrape(url: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=20, headers=_HEADERS, follow_redirects=True) as client:
        resp = await client.get(_API_URL)
        resp.raise_for_status()
        jobs = resp.json()

    roles = []
    for job in jobs:
        title = html.unescape(job.get("position") or "") or None
        company = html.unescape(job.get("company") or "") or None
        job_url = job.get("url") or job.get("apply_url")
        if not title or not company or not job_url:
            continue

        description_html = job.get("description") or ""
        jd_text = BeautifulSoup(description_html, "html.parser").get_text(separator="\n").strip()
        tags = job.get("tags") or []
        if tags:
            jd_text = f"Tags: {', '.join(tags)}\n\n{jd_text}"

        salary_hint = None
        if job.get("salary_min") or job.get("salary_max"):
            salary_hint = f"${job.get('salary_min', 0):,} - ${job.get('salary_max', 0):,}"

        roles.append({
            "company": company,
            "title": title,
            "url": job_url,
            "location": _location_str(job),
            "salary_hint": salary_hint,
            "jd_text": jd_text or None,
            "posted_at": job.get("date"),
        })
    return roles
