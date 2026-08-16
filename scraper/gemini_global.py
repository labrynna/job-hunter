"""
Adapter for gemini-global.com (Gemini Personnel recruitment agency).

Individual job postings are indexed in a plain sitemap.xml with the title
slugified into the URL itself, and each posting page embeds a schema.org
JobPosting JSON-LD block (used for Google for Jobs SEO) with structured
title/description/location/salary data — all reachable via plain HTTP,
no browser needed.
"""
import json
import re
import httpx
from bs4 import BeautifulSoup
from urllib.parse import unquote

_SITEMAP_URL = "https://gemini-global.com/sitemap.xml"
_HEADERS = {"User-Agent": "Mozilla/5.0 (JobHunter/1.0)"}
_JOB_URL_RE = re.compile(r"https://gemini-global\.com/job-search/post/\?jref=([\w]+)&(?:amp;)?job=([\w-]+)")


def _slug_to_title(slug: str) -> str:
    return unquote(slug).replace("-", " ").title()


async def scrape_listings(url: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=20, headers=_HEADERS, follow_redirects=True) as client:
        resp = await client.get(_SITEMAP_URL)
        resp.raise_for_status()
        xml = resp.text

    roles = []
    seen = set()
    for m in _JOB_URL_RE.finditer(xml):
        jref, slug = m.group(1), m.group(2)
        job_url = f"https://gemini-global.com/job-search/post/?jref={jref}&job={slug}"
        if job_url in seen:
            continue
        seen.add(job_url)
        roles.append({
            "company": "Gemini Personnel",
            "title": _slug_to_title(slug),
            "url": job_url,
            "location": None,
            "salary_hint": None,
            "jd_text": None,
        })
    return roles


_TITLE_DESC_RE = re.compile(
    r'"title"\s*:\s*"(?P<title>.*?)"\s*,\s*\n\s*"description"\s*:\s*"(?P<description>.*?)"\s*,\s*\n\s*"identifier"',
    re.DOTALL,
)


def _extract_jobposting_jsonld(html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or ""

        try:
            # strict=False: the site embeds literal newlines inside string
            # values (e.g. the HTML description), which is invalid per the
            # JSON spec's strict mode but common enough in the wild to warrant
            # tolerating it here.
            data = json.loads(raw, strict=False)
            if isinstance(data, dict) and data.get("@type") == "JobPosting":
                return data
        except Exception:
            pass

        # Some postings embed genuinely malformed JSON (unescaped quotes
        # inside the description's HTML). Fall back to pulling just the
        # title/description fields out with a regex — enough for a usable JD.
        if '"@type" : "JobPosting"' in raw or '"@type":"JobPosting"' in raw:
            m = _TITLE_DESC_RE.search(raw)
            if m:
                return {"title": m.group("title"), "description": m.group("description")}
    return None


async def scrape_details_batch(urls: list[str]) -> dict[str, dict]:
    """
    Uses a fresh, cookie-less httpx.AsyncClient per request rather than one
    shared client for the whole batch. Confirmed empirically: after ~4
    requests on a shared client/cookie-jar, the site silently serves a
    degraded page missing the JobPosting schema (a soft per-session view
    limit) — a fresh anonymous session per request avoids that entirely.
    """
    results = {}
    if not urls:
        return results

    for url in urls:
        try:
            async with httpx.AsyncClient(timeout=20, headers=_HEADERS, follow_redirects=True) as client:
                resp = await client.get(url)
            posting = _extract_jobposting_jsonld(resp.text)
            if not posting:
                results[url] = {"jd_text": None, "error": "no JobPosting schema found"}
                continue

            parts = [posting.get("title", "")]
            location = posting.get("jobLocation", {}).get("address", {})
            loc_str = ", ".join(filter(None, [location.get("addressLocality"), location.get("addressCountry")]))
            if loc_str:
                parts.append(f"Location: {loc_str}")
            salary = posting.get("baseSalary", {}).get("value", {})
            if salary.get("minValue"):
                parts.append(f"Salary: {salary.get('minValue')}-{salary.get('maxValue')} {posting.get('baseSalary', {}).get('currency', '')}")
            description_html = posting.get("description", "")
            parts.append(BeautifulSoup(description_html, "html.parser").get_text(separator="\n").strip())

            results[url] = {
                "jd_text": "\n\n".join(p for p in parts if p),
                "posted_at": posting.get("datePosted"),
            }
        except Exception as e:
            results[url] = {"jd_text": None, "error": str(e)}

    return results
