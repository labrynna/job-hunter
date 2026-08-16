import sqlite3
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta


def _load_config() -> dict:
    import yaml
    config_path = Path(__file__).parent.parent / "config.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def _db_path() -> Path:
    config = _load_config()
    path = Path(__file__).parent.parent / config["database_path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# Columns added after the initial release. Each is applied via ALTER TABLE
# guarded by a duplicate-column check, so this is safe to re-run against an
# existing database created by an older version of this file.
_MIGRATIONS = [
    "ALTER TABLE roles ADD COLUMN location TEXT",
    "ALTER TABLE roles ADD COLUMN salary_hint TEXT",
    "ALTER TABLE roles ADD COLUMN pre_score REAL",
    "ALTER TABLE roles ADD COLUMN pre_score_notes TEXT",
    "ALTER TABLE roles ADD COLUMN pre_scored_at TIMESTAMP",
    "ALTER TABLE roles ADD COLUMN jd_fetched_at TIMESTAMP",
    "ALTER TABLE roles ADD COLUMN first_seen_at TIMESTAMP",
    "ALTER TABLE roles ADD COLUMN posted_at TIMESTAMP",
    "ALTER TABLE roles ADD COLUMN tailored_cover_letter_md TEXT",
    "ALTER TABLE roles ADD COLUMN tailored_cover_letter_pdf TEXT",
]


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS job_boards (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                url     TEXT NOT NULL UNIQUE,
                company TEXT NOT NULL,
                board_type TEXT,
                added_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_scanned TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS roles (
                id              TEXT PRIMARY KEY,
                company         TEXT NOT NULL,
                title           TEXT NOT NULL,
                url             TEXT NOT NULL,
                board_type      TEXT,
                jd_text         TEXT,
                jd_structured   TEXT,
                scraped_at      TIMESTAMP,
                jd_fingerprint  TEXT,
                tfidf_score     REAL,
                ats_score       REAL,
                score_breakdown TEXT,
                missing_skills  TEXT,
                scored_at       TIMESTAMP,
                tailored_cv_md  TEXT,
                tailored_cv_pdf TEXT,
                tailored_cv_docx TEXT,
                cv_generated_at TIMESTAMP,
                status          TEXT DEFAULT 'new',
                applied_at      TIMESTAMP,
                notes           TEXT,
                updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        for stmt in _MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e):
                    raise
        # Backfill first_seen_at for rows inserted before this column existed,
        # so existing data doesn't fail the "couple months later = genuinely
        # new" dedup rule with a NULL first_seen_at.
        conn.execute("UPDATE roles SET first_seen_at = scraped_at WHERE first_seen_at IS NULL")


def role_id(company: str, title: str, url: str) -> str:
    key = f"{company.lower()}|{title.lower()}|{url}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def jd_fingerprint(jd_text: str) -> str:
    return hashlib.md5(jd_text.encode()).hexdigest()


def _normalize(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _duplicate_window_days() -> int:
    return _load_config().get("duplicate_window_days", 60)


def find_recent_duplicate(conn: sqlite3.Connection, company: str, title: str, window_days: int) -> sqlite3.Row | None:
    """
    Look for an existing role at the same company with the same (normalized)
    title, first seen within the last `window_days`. This is the core
    duplicate guard: the same posting often reappears under a different URL
    (re-scraped with a new query-string job ID, listed on a different page of
    results, etc.) — company+title within a short window is almost always the
    same underlying vacancy. If the same company+title reappears after the
    window has passed, treat it as a genuinely new opening instead.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    norm_company = _normalize(company)
    norm_title = _normalize(title)
    rows = conn.execute("""
        SELECT id, url, title, jd_text, first_seen_at FROM roles
        WHERE LOWER(TRIM(company)) = ? AND first_seen_at >= ?
    """, (norm_company, cutoff)).fetchall()
    for r in rows:
        if _normalize(r["title"]) == norm_title:
            return r
    return None


# ── Listing-level ingestion (Phase 1: cheap, no JD) ─────────────────────────

def upsert_role_listing(
    company: str, title: str, url: str, board_type: str,
    location: str = None, salary_hint: str = None, posted_at: str = None,
) -> dict:
    """
    Store a role from a lightweight listing scrape (title/location/salary
    visible on the board page, no full JD yet). Never downgrades a role that
    has already progressed past 'listed' (e.g. don't blow away a scored role
    just because it reappeared in a listing scan). Duplicate-guarded: a role
    at the same company with the same title seen within the configured
    duplicate window is treated as the same vacancy even if the URL differs.
    """
    rid = role_id(company, title, url)
    now = datetime.now(timezone.utc).isoformat()
    window = _duplicate_window_days()

    with get_conn() as conn:
        existing = conn.execute("SELECT id, status FROM roles WHERE id = ?", (rid,)).fetchone()
        if existing is not None:
            conn.execute("""
                UPDATE roles SET location=?, salary_hint=?, updated_at=? WHERE id=?
            """, (location, salary_hint, now, rid))
            return {"action": "unchanged", "id": rid}

        dup = find_recent_duplicate(conn, company, title, window)
        if dup is not None:
            conn.execute("""
                UPDATE roles SET location=COALESCE(?, location), salary_hint=COALESCE(?, salary_hint),
                    updated_at=? WHERE id=?
            """, (location, salary_hint, now, dup["id"]))
            return {"action": "duplicate_skipped", "id": dup["id"]}

        conn.execute("""
            INSERT INTO roles (id, company, title, url, board_type, location, salary_hint,
                scraped_at, first_seen_at, posted_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'listed')
        """, (rid, company, title, url, board_type, location, salary_hint, now, now, posted_at))
        return {"action": "inserted", "id": rid}


# ── Full-JD ingestion (used directly by API-based boards, and by
#    fetch_job_description for generic/Playwright boards after a role
#    is shortlisted from the pre-score pass) ─────────────────────────────────

def upsert_role(company: str, title: str, url: str, jd_text: str, board_type: str, posted_at: str = None) -> dict:
    """
    Insert/update a role that already has its full JD in hand (API-based
    boards). Lands at status='listed' — NOT 'new' — even though the JD is
    already present, so it still passes through the cheap title-only
    pre-score gate before any full-JD ATS-scoring reasoning is spent on it.
    Use promote_ready_roles() after pre-scoring to advance qualifying roles
    to 'new'. Duplicate-guarded the same way as upsert_role_listing.
    """
    rid = role_id(company, title, url)
    fp = jd_fingerprint(jd_text)
    now = datetime.now(timezone.utc).isoformat()
    window = _duplicate_window_days()

    with get_conn() as conn:
        existing = conn.execute("SELECT id, jd_fingerprint, status FROM roles WHERE id = ?", (rid,)).fetchone()

        if existing is None:
            dup = find_recent_duplicate(conn, company, title, window)
            if dup is not None:
                # Same vacancy reappeared under a different URL within the
                # window — refresh its JD if changed, don't create a new row.
                if dup["jd_text"] != jd_text:
                    conn.execute("""
                        UPDATE roles SET jd_text=?, jd_fingerprint=?, jd_fetched_at=?, updated_at=?
                        WHERE id=?
                    """, (jd_text, fp, now, now, dup["id"]))
                return {"action": "duplicate_skipped", "id": dup["id"]}

            conn.execute("""
                INSERT INTO roles (id, company, title, url, board_type, jd_text, jd_fingerprint,
                    scraped_at, first_seen_at, jd_fetched_at, posted_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'listed')
            """, (rid, company, title, url, board_type, jd_text, fp, now, now, now, posted_at))
            return {"action": "inserted", "id": rid}

        if existing["jd_fingerprint"] != fp:
            # JD changed — reset scoring and send back through the pre-score
            # gate rather than assuming the new content still qualifies.
            conn.execute("""
                UPDATE roles SET jd_text=?, jd_fingerprint=?, jd_fetched_at=?, scored_at=NULL,
                    ats_score=NULL, score_breakdown=NULL, missing_skills=NULL,
                    pre_score=NULL, pre_score_notes=NULL, pre_scored_at=NULL,
                    status='listed', updated_at=?
                WHERE id=?
            """, (jd_text, fp, now, now, rid))
            return {"action": "updated_jd", "id": rid}

        return {"action": "unchanged", "id": rid}


def save_jd(role_id: str, jd_text: str, posted_at: str = None) -> dict:
    """
    Attach a full JD fetched from a role's detail page (Phase 2, generic/
    Playwright boards only) to an already-listed role. Moves status to 'new'
    — this path is only reached for roles that already cleared the
    pre-score gate (fetch_job_descriptions is only called on the shortlist).

    Some boards only expose their posting date on the detail page (not the
    listing page) — pass it here if the Phase 2 scraper found one.
    """
    fp = jd_fingerprint(jd_text)
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        existing = conn.execute("SELECT jd_fingerprint FROM roles WHERE id = ?", (role_id,)).fetchone()
        if existing is None:
            return {"action": "error", "error": "role not found"}
        if existing["jd_fingerprint"] == fp:
            if posted_at:
                conn.execute("UPDATE roles SET posted_at=COALESCE(posted_at, ?) WHERE id=?", (posted_at, role_id))
            return {"action": "unchanged", "id": role_id}
        conn.execute("""
            UPDATE roles SET jd_text=?, jd_fingerprint=?, jd_fetched_at=?, scored_at=NULL,
                ats_score=NULL, score_breakdown=NULL, missing_skills=NULL, status='new',
                posted_at=COALESCE(?, posted_at), updated_at=?
            WHERE id=?
        """, (jd_text, fp, now, posted_at, now, role_id))
        return {"action": "jd_fetched", "id": role_id}


# ── Pre-scoring (Phase 1.5: coarse relevance score on listing data only) ────

def save_pre_score(role_id: str, pre_score: float, notes: str = None):
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute("""
            UPDATE roles SET pre_score=?, pre_score_notes=?, pre_scored_at=?, updated_at=?
            WHERE id=?
        """, (pre_score, notes, now, now, role_id))


def promote_ready_roles(threshold: float) -> int:
    """
    For roles that already have a full JD (API-based boards, inserted via
    upsert_role) and have now cleared the pre-score threshold, flip them to
    status='new' so they enter full ATS scoring. No JD fetch needed — they
    already have it. Returns the number of roles promoted.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute("""
            UPDATE roles SET status='new', updated_at=?
            WHERE status='listed' AND jd_text IS NOT NULL AND pre_score >= ?
        """, (now, threshold))
        return cur.rowcount


def filter_stale_roles(max_age_days: int, exempt_location_substring: str) -> int:
    """
    Purely mechanical (no LLM reasoning) recency filter: for 'listed' roles
    that have never been pre-scored, have a known posted_at date older than
    max_age_days, and whose location does NOT contain exempt_location_substring
    (e.g. "Beijing"), set pre_score=0 directly so they never cross the
    pre-score threshold and are never scored/tailored. Roles with no
    posted_at on record are left untouched (we can't judge their age).
    Returns the number of roles filtered.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute("""
            UPDATE roles SET pre_score=0, pre_score_notes='Filtered: posted over ' || ? ||
                ' days ago and not in ' || ?, pre_scored_at=?, updated_at=?
            WHERE status='listed' AND pre_score IS NULL AND posted_at IS NOT NULL
                AND posted_at < ? AND (location IS NULL OR location NOT LIKE ?)
        """, (max_age_days, exempt_location_substring, now, now, cutoff, f"%{exempt_location_substring}%"))
        return cur.rowcount


def dedupe_existing_roles(window_days: int) -> dict:
    """
    One-time/ad-hoc cleanup: scan the whole table for (company, normalized
    title) groups whose members were first seen within window_days of each
    other, and collapse each group down to a single row. Keeps the row with
    the richest jd_text (longest), or the earliest first_seen_at if tied.
    Roles progressed further (scored/cv_ready/applied) are preferred as the
    keeper over ones still at 'listed', so we don't throw away scoring work.
    Returns {"groups_collapsed": n, "rows_removed": n}.
    """
    _STATUS_RANK = {"listed": 0, "new": 1, "scored": 2, "cv_ready": 3,
                     "applied": 4, "interviewing": 5, "offer": 6, "rejected": 4}

    with get_conn() as conn:
        rows = conn.execute("SELECT id, company, title, jd_text, first_seen_at, status FROM roles").fetchall()

        groups: dict[tuple, list] = {}
        for r in rows:
            key = (_normalize(r["company"]), _normalize(r["title"]))
            groups.setdefault(key, []).append(dict(r))

        groups_collapsed = 0
        rows_removed = 0
        for key, members in groups.items():
            if len(members) < 2:
                continue

            members.sort(key=lambda m: m["first_seen_at"] or "")
            clusters: list[list[dict]] = []
            for m in members:
                placed = False
                for cluster in clusters:
                    last = cluster[-1]
                    t1 = datetime.fromisoformat(last["first_seen_at"])
                    t2 = datetime.fromisoformat(m["first_seen_at"])
                    if abs((t2 - t1).days) <= window_days:
                        cluster.append(m)
                        placed = True
                        break
                if not placed:
                    clusters.append([m])

            for cluster in clusters:
                if len(cluster) < 2:
                    continue
                cluster.sort(key=lambda m: (
                    -_STATUS_RANK.get(m["status"], 0),
                    -(len(m["jd_text"]) if m["jd_text"] else 0),
                ))
                keeper = cluster[0]
                losers = cluster[1:]
                for loser in losers:
                    conn.execute("DELETE FROM roles WHERE id = ?", (loser["id"],))
                    rows_removed += 1
                groups_collapsed += 1

        return {"groups_collapsed": groups_collapsed, "rows_removed": rows_removed}


def get_role(role_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM roles WHERE id = ?", (role_id,)).fetchone()
        return dict(row) if row else None


def get_roles(status: str = None, min_score: float = None, min_pre_score: float = None,
              needs_pre_score: bool = False) -> list[dict]:
    with get_conn() as conn:
        query = """
            SELECT id, company, title, url, location, salary_hint, status,
                   pre_score, ats_score, tfidf_score, posted_at, first_seen_at,
                   scored_at, cv_generated_at, applied_at
            FROM roles WHERE 1=1
        """
        params = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if min_score is not None:
            query += " AND ats_score >= ?"
            params.append(min_score)
        if min_pre_score is not None:
            query += " AND pre_score >= ?"
            params.append(min_pre_score)
        if needs_pre_score:
            query += " AND pre_score IS NULL"
        query += " ORDER BY ats_score DESC NULLS LAST, pre_score DESC NULLS LAST, scraped_at DESC"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def save_score(role_id: str, ats_score: float, score_breakdown: dict, missing_skills: list, tfidf_score: float = None):
    now = datetime.now(timezone.utc).isoformat()
    new_status = "scored"
    with get_conn() as conn:
        conn.execute("""
            UPDATE roles SET ats_score=?, score_breakdown=?, missing_skills=?, scored_at=?,
                tfidf_score=?, status=?, updated_at=?
            WHERE id=?
        """, (ats_score, json.dumps(score_breakdown), json.dumps(missing_skills),
              now, tfidf_score, new_status, now, role_id))


def save_tailored_cv(role_id: str, cv_markdown: str, cv_pdf_path: str,
                      cover_letter_markdown: str, cover_letter_pdf_path: str):
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute("""
            UPDATE roles SET tailored_cv_md=?, tailored_cv_pdf=?,
                tailored_cover_letter_md=?, tailored_cover_letter_pdf=?,
                cv_generated_at=?, status='cv_ready', updated_at=?
            WHERE id=?
        """, (cv_markdown, cv_pdf_path, cover_letter_markdown, cover_letter_pdf_path,
              now, now, role_id))


def update_status(role_id: str, status: str, notes: str = None):
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        if status == "applied":
            conn.execute("""
                UPDATE roles SET status=?, notes=?, applied_at=?, updated_at=? WHERE id=?
            """, (status, notes, now, now, role_id))
        else:
            conn.execute("""
                UPDATE roles SET status=?, notes=?, updated_at=? WHERE id=?
            """, (status, notes, now, role_id))


def delete_role(role_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM roles WHERE id = ?", (role_id,))


def get_pipeline_summary() -> dict:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT status, COUNT(*) as count FROM roles GROUP BY status
        """).fetchall()
        return {r["status"]: r["count"] for r in rows}


def add_board(url: str, company: str, board_type: str):
    with get_conn() as conn:
        try:
            conn.execute("""
                INSERT INTO job_boards (url, company, board_type) VALUES (?, ?, ?)
            """, (url, company, board_type))
            return True
        except sqlite3.IntegrityError:
            return False  # already exists


def list_boards() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM job_boards ORDER BY company").fetchall()
        return [dict(r) for r in rows]


def remove_board(url: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM job_boards WHERE url = ?", (url,))


def touch_board_scanned(url: str):
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute("UPDATE job_boards SET last_scanned=? WHERE url=?", (now, url))
