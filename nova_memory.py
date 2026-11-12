"""
Nova's persistent long-term memory: stores user preferences, pinned instructions,
frequently used applications, episodic interactions, and session summaries in SQLite.
"""
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DATA = Path.home() / ".laptop_assistant"
DATA.mkdir(exist_ok=True)
DB_FILE = DATA / "memory.db"


def _slug(text):
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "_".join(words[:6]) or "note"


class Memory:
    """Thread-safe SQLite memory store for user facts, preferences, and episodic history."""

    def __init__(self, db_path=DB_FILE):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS facts (
                    key TEXT PRIMARY KEY,
                    category TEXT NOT NULL DEFAULT 'general',
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'user'
                )"""
            )
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(facts)").fetchall()}
            for name, ddl in (
                ("access_count", "INTEGER NOT NULL DEFAULT 0"),
                ("pinned", "INTEGER NOT NULL DEFAULT 0"),
                ("source", "TEXT NOT NULL DEFAULT 'user'"),
            ):
                if name not in cols:
                    conn.execute(f"ALTER TABLE facts ADD COLUMN {name} {ddl}")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    user_text TEXT NOT NULL,
                    assistant_text TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS session_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    summary TEXT NOT NULL,
                    topics TEXT NOT NULL DEFAULT ''
                )"""
            )

    def remember(self, key, value, category="general", pinned=False, source="user"):
        clean_key = _slug(key)
        clean_val = " ".join(str(value).strip().split())
        if not clean_val:
            return None
        clean_cat = _slug(category or "general")
        now = time.time()
        pin_int = 1 if pinned else 0
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO facts(key, category, value, updated_at, access_count, pinned, source)
                   VALUES (?, ?, ?, ?, 1, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     category = excluded.category,
                     value = excluded.value,
                     updated_at = excluded.updated_at,
                     access_count = facts.access_count + 1,
                     pinned = MAX(facts.pinned, excluded.pinned),
                     source = excluded.source""",
                (clean_key, clean_cat, clean_val, now, pin_int, str(source or "user")),
            )
        return clean_key, clean_val

    def pin_fact(self, query, pinned=True):
        q = query.strip().lower()
        slug = _slug(q)
        tokens = [w for w in re.findall(r"[a-z0-9]+", q) if len(w) > 1]
        pin_val = 1 if pinned else 0
        with self._lock, self._connect() as conn:
            cur = conn.execute("UPDATE facts SET pinned = ? WHERE key = ?", (pin_val, slug))
            if cur.rowcount:
                return cur.rowcount
            if not tokens:
                return 0
            rows = conn.execute("SELECT key, value FROM facts").fetchall()
            matches = [
                r["key"] for r in rows
                if all(t in f"{r['key'].replace('_', ' ')} {r['value']}".lower() for t in tokens)
            ]
            if not matches:
                return 0
            conn.executemany("UPDATE facts SET pinned = ? WHERE key = ?", [(pin_val, k) for k in matches])
            return len(matches)

    def track_app_usage(self, app_name):
        name = " ".join(str(app_name).strip().split())
        if not name:
            return None
        key = _slug("frequent app " + name)
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT access_count FROM facts WHERE key = ?", (key,)).fetchone()
            nxt = (row["access_count"] + 1) if row else 1
            conn.execute(
                """INSERT INTO facts(key, category, value, updated_at, access_count, pinned, source)
                   VALUES (?, 'frequent_app', ?, ?, 1, 0, 'usage')
                   ON CONFLICT(key) DO UPDATE SET
                     value = excluded.value,
                     updated_at = excluded.updated_at,
                     access_count = facts.access_count + 1""",
                (key, f"{name} ({nxt}x)", now),
            )
        return nxt

    def top_apps(self, limit=5):
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT value, access_count FROM facts WHERE category = 'frequent_app' "
                "ORDER BY access_count DESC, updated_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [re.sub(r"\s*\(\d+x\)$", "", r["value"]) for r in rows]

    def all_facts(self, limit=20):
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT key, category, value, updated_at, access_count, pinned, source "
                "FROM facts ORDER BY pinned DESC, updated_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def recall(self, query="", limit=8):
        tokens = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1]
        with self._lock, self._connect() as conn:
            if not tokens:
                facts = conn.execute(
                    "SELECT key, category, value, updated_at, access_count, pinned FROM facts "
                    "ORDER BY pinned DESC, updated_at DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
                episodes = conn.execute(
                    "SELECT ts, user_text, assistant_text FROM episodes ORDER BY id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            else:
                f_rows = conn.execute(
                    "SELECT key, category, value, updated_at, access_count, pinned FROM facts ORDER BY updated_at DESC"
                ).fetchall()
                scored = []
                for r in f_rows:
                    hay = f"{r['key']} {r['category']} {r['value']}".lower()
                    hits = sum(1 for t in tokens if t in hay)
                    if hits:
                        scored.append((hits * 10 + (r["pinned"] * 5) + min(r["access_count"], 10), dict(r)))
                scored.sort(key=lambda item: (item[0], item[1]["updated_at"]), reverse=True)
                facts = [item[1] for item in scored[:limit]]
                if facts:
                    conn.executemany(
                        "UPDATE facts SET access_count = access_count + 1 WHERE key = ?",
                        [(f["key"],) for f in facts],
                    )
                e_rows = conn.execute(
                    "SELECT ts, user_text, assistant_text FROM episodes ORDER BY id DESC LIMIT 200"
                ).fetchall()
                episodes = [
                    r for r in e_rows
                    if any(t in f"{r['user_text']} {r['assistant_text']}".lower() for t in tokens)
                ][:limit]
        return {"facts": [dict(r) for r in facts], "episodes": [dict(r) for r in episodes]}

    def forget(self, query):
        q = query.strip().lower()
        slug = _slug(q)
        tokens = [w for w in re.findall(r"[a-z0-9]+", q) if len(w) > 1]
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM facts WHERE key = ?", (slug,))
            if cur.rowcount:
                return cur.rowcount
            if not tokens:
                return 0
            rows = conn.execute("SELECT key, value FROM facts").fetchall()
            matches = [
                r["key"] for r in rows
                if all(t in f"{r['key'].replace('_', ' ')} {r['value']}".lower() for t in tokens)
            ]
            if not matches:
                return 0
            conn.executemany("DELETE FROM facts WHERE key = ?", [(k,) for k in matches])
            return len(matches)

    def clear_all(self):
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM facts")
            conn.execute("DELETE FROM episodes")
            conn.execute("DELETE FROM session_summaries")

    def add_episode(self, user_text, assistant_text, keep=400):
        u = " ".join(str(user_text).strip().split())[:400]
        a = " ".join(str(assistant_text).strip().split())[:400]
        if not (u or a):
            return
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO episodes(ts, user_text, assistant_text) VALUES (?, ?, ?)",
                (time.time(), u, a),
            )
            conn.execute(
                "DELETE FROM episodes WHERE id NOT IN (SELECT id FROM episodes ORDER BY id DESC LIMIT ?)",
                (int(keep),),
            )

    def recent_episodes(self, limit=4, max_age_hours=24):
        cutoff = time.time() - max_age_hours * 3600
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, user_text, assistant_text FROM episodes WHERE ts >= ? ORDER BY id DESC LIMIT ?",
                (cutoff, int(limit)),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def compact_episodes(self, keep_recent=12):
        """Summarize older interactions into session_summaries to keep prompt context compact."""
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT id, ts, user_text, assistant_text FROM episodes ORDER BY id ASC").fetchall()
            if len(rows) <= keep_recent:
                return 0
            older = rows[:-keep_recent] if keep_recent > 0 else rows
            cutoff_id = older[-1]["id"]
            snippets = [f"{r['user_text'][:80]}" for r in older[-8:]]
            summary = "Earlier topics: " + "; ".join(snippets)
            summary = summary[:360]
            conn.execute(
                "INSERT INTO session_summaries(ts, summary, topics) VALUES (?, ?, ?)",
                (time.time(), summary, ", ".join(snippets[:4])[:160]),
            )
            conn.execute("DELETE FROM episodes WHERE id <= ?", (cutoff_id,))
            conn.execute(
                "DELETE FROM session_summaries WHERE id NOT IN "
                "(SELECT id FROM session_summaries ORDER BY id DESC LIMIT 20)"
            )
            return len(older)

    def recent_summaries(self, limit=2):
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, summary FROM session_summaries ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def stats(self):
        with self._lock, self._connect() as conn:
            facts = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            episodes = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
            summaries = conn.execute("SELECT COUNT(*) FROM session_summaries").fetchone()[0]
        return {"facts": int(facts), "episodes": int(episodes), "summaries": int(summaries)}

    def context_summary(self, query="", max_facts=10, max_episodes=3):
        """Compact, relevance-ranked prompt context injected into Nova's AI system prompt."""
        parts = []
        facts = self.all_facts(limit=50)
        if facts:
            tokens = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1} if query else set()

            def rank(f):
                hay = f"{f['key'].replace('_', ' ')} {f['category']} {f['value']}".lower()
                overlap = sum(1 for t in tokens if t in hay) if tokens else 0
                is_core = 1 if f["key"] in ("user_name", "name", "first_name") else 0
                # Filter out low-value automatic app counters unless query asks about apps
                freq_penalty = -5 if (f["category"] == "frequent_app" and not ({"app", "open", "favorite"} & tokens)) else 0
                return (is_core * 100 + f.get("pinned", 0) * 50 + overlap * 15 + freq_penalty, f["updated_at"])

            selected = sorted(facts, key=rank, reverse=True)[:max_facts]
            lines = [f"- {f['key'].replace('_', ' ')}: {f['value']}" for f in selected]
            parts.append("Known user facts:\n" + "\n".join(lines))
        sums = self.recent_summaries(limit=1)
        if sums:
            parts.append("Earlier summary: " + sums[-1]["summary"])
        eps = self.recent_episodes(limit=max_episodes)
        if eps:
            lines = [
                f"- [{datetime.fromtimestamp(e['ts']):%H:%M}] User: {e['user_text']} -> Nova: {e['assistant_text']}"
                for e in eps
            ]
            parts.append("Recent interactions:\n" + "\n".join(lines))
        return "\n\n".join(parts)


def remember_phrase(mem, text, pinned=False):
    """Parse 'my favorite color is blue' or 'car is on floor 3' into a structured fact."""
    cleaned = re.sub(r"^(?:that|me|this)\s+", "", text.strip(), flags=re.I)
    m = re.match(r"^(?:my\s+)?(.+?)\s+(?:is|are|was|will be)\s+(.+)$", cleaned, flags=re.I)
    if m and len(m.group(1).split()) <= 5:
        key, val = m.group(1).strip(), m.group(2).strip()
    else:
        key = " ".join(re.findall(r"[a-z0-9]+", cleaned.lower())[:4]) or "note"
        val = cleaned
    res = mem.remember(key, val, pinned=pinned)
    return f"Got it, I'll remember that {key.replace('_', ' ')} is {res[1]}." if (res and m) else \
           f"Got it, I'll remember: {res[1]}." if res else "What should I remember?"
