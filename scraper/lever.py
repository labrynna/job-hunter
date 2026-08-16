"""
Lever JSON API scraper.
API: https://api.lever.co/v0/postings/{company}?mode=json
"""
import httpx
from datetime import datetime, timezone
from urllib.parse import urlparse
from bs4 import BeautifulSoup


def _posted_at(posting: dict) -> str | None:
    ms = posting.get("createdAt")
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _company_slug(url: str) -> str:
    # https://jobs.lever.co/anthropic  →  anthropic
    return urlparse(url).path.strip("/").split("/")[0]


def _strip_html(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(separator="\n").strip()


def _format_jd(posting: dict) -> str:
    parts = []
    location = posting.get("categories", {}).get("location", "")
    if location:
        parts.append(f"Location: {location}")
    commitment = posting.get("categories", {}).get("commitment", "")
    if commitment:
        parts.append(f"Type: {commitment}")
    for section in posting.get("descriptionBody", {}).get("descriptionSections", []):
        title = section.get("descriptionSectionHeader", "")
        body = _strip_html(section.get("descriptionSectionBody", ""))
        if title:
            parts.append(f"\n{title}")
        if body:
            parts.append(body)
    if not parts:
        parts.append(_strip_html(posting.get("description", "")))
        parts.append(_strip_html(posting.get("additional", "")))
    return "\n".join(parts).strip()


async def scrape(url: str) -> list[dict]:
    slug = _company_slug(url)
    company = slug.replace("-", " ").title()
    api_url = f"https://api.lever.co/v0/postings/{slug}?mode=json"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(api_url, headers={"User-Agent": "JobHunter/1.0"})
        resp.raise_for_status()
        data = resp.json()

    roles = []
    for posting in data:
        roles.append({
            "company": company,
            "title": posting["text"],
            "url": posting["hostedUrl"],
            "jd_text": _format_jd(posting),
            "posted_at": _posted_at(posting),
        })
    return roles
