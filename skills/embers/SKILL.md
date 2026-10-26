---
name: embers
description: Embers is a local, lossless memory for Claude Code. It ingests every session transcript into a SQLite archive with full-text search and a summary tree over it. Reach for it whenever the user asks what was said or decided in a past session, wants a day, week or month read at altitude, needs to re-ingest after a migration, or sets up Embers for the first time. Also use it when a summary looks suspiciously thin and needs checking against the raw record.
---

# Embers: operating manual

Embers keeps a local SQLite archive of Claude Code session transcripts. Search finds the exact words. The summary tree (a DAG) orients you. The raw record is permanent and settles disputes. Summaries are an index into the raw transcripts, never a replacement for them.

The CLI is `embers`. `mem` is a second name for the same command. `python -m embers` also works.

## The law: transcripts never leave the machine

- Never paste transcript text into an issue, a PR, a commit message, a gist, or any third-party service.
- Never add real transcripts, real names, or real home paths to the Embers repo. Tests and CI use synthetic fixtures only (`python -m fixtures.gen --out DIR`).
- Embers only reads sources. It never modifies or prunes a transcript.
- The only network use is the optional `anthropic` summarizer backend, and it is off by default. It sends the text being summarized to the Anthropic API. Tell the user that before running it on real data.

## Where transcripts live

Claude Code writes one JSONL file per session:

```
~/.claude/projects/<slug>/<session>.jsonl
~/.claude/projects/<slug>/<session>/subagents/agent-<id>.jsonl   # subagent sessions
```

Each root is laid out as `<root>/<projdir>/<session>.jsonl`. Embers takes a list of roots. Compaction happens inside a file, and a resumed session is a prefix copy of an earlier one. Embers stitches such files into one logical thread by shared message uuids.

## Setup

```bash
pipx install embers-memory      # or ./setup.sh from a checkout
embers install-skill            # copies this skill to ~/.claude/skills/embers/
embers detect                   # finds transcript roots, reads no transcript content
```

`embers install-skill` refuses to overwrite an existing install unless you pass `--force`.

## First ingest

```bash
embers ingest                   # ingests the roots that `embers detect` found
embers ingest ROOT [ROOT...]    # or name roots explicitly
embers stats
```

The archive defaults to `~/.memory-archive/mem.db`. Override it with `--db PATH` or `$MEM_DB`. Add `--json` to any command for one JSON object per line.

A good first ingest looks like this:

- `0 bad` lines. A partial last line is normal for a file being written. It is retried on the next run and shows in `embers stats` as a partial tail.
- The session count is close to the number `embers detect` reported. Subagent sessions are counted too.
- Running `embers ingest` again prints mostly `unchanged` files and is much faster than the first. Ingest is incremental by (path, mtime, size). A grown file resumes from its last complete line.
- `embers stats` shows a span of dates that matches the user's history, and non-zero `human`, `assistant` and `thinking` classes.

Skipped payloads (base64 images and documents, thinking signatures, the duplicate top-level `toolUseResult`) are stored as a pointer and a byte count only. That is expected.

## Everyday queries

Search the raw record. It uses FTS5 syntax and falls back to a plain phrase:

```bash
embers search "retry budget" --since 2026-03-01 --class human,assistant --limit 10
```

Read around a hit, a session, or a stitched thread:

```bash
embers session 56d4c7f7 --around 351      # id or prefix, window number, or agent-<id>
embers session 56d4c7f7 --thread          # the deduped thread across files
```

Read at altitude. The summaries must be built first (see below):

```bash
embers describe SESSION-ID      # a session id (prefix works)
embers describe window 10       # also: w10, or just 10
embers describe thread THREAD-ID
embers describe NODE-ID         # node ids are shown by `embers flags`
embers grep "decision" --level session      # full-text search over accepted summaries
embers expand NODE-ID           # one level down; at a leaf, the raw messages
```

Other listings: `embers sessions [--since DATE]`, `embers windows`, `embers stats`.

Rule of thumb: search finds the needle, describe situates it, expand and session settle it. When a user asks what was decided, quote the raw hit, not only the summary.

## Summaries are optional

Search works with no summaries at all. To build the tree:

```bash
embers build --dry-run                    # plan only: nodes, bytes, estimated tokens. Writes nothing.
embers build --all --backend stub         # offline extractive stand-in. No network, no key.
```

For real summaries, use the Anthropic backend on the user's machine:

```bash
pip install 'embers-memory[anthropic]'    # pulls anthropic>=0.125; the backend passes output_config, which older SDKs reject
export ANTHROPIC_API_KEY=...              # the user sets this in their own shell. Never print or store it.
embers build --all --backend anthropic --budget-usd 5 --dry-run   # estimate first
embers build --all --backend anthropic --budget-usd 5
```

- `--budget-usd` (or `$MEM_BUDGET_USD`) is mandatory for this backend. The build stops cleanly with exit code 3 when the next call could pass the cap. Re-run to continue; build is idempotent and resumes.
- `$MEM_PARTICIPANTS='Name1 (the user), Name2 (the assistant)'` tells the summarizer who is talking. Set it locally and never commit it.
- Models: `small` for leaves and `mid` for the upper levels. Defaults are `claude-haiku-5-5` and `claude-sonnet-5-5`. Override with `$MEM_MODEL_SMALL` and `$MEM_MODEL_MID`.
- `--workers N` (or `$MEM_WORKERS`) runs N calls at once. Start with 4 to 8 to stay under rate limits.
- A user-scoped key (`sk-ant-usr...`) needs `$MEM_WORKSPACE_ID` set to the workspace id. It is sent as the `anthropic-workspace-id` header. A workspace-scoped key needs nothing.

Do a slice first, then inspect it:

```bash
embers build --level leaf --workers 8 --limit 200 --backend anthropic --budget-usd 5
embers flags
```

Levels, bottom to top: leaf (about 24 KB of prose from one session), session, thread, month (UTC calendar month). A one-session thread has no node of its own.

## What to check in the first 10 summaries

Open ten with `embers describe` and compare each against `embers expand NODE-ID`. Check that:

1. Real names, files, identifiers and numbers from the raw text appear in the summary.
2. Difficult or uncomfortable passages are stated plainly, not softened, skipped, or replaced by a generic line.
3. Nobody is named from a file path, username or email. See the name pitfall below.
4. The summary is neither a stub-length one-liner nor a near copy of the source.
5. Thinking-bearing leaves (tagged `[thinking]` in the source) are covered as faithfully as the rest.
6. `embers flags` is not mostly full of one cause. A cluster of the same failure points at a model or prompt problem, not at the data.

## Flags and markers

The flinch detector runs three checks on every summary before it is accepted. `embers flags` lists nodes that failed.

| Check | Fails when |
|---|---|
| `ratio` | Summary bytes over source bytes falls outside the fences. Too short is the classic evasion sign. Sources under 3 KB are not tested. |
| `evasion` | The summary contains a phrase from `EVASION_MARKERS`, such as "I can't", "as an AI", "sensitive", "let's focus on", "omitted". |
| `coverage` | Fewer than 60% of the source's 30 most frequent proper nouns appear in the summary. |

Sources containing a thinking block use the tighter `high` sensitivity set (coverage 75%). That flag propagates up to every ancestor.

Node statuses:

- `pending`: not built yet.
- `ok`: accepted and searchable.
- `flagged`: rejected once, a rerun is pending (usually an interrupted build).
- `manual`: attempts failed. The node needs a human summary or a different model. Nodes above it see "summary unavailable" until it is resolved.

The manual loop:

```bash
embers flags --export DIR          # one <node-id>.md per node: span, metrics, raw source
# write DIR/<node-id>.summary.md for each node you resolve, from the raw source
embers manual --import DIR         # or: embers manual NODE-ID --text "..."
embers build                       # rebuilds the nodes above the ones you changed
```

## First-run pitfalls

- **Name inference from paths.** Summaries must use only names that appear in the transcript text, never names guessed from paths, usernames or emails. If you see an invented or path-derived name, set `$MEM_PARTICIPANTS` and rebuild the affected nodes. Use `embers manual` to fix a single one.
- **Quoted markers.** If someone in the transcript said "I can't" and the summary quotes it, the evasion check does not fail it, because a marker that appears verbatim in the source counts as a quotation. `embers flags --json` lists these under `quoted`. A hit under `hits` is a real flinch.
- **A model refusing thinking-bearing leaves.** If the mid model refuses leaves that contain thinking blocks, route reruns to a smaller model: `--rerun-model-leaf MODEL` or `$MEM_MODEL_RERUN_LEAF` (upper levels: `--rerun-model-upper` or `$MEM_MODEL_RERUN_UPPER`). The flag beats the env var. If the rerun ends in a refusal, the node gets one more try on the small tier at effort `medium` before it is marked `manual`. A node has at most three attempts.
- **Truncation.** A summary cut off at the output limit never passes, whatever the checks say. It is rerun with a word cap. The output cap is 16000 tokens by default (`$MEM_MAX_TOKENS`). Separately, tool activity in a leaf is listed rather than transcribed: tool name plus the first 120 characters, capped at 60 items per leaf. The full text stays in the archive; use `embers expand NODE-ID` to read it.
- **Workspace-id header.** A user-scoped API key fails without `$MEM_WORKSPACE_ID`. See the summarizer notes above.
- **Rebuild after changes.** After new ingest, `embers build` rebuilds only the leaves that changed and the nodes above them. Changing `--chunk-bytes` (or `$MEM_CHUNK_BYTES`) rebuilds every leaf.

## Re-ingest after a migration

If transcripts moved to a new machine or root, copy them in the same `<root>/<projdir>/<session>.jsonl` layout, then run `embers ingest NEWROOT`. Files are matched by path, mtime and size. `embers ingest ROOT --rebuild` re-parses every file even if unchanged. Message uuids dedupe, so overlap with earlier ingests does not double count. Afterwards run `embers stats` and `embers build` to refresh the affected summaries.

## When you are unsure

Read `docs/REFERENCE.md` in the Embers repo for the full manual and `SPEC.md` for the design. Adapters for other harnesses are not part of this release. Embers reads Claude Code transcripts only.
