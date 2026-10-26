"""The archive holds private conversations: the DB directory and files must be owner-only."""
import os
import stat
import sys

import pytest

from embers.ingest import open_db

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX modes only")


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_open_db_creates_private_dir_and_files(tmp_path):
    d = tmp_path / "archive"
    db = d / "mem.db"
    conn = open_db(str(db))
    conn.execute("CREATE TABLE IF NOT EXISTS t(x)")
    conn.commit()
    assert _mode(d) == 0o700
    assert _mode(db) == 0o600
    for side in ("-wal", "-shm"):
        p = str(db) + side
        if os.path.exists(p):
            assert _mode(p) == 0o600
    conn.close()


def test_open_db_tightens_existing_loose_db(tmp_path):
    d = tmp_path / "archive"
    d.mkdir(mode=0o755)
    db = d / "mem.db"
    db.write_bytes(b"")
    os.chmod(db, 0o644)
    conn = open_db(str(db))
    conn.close()
    assert _mode(d) == 0o700
    assert _mode(db) == 0o600
