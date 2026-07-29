from app.db import connect, init_db


def test_init_db_creates_tables(tmp_path):
    db_path = tmp_path / "t.db"
    conn = connect(db_path)
    init_db(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = {r["name"] for r in rows}
    assert {"sources", "articles", "drafts", "runs", "settings"} <= names


def test_connect_row_factory(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    conn.execute("INSERT INTO settings(key, value) VALUES('k','v')")
    row = conn.execute(
        "SELECT key, value FROM settings WHERE key='k'").fetchone()
    assert row["key"] == "k" and row["value"] == "v"


def test_init_db_adds_has_video_to_legacy_articles_table(tmp_path):
    conn = connect(tmp_path / "legacy.db")
    conn.execute("CREATE TABLE articles (id INTEGER PRIMARY KEY)")
    init_db(conn)
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(articles)")
    }
    assert "has_video" in columns
