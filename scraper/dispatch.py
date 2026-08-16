from urllib.parse import urlparse

# Board types whose JSON/API returns the full JD in the same call used to
# list jobs — for these there's no separate "listing" vs "detail" cost, so
# the two-phase pipeline collapses into one scrape.
API_BOARD_TYPES = {"greenhouse", "lever", "ashby", "de_talents", "mercedes_benz", "remoteok"}

# Board types reachable entirely over plain HTTP (no browser) but still
# split into a listing phase and a separate detail-page phase.
HTTP_DETAIL_BOARD_TYPES = {"aiib", "tupu360", "gemini_global"}


def detect_board_type(url: str) -> str:
    host = urlparse(url).hostname or ""
    if "greenhouse.io" in host:
        return "greenhouse"
    if "lever.co" in host:
        return "lever"
    if "ashby.com" in host or "ashbyhq.com" in host:
        return "ashby"
    if "aiib.org" in host:
        return "aiib"
    if "de-talents.com" in host:
        return "de_talents"
    if "gemini-global.com" in host:
        return "gemini_global"
    if "mercedes-benz.com" in host:
        return "mercedes_benz"
    if "remoteok.com" in host:
        return "remoteok"
    if "workday.com" in host or "myworkdayjobs.com" in host:
        return "workday"
    return "generic"


async def scrape_board(url: str, board_type: str) -> list[dict]:
    """Full scrape (listing + JD) — used for boards in API_BOARD_TYPES."""
    if board_type == "greenhouse":
        from .greenhouse import scrape as gh_scrape
        return await gh_scrape(url)
    if board_type == "lever":
        from .lever import scrape as lv_scrape
        return await lv_scrape(url)
    if board_type == "ashby":
        from .ashby import scrape as ab_scrape
        return await ab_scrape(url)
    if board_type == "de_talents":
        from .de_talents import scrape as dt_scrape
        return await dt_scrape(url)
    if board_type == "mercedes_benz":
        from .mercedes_benz import scrape as mb_scrape
        return await mb_scrape(url)
    if board_type == "remoteok":
        from .remoteok import scrape as ro_scrape
        return await ro_scrape(url)
    from .generic import scrape as gen_scrape
    return await gen_scrape(url)


async def scrape_board_listings(url: str, board_type: str) -> list[dict]:
    """
    Phase 1: cheap scrape. For API boards this returns full JD too (it's the
    same request, no extra cost). For HTTP_DETAIL boards (plain HTTP, but a
    separate detail page per job) and for generic/workday boards, this
    returns listing metadata only (jd_text=None).

    Also probes plain "generic" boards for a known white-labelled platform
    (currently: tupu360) that's reachable over plain HTTP even though it
    lives on a custom domain we can't detect from the hostname alone. When
    matched, roles are tagged with an internal "_board_type" override so the
    caller stores the more specific type in the database.
    """
    if board_type in API_BOARD_TYPES:
        roles = await scrape_board(url, board_type)
        for r in roles:
            r.setdefault("location", None)
            r.setdefault("salary_hint", None)
        return roles

    if board_type == "aiib":
        from .aiib import scrape_listings as aiib_listings
        return await aiib_listings(url)

    if board_type == "tupu360":
        from .tupu360 import scrape_listings as tupu_listings
        return await tupu_listings(url)

    if board_type == "gemini_global":
        from .gemini_global import scrape_listings as gg_listings
        return await gg_listings(url)

    if board_type == "generic":
        from .tupu360 import probe as tupu_probe, scrape_listings as tupu_listings
        if await tupu_probe(url):
            roles = await tupu_listings(url)
            for r in roles:
                r["_board_type"] = "tupu360"
            return roles

    from .generic import scrape_listings
    return await scrape_listings(url)


async def scrape_role_details(urls: list[str], board_type: str) -> dict[str, dict]:
    """
    Phase 2: fetch full JD for a shortlist of URLs. No-op for API boards
    (their listing scrape already included the JD).
    """
    if board_type in API_BOARD_TYPES:
        return {}
    if board_type == "aiib":
        from .aiib import scrape_details_batch
        return await scrape_details_batch(urls)
    if board_type == "tupu360":
        from .tupu360 import scrape_details_batch
        return await scrape_details_batch(urls)
    if board_type == "gemini_global":
        from .gemini_global import scrape_details_batch as gg_details
        return await gg_details(urls)
    from .generic import scrape_details_batch
    return await scrape_details_batch(urls)
