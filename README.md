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
| `memarchive/dag.py` | Layer D storage: node tables, leaf chunking, session/thread/month structure, staleness sync. |
| `memarchive/build.py` | Prompt packs and the build loop (summarizer -> flinch detector -> accept / rerun / manual). |
| `memarchive/summarize.py` | `Summarizer` interface, offline `StubSummarizer`, budget-capped `AnthropicSummarizer`. |
| `memarchive/flinch.py` | The summary-quality guard (three checks, `Thresholds`, `EVASION_MARKERS`). |
| `memarchive/cli.py`, `mem` | The `mem` CLI. |
| `tests/` | pytest suite run against the generated fixtures. |
| `tools/schema_probe.py` | P0 reconnaissance probe (operator-side). |

## Behaviour notes

- **Stitching.** A logical thread is the connected component of files sharing message uuids. Messages are deduped by uuid; the canonical owner is the owner file with the earliest first timestamp. `mem session <id>` shows the file as written; `--thread` shows the deduped thread. `compact_boundary` records keep `logicalParentUuid`.
- **Block classes.** `human`, `assistant`, `thinking` (own class, always indexed), `tool_use`, `tool_result`, `attachment`, plus `system` and `summary` so nothing with text is dropped.
- **Skipped payloads.** Base64 images/documents, thinking signatures and the top-level `toolUseResult` duplicate are stored as a pointer (`file:line:jsonpath`) and byte count only.
- **Incremental.** `(path, mtime, size)` watermark; grown files resume from the last complete line; a partial last line is retried next run; a shrunk or rewritten file is re-parsed.

## Phase 2: the summary DAG (Layer D)

Summaries are an index into the raw record, never a replacement. `mem build` adds a DAG of hierarchical summaries to the same SQLite file; `mem grep` / `describe` / `expand` navigate it. Phase 1 data is untouched, `mem ingest` keeps working on the same database, and the new tables (`nodes`, `node_sources`, `nodes_fts`) are created by the first `mem build`.

```
leaf     a run of one session's messages, 24 KB of prose (human/assistant/thinking) by default;
         a prompt is never split from the reply that answers it; always breaks at a compact_boundary
session  all leaves of one session file (subagent sessions get their own node)
thread   all sessions of a stitched thread. A one-session thread has no node of its own:
         its session node doubles as the thread node
month    all thread nodes whose first timestamp falls in that calendar month (UTC)
```

Tool noise (tool calls and results, attachments, system records) is not transcribed into what the summarizer sees; it is listed (tool name plus the first 120 characters). Thinking blocks are included and tagged `[thinking]`. Upper nodes summarize their children's summaries, not the raw text.

### Build with the offline stub (no network, no key)

```bash
mem build --dry-run                 # plan: nodes, source bytes, estimated tokens; writes nothing
mem build --all --backend stub      # extractive stand-in summarizer, deterministic
mem build --level leaf --limit 50   # or in slices; --thread ID scopes to one thread
mem describe window 10              # summary at the window's altitude, children one per line
mem expand <node-id>                # one level down; at a leaf, the raw messages
mem grep "some phrase" --level session
mem flags                           # flagged / manual nodes with their detector metrics
```

`build` is idempotent: only pending nodes are built, each node is committed on its own, and an interrupted run resumes where it stopped. After you ingest new data, the next `build` rebuilds only the leaves that changed and the ancestors above them. `--chunk-bytes` (or `$MEM_CHUNK_BYTES`) sets the leaf budget; changing it rebuilds the leaves. `--json` works on every command.

### Point it at the real backend (on your machine)

```bash
pip install anthropic                       # optional dependency, imported only by this backend
export ANTHROPIC_API_KEY=...                # set in your shell; the tool never stores or logs it
mem build --all --backend anthropic --budget-usd 5 --dry-run   # estimate first
mem build --all --backend anthropic --budget-usd 5
```

- `--budget-usd` is mandatory for this backend (or `$MEM_BUDGET_USD`). Before every call the cost is estimated from the prompt size plus the maximum output; a call that could push spend past the budget is not made, the build stops cleanly (exit code 3), and the next run continues from there. Spend is computed from reported token usage. Nothing here enables overage; if you want a hard ceiling at the account level, set that in your Anthropic console too.
- Two tiers: `small` for leaves, `mid` for session/thread/month nodes and for every rerun. Defaults are `claude-haiku-5-5` and `claude-sonnet-5-5`; override with `$MEM_MODEL_SMALL` / `$MEM_MODEL_MID`. Prices for the cost estimate live in `PRICES` in `memarchive/summarize.py`; an unlisted model is priced conservatively.
- A user-scoped key (`sk-ant-usr…`) must name a workspace on every call: set `$MEM_WORKSPACE_ID` to the workspace id (`wrkspc_…`, from the Console under Settings → Workspaces) and the backend sends it as the `anthropic-workspace-id` header. Workspace-scoped keys need nothing extra.
- The SDK's own two retries apply; the tool adds none. Select the backend with `--backend` or `$MEM_BACKEND` (default `stub`).
- The tests and the cloud never call this backend.

### The flinch detector

Small models sometimes refuse, soften or skip difficult source material instead of summarizing it. Every summary, from any backend, passes three independent checks before it is accepted (`memarchive/flinch.py`; all numbers in one `Thresholds` dataclass):

| Check | Fails when |
|---|---|
| `ratio` | summary bytes / source bytes is outside the fences. Fences come from the median and IQR of already accepted nodes of the same level once at least 20 exist (before that, fixed defaults). A summary far shorter than the baseline is the classic evasion signature. Sources under 1.5 KB are not tested; an empty summary always fails. |
| `evasion` | the text contains any phrase in `EVASION_MARKERS` (case-insensitive: "I can't", "as an AI", "sensitive", "let's focus on", "omitted", ...). Extend the tuple in `flinch.py`; ordinary compounds such as "case-sensitive" are exempt via `EVASION_BENIGN`. |
| `coverage` | fewer than 60% of the source's 30 most frequent proper nouns (capitalized words mid-sentence, ALL-CAPS tokens of 3+ characters, camelCase) appear in the summary. |

When the source contains a thinking block, the tighter `Thresholds.high_sensitivity()` set applies (coverage 75%, narrower ratio fences); the flag propagates up to every ancestor.

A rejected summary marks the node `flagged` and is rerun once on the `mid` tier with a note saying what failed. A second rejection marks it `manual` and the build moves on; upper nodes then see "summary unavailable" for that part instead of the rejected text, and rejected text is never added to the search index.

Reading `mem flags`:

```
Lcf732936d079  leaf  manual  attempts=2  2026-03-15  sensitivity=high  thresholds=high
    ratio 0.058 outside [0.091, 0.380] (too short, baseline)
    evasion markers: "I can't"
    coverage 0.20 < 0.75 of 12 nouns, missing: Zorblax, Quibble, ...
    last output: ...
```

Each line names a failed check with its score and the threshold it missed. `flagged` means a rerun is pending (an interrupted build); `manual` means both attempts failed and the node needs a human summary or a different model. `mem flags --json` gives the same data, and `mem expand <id>` shows the raw messages to summarize by hand.
