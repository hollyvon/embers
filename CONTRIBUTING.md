# Contributing to Embers

Thanks for helping. Embers is small and the rules are few.

## The one rule: fixtures only

Never add a real transcript, a real name, or a real home path to this repo. That covers code, tests, fixtures, docs, issues, pull requests, and commit messages. Do not paste "just a snippet" of your own sessions either. If a bug only shows up on your data, reduce it to a synthetic case and describe the shape of the record, not its content. Anything beyond the `~/.claude/projects` convention does not belong in the tree.

## Running the tests

```bash
git clone https://github.com/hollyvon/embers && cd embers
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
```

Python 3.9 or newer, no runtime dependencies. The suite generates a synthetic corpus with `fixtures/gen.py` (seeded and deterministic) and runs against that. The full run takes a couple of minutes. CI runs it on Python 3.9 and 3.12.

To poke at the CLI on synthetic data:

```bash
python -m fixtures.gen --out /tmp/fx
embers --db /tmp/fx/t.db ingest /tmp/fx/root1 /tmp/fx/root2 --index /tmp/fx/sessions-index
embers --db /tmp/fx/t.db search quokka
```

## Pull request expectations

- Keep the suite green. Add tests for anything you add or fix.
- Keep a PR to one change. Say what it does and why in the description.
- The offline `stub` summarizer is the default for tests. Tests and CI never call a real model or the network.
- Raw sources are only ever read. Nothing may modify or prune a transcript.
- Update `docs/REFERENCE.md` and `skills/embers/SKILL.md` when behavior they describe changes.
- Adapters for other harnesses are a separate effort. Please open an issue to discuss before sending one.

## Reporting problems

Open an issue with the command you ran, the error text, and your Python and Embers versions (`embers --version`). Do not paste transcript content. See [SECURITY.md](SECURITY.md) for security issues.
