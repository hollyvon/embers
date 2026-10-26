# Embers reference

The long-form manual: every command, the summary DAG, the build backends and the flinch detector. Start with the [README](../README.md). See also [SPEC.md](../SPEC.md) and [P0-schema-recon.md](P0-schema-recon.md).

**Cardinal rule:** this repository contains tooling and synthetic fixtures ONLY. Real transcript data never enters the repo in any form — not in code, tests, fixtures, issues, or commit messages. Development runs against `fixtures/`; the operator runs the finished tools locally against real data.

## Install

Python 3.9+ with SQLite FTS5 (standard in CPython builds). No runtime dependencies.

```bash
pip install -e .            # provides the `embers` command (and the `mem` alias)
pip install -e '.[dev]'     # adds pytest
# or skip installing and use ./mem from a checkout
```

## One-line local run (synthetic data)

```bash
python -m fixtures.gen --out /tmp/fx && embers ingest /tmp/fx/root1 /tmp/fx/root2 --index /tmp/fx/sessions-index && embers search quokka
```

Tests: `pytest`.

## Using it on your own transcripts

```bash
embers ingest ~/.claude/projects <second-root> --index <sessions-index-dir>   # incremental; re-run any time
embers search "exact phrase or fts AND query" --class human,assistant,thinking --since 2026-03-01 --limit 10
embers session <id-or-prefix-or-window-number> [--around <hit-id>] [--thread] [--full]
embers windows        # window map from [28] … / Context window #28 titles, sorted by date; untitled sessions show as (untitled)
embers sessions       # list sessions
embers stats
```

The database lives at `~/.memory-archive/mem.db` (override with `--db` or `$MEM_DB`). `--json` on any command prints one JSON object per line. Sources are only ever read.

## What is where

| Path | Purpose |
|---|---|
| `fixtures/gen.py` | Seeded synthetic corpus generator (P0 §f): two roots, all 15 record types, compaction, resume chains, subagents, edge cases, `sessions-index/`, `truth.json`. `--legacy-summary` adds legacy summary records. |
| `embers/records.py` | Pure record logic: block extraction and classes, the human-prompt predicate. |
| `embers/ingest.py` | Incremental ingester, stitching, SQLite schema + FTS5. |
| `embers/dag.py` | Layer D storage: node tables, leaf chunking, session/thread/month structure, staleness sync. |
| `embers/build.py` | Prompt packs and the build loop (summarizer -> flinch detector -> accept / rerun / manual), the worker pool, manual entry. |
| `embers/summarize.py` | `Summarizer` interface, offline `StubSummarizer`, budget-capped `AnthropicSummarizer`. |
| `embers/flinch.py` | The summary-quality guard (three checks, `Thresholds`, `EVASION_MARKERS`). |
| `embers/cli.py`, `embers` (alias `mem`) | The `embers` (alias `mem`) CLI. |
| `tests/` | pytest suite run against the generated fixtures. |
| `tools/schema_probe.py` | P0 reconnaissance probe (operator-side). |

## Behaviour notes

- **Stitching.** A logical thread is the connected component of files sharing message uuids. Messages are deduped by uuid; the canonical owner is the owner file with the earliest first timestamp. `embers session <id>` shows the file as written; `--thread` shows the deduped thread. `compact_boundary` records keep `logicalParentUuid`.
- **Block classes.** `human`, `assistant`, `thinking` (own class, always indexed), `tool_use`, `tool_result`, `attachment`, plus `system` and `summary` so nothing with text is dropped.
- **Skipped payloads.** Base64 images/documents, thinking signatures and the top-level `toolUseResult` duplicate are stored as a pointer (`file:line:jsonpath`) and byte count only.
- **Incremental.** `(path, mtime, size)` watermark; grown files resume from the last complete line; a partial last line is retried next run; a shrunk or rewritten file is re-parsed.

## Phase 2: the summary DAG (Layer D)

Summaries are an index into the raw record, never a replacement. `embers build` adds a DAG of hierarchical summaries to the same SQLite file; `embers grep` / `describe` / `expand` navigate it. Phase 1 data is untouched, `embers ingest` keeps working on the same database, and the new tables (`nodes`, `node_sources`, `nodes_fts`) are created by the first `embers build`.

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
embers build --dry-run                 # plan: nodes, source bytes, estimated tokens; writes nothing
embers build --all --backend stub      # extractive stand-in summarizer, deterministic
embers build --level leaf --limit 50   # or in slices; --thread ID scopes to one thread
embers describe window 10              # summary at the window's altitude, children one per line (also: "window 10", w10, 10)
embers expand <node-id>                # one level down; at a leaf, the raw messages
embers grep "some phrase" --level session
embers flags                           # flagged / manual nodes with their detector metrics
```

`build` is idempotent: only pending nodes are built, each node is committed on its own, and an interrupted run resumes where it stopped. After you ingest new data, the next `build` rebuilds only the leaves that changed and the ancestors above them. `--chunk-bytes` (or `$MEM_CHUNK_BYTES`) sets the leaf budget; changing it rebuilds the leaves. `--json` works on every command.

### Point it at the real backend (on your machine)

```bash
pip install 'anthropic>=0.125'              # optional dependency, imported only by this backend; older SDKs reject output_config
export ANTHROPIC_API_KEY=...                # set in your shell; the tool never stores or logs it
embers build --all --backend anthropic --budget-usd 5 --dry-run   # estimate first
embers build --all --backend anthropic --budget-usd 5
```

- `--budget-usd` is mandatory for this backend (or `$MEM_BUDGET_USD`). Before every call the cost is estimated from the prompt size plus the maximum output; a call that could push spend past the budget is not made, the build stops cleanly (exit code 3), and the next run continues from there. Spend is computed from reported token usage. Nothing here enables overage; if you want a hard ceiling at the account level, set that in your Anthropic console too.
- Two tiers: `small` for leaves, `mid` for session/thread/month nodes. Defaults are `claude-haiku-5-5` and `claude-sonnet-5-5`; override with `$MEM_MODEL_SMALL` / `$MEM_MODEL_MID`. The rerun tier is configured per level, see "Reruns, refusals" below. Prices for the cost estimate live in `PRICES` in `embers/summarize.py`; an unlisted model is priced conservatively.
- A user-scoped key (`sk-ant-usr…`) must name a workspace on every call: set `$MEM_WORKSPACE_ID` to the workspace id (`wrkspc_…`, from the Console under Settings → Workspaces) and the backend sends it as the `anthropic-workspace-id` header. Workspace-scoped keys need nothing extra.
- The SDK's own two retries apply; the tool adds none. Select the backend with `--backend` or `$MEM_BACKEND` (default `stub`).
- The tests and the cloud never call this backend.

### Concurrent builds

```bash
embers build --level leaf --workers 8              # or $MEM_WORKERS; default 1 = one call at a time
```

Summarizing is network-bound (about 9 s per leaf call on the small model), so a first pass over thousands of leaves takes hours at one call at a time. `--workers N` keeps up to N calls in flight within a level. Only the summarizer call and the flinch evaluation run in worker threads; SQLite has a single writer, the main thread, which stores and commits each node as it completes (completion order, one commit per attempt, exactly as with one worker). The ratio baseline is read from the database once per 25 accepted nodes, not per node.

- A level starts only after every node of the level below it has finished, so session/thread/month nodes still wait for all their children. Upper levels use the workers too.
- `--limit N` means at most N nodes are started and stored; calls already in flight finish their node first.
- **Budget.** The Anthropic backend reserves the worst-case cost of a call under a lock before making it, so N calls in flight can never jointly exceed `--budget-usd`; the reservation is replaced by the real cost afterwards (or released if the call fails). Near the cap that is deliberately pessimistic: the build stops cleanly (exit code 3) a few calls early rather than late.
- **Interruption.** Ctrl-C or an exception leaves the database consistent: finished nodes are stored, unfinished ones are untouched (still pending, or flagged awaiting their rerun) and the next run picks them up. After Ctrl-C, calls already on the wire run to completion in the background and their results are discarded, so the process may take a few seconds to exit. An exception in a worker stops new dispatch, lets the other in-flight calls finish and be stored, then re-raises; the failed node stays as it was.
- Mind your account's rate limits: a high `--workers` can turn into SDK retries and 429s. Start with 4-8.

The operator pattern for a first real run:

```bash
embers build --level leaf --workers 8 --limit 200 --backend anthropic --budget-usd 5   # a slice
embers flags                                      # inspect what the detector flagged / sent to manual
embers build --level leaf --workers 8 --limit 200 --backend anthropic --budget-usd 5   # continue (idempotent)
# ... repeat, then the upper levels:
embers build --all --workers 8 --backend anthropic --budget-usd 5
```

### Reruns, refusals and manual summaries

A rejected first attempt is rerun once on the **rerun tier**, which is configurable per level. The default is the `mid` model for both; leaves can be pointed elsewhere, for example when the mid model refuses leaves that contain thinking blocks:

| Level | Env | Flag |
|---|---|---|
| leaf | `MEM_MODEL_RERUN_LEAF` | `--rerun-model-leaf MODEL` |
| session, thread, month | `MEM_MODEL_RERUN_UPPER` | `--rerun-model-upper MODEL` |

The flag beats the env var. The tier and model of every attempt are recorded in the node's history (`embers flags --json`).

If the rerun ends with `stop_reason=refusal`, that does not silently use up the node's second attempt: the refusal is recorded in the history (with its category), and the node gets **one more try on the small tier at effort `medium`** before it is marked `manual`. The history entry for that try says why (`reason: rerun refused ...`, `effort: medium`). A node therefore has at most three attempts, and the third only after a refused rerun. An interrupted build resumes at whichever attempt was next.

```bash
embers flags                                           # what needs a human, and why
embers manual <node-id> --text "..."                   # enter the summary (or --file PATH, or --stdin)
embers flags --export DIR                              # one <node-id>.md per flagged/manual node: span, metrics, raw source
#   write DIR/<node-id>.summary.md for each one you want to resolve, then
embers manual --import DIR                             # stores every <node-id>.summary.md
embers build                                           # rebuilds the nodes above the ones you changed
```

`embers manual` works on any node, not only `manual` ones. It sets the summary, backend `manual`, model `human`, status `ok`, adds the text to the search index, keeps the metrics and history, and appends a `{"tier": "manual"}` history entry. Because the ancestors of a node that was `manual` were built over a "summary unavailable" placeholder, the next `embers build` sees the changed child and rebuilds them.

### The flinch detector

Small models sometimes refuse, soften or skip difficult source material instead of summarizing it. Every summary, from any backend, passes three independent checks before it is accepted (`embers/flinch.py`; all numbers in one `Thresholds` dataclass):

| Check | Fails when |
|---|---|
| `ratio` | summary bytes / source bytes is outside the fences. Fences come from the median and IQR of already accepted nodes of the same level once at least 20 exist (before that, fixed defaults). A summary far shorter than the baseline is the classic evasion signature. Sources under 3 KB are not tested; an empty summary always fails. |
| `evasion` | the text contains any phrase in `EVASION_MARKERS` (case-insensitive: "I can't", "as an AI", "sensitive", "let's focus on", "omitted", ...). Extend the tuple in `flinch.py`; ordinary compounds such as "case-sensitive" are exempt via `EVASION_BENIGN`. |
| `coverage` | fewer than 60% of the source's 30 most frequent proper nouns (capitalized words mid-sentence, ALL-CAPS tokens of 3+ characters, camelCase) appear in the summary. |

When the source contains a thinking block, the tighter `Thresholds.high_sensitivity()` set applies (coverage 75%, narrower ratio fences); the flag propagates up to every ancestor.

A rejected summary marks the node `flagged` and is rerun once on the rerun tier (default `mid`) with a note saying what failed. A second rejection (after the refusal fallback described above, if it applies) marks it `manual` and the build moves on; upper nodes then see "summary unavailable" for that part instead of the rejected text, and rejected text is never added to the search index.

Reading `embers flags`:

```
Lcf732936d079  leaf  manual  attempts=2  2026-03-15  sensitivity=high  thresholds=high
    ratio 0.058 outside [0.091, 0.380] (too short, baseline)
    evasion markers: "I can't"
    coverage 0.20 < 0.75 of 12 nouns, missing: Zorblax, Quibble, ...
    last output: ...
```

Each line names a failed check with its score and the threshold it missed. `flagged` means a rerun is pending (an interrupted build); `manual` means the attempts failed and the node needs a human summary or a different model. `embers flags --json` gives the same data, `embers flags --export DIR` writes the source next to the metrics for each node, and `embers expand <id>` shows the raw messages.
