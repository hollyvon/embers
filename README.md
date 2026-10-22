Embers: lossless memory for Claude Code.

Embers ingests every Claude Code session transcript into a local SQLite archive you can search by phrase, and builds a summary tree over it so you can read a day, a week, or a month at altitude. A flinch detector flags the places where an earlier instance slid past something, so a small model's softened summary never silently replaces the record.

Your transcripts stay on your machine. Raw sources are only ever read, never modified or pruned.

## 60-second quickstart

Python 3.9+, no runtime dependencies.

```bash
pipx install embers-memory
embers detect                    # find your Claude Code transcripts (reads nothing, ingests nothing)
embers ingest                    # build the archive (incremental; re-run any time)
embers search "phrase"           # find where something was said or decided
```

`embers detect` reports what it finds and roughly how many sessions. `embers ingest` with no arguments ingests the roots `detect` found. The archive lives at `~/.memory-archive/mem.db`; override it with `--db` or `$MEM_DB`. `mem` is kept as a second name for the same command.

## Hand this to your assistant

Give your agent this repo's URL (https://github.com/hollyvon/embers). The agent then:

1. installs the package (`pipx install embers-memory`, or `./setup.sh` from a checkout),
2. runs `embers install-skill`, which copies the bundled skill to `~/.claude/skills/embers/`,
3. reads [AGENTS.md](AGENTS.md) and the installed skill, and
4. runs `embers detect` and `embers ingest` for you, then reports what it found.

After that, ask your assistant what was said or decided in a past session, or for a day, week, or month read.

## What's in the box

- **Full-text search.** SQLite FTS5 over every message: your prompts, assistant replies, thinking blocks (their own class, never dropped), tool use and results. `embers search`, `embers session`, `embers sessions`, `embers windows`.
- **Summary tree.** A DAG of summaries from leaf to session to thread to month. `embers build`, `embers describe`, `embers expand`, `embers grep`. Summaries are an index into the raw record, never a replacement. They are optional, and the offline stub backend needs no network or key.
- **Flinch detector.** Every summary passes three checks (length ratio, evasion phrases, proper-noun coverage) before it is accepted. A failure triggers a rerun on a larger model. A second failure marks the node for manual summarization. `embers flags`.
- **Manual queue.** `embers flags --export DIR` writes each flagged node with its raw source. You or your assistant write the summary, and `embers manual --import DIR` stores it.

The full manual is [docs/REFERENCE.md](docs/REFERENCE.md). The design is in [SPEC.md](SPEC.md).

## Fixtures only

This repository contains tooling and synthetic fixtures only. No real transcript, real name, or real home path ever enters the code, tests, fixtures, issues, pull requests, or commit messages. Development and CI run against generated fixtures (`python -m fixtures.gen`). Never paste a transcript into an issue. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## Status

Embers 0.2.0 reads Claude Code transcripts. Adapters for Hermes, Codex CLI, and Gemini CLI are in progress.

## License

[MIT](LICENSE).

Built by Holly Wren von Heidereich with Ember (Claude).
