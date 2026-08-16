"""
Adapter for de-talents.com (German-speaking recruiter site, jobs in China).

The listing page is a Nuxt SPA whose job data comes from an unauthenticated
JSON API that already includes the full JD (title, description, requirement)
in the same paginated response — no separate detail fetch needed, same
pattern as Greenhouse/Lever/Ashby.
"""
import httpx
from bs4 import BeautifulSoup

_API_URL = "https://www.de-talents.com/recruitapi/api/job/getPositionList"
_HEADERS = {"User-Agent": "Mozilla/5.0 (JobHunter/1.0)", "Content-Type": "application/json;charset=UTF-8"}
_PAGE_SIZE = 50
_BODY = {
    "hostFlag": 0, "type": 1, "companyId": -1, "province": "", "city": "",
    "positionCategoryDictId": -1, "salaryUp": -1, "salaryDown": -1, "searchText": "",
}


def _strip_html(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(separator="\n").strip()


async def scrape(url: str) -> list[dict]:
    """Full scrape (listing + JD in one call) — this board has no separate detail phase."""
    roles = []
    async with httpx.AsyncClient(timeout=20, headers=_HEADERS) as client:
        page_num = 1
        while True:
            resp = await client.post(
                _API_URL, params={"pageNum": page_num, "pageSize": _PAGE_SIZE}, json=_BODY,
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})
            positions = data.get("positionList", [])
            if not positions:
                break

            for job in positions:
                location_parts = [p for p in (job.get("cityLabel"), job.get("provinceLabel")) if p]
                location = ", ".join(location_parts) if location_parts else None
                jd_parts = [_strip_html(job.get("description")), _strip_html(job.get("requirement"))]
                jd_text = "\n\n".join(p for p in jd_parts if p)

                roles.append({
                    "company": job.get("companyName") or "DE Talents",
                    "title": job["title"],
                    "url": f"https://www.de-talents.com/server/positionDetail?id={job['id']}",
                    "jd_text": jd_text,
                    "posted_at": job.get("releaseDate"),
                })

            total = data.get("totalCount", 0)
            if page_num * _PAGE_SIZE >= total:
                break
            page_num += 1

    return roles
