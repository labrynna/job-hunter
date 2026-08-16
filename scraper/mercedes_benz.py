"""
Adapter for jobs.mercedes-benz.com (Nuxt.js frontend on the "beesite" ATS
platform — a common backend used by several DAX-listed German companies).

The frontend calls an unauthenticated POST /search/ endpoint on
mercedes-benz-beesite-production-gjb.app.beesite.de with a SearchCriteria
list (found by reading the frontend's compiled JS bundle for its request
schema). The response already includes the full JD (Tasks + Qualifications)
per result — no separate detail-page fetch needed.
"""
import httpx
from bs4 import BeautifulSoup

_SEARCH_URL = "https://mercedes-benz-beesite-production-gjb.app.beesite.de/search/"
_HEADERS = {"User-Agent": "Mozilla/5.0 (JobHunter/1.0)", "Content-Type": "application/json"}
_PAGE_SIZE = 100

# China (per the site's internal location taxonomy, confirmed by testing)
_CHINA_COUNTRY_CODE = 446


def _strip_html(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(separator="\n").strip()


async def scrape(url: str) -> list[dict]:
    """Full scrape (listing + JD in one call) — filtered to China postings."""
    roles = []
    seen_ids = set()

    async with httpx.AsyncClient(timeout=20, headers=_HEADERS) as client:
        first_item = 1
        while True:
            body = {
                "LanguageCode": "EN",
                "SearchParameters": {"FirstItem": first_item, "CountItem": _PAGE_SIZE},
                "SearchCriteria": [
                    {"CriterionName": "PositionLocation.Country", "CriterionValue": [_CHINA_COUNTRY_CODE]},
                ],
            }
            resp = await client.post(_SEARCH_URL, json=body)
            resp.raise_for_status()
            data = resp.json()
            sr = data.get("SearchResult", {})
            items = sr.get("SearchResultItems", [])
            if not items:
                break

            for item in items:
                d = item.get("MatchedObjectDescriptor", {})
                job_id = d.get("ID")
                # The same underlying vacancy (same PositionID) frequently
                # reappears in results under several different MatchedObjectIds
                # — dedupe on PositionID here so we don't even hand duplicates
                # to the database layer.
                position_id = d.get("PositionID") or job_id
                if not job_id or position_id in seen_ids:
                    continue
                seen_ids.add(position_id)

                title = d.get("PositionTitle")
                job_url = d.get("PositionURI")
                if not title or not job_url:
                    continue

                location_parts = []
                for loc in d.get("PositionLocation", []):
                    city = loc.get("CityName")
                    if city:
                        location_parts.append(city)
                location = ", ".join(dict.fromkeys(location_parts)) or None

                desc = d.get("PositionFormattedDescription") or []
                desc = desc[0] if isinstance(desc, list) and desc else (desc if isinstance(desc, dict) else {})
                jd_parts = [_strip_html(desc.get("Tasks")), _strip_html(desc.get("Qualifications"))]
                jd_text = "\n\n".join(p for p in jd_parts if p)

                roles.append({
                    "company": d.get("ParentOrganizationName") or "Mercedes-Benz",
                    "title": title,
                    "url": job_url,
                    "location": location,
                    "jd_text": jd_text,
                    "posted_at": d.get("PublicationStartDate"),
                })

            total = sr.get("SearchResultCountAll", 0)
            first_item += _PAGE_SIZE
            if first_item > total or first_item > 2000:  # safety cap
                break

    return roles
