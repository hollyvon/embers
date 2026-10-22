# Embers: notes for agents

Embers is lossless memory for Claude Code: it ingests session transcripts into a local SQLite archive with phrase search, a summary tree, and a flinch detector.
It reads Claude Code transcripts only; adapters for other harnesses are not built yet.

Run `embers install-skill`, then read the skill at `~/.claude/skills/embers/SKILL.md`. It is the operating manual (also in `skills/embers/SKILL.md`).

## The law

Fixtures only. Never put a real transcript, a real name, or a real home path into this repo, its tests, issues, PRs, or commit messages. Tests run on synthetic data from `python -m fixtures.gen`. Transcripts never leave the user's machine.
