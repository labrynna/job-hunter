# Job Hunter — Setup Guide

## 1. Install Python dependencies

```bash
cd /path/to/your/job-hunter-clone
pip install -r requirements.txt
playwright install chromium
```

## 2. Place your master CV

Copy your master CV as a `.docx` file to the project root and name it `master_cv.docx`.
Or place it anywhere and update `config.yaml`:

```yaml
master_cv_path: "master_cv.docx"   # relative to project root
```

## 3. Register the MCP server with Claude Code

Copy `.mcp.json.example` to `.mcp.json` and fill in the absolute path to your clone:

```json
{
  "mcpServers": {
    "job-hunter": {
      "command": "python",
      "args": ["/absolute/path/to/your/job-hunter-clone/mcp_server.py"],
      "type": "stdio"
    }
  }
}
```

Place this either in the project's `.claude/settings.json` (project-scoped) or wherever you normally register Claude Code MCP servers.

## 4. Set your own career-fit criteria

Open `filters.yaml` and replace the placeholder content:

- `location_rules` / `posting_recency`: set `YourCity` to your actual home base in both places.
- `employer_exemptions`: the `acme_infrastructure_bank` block is a worked example — replace it with your own priority employer(s), or delete it entirely if you don't want any standing overrides yet. Keep the structure (seniority-band guidance, domain-transfer guidance, education guidance, department affinity, score-threshold override) — it's what lets Claude apply real insider hiring knowledge you've picked up (from a contact, research, an informational interview) instead of falling back to generic keyword matching.
- `cover_letter_guidance`: the `general_principles` are broadly reusable as-is; add employer-specific addenda the same way the example does.

## 5. Add your job boards

**Option A — Edit the file directly**

Open `job_boards.txt` and replace the example URLs with your own target companies:
```
https://job-boards.greenhouse.io/anthropic Anthropic
https://jobs.lever.co/openai OpenAI
https://jobs.ashby.com/mistral Mistral
```

Then in Claude Code say: **"import job boards from file"**

**Option B — Ask Claude Code to add them**

In Claude Code: *"Add Anthropic's job board: https://job-boards.greenhouse.io/anthropic"*

## 6. Start using it

Restart Claude Code (so it picks up the MCP server), then just talk to it:

- *"Scan all job boards"*
- *"Show me all new roles"*
- *"Score all unscored roles against my master CV"*
- *"List roles with a score above 70"*
- *"Tailor a CV and cover letter for role abc123"*
- *"Mark role abc123 as applied, notes: submitted via LinkedIn"*
- *"Show me the pipeline overview"*

## 7. (Optional) Set up daily automation

`scheduled-task-template/SKILL.md.example` is a full 14-step pipeline prompt — pre-score, JD-fetch, full-score, tailor CVs, draft outreach emails, write a report, send a notification — meant to be adapted into your own Claude Code scheduled task.

Copy it somewhere Claude Code's scheduled-task system reads from, fill in every `[PLACEHOLDER]` (your name, your clone's absolute path, your career-background summary, your task-tracker of choice), and remove the steps you don't want (e.g. email ingestion, or the email-drafting Option C). Then create the scheduled task pointing at that file, on whatever cadence you like (a daily morning run is the intended cadence, but nothing requires that specifically).

Note that this template describes but does not implement live browser-driven application submission ("Option B") — that's meant to happen in an interactive Claude Code session where you confirm each submission live, not inside an unattended scheduled run.

## Workflow Claude Code will follow

When you say **"score all unscored roles"**, Claude will:
1. Call `list_roles(status="new")` to get unscored roles
2. Call `get_master_cv()` to get your CV text
3. Call `get_scoring_criteria()` to get your `filters.yaml` rules
4. For each role, call `get_role_detail(id)` to get the JD
5. Reason about the fit across the 5 weighted dimensions, applying your location/employer-exemption rules
6. Call `store_score(id, score, breakdown, missing_skills)` to persist

When you say **"tailor a CV for role X"**, Claude will:
1. Call `get_role_detail(id)` → JD + current score + missing skills
2. Call `get_master_cv()` → master CV text
3. Run the gap analysis and write two separate Markdown documents — a CV and a cover letter
4. Call `store_tailored_cv(id, cv_markdown, cover_letter_markdown)` → server renders two separate PDFs
5. Read both PDFs back to check the formatting actually came out right (see the rendering convention below) before telling you it's done

## CV/cover-letter formatting convention

`cv_engine/renderer.py` produces a properly styled document — bold job titles, italic company/location, real bullet points — but only if the Markdown follows a specific structure:

- Candidate name as `# Name` (H1), with the contact-info line as the next paragraph.
- Every job/education entry as `### Title` (H3) immediately followed by one paragraph in the exact form `Company – Location | Date range` (pipe-separated).
- A blank line between that meta line and the bullet list beneath it.
- The cover letter's subject line as a `##` heading.

Deviating from this (e.g. using `**Title**` instead of `### Title`) doesn't error — it just falls back to a plain, unstyled run-on paragraph. Always check the rendered PDF, not just that the tool call succeeded.

## File layout

```
job-hunter/
├── mcp_server.py              ← MCP server entrypoint
├── config.yaml                ← settings (CV path, thresholds, your home city)
├── filters.yaml                ← your career-fit criteria
├── job_boards.txt             ← your target companies
├── master_cv.docx             ← YOUR MASTER CV (place here)
├── requirements.txt
├── SETUP.md
├── data/
│   └── jobs.db                ← SQLite database (auto-created)
├── output/
│   └── cvs/                   ← generated CV + cover-letter PDFs saved here
├── db/                        ← database layer
├── scraper/                   ← board-specific scrapers
├── parser/                    ← DOCX CV reader
├── cv_engine/                 ← PDF renderer
└── scheduled-task-template/   ← adapt into your own daily-scan automation
```
