"""Shared fixtures: a generated corpus and an ingested DB, built once per test session."""
import json
import os

import pytest

from fixtures.gen import generate
from memarchive.ingest import Ingester, open_db

ROOTS = ("root1", "root2")


class Corpus:
    def __init__(self, out, truth):
        self.out = out
        self.truth = truth
        self.roots = [os.path.join(out, r) for r in ROOTS]
        self.index = os.path.join(out, "sessions-index")

    def path(self, rel):
        return os.path.join(self.out, rel)

    def sid(self, rel):
        return self.truth["files"][rel]["session_id"]

    def records(self, rel):
        out = []
        with open(self.path(rel), "rb") as f:
            for raw in f:
                try:
                    out.append(json.loads(raw))
                except ValueError:
                    out.append(None)
        return out


@pytest.fixture(scope="session")
def corpus(tmp_path_factory):
    out = str(tmp_path_factory.mktemp("fx"))
    truth = generate(out, seed=1234)
    return Corpus(out, truth)


@pytest.fixture(scope="session")
def corpus_legacy(tmp_path_factory):
    out = str(tmp_path_factory.mktemp("fx-legacy"))
    truth = generate(out, seed=1234, legacy_summary=True)
    return Corpus(out, truth)


class DB:
    """A connection plus the stats of the ingest that built it."""

    def __init__(self, path, stats, conn):
        self.path = path
        self.stats = stats
        self.conn = conn

    def __getattr__(self, name):
        return getattr(self.conn, name)


@pytest.fixture(scope="session")
def db(corpus, tmp_path_factory):
    path = str(tmp_path_factory.mktemp("db") / "mem.db")
    conn = open_db(path)
    stats = Ingester(conn).ingest(corpus.roots, index_dir=corpus.index)
    yield DB(path, stats, conn)
    conn.close()
