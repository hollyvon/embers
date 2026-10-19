# memory-archive

Local-first searchable archive over Claude Code session transcripts: SQLite/FTS5 exact-recall layer (this repo, Phase 1) + hierarchical DAG summary layer (Phase 2). See [SPEC.md](SPEC.md) and [docs/P0-schema-recon.md](docs/P0-schema-recon.md).

**Cardinal rule:** this repository contains tooling and synthetic fixtures ONLY. Real transcript data never enters the repo in any form — not in code, tests, fixtures, issues, or commit messages. Development runs against `fixtures/`; the operator runs the finished tools locally against real data.

## Install

Python 3.9+ with SQLite FTS5 (standard in CPython builds). No runtime dependencies.

```bash
pip install -e .            # provides the `mem` command
pip install -e '.[dev]'     # adds pytest
# or skip installing and use ./mem from a checkout
```

## One-line local run (synthetic data)

```bash
python -m fixtures.gen --out /tmp/fx && mem ingest /tmp/fx/root1 /tmp/fx/root2 --index /tmp/fx/sessions-index && mem search quokka
```

Tests: `pytest`.

## Using it on your own transcripts

```bash
mem ingest ~/.claude/projects <second-root> --index <sessions-index-dir>   # incremental; re-run any time
mem search "exact phrase or fts AND query" --class human,assistant,thinking --since 2026-03-01 --limit 10
mem session <id-or-prefix-or-window-number> [--around <hit-id>] [--thread] [--full]
mem windows        # window map from [28] … / Context window #28 titles
mem sessions       # list sessions
mem stats
```

The database lives at `~/.memory-archive/mem.db` (override with `--db` or `$MEM_DB`). `--json` on any command prints one JSON object per line. Sources are only ever read.

## What is where

| Path | Purpose |
|---|---|
| `fixtures/gen.py` | Seeded synthetic corpus generator (P0 §f): two roots, all 15 record types, compaction, resume chains, subagents, edge cases, `sessions-index/`, `truth.json`. `--legacy-summary` adds legacy summary records. |
| `memarchive/records.py` | Pure record logic: block extraction and classes, the human-prompt predicate. |
| `memarchive/ingest.py` | Incremental ingester, stitching, SQLite schema + FTS5. |
| `memarchive/cli.py`, `mem` | The `mem` CLI. |
| `tests/` | pytest suite run against the generated fixtures. |
| `tools/schema_probe.py` | P0 reconnaissance probe (operator-side). |

## Behaviour notes

- **Stitching.** A logical thread is the connected component of files sharing message uuids. Messages are deduped by uuid; the canonical owner is the owner file with the earliest first timestamp. `mem session <id>` shows the file as written; `--thread` shows the deduped thread. `compact_boundary` records keep `logicalParentUuid`.
- **Block classes.** `human`, `assistant`, `thinking` (own class, always indexed), `tool_use`, `tool_result`, `attachment`, plus `system` and `summary` so nothing with text is dropped.
- **Skipped payloads.** Base64 images/documents, thinking signatures and the top-level `toolUseResult` duplicate are stored as a pointer (`file:line:jsonpath`) and byte count only.
- **Incremental.** `(path, mtime, size)` watermark; grown files resume from the last complete line; a partial last line is retried next run; a shrunk or rewritten file is re-parsed.
