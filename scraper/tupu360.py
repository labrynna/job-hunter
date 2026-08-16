"""
Adapter for the "tupu360" China career-site platform (careersite.tupu360.com
and white-labelled deployments on a company's own domain, e.g.
jobs.siemens.com.cn). Confirmed live on Siemens China and BMW China.

Unlike a bespoke Playwright scrape, this platform server-renders its listing
and detail pages as plain HTML and exposes an unauthenticated pagination
endpoint (POST .../position/nextPageList), so the whole thing — listings AND
full JD — is reachable via plain HTTP requests. No browser needed.

Because deployments live on arbitrary custom domains (not just
*.tupu360.com), detection can't be done from the hostname alone — probe()
fetches the page once and checks for the platform's CDN fingerprint.
"""
import re
import httpx
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs, urljoin

_PLATFORM_FINGERPRINT = "careersite.tupu360.com"
_HEADERS = {"User-Agent": "Mozilla/5.0 (JobHunter/1.0)"}
_PAGE_SIZE = 50


def _origin_and_company(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    company_slug = parsed.path.strip("/").split("/")[0]
    return origin, company_slug


def _recruitment_type(url: str) -> str | None:
    qs = parse_qs(urlparse(url).query)
    values = qs.get("recruitmentType")
    return values[0] if values else None


async def probe(url: str) -> bool:
    """Cheap check: does this page's HTML reference the tupu360 CDN?"""
    try:
        async with httpx.AsyncClient(timeout=15, headers=_HEADERS, follow_redirects=True) as client:
            resp = await client.get(url)
            return _PLATFORM_FINGERPRINT in resp.text
    except Exception:
        return False


# The platform has (at least) two front-end templates that different
# tenants use — a "card" style (pid attribute, JS onclick navigation) and a
# "table" style (plain <a href> per row, no pid). Both are handled here.
def _parse_position_items(html: str, origin: str, company: str, recruitment_type: str | None) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for card in soup.select("div.position-item"):
        if "small" in card.get("class", []):
            continue  # table-style header row, not a posting

        # Table-style template: title is a real <a href>. Some tenants (e.g.
        # BMW) embed the href with a literal unescaped "&" (invalid HTML) —
        # "&currentLang=en" then gets legacy-entity-decoded by the parser as
        # "&curren" + "tLang=en" (¤ + garbage), corrupting anything after
        # "positionId=...". Rebuild the URL from just the positionId instead
        # of trusting the raw href tail.
        link = card.select_one(".ele.e-title a[href]")
        if link:
            title = link.get_text(strip=True)
            href = link.get("href")
            if not title or not href:
                continue
            location_el = card.select_one(".ele.e-city")
            location = location_el.get_text(strip=True) if location_el else None

            pid = (parse_qs(urlparse(href).query).get("positionId") or [None])[0]
            if pid:
                detail_url = f"{origin}/{company}/position/detail?positionId={pid}"
                if recruitment_type:
                    detail_url += f"&recruitmentType={recruitment_type}"
            else:
                detail_url = urljoin(origin, href)

            items.append({"title": title, "url": detail_url, "location": location})
            continue

        # Card-style template: needs a pid attribute + constructed detail URL
        pid = card.get("pid")
        if not pid:
            continue
        title_el = card.select_one(".position-name h4.title .txt, .position-name h4.title")
        title = title_el.get_text(strip=True) if title_el else None
        if not title:
            continue

        location = None
        for li in card.select("li.e-city[data-original-title]"):
            label = li.select_one(".txt")
            label_text = label.get_text(strip=True) if label else ""
            if label_text.startswith("Location"):
                location = li.get("data-original-title")
                break

        detail_url = f"{origin}/{company}/position/detail?positionId={pid}"
        if recruitment_type:
            detail_url += f"&recruitmentType={recruitment_type}"

        items.append({"title": title, "url": detail_url, "location": location})
    return items


async def scrape_listings(url: str) -> list[dict]:
    origin, company = _origin_and_company(url)
    recruitment_type = _recruitment_type(url)
    company_name = company.replace("-", " ").title()

    async with httpx.AsyncClient(timeout=20, headers=_HEADERS, follow_redirects=True) as client:
        index_resp = await client.get(url)
        all_items = _parse_position_items(index_resp.text, origin, company, recruitment_type)

        # Paginate by looping until a page comes back empty rather than
        # trusting a total-count attribute — templates vary in whether/how
        # they expose one (e.g. the table-style template doesn't at all).
        offset = len(all_items)
        for _ in range(100):  # safety cap: 100 pages (5000 roles at _PAGE_SIZE)
            form = {"offset": offset, "max": _PAGE_SIZE}
            if recruitment_type:
                form["recruitmentType"] = recruitment_type
            resp = await client.post(f"{origin}/{company}/position/nextPageList", data=form)
            page_items = _parse_position_items(resp.text, origin, company, recruitment_type)
            if not page_items:
                break
            all_items.extend(page_items)
            offset += len(page_items)

    roles = []
    for item in all_items:
        roles.append({
            "company": company_name,
            "title": item["title"],
            "url": item["url"],
            "location": item["location"],
            "salary_hint": None,
            "jd_text": None,
        })
    return roles


_JD_SELECTORS = [".position-description", ".job-description", ".description"]


async def scrape_details_batch(urls: list[str]) -> dict[str, dict]:
    results = {}
    if not urls:
        return results

    async with httpx.AsyncClient(timeout=20, headers=_HEADERS, follow_redirects=True) as client:
        for url in urls:
            try:
                resp = await client.get(url)
                soup = BeautifulSoup(resp.text, "html.parser")
                jd_text = ""
                for sel in _JD_SELECTORS:
                    el = soup.select_one(sel)
                    if el:
                        jd_text = el.get_text(separator="\n").strip()
                        break
                results[url] = {"jd_text": jd_text}
            except Exception as e:
                results[url] = {"jd_text": None, "error": str(e)}

    return results
