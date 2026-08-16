"""
Greenhouse JSON API scraper.
API: https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true
"""
import httpx
from urllib.parse import urlparse
from bs4 import BeautifulSoup


def _company_slug(url: str) -> str:
    # https://job-boards.greenhouse.io/anthropic  →  anthropic
    # https://boards.greenhouse.io/anthropic       →  anthropic
    return urlparse(url).path.strip("/").split("/")[0]


def _strip_html(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(separator="\n").strip()


async def scrape(url: str) -> list[dict]:
    slug = _company_slug(url)
    company = slug.replace("-", " ").title()
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(api_url, headers={"User-Agent": "JobHunter/1.0"})
        resp.raise_for_status()
        data = resp.json()

    roles = []
    for job in data.get("jobs", []):
        jd_html = job.get("content", "")
        metadata = job.get("metadata", [])
        location = job.get("location", {}).get("name", "")
        jd_text = f"Location: {location}\n\n{_strip_html(jd_html)}"
        roles.append({
            "company": company,
            "title": job["title"],
            "url": job["absolute_url"],
            "jd_text": jd_text,
        })
    return roles
