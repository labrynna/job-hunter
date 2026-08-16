"""
Adapter for AIIB (Asian Infrastructure Investment Bank) career pages.

The listing page itself is a static shell — job data lives in a sibling
".content/index/current-jobs.js" file as a plain JS array literal
(jobs[0]["title"]="...";  jobs[0]["path"]="...";  etc.), not JSON. Cheap to
regex out; no browser needed.
"""
import re
import httpx
from datetime import datetime
from urllib.parse import urljoin

_HEADERS = {"User-Agent": "Mozilla/5.0 (JobHunter/1.0)"}
_FIELD_RE = re.compile(r'jobs\[(\d+)\]\["([\w-]+)"\]\s*=\s*"((?:[^"\\]|\\.)*)"\s*;')


def _to_iso_date(text: str | None) -> str | None:
    """AIIB's 'positioning-date' field looks like 'Jul 02, 2026' — not ISO."""
    if not text:
        return None
    try:
        return datetime.strptime(text.strip(), "%b %d, %Y").date().isoformat()
    except ValueError:
        return None


def _jobs_js_url(page_url: str) -> str:
    # .../staff/index.html  ->  .../staff/.content/index/current-jobs.js
    base = page_url.rsplit("/", 1)[0]
    return f"{base}/.content/index/current-jobs.js"


async def scrape_listings(url: str) -> list[dict]:
    js_url = _jobs_js_url(url)

    async with httpx.AsyncClient(timeout=20, headers=_HEADERS, follow_redirects=True) as client:
        resp = await client.get(js_url)
        resp.raise_for_status()
        body = resp.text

    jobs: dict[str, dict] = {}
    for m in _FIELD_RE.finditer(body):
        idx, field, value = m.group(1), m.group(2), m.group(3)
        jobs.setdefault(idx, {})[field] = value.replace('\\"', '"')

    roles = []
    for job in jobs.values():
        title = job.get("title")
        path = job.get("path")
        if not title or not path:
            continue
        roles.append({
            "company": "AIIB",
            "title": title,
            "url": urljoin(url, path),
            "location": job.get("location"),
            "salary_hint": None,
            "jd_text": None,
            "posted_at": _to_iso_date(job.get("positioning-date")),
        })
    return roles


def _looks_like_closed_posting_shell(jd_text: str) -> bool:
    """
    Postings that have closed/been removed still return HTTP 200 from their
    old detail URL, but AIIB's site silently falls back to rendering the
    generic "Career Opportunities" listing shell instead (same page you'd get
    from the board index) rather than a 404. That shell contains none of the
    fields every real posting has, so detect it by their absence rather than
    trying to positively match the shell's own text (which could change).
    """
    return not ("Responsibilities:" in jd_text and "Requirements:" in jd_text)


async def scrape_details_batch(urls: list[str]) -> dict[str, dict]:
    """Detail pages are regular server-rendered HTML — plain GET + text extraction."""
    from bs4 import BeautifulSoup
    results = {}
    if not urls:
        return results

    async with httpx.AsyncClient(timeout=20, headers=_HEADERS, follow_redirects=True) as client:
        for url in urls:
            try:
                resp = await client.get(url)
                soup = BeautifulSoup(resp.text, "html.parser")
                container = soup.select_one(".main-column") or soup.select_one(".content, article, main") or soup
                jd_text = container.get_text(separator="\n").strip()
                if _looks_like_closed_posting_shell(jd_text):
                    results[url] = {
                        "jd_text": None,
                        "error": "posting appears closed/removed — AIIB returned the generic listing shell instead of the job detail page",
                    }
                    continue
                results[url] = {"jd_text": jd_text}
            except Exception as e:
                results[url] = {"jd_text": None, "error": str(e)}

    return results
