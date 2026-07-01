"""Full reclassification:
1. Walk all archive files on disk
2. Re-run keyword classifier (with word-boundary fix)
3. Move files to correct topic directory
4. Update DB topic + archive_path
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from pathlib import Path

import frontmatter
import yaml

PROJECT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT / "data"
DB_PATH = DATA_DIR / "media.db"
FEEDS_PATH = PROJECT / "feeds.yaml"

_SHORT_KW_LEN = 4


def load_topics(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)["topics"]


def keyword_match(
    topics: list[dict], title: str, content_md: str
) -> tuple[str | None, str | None]:
    haystack = f"{title}\n{content_md}".lower()
    for t in topics:
        for kw in t["keywords"]:
            pattern = re.escape(kw.lower())
            if len(kw) <= _SHORT_KW_LEN:
                pattern = r"\b" + pattern + r"\b"
            if re.search(pattern, haystack):
                return t["name"], kw
    return None, None


def _cleanup_empty_dir(path: Path) -> None:
    try:
        while path.exists() and not any(path.iterdir()):
            path.rmdir()
            path = path.parent
    except OSError:
        pass


def main():
    topics = load_topics(FEEDS_PATH)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Build DB lookups
    all_rows = conn.execute(
        "SELECT id, topic, archive_path, fingerprint FROM articles ORDER BY id"
    ).fetchall()

    path_to_row: dict[str, sqlite3.Row] = {}
    slug_to_row: dict[str, sqlite3.Row] = {}
    for r in all_rows:
        ap = r["archive_path"]
        if ap:
            path_to_row[str(Path(ap).as_posix())] = r
            slug_to_row[Path(ap).stem] = r

    archive_root = DATA_DIR / "archive"
    if not archive_root.exists():
        print("No archive directory found!")
        return

    stats: Counter = Counter()
    changes = 0
    moves = 0
    db_updates = 0
    no_db = 0

    for md_file in sorted(archive_root.rglob("*.md")):
        current_dir = md_file.parent.name
        rel_str = str(md_file.relative_to(DATA_DIR).as_posix())

        post = frontmatter.load(str(md_file))
        meta = dict(post.metadata)
        fm_topic = meta.get("topic", "")
        title = str(meta.get("title", ""))
        body = post.content

        new_topic, matched_kw = keyword_match(topics, title, body)
        if new_topic is None:
            new_topic = "uncategorized"

        stats[new_topic] += 1

        expected_dir = archive_root / new_topic
        expected_path = expected_dir / md_file.name
        expected_rel_str = str((Path("archive") / new_topic / md_file.name).as_posix())

        # Find DB record
        row = path_to_row.get(rel_str)
        if row is None:
            row = slug_to_row.get(md_file.stem)
        if row is None:
            for r in all_rows:
                if r["archive_path"] and md_file.stem == Path(r["archive_path"]).stem:
                    row = r
                    break

        if row is None:
            no_db += 1
            if current_dir != new_topic:
                expected_dir.mkdir(parents=True, exist_ok=True)
                if expected_path.exists():
                    print(f"  ⚠ 目标已存在: {expected_rel_str}")
                else:
                    md_file.rename(expected_path)
                    _cleanup_empty_dir(md_file.parent)
                    moves += 1
                    print(f"  (无DB): {current_dir:12s} -> {new_topic:12s}  | {title[:45]}")
            continue

        db_topic = row["topic"]
        db_path = row["archive_path"]
        needs_db_topic = db_topic != new_topic
        needs_db_path = db_path != expected_rel_str

        if current_dir == new_topic and not needs_db_topic and not needs_db_path:
            continue

        # Move file
        if current_dir != new_topic:
            expected_dir.mkdir(parents=True, exist_ok=True)
            if expected_path.exists():
                print(f"  ⚠ ID {row['id']}: 目标已存在 {expected_rel_str}")
            else:
                md_file.rename(expected_path)
                _cleanup_empty_dir(md_file.parent)
                moves += 1
                rel_str = expected_rel_str

        # Update front-matter
        if fm_topic != new_topic:
            post.metadata["topic"] = new_topic
            (expected_path if current_dir != new_topic else md_file).write_text(
                frontmatter.dumps(post), encoding="utf-8"
            )

        # Update DB
        if needs_db_topic or needs_db_path:
            conn.execute(
                "UPDATE articles SET topic=?, archive_path=? WHERE id=?",
                (new_topic, expected_rel_str, row["id"]),
            )
            db_updates += 1

        changes += 1
        dir_note = f" ({current_dir}->{new_topic})" if current_dir != new_topic else ""
        kw_info = f" kw:{matched_kw}" if matched_kw else ""
        print(
            f"  ID {row['id']:5d}: "
            f"{db_topic or '':20s} -> {new_topic:20s}{kw_info}{dir_note}"
            f"  | {title[:45]}"
        )

    conn.commit()
    conn.close()

    print(f"\n{'='*60}")
    print(f"  分类变化:         {changes}")
    print(f"  文件迁移:         {moves}")
    print(f"  DB 更新:          {db_updates}")
    print(f"  未匹配 DB:        {no_db}")
    print(f"\n  新分类分布:")
    for t, c in stats.most_common():
        print(f"    {c:4d}  {t}")


if __name__ == "__main__":
    main()
