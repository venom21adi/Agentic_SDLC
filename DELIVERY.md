# Status

Honest state of the harness. Update this as things change.

## Verified (pytest, SQLite in-memory, fake LLM — `pytest -q`)
- State store round-trips tickets/plans/specs/DAG edges; retry counts persist.
- Orchestrator: dependency blocking at any stage, gate pass/blocker/escalation, human decisions
  (approve, force_advance, reject, resume_with_guidance), bounded retries on agent failure.
- Business Analysis: decomposes an Ask into features, sub-tickets and DAG edges atomically; rejects bad LLM output (empty, duplicate keys, unknown refs, self-deps, cycles) without writing anything.
- Status history: every transition is logged (creation, advance, send-back, escalation); `get_stage_entered_at` gives time-in-stage.
- Command Center (`command_center.py`, `api.py`): Ask/Feature/Ticket rollups, stuck detection (needs_human, stage timeout, blocked by a stuck dependency, transitively), decision queue, and a POST endpoint that applies human decisions. Tested through the HTTP layer with FastAPI TestClient.
- Plan critique gate: four-pillar review; findings must quote the artifact verbatim (checked mechanically), clean pillars need a real justification, invalid critic output is rejected and counted as a retry; blockers send the ticket back to spec authoring (which sees the findings), bounded by the retry count, then escalate to needs_human. Latest plan/spec wins on re-authoring.
- Implementation agent (`agents/implementation.py`, `workspace.py`): commits LLM-generated files to a per-ticket git branch via a temporary worktree (main never touched); validates paths (no absolute, `..`, `.git`, duplicates), size limits and that `.py` files compile (syntax only, nothing is executed); revisions build on the previous files and delete omitted ones; blocking code-critique findings are fed back on retry. Tested against real git in temp repos.
- Code critique gate (`agents/code_critique.py`): same integrity rules as the plan gate (shared in `agents/critique.py`); reads the code back from the git commit and refuses to review if it differs from the recorded artifact; critic never sees implementer notes or earlier critiques; a change with no test files always gets a major maintainability finding regardless of the model; blockers send the ticket back to implementation (which sees the findings), bounded, then needs_human.
- QA strategy agent (`agents/qa_strategy.py`): LLM plans scope, priorities and risk areas for the committed change (validated, specific); feeds the implementer on retries.
- Test executor (`agents/executor.py`, `runners.py`): runs the committed pytest suite (code read back from git and verified) and records counts and output. `DockerRunner` = no network, read-only fs, caps dropped, cpu/mem/pid/time limits, scratch dir deleted; `LocalRunner` (no isolation) refuses unless ALLOW_UNSAFE_LOCAL_RUNNER=1. Pass requires exit 0, no failures, >=1 test actually run, and a sane junit report (DTD/oversize rejected). Failing tests loop back to implementation with the pytest output; infra failures (docker down) are agent errors, never blamed on the implementer.
- Retry regions: the retry counter spans the whole plan region (spec+plan critique) and build region (implementation..testing) and resets only on leaving it. This fixed a real bug where test failures could loop forever because intermediate gate passes reset the count.
- End to end: Ask -> DECOMPOSED, children planned in dependency order through plan critique, implementation, code critique, QA strategy and real pytest runs, ending at PR_READY (human-owned packaging and merge are not built).

## Not verified
- **Postgres via Docker**: not run yet (Docker daemon was not running). Tests use SQLite.
- **Real OpenAI calls**: never made. Prompts are untested against a real model.
- `setup.py` and the QUICKSTART steps have not been run.

## Known gaps
- Agents store LLM output as parsed JSON if possible, else `{"raw": ...}`; no schema validation.
- No PR packager, Command Center UI, observability or evals (see ROADMAP.md).
- No migrations (tables via `create_all`); `datetime.utcnow` deprecation warnings.
- Command Center limits: stuck threshold is one fixed timeout for every stage (no per-stage p95 yet); rollups issue per-ticket queries (fine at small scale, needs batching later); no auth on the API, including the decision endpoint; no UI yet.
- Plan critique: majors and nitpicks are stored but never routed anywhere (only blockers act); no second-opinion pass for high-stakes tickets; the quote check proves a quote exists, not that the finding is correct; whether a real model will quote accurately enough to pass the check is untested and may cause escalations until prompts are tuned.
- Implementation: one generic agent (no frontend/backend/schema split); branches start from the workspace repo HEAD, so a ticket does not see code from its not-yet-merged dependencies; generated code is never executed or tested yet (needs the test stage); a single commit holds the whole file set per attempt; serial use only (concurrent worktree creation is untested); the workspace repo is local, there is no remote or PR.
- Code critique: majors are not routed anywhere (same as plan gate); the no-tests rule is path-pattern based (a file named like a test counts, whether or not it tests anything); no migration/security auto-blockers from the roadmap; diff-only review is not done, the critic reads whole files; critics have never been run against a real model.
- Postgres has foreign keys on `code_critiques.artifact_id`; SQLite (used by tests) does not enforce them, so FK problems would only show on Postgres.
- Test execution: **DockerRunner has never run against a real Docker daemon** (it was down); its command line, error handling and cleanup are tested against a faked `docker`, and the sandbox image (`sandbox/Dockerfile`) has never been built. Real pytest execution is verified only through LocalRunner. Only Python/pytest is supported (a frontend-only change fails with "no runnable tests"); no network in the sandbox, so generated code may only use what the image installs; no Playwright/e2e evidence; unit vs integration split is by test name/path containing "integration".
- Retry counter is shared across critique blocks, test failures and agent errors within a region (3 total, not 3 each).

## Command Center (launched)
- Run: `docker compose up -d`, then `python -m uvicorn api:create_app --factory --port 8000`, open http://127.0.0.1:8000.
- `demo_seed.py` fills an EMPTY database with sample asks/tickets (an escalated ticket, a retrying one, stage timeouts, a finished ask).
- Postgres: **verified for the first time** (compose up, tables created, seed, read endpoints, decision write with FKs). Found and fixed: SQLAlchemy 2.1 picks the psycopg3 driver for plain `postgresql://` URLs; the store now normalises to psycopg2.
- Dashboard (`ui/index.html`): one dependency-free page. Asks on the left; for the selected ask it shows a dependency graph (tickets as nodes laid out by dependency depth, arrows = 'must be merged first', colour = state), that ask's own 'needs your decision' box (Accept and move on / Send back, each stating the destination), and tickets by feature. Verified by running the page's real JS against the live server in jsdom (graph nodes/edges, no overlaps, per-ask scoping, detail panel, no script errors) and a headless Edge screenshot. Not tried on other browsers or small screens. No auth: keep it on 127.0.0.1.
- UI deliberately does not show the backend's 'too slow' (stage timeout) signal or a global stuck list; the API still computes them (`/api/stuck`, `/api/decisions`). Human guidance text is stored but no agent reads it yet.
