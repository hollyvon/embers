# memory-archive

Local-first searchable archive over Claude Code session transcripts: SQLite/FTS5 exact-recall layer + hierarchical DAG summary layer. See [SPEC.md](SPEC.md).

**Cardinal rule:** this repository contains tooling and synthetic fixtures ONLY. Real transcript data never enters the repo in any form — not in code, tests, fixtures, issues, or commit messages. Development runs against `fixtures/`; the operator runs the finished tools locally against real data.
