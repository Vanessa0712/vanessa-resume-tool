# vanessa-resume-tool

An intelligent resume screening tool, still under development.

> English translation note: commands, paths, file names, JSON keys and sample data values are kept verbatim. UI labels are rendered in English; the shipped interface is currently in Chinese.

# Skill package included

> `resume-tool-agent`
>
> Tip: To handle resumes through the Harness, first install the `resume-tool-agent` skill package. Once it is loaded, you can manage the Harness with plain natural-language instructions.

# Resume Tool — Local Resume Screening Tool (Dual-Engine Prototype)

Built in-house against the confirmed requirements: **single-user, local-only use; bulk import/export and management of resumes; template-driven screening; optional integration with multiple AI APIs or a fully offline keyword engine**. The exe packaging approach follows the conclusions in `exe-feasibility-report.md`.

## Quick start

```bash
pip install pypdf          # optional: PDF support (degrades gracefully if missing)
python make_samples.py     # generate 8 test samples (incl. docx / disguised .doc / pdf) into samples/
python resume_tool.py import --dir samples                 # bulk import
python resume_tool.py screen --engine keyword --template config/template.json   # offline screening
python resume_tool.py screen --engine funnel   --template config/template.json  # funnel mode
python resume_tool.py search --q "数据 工程师"              # full-text search over body text (AND)
python resume_tool.py export --out report.csv              # export report
```

> Sample values such as the search query are kept as-is because the bundled test resumes are in Chinese.

## Data directory

Writable data (database, uploaded originals, backups, AI config, screening templates) is kept separate from the program code:

| How it runs | Default data directory |
|---|---|
| Source: `python app.py` | Project directory (backward-compatible with current dev usage) |
| Packaged exe | `%LOCALAPPDATA%\ResumeTool` |
| Custom | Set env var `RESUME_TOOL_DATA_DIR=D:\ResumeToolData` |

When the exe starts for the first time, it copies `resume_tool.db`, `uploads/`, `backups/` and `config/` from the exe's own directory (or the source directory) into the new data directory; **existing files are kept, not deleted**. The "Data Backup & Restore" section on the Settings page shows the actual data directory currently in use.

Both directory import and upload import copy the original file into `uploads/`, and the database stores a relative `uploads/...` path. This means that after restoring a backup on another machine, previewing and opening the original file on the detail page still works; directory imports no longer depend on the source folder.

## Three screening modes

| Mode | Description | Cost |
|------|------|------|
| `keyword` | Engine A: local weighted keyword scoring, fully offline | Zero |
| `ai` | Engine B: per-resume semantic evaluation via LLM calls | Pay per call |
| `funnel` (recommended) | Keyword pre-screen over all resumes → AI reviews only the "needs review" list | Predictable cost |
| `agent` (no API key) | Collaborative evaluation via a WorkBuddy Agent (see below) | Uses the operator's own WorkBuddy account quota |

Engine A scoring model: hard gates (required skills / years of experience) act as a veto, plus weighted core-skill scoring (evidence count mapped to a 0–5 score; core skills draw from a 90-point pool, bonus items from a 10-point pool). This is the same validated logic used by `resume-screener`.

## Agent collaboration mode (no API key, added 2026-11-14)

Designed for distribution to office staff who have no API key of their own: **AI evaluation happens inside the operator's own WorkBuddy session, and the quota is charged to their account**. The tool never touches any credential.

```bash
# 1) Keyword pre-screen (or use --all to export everything)
python resume_tool.py screen --engine keyword --template config/template.json

# 2) Export the "needs review" batch (text is redacted: name / phone / email masked)
python resume_tool.py agent-export --template config/template.json --out agent-batch.json

# 3) In WorkBuddy, tell the Agent: read agent-batch.json, follow its instructions,
#    evaluate each resume, and write the resulting JSON array to agent-results.json

# 4) Write results back (engine recorded as ai; detail carries provider=workbuddy-agent for traceability)
python resume_tool.py agent-import --in agent-results.json --template config/template.json
```

- The batch file carries evaluation instructions, screening criteria and the output JSON schema (isomorphic with Engine B output), so the Agent can simply follow it
- Once written back, the Web UI report page / CSV / xlsx exports display the results **with no code changes**
- `agent-export --all` exports everything; `--ids 1,3,5` exports specific records; by default only resumes marked "needs review" by the keyword pre-screen are exported (i.e. a human-powered replacement for funnel stage 2)
- This mode is unrelated to cloud services: no deployment, no Origin checks; it naturally fits the planned exe packaging path
- Privacy note: redaction covers only names / phone numbers / email addresses; work history and all other content still enter the session context
- **Companion Skill**: `dist/resume-tool-agent.zip` — install it into office staff's WorkBuddy (user-level skill) and a single "help me screen these resumes" triggers the whole flow; source at `~/.workbuddy/skills/resume-tool-agent/SKILL.md`

## Format support (doc/pdf first, per the confirmed requirements)

| Format | Parsing | Dependencies |
|------|---------|------|
| .docx | stdlib zipfile + XML parsing | None |
| .doc (disguised) | Smart detection: many .doc files in the wild are actually docx / RTF / HTML / plain text; each is probed in turn | None |
| .doc (true binary) | Word COM converts to docx, then parse | pywin32 + locally installed Word (Windows only) |
| .pdf | Text extraction via pypdf | `pip install pypdf` (skipped with a warning if missing) |
| .txt / .md | Multi-encoding attempts (utf-8 / gb18030) | None |

Imports are deduplicated by SHA-256 of file content; duplicates are skipped automatically.

## Multi-provider AI integration (OpenAI-compatible protocol)

Edit `config/providers.json`:

```json
{
  "active": "deepseek",
  "providers": {
    "deepseek": {"label": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "api_key": "YOUR_API_KEY", "model": "deepseek-chat"},
    "custom":   {"label": "Custom",   "base_url": "https://your-gateway.example.com/v1", "api_key": "...", "model": "..."}
  }
}
```

- Three presets: DeepSeek / Moonshot Kimi / Custom; **any service compatible with the OpenAI chat/completions protocol works** (including self-hosted gateways and corporate proxies)
- Switching providers = change the `active` field; run `python resume_tool.py providers` to see readiness status
- Keys are stored only in a local JSON file, **never committed to the repo and never bundled into the exe** (per the security requirements in the feasibility report)
- Refer to each provider's official documentation for base_url / model values

## Version updates and distribution (GitHub Release driven)

The client always queries the [latest stable Release](https://github.com/Vanessa0712/vanessa-resume-tool/releases/latest) of `Vanessa0712/vanessa-resume-tool`. By default it performs one background check each time `python app.py` runs, without blocking the web service; Settings → Version Update shows the result of that check, and clicking "Check for updates" runs it again. Reopening the Settings page only reads local state and does not issue repeated GitHub requests.

When a new version is found, the Release title, notes and release page link are shown. Updates are currently semi-automatic — **check + manual download**: nothing is silently downloaded, overwritten or executed.

Maintainer steps to publish an update:

1. Add a new version entry at the top of `CHANGELOG` in `app.py` (the first entry is the client's current version), and update `update-manifest.json` as the release-notes template
2. Commit the code and create a Git tag matching the version, e.g. `ver1.3.0`
3. Create a new Release in the GitHub repo, select that tag, fill in the title and release notes; if exe / zip installers exist, upload them as Release Assets
4. Publish as a **stable Release** — do not check Draft or Pre-release

The client calls `https://api.github.com/repos/Vanessa0712/vanessa-resume-tool/releases/latest`: this endpoint returns only the latest stable Release and excludes drafts and pre-releases; if no stable Release exists yet, the client explicitly explains the 404 instead of incorrectly reporting "you're up to date". The startup check can be disabled in General Settings; manual checks are unaffected.

Back up `resume_tool.db` and `config/` before upgrading; once the exe data directory is moved out, installer updates will not overwrite resume data.

## Data backup and restore

Settings → Data Backup & Restore provides a safety net for daily office use:

- Manual backup: a consistent SQLite snapshot + AI config + screening templates packaged as a ZIP; optionally "include original resume files"
- Daily automatic backup: on by default, creates one database + config backup on the first launch of each day; 7 automatic backups, 3 pre-restore backups and 20 manual backups are retained
- Download and restore: the Settings page lists backup files for direct download; upload a `.zip` or `.db` to restore, and a `pre-restore` backup is always created first
- Restore validation: checks SQLite integrity, required tables, backup size and upload path; a restore is refused while a screening job is running

Backup files are **not encrypted** and contain candidate PII and AI keys. Store them in a company-controlled directory or on an encrypted disk, and do not transfer them through public chat tools. `backups/` is already in `.gitignore`.

## Privacy cleanup, logging and diagnostics

- Permanent deletion: choosing "Delete permanently" or "Empty trash" also deletes the original files in the data directory `uploads/`; historical files imported from external directories are never deleted without consent — only their count is shown in the notice
- Orphan cleanup: Settings → Run Diagnostics & Storage Cleanup counts files in `uploads/` that no resume references (copies left by duplicate imports, failed upload leftovers) and can remove them in one click to reclaim space
- Runtime logs: written to `logs/resume-tool.log` in the data directory; 2 MB per file with 5 rotated files; scenes remain reconstructible after packaging as a console-less exe
- Error IDs: when a 500 occurs, the page shows an 8-character error ID and the stack trace is logged under the same ID
- Diagnostics bundle: "Download diagnostics bundle" exports a ZIP with environment info, data-size statistics, redacted AI config (only whether a key is configured) and the 3 most recent logs; **it contains no resume text, candidate PII, API keys or database files**

## Screening templates

`config/template.json`: `hard_gates` (required skills + minimum years) + `core_skills` (weight + aliases) + `nice_to_have`. To switch roles, copy the file and edit the skill table, then point `--template` at it. Multiple templates can coexist.

## Verified (tested locally)

- Key regression: `python -m unittest discover -s tests -v` (26 tests) covering export ID deduplication, CSV conventions, corrupted-config degradation, template path traversal, cross-site write requests, background job exceptions, DB connection release, backup/restore, restore into a fresh directory, data-directory migration, relative paths, import batches, source-file cleanup on permanent deletion, orphan cleanup, diagnostics-bundle redaction and template selection memory
- 7 samples cover every format path (txt / real docx / docx disguised as .doc / HTML disguised as .doc / RTF disguised as .doc / hand-built PDF), all parsed correctly
- Keyword engine: a senior backend candidate scoring 91.7 passes, a candidate missing required skills scores 0 and fails; gate and scoring logic are correct
- When AI is not configured, actionable guidance is shown (no crash); funnel automatically degrades to a "manual review" hint when AI is unavailable
- Duplicate imports are 100% deduplicated by content hash; CSV export uses utf-8-sig (opens cleanly in Excel)
- **Full Agent collaboration chain (2026-11-14)**: 8 resumes keyword pre-screened → agent-export (PII masking active) → evaluated inside the Agent session → agent-import writes back 8 records (engine=ai + provider traceability) → Web UI /screen and CSV reports both display correctly

## Web UI (Beta ver1.2.3)

```bash
python app.py        # → http://127.0.0.1:5000
```

| Page | Capabilities |
|------|------|
| Resume library `/` | Directory import / drag-and-drop upload (originals copied into the data directory), **import batch records with per-file failure reasons**, stat cards, **paginated main list (15 per page, bottom pager + page jump)**, **full-text search over resume text** (`?q=数据 工程师`, FTS5 + per-character Chinese tokenization, with hit snippets), **view by template** (`?template=<slug>`, stats and status computed for that template only), multi-select bulk delete (confirm dialog + "don't ask again today"), CSV export |
| Import log `/imports` | Batch source, total count, success / duplicate / failure stats; batch detail lists each file as added, restored, duplicate, unparseable or errored, with its reason |
| Candidates `/persons` | Automatic merging of multiple versions of the same person (email → phone → name), showing the latest version and version count; `/person/<id>` lists all versions for comparison |
| Trash `/trash` | Deletion moves to trash; single/batch restore, permanent delete, empty trash; re-importing an identical file automatically restores the original record |
| Screening center `/screen` | **Searchable multi-select templates** (filter as you type, chip display, ★ favorites pinned, recently used, AI call estimate, keyboard support); **selection is remembered and restored on next launch** (stored as `screen_templates` in `config/app_settings.json`, auto-invalidated when a template is deleted); three engines to choose from; resume scope = all / unscreened only (per selected template) / specified resumes (searchable by filename or ID, filter by status, select all); **screening runs in a background thread**, the page polls a progress bar (`/screen/job/<id>`), and the completion card keeps a result summary; results grouped by template |
| Charts `/charts` | Status distribution donut, score distribution, pass rate per template, skill-hit heatmap, import trends (server-side computation + hand-drawn CSS/SVG, no external chart library) |
| Template management `/templates` | Create, visual editing (department / sub-role, add/remove skill rows, weight, aliases, required flag, live weight total), ★ favorites, JSON source mode, bulk import (per-file validation, auto-rename on name collision), single JSON export / export all as ZIP |
| Resume detail `/resume/<id>` | Parsed text preview → screening history (10 per page, bottom pager + page jump) → original file preview (PDF/text embedded, doc/docx prompts to open locally); open with: system default / WPS / Word / reveal in Explorer / cloud docs (copy path + open drive) |
| Export | `GET /export-xlsx`: candidate report (14 columns, frozen header + autofilter + summary sheet, supports q/template/ids filters, IDs auto-deduplicated); `/export` produces CSV with the same search/template conventions, with a Chinese notice for empty results |
| Changelog `/changelog` | Sidebar entry (NEW badge when unread), historical announcements as version cards, expandable/collapsible |
| Settings `/settings` | Startup self-check + comparison against the latest stable GitHub Release + release notes and release-page link, general settings (open method / cloud docs / startup check / daily auto backup), backup list and restore, orphan cleanup and diagnostics bundle export, multi-provider Key / BaseURL / model config (leaving a field blank does not overwrite a stored key) |
| Screening center `/screen` | Confirms estimated AI call volume before submitting AI/funnel jobs; background jobs keep running after leaving the page, with clear messages on failure or invalidation |

Other:

- An update announcement pops up on entry (`CHANGELOG` in `app.py`), once per session; "Don't show today" is stored in localStorage and resets automatically by date
- Template edits take effect immediately (`TEMPLATES_AUTO_RELOAD`); UI iterations need no restart
- All front-end assets are built in-house locally, zero CDN, works offline

## Visual theme (Phase A)

Warm-paper style: `--bg #FAF9F5`, primary coral `#D97757`, white cards with 16px border radius. Styles are split into two layers:

| File | Role |
|------|------|
| `static/style.css` | Base structure and component styles (left untouched) |
| `static/theme.css` | Theme override layer, loaded after style.css; change colors / radius / fonts only here |
| `static/fonts/*.woff2` | Embedded Noto Serif SC subset (full GB2312 set + UI copy glyphs); no local font installation needed |

Font rebuild: `pip install fonttools brotli && python tools/build_fonts.py` (download → subset → verify Han-character coverage).
The font is licensed under SIL OFL 1.1; see `static/fonts/LICENSE.txt`.

## Known limitations

- True binary .doc requires Word installed on the end user's machine (otherwise skipped with a warning; other resumes are unaffected)
- Scanned (image-only) PDFs yield no text and need OCR (not implemented; evaluate separately if required)
- The keyword engine may miss reworded resumes — precisely why funnel has AI review the grey area
- Engine B uses defensive JSON parsing; in extreme cases a single failed resume does not abort the batch
- "Open with" invokes local programs and only works for 127.0.0.1 local access; cloud docs only copies the path, nothing is uploaded
- Candidate merging relies on a detectable name / phone / email: when none are present it falls back to the filename (never merged with others); identical names may be merged incorrectly
- xlsx export depends on `openpyxl` (if missing, the page shows the install command; other features are unaffected)
- Full-text search uses SQLite FTS5: Chinese is tokenized per character + phrase matching (`"数 据"`), English per word; no synonyms or pinyin
- Environments without FTS5 (rare) fall back to a LIKE full-table scan: still functional, but slower on large datasets
- The "All templates" view takes each resume's **most recent** screening result (across templates); use the dropdown to get per-template statistics
- Trash is a soft delete: screening records are kept and source files on disk are always retained; permanent deletion is irreversible
- Background job state lives in process memory, so past job progress is unavailable after a service restart (the data itself is already persisted)
