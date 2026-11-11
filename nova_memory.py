"""
Nova's persistent memory: remembers personal facts, preferences, and recent conversations across sessions.
Stored locally in ~/.laptop_assistant/memory.db (SQLite, no extra packages).
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
    """Thread-safe SQLite memory store for user facts and episodic conversation history."""

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
                    updated_at REAL NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    user_text TEXT NOT NULL,
                    assistant_text TEXT NOT NULL
                )"""
            )

    def remember(self, key, value, category="general"):
        clean_key = _slug(key)
        clean_val = " ".join(str(value).strip().split())
        if not clean_val:
            return None
        clean_cat = _slug(category or "general")
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO facts(key, category, value, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     category = excluded.category,
                     value = excluded.value,
                     updated_at = excluded.updated_at""",
                (clean_key, clean_cat, clean_val, now),
            )
        return clean_key, clean_val

    def all_facts(self, limit=20):
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT key, category, value, updated_at FROM facts ORDER BY updated_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def recall(self, query="", limit=8):
        tokens = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1]
        with self._lock, self._connect() as conn:
            if not tokens:
                facts = conn.execute(
                    "SELECT key, category, value, updated_at FROM facts ORDER BY updated_at DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
                episodes = conn.execute(
                    "SELECT ts, user_text, assistant_text FROM episodes ORDER BY id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            else:
                f_rows = conn.execute(
                    "SELECT key, category, value, updated_at FROM facts ORDER BY updated_at DESC"
                ).fetchall()
                facts = [
                    r for r in f_rows
                    if any(t in f"{r['key']} {r['category']} {r['value']}".lower() for t in tokens)
                ][:limit]
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

    def context_summary(self, max_facts=15, max_episodes=4):
        """Compact prompt context injected into Nova's AI system prompt."""
        parts = []
        facts = self.all_facts(limit=max_facts)
        if facts:
            lines = [f"- {f['key'].replace('_', ' ')}: {f['value']}" for f in facts]
            parts.append("Known user facts:\n" + "\n".join(lines))
        eps = self.recent_episodes(limit=max_episodes)
        if eps:
            lines = [
                f"- [{datetime.fromtimestamp(e['ts']):%H:%M}] User: {e['user_text']} -> Nova: {e['assistant_text']}"
                for e in eps
            ]
            parts.append("Recent interactions:\n" + "\n".join(lines))
        return "\n\n".join(parts)


def remember_phrase(mem, text):
    """Parse 'my favorite color is blue' or 'car is on floor 3' into a structured fact."""
    cleaned = re.sub(r"^(?:that|me|this)\s+", "", text.strip(), flags=re.I)
    m = re.match(r"^(?:my\s+)?(.+?)\s+(?:is|are|was|will be)\s+(.+)$", cleaned, flags=re.I)
    if m and len(m.group(1).split()) <= 5:
        key, val = m.group(1).strip(), m.group(2).strip()
    else:
        key = " ".join(re.findall(r"[a-z0-9]+", cleaned.lower())[:4]) or "note"
        val = cleaned
    res = mem.remember(key, val)
    return f"Got it, I'll remember that {key.replace('_', ' ')} is {res[1]}." if (res and m) else \
           f"Got it, I'll remember: {res[1]}." if res else "What should I remember?"
