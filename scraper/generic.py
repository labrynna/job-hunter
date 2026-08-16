"""
Generic Playwright-based scraper for Workday and unknown board types.

Split into two phases so we don't pay for a full page visit per job unless
the role has already cleared a pre-score filter:
  - scrape_listings(url):        one page load, extracts title/url/location/
                                  salary text visible on the board's listing page.
  - scrape_details_batch(urls):  one browser session, visits only the given
                                  shortlisted URLs, extracts full JD text.
"""
import re
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse


def _strip_html(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(separator="\n").strip()


def _company_from_url(url: str) -> str:
    host = urlparse(url).hostname or ""
    parts = host.replace("www.", "").split(".")
    return parts[0].replace("-", " ").title()


# Common selectors for job listing links across various boards
_JOB_LINK_SELECTORS = [
    "a[href*='/job/']",
    "a[href*='/jobs/']",
    "a[href*='/careers/']",
    "a[href*='/posting/']",
    "a[href*='greenhouse.io']",
    "[data-automation-id='jobTitle'] a",  # Workday
    ".job-title a",
    ".position-title a",
    "li.opening a",
]

_JD_BODY_SELECTORS = [
    "[data-automation-id='jobPostingDescription']",  # Workday
    ".job-description",
    "#job-description",
    ".description",
    "article",
    "main",
]

_LOCATION_HINT_RE = re.compile(
    r"\b(remote|hybrid|beijing|shanghai|shenzhen|guangzhou|hong ?kong|singapore|"
    r"[A-Z][a-z]+,\s*[A-Z]{2,}|China|Germany|USA|UK)\b"
)
_SALARY_HINT_RE = re.compile(r"[$€£¥]\s?\d[\d,\.]*\s?[kK]?(\s?[-–]\s?[$€£¥]?\s?\d[\d,\.]*\s?[kK]?)?")


def _nearby_text(link_text: str, container_text: str) -> tuple[str, str]:
    """Best-effort location/salary extraction from text surrounding a job link."""
    loc_match = _LOCATION_HINT_RE.search(container_text)
    sal_match = _SALARY_HINT_RE.search(container_text)
    return (loc_match.group(0) if loc_match else None,
            sal_match.group(0) if sal_match else None)


# Career-site chrome that matches our loose "/jobs/" or "/careers/" selectors
# but is never itself a job posting: nav links, account pages, taxonomy/
# category filters, pagination, alerts. Deliberately conservative (exact-ish
# phrase matches only) — an ambiguous title is left in for the pre-score pass
# to judge, since a false negative here (silently dropping a real posting)
# is worse than a false positive (Claude pre-scores a category page low).
_NOISE_TITLE_RE = re.compile(
    r"^(faq|apprentic\w*|early careers?|professional careers?|about( us)?|"
    r"help( ?& ?terms)?|employer login|log ?in|credits?:?\s*\d*|\d+ resumes?|"
    r"\d+ jobs? offered|show more jobs?|create job alert|apply now|"
    r"auswahl aufheben|jobs? finden|jobs? in \w+|work from home or hybrid|"
    r"terms( (of|and) (use|conditions))?|privacy( policy)?|cookie\w*|sitemap|"
    # Common job-board taxonomy/category filter labels (not postings)
    r"technology|marketing|sales|legal|digital|construction|education|property|"
    r"healthcare|human resources|finance ?&? ?accounting|life sciences?|"
    r"executive search|secretarial ?&? ?office support|retail ?&? ?sourcing|"
    r"procurement ?&? ?supply chain|banking ?&? ?financial services|"
    r"engineering ?&? ?manufacturing|general manager|"
    r"china|beijing|shanghai|shenzhen|guangzhou|suzhou|hong ?kong|singapore)$",
    re.IGNORECASE,
)


def _is_noise(title: str, url: str) -> bool:
    t = title.strip()
    if not t:
        return True
    return bool(_NOISE_TITLE_RE.match(t))


async def scrape_listings(url: str) -> list[dict]:
    """
    Phase 1: cheap scrape of the board's listing page only. Returns
    {company, title, url, location, salary_hint} — no jd_text (None).
    """
    company = _company_from_url(url)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)

        seen_urls = set()
        roles = []
        for selector in _JOB_LINK_SELECTORS:
            elements = await page.query_selector_all(selector)
            for el in elements:
                href = await el.get_attribute("href")
                if not href:
                    continue
                job_url = urljoin(url, href)
                if job_url in seen_urls:
                    continue
                seen_urls.add(job_url)

                title = (await el.inner_text()).strip()
                if not title or _is_noise(title, job_url):
                    continue

                # Look at the enclosing row/card for location/salary hints
                container_text = title
                try:
                    parent = await el.evaluate_handle(
                        "el => el.closest('li,tr,article,.job,.opening,.posting') || el.parentElement"
                    )
                    if parent:
                        container_text = await parent.as_element().inner_text()
                except Exception:
                    pass

                location, salary_hint = _nearby_text(title, container_text)

                roles.append({
                    "company": company,
                    "title": title,
                    "url": job_url,
                    "location": location,
                    "salary_hint": salary_hint,
                    "jd_text": None,
                })

        await browser.close()
        return roles


async def scrape_details_batch(urls: list[str]) -> dict[str, dict]:
    """
    Phase 2: visit only the given shortlisted job URLs (one shared browser
    session) and extract full JD text. Returns {url: {title, jd_text}}.
    """
    results = {}
    if not urls:
        return results

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        for job_url in urls:
            try:
                await page.goto(job_url, wait_until="networkidle", timeout=20000)
                await page.wait_for_timeout(1000)

                title = await page.title()
                h1 = await page.query_selector("h1")
                if h1:
                    title = (await h1.inner_text()).strip()

                jd_text = ""
                for sel in _JD_BODY_SELECTORS:
                    el = await page.query_selector(sel)
                    if el:
                        jd_text = _strip_html(await el.inner_html())
                        break

                if not jd_text:
                    jd_text = _strip_html(await page.content())[:5000]

                results[job_url] = {"title": title, "jd_text": jd_text}
            except Exception as e:
                results[job_url] = {"title": None, "jd_text": None, "error": str(e)}

        await browser.close()
        return results


async def scrape(url: str) -> list[dict]:
    """
    Legacy one-shot scrape (listings + full JD for every job). Kept for
    callers that want the old all-at-once behaviour; scan_board no longer
    calls this for generic/workday boards.
    """
    listings = await scrape_listings(url)
    details = await scrape_details_batch([r["url"] for r in listings])
    roles = []
    for r in listings:
        d = details.get(r["url"], {})
        roles.append({
            "company": r["company"],
            "title": d.get("title") or r["title"],
            "url": r["url"],
            "jd_text": d.get("jd_text") or "",
        })
    return roles
