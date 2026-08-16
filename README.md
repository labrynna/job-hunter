# Job Hunter

A local-first job-search automation tool for Claude Code. It scrapes company career pages, scores each opening against your CV, writes a tailored CV + cover letter for the roles worth applying to, and tracks the whole pipeline in a local database — all as an MCP server that Claude Code drives directly, with the AI reasoning (scoring, tailoring, judgment calls) done by Claude itself rather than hardcoded logic.

Nothing here talks to a cloud service you don't already use. Your CV, your scraped roles, and your generated applications all stay in a local SQLite database and local files on your machine.

## What it does

- **Scans company career pages** for open roles — dedicated adapters for Greenhouse, Lever, Ashby, and RemoteOK's public APIs (no browser needed, fast), plus a Playwright-based fallback scraper for any other career site, and a couple of worked-example adapters for specific platforms (see `scraper/`) showing how to build one for a company whose site needs bespoke handling.
- **Two-phase scoring** to keep it cheap: a fast title/company-only pre-score filters out obviously irrelevant roles before spending any real reasoning on them; only roles that clear that bar get their full job description fetched and scored on a 5-dimension ATS model (required skills, seniority, experience, education, preferred skills) against your actual CV.
- **Location- and remote-aware filtering**, configurable in `filters.yaml` — different seniority bars for your home city vs. elsewhere, a separate evaluation axis for genuinely remote roles, and a mechanism to encode insider hiring knowledge for specific priority employers (e.g. "this employer's Officer band is my realistic sweet spot, don't score down for X gap, do apply even to imperfect-fit roles").
- **Tailored CV + cover letter generation** as two separate, cleanly-formatted PDFs per application — reordering and reframing your actual master CV content for each role's specific requirements, never inventing experience you don't have.
- **Duplicate detection** at the database level (same company + title within a configurable window counts as the same vacancy, even under a different URL), and mechanical (zero-token) staleness filtering for old postings.
- **Application tracking** — every role's status (listed → scored → cv_ready → applied → interviewing → ...) lives in the database, queryable at any time.
- **Optional daily automation**: a scheduled-task template (`scheduled-task-template/SKILL.md.example`) you can adapt into your own Claude Code scheduled task for a fully autonomous morning scan, plus a pattern for two levels of "apply for me": drafting (never sending) an emailed application, or walking through a company portal live with you confirming before final submission.

## What it deliberately does NOT do

- It never submits an application, sends an email, or creates an account on your behalf without you confirming that specific action in the moment.
- It never invents skills, employers, or experience not present in your actual CV.
- It has no built-in credential storage — logging into an application portal is something you do yourself (or approve explicitly in an interactive session), not something baked into the automation.

## Getting started

See [SETUP.md](SETUP.md) for the full walkthrough. Short version: install the Python dependencies, drop your own CV in as `master_cv.docx`, register the MCP server with Claude Code, add your target companies to `job_boards.txt`, and start talking to Claude Code.

## Project layout

```
job-hunter/
├── mcp_server.py              ← MCP server entrypoint — all the tools Claude Code calls
├── config.yaml                ← settings (CV path, score thresholds, your home city)
├── filters.yaml                ← your career-fit criteria (location rules, employer-specific insider guidance)
├── job_boards.txt             ← your target companies, one URL per line
├── master_cv.docx             ← YOUR master CV (you provide this — not included)
├── requirements.txt
├── SETUP.md
├── data/                      ← SQLite database (auto-created, gitignored)
├── output/cvs/                ← generated tailored CVs + cover letters (gitignored)
├── reports/                   ← daily scan reports if you set up automation (gitignored)
├── db/                        ← database layer (schema, queries)
├── scraper/                   ← board-specific adapters + the generic Playwright fallback
├── parser/                    ← reads your master CV from DOCX
├── cv_engine/                 ← renders tailored Markdown → polished PDF
└── scheduled-task-template/   ← adapt this into your own daily-scan automation
```

## How the pieces fit together

Claude Code is the brain — it calls MCP tools to scrape, fetch, and persist data, but every judgment call (does this role fit, what score does it deserve, how should the CV be reframed, is this the right email address to use) is reasoning Claude does itself against your master CV and your `filters.yaml` criteria, not logic baked into the server. That's a deliberate design choice: the server's job is mechanical (scrape, store, render), and yours — or Claude's, on your behalf — is judgment.
