"""Build bot.db (story titles + episodes) from a MySQL dump of the website database.

Usage:  python import_sql.py audifkzg_aoudio.sql [bot.db]
Only the `stories` and `episodes` tables are read - nothing else in the dump is touched or copied.
"""
import re
import sqlite3
import sys
from datetime import datetime

TOKEN = re.compile(r"'(?:[^'\\]|\\.|'')*'|NULL|-?\d+(?:\.\d+)?|[(),;]", re.S)
ESC = {"n": "\n", "r": "\r", "t": "\t", "0": "\0", "b": "\b", "Z": "\x1a"}


def unquote(tok):
    body = tok[1:-1].replace("''", "'")
    return re.sub(r"\\(.)", lambda m: ESC.get(m.group(1), m.group(1)), body)


def parse_rows(text, table):
    rows = []
    for m in re.finditer(r"INSERT INTO `%s` \([^)]*\) VALUES" % table, text):
        row, depth = None, 0
        for t in TOKEN.finditer(text, m.end()):
            tok = t.group(0)
            if tok == "(":
                row, depth = [], 1
            elif tok == ")":
                rows.append(row)
                row, depth = None, 0
            elif tok == ";":
                break
            elif tok == ",":
                continue
            elif row is not None:
                if tok == "NULL":
                    row.append(None)
                elif tok[0] == "'":
                    row.append(unquote(tok))
                else:
                    row.append(float(tok) if "." in tok else int(tok))
    return rows


def main():
    src = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "bot.db"
    text = open(src, encoding="utf-8", errors="replace").read()
    stories = parse_rows(text, "stories")      # id,title,description,poster,is_popular,is_free,created_at
    episodes = parse_rows(text, "episodes")    # id,story_id,seq,orig_name,stored,mime,size,created_at

    con = sqlite3.connect(out)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS subs(user_id INTEGER PRIMARY KEY, expires_at INTEGER NOT NULL, note TEXT);
    CREATE TABLE IF NOT EXISTS stories(story_id INTEGER PRIMARY KEY, title TEXT);
    CREATE TABLE IF NOT EXISTS episodes(
        key TEXT PRIMARY KEY, story_id INTEGER NOT NULL, seq INTEGER NOT NULL,
        size INTEGER NOT NULL, modified INTEGER NOT NULL, file_id TEXT, kind TEXT,
        UNIQUE(story_id, seq));
    """)
    for s in stories:
        con.execute("INSERT OR REPLACE INTO stories(story_id, title) VALUES(?,?)", (s[0], s[1].strip()))

    skipped_legacy = fixed = 0
    used = {}
    for e in sorted(episodes, key=lambda r: (r[1], r[2] or 10**9, r[0])):
        eid, sid, seq, orig, stored, mime, size, created = e
        if not str(stored).startswith("r2:"):
            skipped_legacy += 1
            continue
        key = stored[3:]
        taken = used.setdefault(sid, set())
        if not seq or seq in taken:
            seq = max(taken, default=0) + 1
            fixed += 1
        taken.add(seq)
        try:
            ts = int(datetime.strptime(created, "%Y-%m-%d %H:%M:%S").timestamp())
        except Exception:
            ts = 0
        con.execute("INSERT OR REPLACE INTO episodes(key, story_id, seq, size, modified) VALUES(?,?,?,?,?)",
                    (key, sid, seq, int(size or 0), ts))
    con.commit()
    n_ep = con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    print(f"Stories: {len(stories)} | episodes imported: {n_ep} | legacy (not on R2) skipped: {skipped_legacy} | renumbered: {fixed}")
    con.close()


if __name__ == "__main__":
    main()
