# Embers: notes for agents

Embers is lossless memory for Claude Code: it ingests session transcripts into a local SQLite archive with phrase search, a summary tree, and a flinch detector.
It reads Claude Code transcripts only; adapters for other harnesses are not built yet.

Run `embers install-skill`, then read the skill at `~/.claude/skills/embers/SKILL.md`. It is the operating manual (also in `skills/embers/SKILL.md`).

## The law

Fixtures only. Never put a real transcript, a real name, or a real home path into this repo, its tests, issues, PRs, or commit messages. Tests run on synthetic data from `python -m fixtures.gen`. Transcripts never leave the user's machine.

The only optional dependency is the `anthropic` SDK for the real summarizer backend, and it must be `>=0.125`: the backend passes `output_config` (effort), which older SDKs reject with a TypeError. The archive directory and DB files are created owner-only (0700/0600); keep it that way in any change you make.
