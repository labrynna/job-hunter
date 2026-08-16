"""
Job Hunter MCP Server
---------------------
Exposes job board management, scraping, CV parsing, scoring storage,
CV rendering, and application tracking as MCP tools for Claude Code.

Claude Code's built-in Claude handles all AI reasoning (scoring, tailoring).
This server handles all data, I/O, and file operations.

Run with: python mcp_server.py
"""
import json
import re
import sys
from pathlib import Path

import yaml
from fastmcp import FastMCP

# Ensure project root is on path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from db.models import (
    init_db, add_board, list_boards, remove_board, touch_board_scanned,
    upsert_role, upsert_role_listing, save_jd, save_pre_score, delete_role,
    get_role, get_roles, save_score, save_tailored_cv,
    update_status as db_update_status, get_pipeline_summary,
    promote_ready_roles, filter_stale_roles, dedupe_existing_roles,
)
from scraper.dispatch import (
    detect_board_type, scrape_board_listings, scrape_role_details, API_BOARD_TYPES,
)
from scraper.generic import _is_noise
from parser.cv_parser import parse_master_cv
from cv_engine.renderer import render_cv, render_cover_letter


def _load_config() -> dict:
    with open(ROOT / "config.yaml") as f:
        return yaml.safe_load(f)


# Initialise DB on startup
init_db()

mcp = FastMCP("job-hunter", instructions="""
Job Hunter MCP server. Handles job board scraping, database persistence,
CV parsing, output rendering, and application tracking.
All AI reasoning (scoring, tailoring) is performed by you (Claude), not this server.
""")


# ── Board Management ─────────────────────────────────────────────────────────

@mcp.tool()
def add_job_board(url: str, company: str) -> dict:
    """
    Add a company job board URL to the tracking list.
    The board type (greenhouse/lever/ashby/workday/generic) is auto-detected.

    Args:
        url: Full URL of the job board (e.g. https://job-boards.greenhouse.io/anthropic)
        company: Human-readable company name (e.g. "Anthropic")
    """
    board_type = detect_board_type(url)
    added = add_board(url.strip(), company.strip(), board_type)
    if added:
        # Also append to job_boards.txt for easy human editing
        boards_file = ROOT / "job_boards.txt"
        with open(boards_file, "a", encoding="utf-8") as f:
            f.write(f"\n{url.strip()} {company.strip()}")
        return {"status": "added", "url": url, "company": company, "board_type": board_type}
    return {"status": "already_exists", "url": url}


@mcp.tool()
def list_job_boards() -> list[dict]:
    """
    List all tracked job board URLs with their company name, board type,
    and when they were last scanned.
    """
    return list_boards()


@mcp.tool()
def remove_job_board(url: str) -> dict:
    """
    Remove a job board URL from the tracking list.
    Does not delete already-scraped roles from the database.

    Args:
        url: The board URL to remove
    """
    remove_board(url)
    return {"status": "removed", "url": url}


@mcp.tool()
def import_job_boards_from_file() -> dict:
    """
    Import/sync job boards from job_boards.txt into the database.
    Lines starting with # or blank lines are ignored.
    Format: URL [Company Name]
    Use this after manually editing job_boards.txt.
    """
    boards_file = ROOT / "job_boards.txt"
    added, skipped = 0, 0
    for line in boards_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        url = parts[0]
        company = parts[1] if len(parts) > 1 else detect_board_type(url).title()
        board_type = detect_board_type(url)
        if add_board(url, company, board_type):
            added += 1
        else:
            skipped += 1
    return {"added": added, "already_existed": skipped}


# ── Scanning ─────────────────────────────────────────────────────────────────

@mcp.tool()
async def scan_board(url: str) -> dict:
    """
    Scrape a job board's listing page and store new/changed roles.

    For API-based boards (Greenhouse/Lever/Ashby/DE-Talents/Mercedes-Benz)
    the JD is included for free in the same request — but roles still land
    at status='listed', NOT 'new', so they pass through the cheap
    title-only pre-score gate before any full-JD ATS-scoring reasoning is
    spent on them. Call promote_pre_scored_roles after pre-scoring to
    advance qualifying ones (their JD is already in hand, no fetch needed).

    For everything else (generic/Playwright, incl. Workday), only cheap
    listing data is captured (title, url, location, salary hint if visible)
    and roles land with status 'listed' — no full page visit per job yet.
    Use pre-score + fetch_job_descriptions on the shortlist before spending
    a Playwright page load on every single posting.

    Roles are duplicate-guarded at the database layer: the same company +
    title reappearing within the configured duplicate_window_days (see
    get_config) is treated as the same vacancy even under a different URL,
    and won't create a second row.

    Args:
        url: Job board URL to scrape
    """
    board_type = detect_board_type(url)
    try:
        roles = await scrape_board_listings(url, board_type)
    except Exception as e:
        return {"status": "error", "url": url, "error": str(e)}

    results = {"inserted": 0, "updated_jd": 0, "unchanged": 0, "duplicate_skipped": 0}
    for role in roles:
        # A "generic" board may turn out to be a recognised platform on a
        # custom domain (e.g. tupu360 white-label) — store the more specific
        # type so Phase 2 (fetch_job_descriptions) routes correctly.
        effective_board_type = role.get("_board_type", board_type)
        if role.get("jd_text"):
            outcome = upsert_role(
                company=role["company"], title=role["title"], url=role["url"],
                jd_text=role["jd_text"], board_type=effective_board_type,
                posted_at=role.get("posted_at"),
            )
        else:
            outcome = upsert_role_listing(
                company=role["company"], title=role["title"], url=role["url"],
                board_type=effective_board_type, location=role.get("location"),
                salary_hint=role.get("salary_hint"), posted_at=role.get("posted_at"),
            )
        results[outcome["action"]] = results.get(outcome["action"], 0) + 1

    touch_board_scanned(url)
    return {
        "status": "ok", "url": url, "board_type": board_type, "roles_found": len(roles),
        "mode": "full_jd" if board_type in API_BOARD_TYPES else "listing_only",
        **results,
    }


@mcp.tool()
async def scan_all_boards() -> list[dict]:
    """
    Scrape all tracked job boards and store results. Returns a summary per board.
    Use this for a full refresh of all roles.
    """
    boards = list_boards()
    if not boards:
        return [{"status": "no_boards", "message": "No job boards tracked yet. Use add_job_board first."}]
    results = []
    for board in boards:
        result = await scan_board(board["url"])
        result["company"] = board["company"]
        results.append(result)
    return results


# ── Role Queries ──────────────────────────────────────────────────────────────

@mcp.tool()
def list_roles(status: str = None, min_score: float = None, min_pre_score: float = None,
                needs_pre_score: bool = False) -> list[dict]:
    """
    List roles from the database, optionally filtered.

    Args:
        status: Filter by status. One of: listed, new, scored, cv_ready, applied, interviewing, rejected, offer
            ('listed' = title/url known but no full JD yet; 'new' = full JD present, not yet scored)
        min_score: Only return roles with full ATS score >= this value (0-100)
        min_pre_score: Only return roles with pre-score (listing-level relevance) >= this value (0-100).
            Use this to build the shortlist before calling fetch_job_descriptions.
        needs_pre_score: If True, only return status='listed' roles that have never been
            pre-scored (pre_score IS NULL). Use this on a recurring run so already-triaged
            roles from previous runs aren't re-processed — combine with status='listed'.
    """
    return get_roles(status=status, min_score=min_score, min_pre_score=min_pre_score,
                      needs_pre_score=needs_pre_score)


@mcp.tool()
def get_role_detail(role_id: str) -> dict:
    """
    Get full details for a single role including the complete job description,
    current score, score breakdown, missing skills, and tailored CV if generated.

    Args:
        role_id: The 16-character role ID (from list_roles)
    """
    role = get_role(role_id)
    if not role:
        return {"error": f"Role {role_id} not found"}
    # Parse JSON fields back to objects for readability
    for field in ("score_breakdown", "missing_skills", "jd_structured"):
        if role.get(field):
            try:
                role[field] = json.loads(role[field])
            except Exception:
                pass
    return role


# ── Master CV ─────────────────────────────────────────────────────────────────

@mcp.tool()
def get_master_cv() -> dict:
    """
    Read and return the master CV as plain text, parsed from the DOCX file
    specified in config.yaml. Use this to get the CV content for scoring
    or tailoring tasks.
    """
    config = _load_config()
    cv_path = ROOT / config["master_cv_path"]
    try:
        text = parse_master_cv(str(cv_path))
        return {"status": "ok", "cv_text": text, "path": str(cv_path)}
    except FileNotFoundError:
        return {
            "status": "error",
            "error": f"Master CV not found at {cv_path}. "
                     f"Place your CV file there and update config.yaml if the filename differs."
        }


# ── Pre-Scoring & JD Fetch (Phase 1.5 / Phase 2) ────────────────────────────

@mcp.tool()
def prune_listing_noise() -> dict:
    """
    Re-check all 'listed' roles (from generic/Playwright boards) against the
    current noise filter (nav links, taxonomy/category pages, pagination,
    account pages that slipped through because they matched a job-link
    selector) and delete the ones that are clearly not real postings.
    Never touches roles with status beyond 'listed' (i.e. anything already
    JD-fetched, scored, or further along is left alone).
    Safe to re-run after tightening the filter in scraper/generic.py.
    """
    listed = get_roles(status="listed")
    removed = 0
    for r in listed:
        if _is_noise(r["title"], r["url"]):
            delete_role(r["id"])
            removed += 1
    return {"checked": len(listed), "removed": removed, "kept": len(listed) - removed}


@mcp.tool()
def store_pre_score(role_id: str, pre_score: float, notes: str = None) -> dict:
    """
    Persist a coarse relevance score for a 'listed' role, computed from
    listing-level data only (title, company, location, salary hint — no JD
    yet). Use this to build a shortlist before spending a Playwright page
    load fetching each role's full job description.

    Suggested dimensions for this score: title/domain relevance to the
    master CV (main driver), location/remote fit, and any seniority signal
    visible in the title (e.g. "Senior", "Director"). This is intentionally
    coarser than the full ATS score — it's a triage filter, not a final verdict.

    Args:
        role_id: The role to pre-score
        pre_score: Coarse relevance score 0-100
        notes: Optional short rationale (e.g. "title matches target domain, location unclear")
    """
    save_pre_score(role_id, pre_score, notes)
    return {"status": "saved", "role_id": role_id, "pre_score": pre_score}


@mcp.tool()
def store_pre_scores(scores: list[dict]) -> dict:
    """
    Bulk version of store_pre_score — call this once with all pre-scores
    rather than one role at a time, to avoid a round trip per role.

    Args:
        scores: List of {"role_id": str, "pre_score": float, "notes": str (optional)}
    """
    for item in scores:
        save_pre_score(item["role_id"], item["pre_score"], item.get("notes"))
    return {"status": "saved", "count": len(scores)}


@mcp.tool()
async def fetch_job_descriptions(role_ids: list[str]) -> dict:
    """
    For a shortlist of 'listed' roles (i.e. after pre-scoring), fetch the
    full job description from each role's detail page and store it. Roles
    move to status 'new', ready for full ATS scoring.

    Batches roles by board type and shares one browser session per batch —
    call this once with the full shortlist rather than one role at a time.

    No-op for roles whose board type already includes the full JD from the
    listing scan (Greenhouse/Lever/Ashby) — those are already at status 'new'.

    Args:
        role_ids: List of role IDs to fetch full JDs for (from list_roles with status='listed')
    """
    roles = [get_role(rid) for rid in role_ids]
    roles = [r for r in roles if r]

    by_board_type: dict[str, list[dict]] = {}
    for r in roles:
        by_board_type.setdefault(r["board_type"], []).append(r)

    results = {"jd_fetched": 0, "unchanged": 0, "already_had_jd": 0, "error": 0}
    failed = []  # [{"role_id", "company", "title", "url"}] — needs manual JD paste
    for board_type, group in by_board_type.items():
        if board_type in API_BOARD_TYPES:
            results["already_had_jd"] += len(group)
            continue

        urls = [r["url"] for r in group]
        try:
            details = await scrape_role_details(urls, board_type)
        except Exception:
            results["error"] += len(group)
            failed.extend({"role_id": r["id"], "company": r["company"], "title": r["title"], "url": r["url"]} for r in group)
            continue

        for r in group:
            detail = details.get(r["url"], {})
            jd_text = detail.get("jd_text")
            if not jd_text:
                results["error"] += 1
                failed.append({"role_id": r["id"], "company": r["company"], "title": r["title"], "url": r["url"]})
                continue
            outcome = save_jd(r["id"], jd_text, posted_at=detail.get("posted_at"))
            results[outcome["action"]] = results.get(outcome["action"], 0) + 1

    return {"status": "ok", "roles_requested": len(role_ids), "failed_roles": failed, **results}


# ── Manual Entry (fallback when a board fails to scan / a JD fails to fetch) ──

@mcp.tool()
def add_manual_roles(roles: list[dict]) -> dict:
    """
    Insert roles by hand — use this when a board failed an automated scan and
    the user pasted what they see on the page instead (title/company/location/
    link, and optionally the full JD text if they pasted that too).

    Args:
        roles: List of dicts, each with:
            company (str, required), title (str, required), url (str, required),
            location (str, optional), salary_hint (str, optional),
            posted_at (str, optional, ISO date) — the board's own posting date if known,
            jd_text (str, optional) — if provided, the role still lands at
            status='listed' pending pre-score (its JD is already attached, so
            promote_pre_scored_roles will advance it with no further fetch needed).
    """
    inserted, updated, jd_added = 0, 0, 0
    for r in roles:
        board_type = "manual"
        if r.get("jd_text"):
            outcome = upsert_role(
                company=r["company"], title=r["title"], url=r["url"],
                jd_text=r["jd_text"], board_type=board_type, posted_at=r.get("posted_at"),
            )
            jd_added += 1
        else:
            outcome = upsert_role_listing(
                company=r["company"], title=r["title"], url=r["url"],
                board_type=board_type, location=r.get("location"),
                salary_hint=r.get("salary_hint"), posted_at=r.get("posted_at"),
            )
        if outcome["action"] == "inserted":
            inserted += 1
        else:
            updated += 1
    return {"status": "ok", "inserted": inserted, "updated": updated, "with_jd": jd_added}


@mcp.tool()
def dedupe_roles(window_days: int = None) -> dict:
    """
    Scan the whole database for roles at the same company with the same
    (normalized) title, first seen within window_days of each other, and
    collapse each such group down to a single row (keeping the one that's
    progressed furthest through the pipeline, or the one with the richest
    JD if tied). This is the same duplicate window used automatically on
    every new scan — run this manually to clean up any duplicates that
    entered before the guard existed, or after changing duplicate_window_days.

    Args:
        window_days: Override the configured duplicate_window_days (default from config.yaml)
    """
    if window_days is None:
        window_days = _load_config().get("duplicate_window_days", 60)
    return dedupe_existing_roles(window_days)


@mcp.tool()
def filter_stale_listings(max_age_days: int = None, exempt_location: str = None) -> dict:
    """
    Mechanically (no LLM reasoning, so no token cost) filter out 'listed'
    roles that have a known posted_at date older than max_age_days and
    whose location doesn't contain exempt_location — sets their pre_score
    to 0 directly so they're permanently excluded from JD-fetch/scoring.
    Roles with no posted_at on record are left untouched (age unknown).
    Run this right after scanning, before pre-scoring, to avoid spending
    any pre-score reasoning on postings too old to bother with.

    Args:
        max_age_days: Override config's max_posting_age_days (default 14)
        exempt_location: Override config's recency_exempt_location (default "Beijing") —
            roles whose location contains this substring are never filtered by age
    """
    config = _load_config()
    if max_age_days is None:
        max_age_days = config.get("max_posting_age_days", 14)
    if exempt_location is None:
        exempt_location = config.get("recency_exempt_location", "Beijing")
    filtered = filter_stale_roles(max_age_days, exempt_location)
    return {"status": "ok", "filtered": filtered, "max_age_days": max_age_days, "exempt_location": exempt_location}


@mcp.tool()
def promote_pre_scored_roles(threshold: float = None) -> dict:
    """
    For roles that already have a full JD attached (API-based boards) and
    have cleared the pre-score threshold, flip them from status='listed' to
    status='new' so they enter full ATS scoring — no JD fetch needed, they
    already have it. Call this right after store_pre_scores/store_pre_score
    for any batch that included full-JD roles.

    Args:
        threshold: Override config's pre_score_threshold (default 50)
    """
    if threshold is None:
        threshold = _load_config().get("pre_score_threshold", 50)
    promoted = promote_ready_roles(threshold)
    return {"status": "ok", "promoted": promoted, "threshold": threshold}


@mcp.tool()
def get_scoring_criteria() -> dict:
    """
    Return the career-fit filter criteria from filters.yaml — location-based
    seniority rules to apply during pre-scoring and full ATS scoring (e.g.
    roles outside Beijing must be senior/career-path-fit; Beijing roles can
    be more flexible but not junior). Read this before pre-scoring or
    scoring any batch of roles.
    """
    criteria_path = ROOT / "filters.yaml"
    if not criteria_path.exists():
        return {"status": "error", "error": f"filters.yaml not found at {criteria_path}"}
    with open(criteria_path, encoding="utf-8") as f:
        return {"status": "ok", "criteria": yaml.safe_load(f)}


@mcp.tool()
def add_manual_jd(role_id: str, jd_text: str) -> dict:
    """
    Attach a full job description that the user pasted by hand, for a role
    whose automated JD fetch failed (see fetch_job_descriptions' failed_roles
    list). Moves the role to status='new', ready for full scoring.

    Args:
        role_id: The role to attach the JD to
        jd_text: The full job description text
    """
    outcome = save_jd(role_id, jd_text)
    return {"status": "ok", "role_id": role_id, "action": outcome["action"]}


# ── Scoring ───────────────────────────────────────────────────────────────────

@mcp.tool()
def store_score(
    role_id: str,
    ats_score: float,
    score_breakdown: dict,
    missing_skills: list[str],
    tfidf_score: float = None,
) -> dict:
    """
    Persist an ATS fit score for a role after Claude has computed it.

    Args:
        role_id: The role to score
        ats_score: Overall score 0-100
        score_breakdown: Dict with per-dimension scores, e.g.
            {"required_skills": 32, "seniority": 22, "experience": 20, "education": 10, "preferred_skills": 4}
        missing_skills: List of required skills from the JD not found in the master CV
        tfidf_score: Optional TF-IDF pre-filter score (0.0-1.0)
    """
    save_score(role_id, ats_score, score_breakdown, missing_skills, tfidf_score)
    return {"status": "saved", "role_id": role_id, "ats_score": ats_score}


@mcp.tool()
def store_scores(scores: list[dict]) -> dict:
    """
    Bulk version of store_score — call this once with all full ATS scores
    rather than one role at a time, to avoid a round trip per role.

    Args:
        scores: List of {"role_id": str, "ats_score": float, "score_breakdown": dict,
            "missing_skills": list[str], "tfidf_score": float (optional)}
    """
    for item in scores:
        save_score(
            item["role_id"], item["ats_score"], item["score_breakdown"],
            item["missing_skills"], item.get("tfidf_score"),
        )
    return {"status": "saved", "count": len(scores)}


@mcp.tool()
def get_role_details_bulk(role_ids: list[str]) -> list[dict]:
    """
    Get full JD text and current scores for multiple roles in one call —
    use this instead of calling get_role_detail once per role, whether you're
    scoring a batch of roles or building a report from already-scored ones.

    Args:
        role_ids: List of role IDs
    """
    results = []
    for rid in role_ids:
        role = get_role(rid)
        if not role:
            continue
        missing_skills = role.get("missing_skills")
        score_breakdown = role.get("score_breakdown")
        try:
            missing_skills = json.loads(missing_skills) if missing_skills else None
        except Exception:
            pass
        try:
            score_breakdown = json.loads(score_breakdown) if score_breakdown else None
        except Exception:
            pass
        results.append({
            "id": role["id"], "company": role["company"], "title": role["title"],
            "jd_text": role["jd_text"], "location": role.get("location"),
            "status": role.get("status"), "pre_score": role.get("pre_score"),
            "ats_score": role.get("ats_score"), "score_breakdown": score_breakdown,
            "missing_skills": missing_skills, "url": role.get("url"),
        })
    return results


# ── CV Tailoring ──────────────────────────────────────────────────────────────

@mcp.tool()
async def store_tailored_cv(role_id: str, cv_markdown: str, cover_letter_markdown: str) -> dict:
    """
    Save a tailored CV and a separate cover letter (each as markdown) and
    render each to its own PDF. The files are saved to output/cvs/ and the
    paths are stored in the database. Always two separate documents — never
    combine the CV and cover letter into one file.

    Args:
        role_id: The role this application was tailored for
        cv_markdown: The complete tailored CV in Markdown format (resume content only)
        cover_letter_markdown: The complete cover letter in Markdown format (letter content only)
    """
    role = get_role(role_id)
    if not role:
        return {"error": f"Role {role_id} not found"}

    config = _load_config()
    output_dir = ROOT / config["output_dir"]

    # Clean filename: CompanyName_RoleTitle_ID
    company_slug = re.sub(r"[^\w]", "_", role["company"])
    title_slug = re.sub(r"[^\w]", "_", role["title"])[:40]
    filename_stem = f"{company_slug}_{title_slug}_{role_id}"

    try:
        cv_pdf_path = await render_cv(cv_markdown, str(output_dir), filename_stem)
        cover_letter_pdf_path = await render_cover_letter(cover_letter_markdown, str(output_dir), filename_stem)
        save_tailored_cv(role_id, cv_markdown, cv_pdf_path, cover_letter_markdown, cover_letter_pdf_path)
        return {
            "status": "ok",
            "role_id": role_id,
            "cv_pdf": cv_pdf_path,
            "cover_letter_pdf": cover_letter_pdf_path,
        }
    except Exception as e:
        return {"status": "error", "role_id": role_id, "error": str(e)}


# ── Application Tracker ───────────────────────────────────────────────────────

@mcp.tool()
def update_application_status(role_id: str, status: str, notes: str = None) -> dict:
    """
    Update the application status for a role.

    Args:
        role_id: The role to update
        status: One of: listed | new | scored | cv_ready | applied | interviewing | rejected | offer | closed
        notes: Optional free-text notes (e.g. "Phone screen with Sarah on Friday", or "posting closed before JD could be fetched")
    """
    valid = {"listed", "new", "scored", "cv_ready", "applied", "interviewing", "rejected", "offer", "closed"}
    if status not in valid:
        return {"error": f"Invalid status '{status}'. Must be one of: {', '.join(sorted(valid))}"}
    db_update_status(role_id, status, notes)
    return {"status": "updated", "role_id": role_id, "new_status": status}


@mcp.tool()
def get_pipeline_overview() -> dict:
    """
    Get a summary of all roles by status — how many are at each stage of
    the application pipeline.
    """
    summary = get_pipeline_summary()
    total = sum(summary.values())
    return {"total_roles": total, "by_status": summary}


# ── Config / Utilities ────────────────────────────────────────────────────────

@mcp.tool()
def get_config() -> dict:
    """Return current configuration from config.yaml."""
    return _load_config()


@mcp.tool()
def set_score_threshold(threshold: float) -> dict:
    """
    Update the minimum ATS score required to generate a tailored CV.

    Args:
        threshold: Score 0-100 (default 70)
    """
    config = _load_config()
    config["score_threshold"] = threshold
    with open(ROOT / "config.yaml", "w") as f:
        yaml.dump(config, f)
    return {"status": "updated", "score_threshold": threshold}


@mcp.tool()
def set_pre_score_threshold(threshold: float) -> dict:
    """
    Update the minimum pre-score (listing-level relevance) required before
    fetching a role's full job description via fetch_job_descriptions.

    Args:
        threshold: Score 0-100 (default 50)
    """
    config = _load_config()
    config["pre_score_threshold"] = threshold
    with open(ROOT / "config.yaml", "w") as f:
        yaml.dump(config, f)
    return {"status": "updated", "pre_score_threshold": threshold}


@mcp.tool()
def set_master_cv_path(path: str) -> dict:
    """
    Update the path to the master CV DOCX file in config.yaml.

    Args:
        path: Relative path from project root, e.g. "master_cv.docx" or "docs/my_cv.docx"
    """
    config = _load_config()
    config["master_cv_path"] = path
    with open(ROOT / "config.yaml", "w") as f:
        yaml.dump(config, f)
    return {"status": "updated", "master_cv_path": path}


if __name__ == "__main__":
    mcp.run()
