"""
Ashby job board API scraper.
API: GET https://api.ashbyhq.com/posting-api/job-board/{company}
"""
import httpx
from urllib.parse import urlparse
from bs4 import BeautifulSoup


def _company_slug(url: str) -> str:
    # https://jobs.ashby.com/anthropic  →  anthropic
    # https://www.ashbyhq.com/anthropic  →  anthropic
    return urlparse(url).path.strip("/").split("/")[0]


def _strip_html(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(separator="\n").strip()


async def scrape(url: str) -> list[dict]:
    slug = _company_slug(url)
    company = slug.replace("-", " ").title()
    api_url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(api_url, headers={
            "User-Agent": "JobHunter/1.0",
            "Accept": "application/json",
        })
        resp.raise_for_status()
        data = resp.json()

    roles = []
    for job in data.get("jobPostings", []):
        location = job.get("locationName") or job.get("location", "")
        dept = job.get("departmentName", "")
        jd_html = job.get("descriptionHtml", "") or job.get("description", "")
        jd_text = f"Location: {location}\nDepartment: {dept}\n\n{_strip_html(jd_html)}"
        job_url = job.get("jobUrl") or job.get("applyUrl") or url
        # Field name varies by tenant configuration; try the common ones.
        posted_at = job.get("publishedDate") or job.get("publishedAt") or job.get("firstPublishedDate")
        roles.append({
            "company": company,
            "title": job["title"],
            "url": job_url,
            "jd_text": jd_text,
            "posted_at": posted_at,
        })
    return roles
