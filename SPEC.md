# Memory Archive — engineering spec (v0.1)

A local-first searchable archive over Claude Code session transcripts (JSONL). Two layers: full-text search for exact recall, hierarchical DAG summaries for orientation. **All real transcript data stays on the owner's machine, forever. This repo holds tooling and synthetic fixtures only.**

## Architecture

**Layer S — Search (Phase 1, standalone-valuable):**
SQLite + FTS5 over every transcript message. Answers "when was X discussed/decided" with ranked hits + session/date/context.

**Layer D — Depth (Phase 2):**
A DAG of hierarchical summaries (leaf = session-chunk → session → month). Navigation: `grep` (search summaries), `describe` (what's at this node), `expand` (descend toward raw). Summaries are an index, never a replacement — raw is permanent.

**Query flow (the hybrid's point):** FTS finds the needle → DAG situates it (which session, what arc) → optional expand to raw lines. Speed finds it; depth explains it; raw settles it.

## Components

1. **`ingest.py`** — walks a transcripts root (`~/.claude/projects/*/*.jsonl` convention), normalizes (session id, ts, role, text), writes SQLite (`sessions`, `messages`, FTS5). Incremental via (path, mtime, size) watermark. Append-only; never touches sources. Must handle: tool-call noise (index prose + tool-result text, skip base64/binary payloads) and compaction-continuation chains (continued sessions = one logical thread, stitched via continuation metadata).
2. **`mem` CLI** — `mem search "query" [--since DATE]` · `mem session <id> [--around <hit>]` · `mem sessions` (list/map). Later: `mem describe` / `mem expand` (Layer D).
3. **DAG builder** (Phase 2) — LCM-style hierarchical summarizer (cf. MIT-licensed lossless-claw as prior art). Leaf summaries via Anthropic Agent SDK on a small model; upper layers on a mid model. Hard stop at plan limits; never enables overage billing.
4. **Summary quality guard ("flinch-detector", REQUIRED):** per-summary checks before acceptance — (a) compression-ratio outlier vs corpus baseline; (b) evasion-marker heuristics in summary text; (c) coverage probe (source proper-noun survival rate). Flagged → re-run on a larger model; twice-flagged → marked for manual summarization. Rationale: small-model refusal/sanitization tendencies must not silently degrade coverage of difficult source material.

## Hard rules

- Development and tests run ONLY against `fixtures/` — synthetic JSONL mimicking Claude Code's schema. **No real transcript ever enters this repo, its issues, or its CI.**
- Raw transcripts are never modified, pruned, or replaced by the tool.
- Summarization backends: Anthropic models or local models only; configurable, no third-party remote default.
- All databases are produced and consumed locally by the operator.

## Phases

- **P0:** schema recon (operator-side, informs the fixture generator) → `fixtures/README.md` documents the JSONL shape.
- **P1:** fixture generator + ingest + FTS + `mem` CLI + tests. Deliverable: standalone-useful search.
- **P2:** DAG builder + quality guard, exercised on fixtures; operator runs against real corpus locally.
- **P3:** full-corpus runs + an in-session usage guide. Operator-side.

## Addendum (2026-10-19) — from the Phase 0 recon
- **Multiple corpus roots.** The ingester takes a LIST of root directories, each laid out as `<root>/<projdir>/<session>.jsonl` (+ `<projdir>/<session>/subagents/agent-<id>.jsonl`). Real deployments have at least two roots (a primary transcript folder and a sandbox-style folder whose `cwd` values look like `/sessions/<adjective-noun>/`). Fixtures must include ≥2 roots.
- **Window map.** A separate metadata index exists outside the roots: one small JSON per session with `createdAt`, `title`, `cwd`, and `cliSessionId` (= the JSONL filename stem). `mem windows` builds the human-numbered window map from these titles. Fixtures: emit a parallel `sessions-index/` of such files.
- **Thinking blocks are indexed, in their own class** (`block_class = thinking`), never dropped; search includes/excludes the class explicitly; the flinch-detector treats this class as highest sensitivity.
- **No `summary` records and no top-level `tool_result` records exist in current schema** (see docs/P0-schema-recon.md §c). Compaction is in-file (`compact_boundary` + `isCompactSummary`); resume is prefix-copy; stitch by uuid overlap. Keep tolerance for legacy `summary` records.
