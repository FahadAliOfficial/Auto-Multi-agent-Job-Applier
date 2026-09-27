# Project Memory — Indeed Application Assistant

Last updated: 2026-08-04 (America/Los_Angeles)

This file is the durable working memory for future sessions. Read it before making changes. Update it at the end of every work session with what changed, what was verified, new decisions, and the next concrete tasks. The code and live database remain the source of truth; this file is a maintained map, not a substitute for re-checking code that has changed.

## 1. Project purpose

This is a personal Python 3.11+ application that automates Indeed job discovery and applications. It supports:

- Indeed search across configured countries and queries.
- Indeed Easy Apply multi-step form automation.
- Semi-automatic approval and fully automatic modes.
- Optional concurrent agents sharing one persistent browser context.
- A Flask dashboard and live control center.
- Learned screening-question answers stored in SQLite.
- Experimental company-site/ATS application automation.
- A separate AI-assisted resume tailoring web app.

The README is incomplete and must not be treated as authoritative. The current understanding below came from source, configuration, git state, the SQLite schema/data, and operational logs.

## 2. Repository and runtime state

- Branch: `main`, one commit ahead of `origin/main` at inspection time.
- Commits visible: `dc5a742 feat: multi-agent support`; remote tip `846a0fc V1 - Indeed Job Applier`.
- Working tree is intentionally dirty and contains substantial user work. Never discard or overwrite it.
- Modified source includes bot, database, orchestrator, detector/filler, search/job pages, delay utility, control-center UI, config, and README.
- `src/pages/external_apply.py` and `tests/test_job_claims.py` are untracked but already imported/used by current code. They must be preserved and eventually committed with the related feature.
- Many tracked `__pycache__/*.pyc` files are modified. This is repository hygiene debt; do not delete or untrack them without the user's approval.
- Ignored runtime state lives under `data/`: SQLite databases, browser profile/session, logs, screenshots, resume outputs, and possible manual-lead CSV.
- `.env` is ignored. At inspection it defines a Gemini key variable. Never copy secret values into this file, logs, commits, or chat.
- Personal contact details and resume data are tracked in `config/config.yaml` and `config/resume_master.json`. Treat them as sensitive; consider moving/redacting them before making the repository public.
- No `AGENTS.md` exists in this repository.

### Environment verification status

- All Python source compiled successfully with `python -m compileall -q src tests` on 2026-08-04.
- The active Python interpreter had none of the project runtime/test packages installed: Playwright, aiosqlite, PyYAML, Rich, Flask, or pytest.
- Therefore unit tests, Flask route smoke tests, resume-engine smoke tests, and browser flows were not runnable in this session.
- `pytest` is not declared in `pyproject.toml`, even as an optional/dev dependency.
- The project also requires a Playwright Chromium install after Python package installation; this is not represented by `pyproject.toml` alone.

## 3. Entrypoints and commands

Package/CLI entrypoint: `indeed-bot = src.main:main`.

Main commands:

- `python -m src.main run [--mode semi|auto] [--agents N] [--config PATH]`
- `python -m src.main login`
- `python -m src.main search [-q QUERY]`
- `python -m src.main status`
- `python -m src.main dashboard [--host ... --port 5000]`
- `python -m src.main resume-maker [--host ... --port 5050]`

The normal `run` command starts the control-center Flask server in a daemon thread and opens it in the default browser when enabled. The standalone dashboard uses Flask debug mode; the embedded control center does not.

## 4. Architecture map

| Area | Main files | Responsibility |
|---|---|---|
| CLI/lifecycle | `src/main.py`, `src/bot.py` | Parse commands; load config; initialize DB/browser; login; search; apply; summarize |
| Browser/session | `src/browser.py` | Playwright persistent Chromium context, saved storage state, browser fingerprint/stealth scripts |
| Search/detail | `src/pages/search_page.py`, `src/pages/job_page.py` | Build country-specific search URLs, parse cards, open detail pages, extract metadata, detect/click apply buttons, CAPTCHA waits |
| Easy Apply | `src/pages/apply_form.py` | Up-to-10-step state machine, resume step, CAPTCHA handling, navigation, completion/error detection |
| Field automation | `src/handlers/form_detector.py`, `src/handlers/form_filler.py` | Detect/classify controls; fill identity/contact data, selects, radios, checkboxes, resume, salary, screening fields |
| Learned answers | `src/handlers/question_matcher.py` | Exact/fuzzy lookup at 0.85 threshold, terminal/control-center prompts, persistence and reuse |
| Multi-agent | `src/orchestrator.py`, `src/control_center.py` | Round-robin query partitions, one page per agent, DB job claims, in-memory live state/actions/prompts |
| External apply | `src/pages/external_apply.py` | Experimental generic ATS flow, login-wall/email detection, recursive apply navigation, form fill, manual lead persistence |
| Persistence | `src/database.py`, `data/indeed_bot.db` | Async SQLite CRUD, sessions, answers, claims, manual leads, aggregate stats |
| Dashboard | `src/dashboard/app.py`, templates | Jobs, sessions, questions editor, stats, live agent control APIs/UI |
| Resume maker | `src/resume_maker/app.py`, `engine.py`, template | Master JSON editing, heuristic/AI tailoring, cache, preview, JSON/Markdown/HTML export |
| Utilities | `src/utils/*` | Randomized delays/human typing, Rich/file/control-center logging, screenshots |

## 5. End-to-end bot flow

1. `IndeedBot.initialize()` loads YAML, connects/migrates SQLite, closes stale sessions, creates `QuestionMatcher`, and configures `BrowserManager`.
2. `BrowserManager.start()` launches a persistent Chromium profile at `data/sessions/browser_profile`, injects stealth scripts, and returns the first page.
3. `LoginPage` checks common logged-in selectors. If needed, login is primarily manual because the bot passes only email, not a password. The context storage state is saved separately, although persistent profile data is already used.
4. `run()` starts the control center, selects single-agent or `AgentOrchestrator`, then iterates countries and configured search queries.
5. `SearchPage.search()` directly navigates to a URL for each page offset and parses job cards into `JobListing` records. It supports US, Pakistan, UK, Canada, Australia, India, and UAE mappings.
6. `_process_listing()` checks prior applied/skipped state, candidate apply type, records the job, opens its detail page, populates control-center job info, applies skip rules, and detects Easy Apply versus company-site apply.
7. In semi mode the user approves/skips/quits via control center (when an agent ID exists) or terminal prompt.
8. Easy Apply opens the Indeed form, selects a resume based on title keywords, builds job context, and runs `ApplyForm.complete_application()`.
9. The detector finds text inputs, textareas, selects, radio/checkbox groups, and uploads; the filler uses config, learned answers, or user prompts. Salary and transient work-history answers are intentionally not globally reused in the same way.
10. Success/failure/skips update SQLite and counters; configured screenshots are written under `data/screenshots`.
11. Company-site mode opens an external tab and uses generic ATS heuristics. It can return success, account-needed, manual-needed, or failure and always closes the external tab.

## 6. Configuration actually used

`config/config.yaml` controls personal data, resume profiles, search criteria, bot mode/agents/delays/browser, skip lists, countries, and company-site behavior.

Important observations:

- Current mode is `semi`, agents is `1`, max applications is `25`, and the browser is headed.
- The orchestrator is enabled, but the multi-agent path only runs when agents > 1.
- Search spans seven configured country codes and many broad technical queries, with date posted = one day and Easy Apply only by default.
- Company-site apply is currently disabled.
- Only `preferences.education` is configured. Many mapped fields such as years of experience, start date, work authorization, relocation, salary, and language preferences are absent and may prompt.
- `config/answers.yaml` contains sensible defaults but is never loaded or referenced by source code. It currently has no effect.
- `bot.orchestrator.headless_per_agent` is configured but never read.
- `BrowserManager.has_saved_session()` is defined but unused.
- `rich.prompt.Confirm` and a few imports are unused.
- There is a duplicated “Company Site Apply” comment block at the end of config.
- Character encoding appears mojibaked in several comments/log strings when read through the current shell; verify whether files are UTF-8 and normalize carefully rather than mass-replacing blindly.

## 7. Database model and observed data

Intended tables:

- `jobs`: Indeed ID, metadata, URL/description, status, apply type, timestamps, notes.
- `search_sessions`: query/location/filter plus found/applied/skipped counts and timing.
- `screening_answers`: question/answer, optional job link, confidence, manual flag, reuse count.
- `job_claims`: atomic ownership for concurrent agents (`claimed`, `applied`, `released`).
- `manual_apply_leads`: company-site jobs needing email/manual follow-up.

Observed `data/indeed_bot.db` on 2026-08-04:

- 259 jobs: 59 applied, 153 skipped, 10 failed, 37 found.
- 375 search sessions, with 2 still open in the inspected file.
- 184 learned screening answers.
- 214 job claims.
- 0 manual apply leads.
- All 259 jobs have `apply_type = easy_apply`.
- Most recent recorded session is 2026-06-21; the log is also last modified 2026-06-21.
- Eight of ten persisted failures say `Form submission failed`; the other observed reasons are missing apply/form or failure opening a company-site tab.

Do not edit or migrate the live DB without a backup and an explicit migration plan.

## 8. Implemented feature status

### Proven by runtime artifacts

- Search and Easy Apply have been used substantially.
- SQLite job/session/question persistence works in the previously used environment.
- Application screenshots and error screenshots are produced.
- Screening questions are learned and reused.
- The project has recorded 59 applied jobs.

### Implemented and partly evidenced, but not fully verified now

- Multi-agent orchestration, claims, query partitioning, per-agent pages, and control-center controls.
- CAPTCHA pause/continue behavior.
- Resume upload refinements and broader company-site field detection.
- Generic external ATS/company-site apply.
- Resume maker with OpenAI/Gemini and optional local Ollama JD summarization.

### Present but incomplete/inactive

- Company-site apply is disabled in config, currently uncommitted, has no successful/manual-lead records, and conflicts with the live DB constraint (see blockers).
- Resume maker exports JSON, Markdown, and HTML only; it does not create PDF/DOCX despite producing printable HTML.
- The dashboard does not display manual leads and understands only legacy job statuses in its schema/stats/colors.
- Default answers YAML is disconnected.
- Master resume `skill_sections` is disconnected: the renderer looks for `skills_grouped`, `skills_by_category`, a skills dict, or flat `skills`; it never reads `skill_sections`.

## 9. High-priority defects and risks

### P0 — blocks current company-site feature

1. **Broken SQLite status migration.** Current source creates `jobs.status` with `external_applied` and `manual_needed`, but the existing DB retains the legacy CHECK constraint allowing only `found/applied/skipped/failed`. The code only adds `apply_type`; it does not rebuild the table constraint. Writes of the new statuses will raise an integrity error.
2. **Untracked required module.** `src/bot.py` imports `src/pages/external_apply.py`, but that file is untracked. A commit/deploy that omits it will fail at import time.

### P1 — correctness and observability

3. **Application summary double-counts external success.** External success increments both `jobs_applied` and `jobs_external_applied`; `_print_summary()` then sets total to their sum. The displayed total is too high.
4. **Single-agent session statistics are cumulative.** `_run_search_query()` writes global `self.jobs_found/applied/skipped` into every query session rather than per-query deltas, so later session rows are inflated.
5. **Multi-agent registry counters can double count.** `_process_listing()` increments registry outcomes and `AgentOrchestrator._run_agent()` increments them again after inspecting DB status.
6. **Claim handling misclassifies already-processed listings.** The orchestrator claims before `_process_listing()`. If `_process_listing()` sees an already-applied row, it counts an internal skip, but the orchestrator reads status `applied`, marks the claim applied, and counts a fresh application.
7. **Dashboard schema and stats are stale.** `_INIT_SQL` has only four statuses and no `apply_type`, claims, or manual-lead schema; filters/colors/stats omit `external_applied` and `manual_needed`.
8. **`apply_type` is never written for company-site jobs.** `Job` lacks an `apply_type` field and `add_job()` omits the column, leaving every row as the default `easy_apply`.
9. **UAE URL construction is wrong.** UAE's domain map already includes `/jobs`, then `SearchPage` appends `/jobs` and `/viewjob`, producing paths like `/jobs/jobs` and `/jobs/viewjob`.
10. **Single-agent control-center actions are incomplete.** The single-agent loop does not call the orchestrator’s action service or `wait_if_paused()`. Pause/focus/retry are therefore multi-agent-only in practice; stop/skip are only noticed at limited checkpoints.

### P1 — operational reliability

11. Operational logs show repeated 20-second job-detail timeouts, empty/blocked search pages, stuck form navigation, and generic ATS failures on ADP/Workday.
12. External apply assumes the initial Indeed click opens a new tab. Same-tab redirects are treated as failure in `JobPage.click_company_site_apply()`.
13. Generic external form detection can mistake unrelated forms for job applications (only two visible inputs required), and generic Apply/Submit selectors carry real risk on unknown sites.
14. The configured global application maximum can be overshot by concurrent agents already applying at the same time.
15. External form mode uses `config.bot.mode`, not the CLI-effective mode passed to the running bot, so CLI mode overrides may not propagate into external form behavior.

### P2 — maintainability, security, and quality

16. Dependencies are unconstrained lower bounds only; there is no lock file or documented reproducible environment.
17. Tests cover only classifier mapping, question matching, and DB claims. There are no tests for URL construction, bot counters/session accounting, migrations, form state machine, dashboard routes, external apply, or resume engine.
18. The dashboard uses a hard-coded development secret and has unauthenticated state-changing endpoints. Binding defaults to localhost mitigates exposure, but it must not be exposed publicly as-is.
19. Browser fingerprint code hardcodes Chrome 131, disables several browser security/features, ignores HTTPS errors, and uses `--no-sandbox`. This is brittle and security-sensitive.
20. Persistent browser profile and logs contain sensitive session/application information. Keep `data/` ignored and never attach it wholesale.
21. Tracked bytecode pollutes diffs and can diverge from source.
22. Dashboard and resume-maker Flask routes perform blocking SQLite/network/model work without production server/error-boundary design; acceptable for a local tool, not for shared deployment.

## 10. Resume maker details

- Master source: `config/resume_master.json`; generated cache/exports: `data/resume_outputs`.
- UI can edit/save master JSON, paste a JD, select OpenAI or Gemini, tailor, preview, and export.
- Tailoring first reorders relevant bullets/skills heuristically, then optionally asks a remote model to rewrite only the summary and list changes. A deterministic role-focus post-pass reorders content without inventing facts.
- OpenAI uses a hardcoded dated `gpt-4o-mini` model; Gemini uses a hardcoded `gemma-4-31b-it` string. Both need current compatibility verification before relying on them.
- Optional Ollama summarization points to local `orca-mini:3b-q4_0` at `127.0.0.1:11434`; config currently disables local JD summarization.
- Cache key includes resume, JD, model/provider, API-key availability, polish flag, and local-summary flag. Failed remote refinements are intentionally not cached.
- API keys are read from environment and several possible `.env` paths. Never expose values.
- Export creates a timestamped folder containing `resume.json`, `resume.md`, and `resume.html`.
- Current master JSON uses valid list-valued bullets and a flat `skills` list. Its separate `skill_sections` mapping is currently ignored.

## 11. Recommended work order

1. Establish a reproducible dev environment: virtual environment, install package plus test tooling, install Playwright Chromium, record exact commands/versions.
2. Add a safe, tested SQLite migration that rebuilds the legacy `jobs` table constraint while preserving all live data; back up the DB first.
3. Finish and commit the company-site feature as one coherent change, including `external_apply.py`, schema/status/dashboard support, `apply_type` persistence, and tests.
4. Fix counters, per-query session deltas, duplicate registry increments, and already-applied claim semantics.
5. Add focused unit/integration tests for all fixes before live browser runs.
6. Fix country URL handling (especially UAE) and test every configured domain.
7. Make single-agent controls behave consistently with multi-agent controls.
8. Connect or remove `answers.yaml`; connect/rename `skill_sections`; add config validation so missing preferences are visible before a run.
9. Improve timeout/block diagnostics and robust same-tab/new-tab external navigation.
10. Clean repository hygiene and sensitive tracked config only with user approval.

## 12. Safe working rules for future sessions

- At session start: read this file, inspect `git status`, and re-open the specific source files being changed.
- Preserve all pre-existing dirty changes. Never reset, checkout, clean, or mass-format without explicit approval.
- Do not print `.env`, browser cookies/profile data, full logs, or personal resume/config content.
- Before DB schema work: copy `data/indeed_bot.db` to a timestamped backup, inspect its schema, and test migration on a copy.
- Do not run live auto-apply tests unless the user explicitly asks; they can submit real job applications.
- Prefer deterministic unit tests and mocked Playwright pages before headed browser tests.
- Treat external company sites as untrusted. Do not bypass account walls, CAPTCHA, or consent controls.
- After source edits: run compile checks, tests, relevant route/engine smoke checks, and inspect the diff.
- Update this file only with durable facts, decisions, verification results, blockers, and next steps. Do not include secrets or noisy command transcripts.

## 13. Session journal

### 2026-08-04 — Repository comprehension and memory bootstrap

Goal: understand the project from code/runtime evidence rather than README and create durable memory.

Completed:

- Inventoried source, config, tests, runtime artifacts, database, log, git history, and dirty working tree.
- Traced CLI, lifecycle, browser/login, search/detail, Easy Apply, field detection/filling, learned answers, multi-agent orchestration, dashboard, company-site apply, and resume maker.
- Compiled all source successfully.
- Queried live SQLite schema/counts without modifying records.
- Identified inactive config, schema drift, counter/session bugs, URL defect, control-center inconsistencies, operational failure patterns, and test/environment gaps.
- Created this `memory.md`.

Verification limitations:

- Active interpreter lacks all declared runtime dependencies and pytest, so no executable tests or app/browser smoke tests completed.
- No network or live Indeed application flow was run.

Next recommended session:

- Set up/locate the intended Python environment, run the existing tests, then address the P0 database migration and untracked company-site feature before further live use.

### 2026-08-11 — Dependency setup and job-detail readiness fix

Goal: centralize dependencies, then diagnose repeated job-detail failures from a live semi-mode run.

Completed:

- Added root `requirements.txt` with the five runtime packages and `pytest`.
- Updated `JobPage.open()` to recognize current title markup, job descriptions, and Apply controls as valid detail-page readiness signals.
- Navigation timeouts now continue when usable job content has already rendered; blank/timed-out pages still fail safely.
- Added the newer title selectors to title extraction and improved timeout logging with the current URL.
- Added `tests/test_job_page.py` covering normal readiness, usable content after navigation timeout, and blank-page rejection.
- A follow-up live run still showed blank detail shells at synthesized `/viewjob?jk=...` URLs.
- Updated search-card parsing to preserve and resolve Indeed's original result-card `href`; direct `/viewjob` construction is now fallback-only.
- Added `tests/test_search_page.py` covering absolute, relative, missing, and JavaScript card links.
- A third live run confirmed the real card link redirected correctly and the job was fully visible, but stable structural selectors were absent.
- `JobPage.open()` is now title-aware: it accepts the page when the exact known search-card title is visible in body text and uses that title as an extraction fallback.
- The bot passes the listing title into the readiness check; this avoids accepting arbitrary pages while supporting the observed Indeed layout.
- A fourth live run reached apply classification but ignored a visible `Apply now` control because detection excluded that label while clicking supported it.
- `has_easy_apply()` now accepts generic `Apply now` controls only when the search-card context says Easy Apply and company-site mode is disabled.
- Existing rows skipped specifically as `No apply button` or `No Easy Apply button` are retried automatically; user/rule skips remain preserved.
- Future Apply-button detection misses are recorded as technical failures rather than permanent skips.
- Screenshot evidence showed the current blue `Apply now` control is an anchor styled as a button; the prior fix covered buttons/roles but not anchors in Easy Apply classification.
- Context-gated Easy Apply detection and `click_apply()` now both support `a:has-text("Apply now")`, role buttons, and Apply-now aria labels.
- They also support Indeed's generic `[data-testid="job-apply-button"]`, the other control recognized by the branch that produced `No Easy Apply button`.
- The separately opened Control Center polled successfully but missed most single-agent logs because `_CURRENT_AGENT_ID` was only set by multi-agent workers.
- Single-agent `run()` now binds `agent-1` to the logger/`cc_print` routing context for the run lifetime and resets it safely after shutdown.
- Added `tests/test_control_center_logging.py` for context-aware logger-to-registry routing and the no-context case.

Verification:

- `python -m pytest -q tests\\test_job_page.py`: 3 passed.
- `python -m pytest -q`: 13 passed.
- `python -m compileall -q src tests`: passed.
- After the card-URL fix, `python -m pytest -q`: 17 passed and the compile check passed again.
- After title-aware readiness, focused tests passed (4) and the full suite passed (18); compile check passed.
- After Apply-now classification, focused tests passed (5), the full suite passed (19), compile passed, and focused `git diff --check` passed.
- After anchor-control support, focused tests passed (6), the full suite passed (20), compile passed, and focused `git diff --check` passed.
- After generic job-apply test-ID support, focused tests passed (7), the full suite passed (21), compile passed, and focused `git diff --check` passed.
- After the single-agent Control Center log bridge, focused tests passed (2), the full suite passed (23), compile passed, and focused `git diff --check` passed.
- No live application flow was launched by Codex; the user-provided run log was diagnostic input only.

Next steps:

- Re-run semi mode and confirm the separate Control Center receives normal terminal log events in near real time and the pictured Apply-now job proceeds to approval. Do not submit unless intentionally testing a real application.
- Then implement and test the safe SQLite status migration before enabling company-site apply.

## 14. Update template for future sessions

Append a new dated journal entry with:

- Goal/request.
- Files changed.
- Behavior implemented or fixed.
- Decisions and rationale.
- Verification commands and outcomes.
- Known limitations/new risks.
- Git/DB/runtime state that materially changed.
- Exact next tasks.

### 2026-09-22  Normal-Chrome extension browser mode

Goal: add an optional extension-backed browser mode that controls dedicated tabs
inside the user's normal Chrome profile without removing the legacy Playwright
browser flow.

Completed:

- Added `bot.browser.mode` with `legacy` as the unchanged default and
  `extension` as the new opt-in backend.
- Added an authenticated loopback WebSocket bridge and a Playwright-shaped
  adapter covering the page/context/locator operations used by the bot.
- Added a Manifest V3 unpacked Chrome extension with dedicated-tab ownership,
  reconnection/heartbeat behavior, worker-restart persistence, DOM operations,
  uploads, popups, multiple tabs, and screenshots.
- Restricted default host access to Indeed and loopback. Company-site access
  and screenshot/debugger access require explicit Chrome permission grants.
- Added setup documentation, focused unit/integration tests, and a disposable
  local-only headed Chromium smoke test.

Verification:

- `python -m pytest -q`: 29 passed.
- `python -m compileall -q src tests scripts`: passed.
- `node --check` for both extension JavaScript files: passed.
- JSON/YAML/TOML parsing and `python -m src.main --help`: passed.
- `python scripts/smoke_test_extension.py`: passed navigation, CSS/role
  locators, typing, selects, upload, popup capture, extra agent tab, and PNG
  screenshot in a disposable Chrome profile.
- No real Indeed application was submitted during verification.

Preservation notes:

- Existing dirty-tree changes were retained.
- Legacy mode remains the default and all pre-existing tests still pass.
- Generated pairing material stays under ignored `data/`.

### 2026-09-22 — Extension live-run iframe regression

A user live run connected successfully, searched Indeed, opened an Easy Apply
job, and then stalled at form step 1. Evidence showed two defects:

- Normal remote locators executed only in the top document while the current
  Indeed application UI was inside an apply iframe.
- A denied optional screenshot permission raised during error capture, causing
  repeated screenshot attempts and terminating the whole bot.

Fixes:

- Extension DOM commands now execute in all permitted frames and prefer
  meaningful results from Indeed/apply frames, while retaining top-frame
  behavior for ordinary search and job pages.
- Screenshot capture is best-effort and returns `None` on permission/capture
  failures; diagnostic screenshots can no longer abort applications.
- Extension version bumped to 1.0.1 so Chrome reload state is visible.
- The disposable browser smoke test now fills and advances a form inside an
  iframe before exercising popups, multi-tab behavior, and screenshots.

Verification:

- Full suite: 30 passed.
- Python and extension JavaScript syntax checks: passed.
- Disposable headed Chrome iframe smoke test: passed.

### 2026-09-22 — Role-specific resume routing

- Registered all eight PDFs under `data/resume/` as named, prioritized profiles
  in `config/config.yaml`; the general Software Engineer resume is the fallback.
- Resume selection normalizes job text, matches the job title first, and uses the
  description only when the title has no match. Missing profile files are skipped
  safely and reported in the log.
- The chosen path is used by both Indeed Easy Apply and company-site flows.
- Added coverage for every role family, title-over-description precedence,
  description fallback, and configured-file existence.

Verification:

- Full suite: 43 passed.
- `python -m compileall -q src tests scripts`: passed.

### 2026-09-22 — Requirements warning, extension latency, and screenshots

- Indeed's unmet-employer-requirements page is now a first-class decision
  state. It prompts through the control center/terminal with Apply anyway and
  Return to job search choices; declines are recorded as skipped, not failed.
- Apply-form readiness now performs one combined selector wait and recognizes
  the apply iframe and requirements warning immediately, eliminating repeated
  per-selector/per-frame timeout penalties observed in the live log.
- Removed the invalid optional `debugger` screenshot design. Chrome does not
  permit that API permission to be optional. Screenshots now use visible-tab
  capture with the existing explicit optional `<all_urls>` grant.
- Company-site and screenshot choices are persisted independently while safely
  sharing the same optional host permission. The permission is removed only
  when both features are unchecked.
- Extension version bumped to 1.0.3.

Verification:

- Full suite: 50 passed.
- Python and extension JavaScript syntax plus manifest parsing: passed.
- Disposable Chrome smoke test: passed iframe automation and PNG capture.

### 2026-09-22 — Extension live-run performance and lifecycle follow-up

- Live timing showed 10–16 seconds per form step in extension readiness and
  fingerprint waits. Added an opt-out `fast_form_mode` (enabled by default)
  using short deterministic bridge pauses while preserving legacy waits.
- Filtered Indeed's progress input labelled Current page so it is no longer
  treated as a screening question.
- Added application-frame scoring so application content wins even when its
  frame URL/title does not match known Indeed apply patterns.
- Hardened WebSocket reconnects with per-socket callbacks, guarded sends, and
  exponential retry; stale sockets can no longer close/send through a newer
  connection.
- The primary remote page now re-adopts or recreates a lost dedicated tab and
  retries the interrupted command once.
- Options now displays its loaded extension version and reports stale-manifest
  permission errors with an explicit reload instruction. Version is 1.0.4.

Verification:

- Full suite: 54 passed.
- Python/JavaScript/manifest checks: passed.
- Disposable headed Chrome extension smoke test: passed.

### 2026-09-22 — Current Indeed job-detail detection

- Live logs showed successful navigation to valid `/viewjob?jk=...` pages
  followed by false readiness failures. Search-card and detail titles could
  differ slightly (for example, Full Stack Web Developer vs Full Stack
  Developer), while current Indeed markup lacked the older stable selectors.
- Added current layout selectors, normalized token-based title similarity, and
  a guarded valid-viewjob/substantial-job-body fallback.
- Extension job pages now use up to three seconds of 250 ms DOM polling instead
  of the legacy ten-second cross-frame selector wait. CAPTCHA checks remain
  ahead of the fallback, and blank/fabricated viewjob pages remain rejected.
- Extension version bumped to 1.0.5.

Verification:

- Full suite: 57 passed.
- Python/JavaScript/manifest checks: passed.
- Disposable Chrome extension smoke test: passed.

### 2026-09-23  Control-center button reliability

- Fixed Continue so it resumes paused agents and chooses the affirmative value
  from the active prompt (for example, `y`, `ok`, or `Apply anyway`)
  instead of always sending the invalid value `ok`.
- Wired Pause, Resume, Continue, Stop, Skip Job, Focus Tab, and Retry Last into
  the single-agent runner. Controls are now serviced between searches, jobs,
  job-review stages, and active Easy Apply form steps.
- Stop now exits the single-agent search loop without changing the agent back
  to `done`; stopping during a form no longer records a false application
  failure. Skip is persisted against the current job and cleared after use.
- Retry Last now targets the actual previous/current failed job and refuses to
  retry an already-applied job.
- The dashboard now shows running/paused/stopped control state and immediate
  action success or error feedback instead of silently ignoring failed HTTP
  requests.

Verification:

- Focused control/form tests: 17 passed.
- Full suite: 70 passed.
- Python compilation and control-center JavaScript syntax checks: passed.

### 2026-09-23 — Canonical Indeed redirect recognition

- A follow-up live run still rejected visibly opened jobs because the search
  card title could differ substantially from Indeed's canonical detail title.
- Extension-mode job opening now recognizes the successful Indeed redirect
  itself: the current page must be `/viewjob`, its immutable `jk` must exactly
  match the requested tracking URL, and both URLs must be on Indeed. Current
  `/rc/clk` and `/pagead/clk` redirects are accepted even when Indeed omits
  the older `t` and `cmp` query metadata. Direct blank `/viewjob` URLs still
  require content or metadata and remain rejected.

Verification:

- Focused job-page tests: 15 passed.
- Full suite: 73 passed.
- Disposable Chrome extension smoke test: passed.

Also update the earlier architecture/status/defect sections when facts change; do not merely append contradictory notes.

### 2026-09-23  Trigger-first forms, CAPTCHA job preservation, and ARIA dropdowns

- Extension form readiness now starts its existing 100 ms usable-control
  trigger immediately. It no longer waits up to five seconds for a container
  selector before checking already-visible fields.
- CAPTCHA auto-clear polling is 250 ms in extension mode. If verification
  clears without restoring the requested detail URL, the bot reopens the same
  tracking URL once instead of skipping that job and moving to the next one.
- Added detection and filling for Indeed's custom ARIA combobox/listbox
  dropdowns, including option extraction, question-label association,
  prefilled detection, cached/manual answers, and visible option selection.
- Extension version bumped to 1.0.7.

Verification:

- Focused readiness/CAPTCHA/form-field tests: 42 passed.
- Full suite: 80 passed.
- Python, extension JavaScript, and manifest validation: passed.

### 2026-09-23  Easy Apply loading-spinner handling

- Failure screenshots showed the application open at 38% with only a loading
  spinner. Extension fast-form mode treated the outer container as ready after
  250 ms, found no fields or navigation button, and marked the application
  failed before navigating to the next job.
- Form readiness now waits up to 20 seconds for usable fields, a real
  Continue/Review/Submit action, a warning, or a completion screen. Save and
  close, Report, the outer iframe, and the underlying job-page Apply button no
  longer count as a ready application step.
- A slow form receives one automatic retry. In semi-auto mode, a still-stalled
  form remains open and asks the user to Retry or Skip job instead of silently
  moving on.
- Stalled forms intentionally skipped by the user are recorded as skipped
  rather than failed. Extension version bumped to 1.0.6.

Verification:

- Focused form/extension tests: 19 passed.
- Full suite: 75 passed.
- Python and extension JavaScript syntax checks: passed.
