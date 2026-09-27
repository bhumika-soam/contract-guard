## What We Built

ContractGuard is a working, end-to-end pipeline — not a mockup. Three scripted
"drift" branches each demonstrate a real breaking API change, caught, explained,
patched, and verified automatically:

| Scenario | Change | Endpoints affected | Files auto-patched |
|---|---|---|---|
| `drift/field-renamed` | `title` → `name` | 4 (list GET, POST, single GET, PUT) | 4 |
| `drift/type-changed` | `description`: string → array | 3 | 7 |
| `drift/endpoint-removed` | `DELETE /items/{id}` removed | — | 9 |

**Pipeline stages, and where to find each one:**

1. **Diff Agent** (`backend/contractguard/diff_agent.py`) — snapshots
   `openapi.json` before/after a change, walks every endpoint's request/response
   schema, and classifies each difference (renamed, type-changed, removed, etc.)
   into a structured `diff_report.json` — including every endpoint a single
   field touches, not just the first one found.

2. **Orchestrator + subagents** (`backend/contractguard/orchestrator.py`,
   `backend/contractguard/subagents/`) — three IBM Bob 2.0 subagents, run via
   Agent mode:
   - **Impact Agent** — searches the frontend for every real usage of the
     changed field/endpoint (parallelized: up to 4 concurrent Bob calls across
     candidate files, with automatic fallback to a full search).
   - **Repair Agent** — applies the actual code patch: updated types, fixed
     call sites, adjusted rendering.
   - **Verify Agent** — runs the build and test suite; on failure, the error
     is fed back into the Repair Agent for up to 3 bounded retries before the
     pipeline honestly reports a failed patch rather than faking success.

3. **Report viewer** (`frontend/src/routes/impact-report.tsx`) — renders each
   detected break as a plain-English card: what changed, why it matters,
   which files were touched, and whether the fix was verified — built for a
   non-specialist (PM, junior dev) to understand at a glance, not just for
   whoever wrote the diff tool.

**IBM Bob 2.0 evidence:** task-session screenshots from all three team
members are in `evidence/bob-task-sessions/`. See
`docs/bob-usage-statement.md` for a full breakdown of where and how Bob's
Agent mode, subagents, and parallel tasks were used.