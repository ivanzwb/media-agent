"""Re-run keyword-based classification on all archived articles and
update the DB topic + move archive files to correct directories.

Usage:
    python scripts/reclassify.py
"""
from __future__ import annotations

import re
import sqlite3
import shutil
from pathlib import Path

import frontmatter
import yaml

# ---- paths ----
PROJECT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT / "data"
DB_PATH = DATA_DIR / "media.db"
FEEDS_PATH = PROJECT / "feeds.yaml"

_SHORT_KW_LEN = 4


def load_feeds(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg["topics"]


def keyword_match(
    topics: list[dict], title: str, content_md: str
) -> tuple[str | None, str | None]:
    """Returns (topic_name, matched_keyword) or (None, None)."""
    haystack = f"{title}\n{content_md}".lower()
    for t in topics:
        for kw in t["keywords"]:
            pattern = re.escape(kw.lower())
            if len(kw) < _SHORT_KW_LEN:
                pattern = r"\b" + pattern + r"\b"
            if re.search(pattern, haystack):
                return t["name"], kw
    return None, None


def main():
    topics = load_feeds(FEEDS_PATH)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, title, topic, archive_path FROM articles "
        "WHERE archive_path IS NOT NULL ORDER BY id"
    ).fetchall()

    changed = 0
    moved = 0
    stats: dict[str, int] = {}

    for row in rows:
        aid = row["id"]
        old_topic = row["topic"]
        rel_str = row["archive_path"]
        if not rel_str:
            continue
        abs_path = DATA_DIR / rel_str
        if not abs_path.exists():
            print(f"  ⚠ ID {aid}: file not found: {abs_path}")
            continue

        # read archive file
        post = frontmatter.load(str(abs_path))
        title = str(post.metadata.get("title", row["title"] or ""))
        body = post.content

        new_topic, matched_kw = keyword_match(topics, title, body)
        if new_topic is None:
            new_topic = "uncategorized"

        stats[new_topic] = stats.get(new_topic, 0) + 1

        # always update DB topic (fixes old misclassification)
        conn.execute(
            "UPDATE articles SET topic=? WHERE id=?",
            (new_topic, aid),
        )

        # check if archive_path needs updating
        old_rel = Path(rel_str)
        expected_path = Path("archive") / new_topic / old_rel.name
        expected_str = str(expected_path).replace("\\", "/")

        if rel_str != expected_str:
            # move file
            new_abs = DATA_DIR / expected_path
            new_abs.parent.mkdir(parents=True, exist_ok=True)
            if new_abs.exists():
                print(f"  ⚠ ID {aid}: target exists, skipping move: {expected_str}")
                # still update DB path in case it differs
                conn.execute(
                    "UPDATE articles SET archive_path=? WHERE id=?",
                    (expected_str, aid),
                )
            else:
                # update front-matter topic
                post.metadata["topic"] = new_topic
                new_abs.write_text(
                    frontmatter.dumps(post), encoding="utf-8"
                )
                # remove old file
                abs_path.unlink()
                # clean up empty old directory
                _cleanup_empty_dir(abs_path.parent)
                # update DB
                conn.execute(
                    "UPDATE articles SET archive_path=? WHERE id=?",
                    (expected_str, aid),
                )
                moved += 1

            if old_topic != new_topic:
                changed += 1
                kw_info = f" (kw: {matched_kw})" if matched_kw else ""
                print(
                    f"  ID {aid:5d}: {old_topic or '':20s} -> {new_topic:20s}{kw_info}"
                    f"  | {title[:50]}"
                )
        elif old_topic != new_topic:
            # topic changed but path is already correct (shouldn't normally happen)
            changed += 1
            kw_info = f" (kw: {matched_kw})" if matched_kw else ""
            print(
                f"  ID {aid:5d}: {old_topic or '':20s} -> {new_topic:20s}{kw_info} (path ok)"
                f"  | {title[:50]}"
            )

    conn.commit()
    conn.close()

    print(f"\n=== 完成 ===")
    print(f"  分类变化: {changed}")
    print(f"  文件迁移: {moved}")
    print(f"\n  新分类分布:")
    for t, c in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"    {c:4d}  {t}")


def _cleanup_empty_dir(path: Path) -> None:
    """Remove directory if empty, bubbling up."""
    try:
        while path.exists() and not any(path.iterdir()):
            path.rmdir()
            path = path.parent
    except OSError:
        pass


if __name__ == "__main__":
    main()
