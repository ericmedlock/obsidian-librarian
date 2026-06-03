from librarian.rag.graph import VaultGraph


def test_wal_mode_enabled(cfg):
    g = VaultGraph(cfg)
    mode = g._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    g.close()


def test_busy_timeout_set(cfg):
    g = VaultGraph(cfg)
    timeout = g._conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout == 5000
    g.close()
