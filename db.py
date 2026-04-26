import aiosqlite
import base64
import hashlib
import json
import os
import uuid
from datetime import datetime

import settings_store


# ── Encryption helpers for secrets vault ──────────────────────────────────────

def _fernet():
    from cryptography.fernet import Fernet
    secret = os.getenv("JWT_SECRET", "bud-jwt-secret-please-change-in-production")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def _decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()

DB_PATH = "emulaiator.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # Enable WAL mode for better concurrent write performance
        await db.execute("PRAGMA journal_mode=WAL")

        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                name TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')),
                settings TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                title TEXT DEFAULT 'New Chat',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_calls TEXT,
                tool_call_id TEXT,
                name TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );

            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                fact TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS vault (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS analytics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                event_type TEXT NOT NULL,
                tool_name TEXT,
                model TEXT,
                tokens INTEGER DEFAULT 0,
                session_id TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS custom_tools (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                name TEXT NOT NULL,
                description TEXT,
                command TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                session_id TEXT,
                tool_name TEXT NOT NULL,
                args_summary TEXT,
                result_summary TEXT,
                status TEXT DEFAULT 'info',
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS secrets (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                encrypted_value TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(user_id, name)
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                session_id TEXT,
                title TEXT NOT NULL,
                status TEXT DEFAULT 'running',
                result TEXT,
                error TEXT,
                tool_calls_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS webhooks (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                token TEXT UNIQUE NOT NULL,
                prompt_template TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS cost_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                session_id TEXT,
                model TEXT NOT NULL,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)

        # FTS5 virtual tables for fast full-text memory/vault search
        await db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
            USING fts5(fact, user_id UNINDEXED, content='facts', content_rowid='id')
        """)
        await db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS vault_fts
            USING fts5(title, content, user_id UNINDEXED, content='vault', content_rowid='rowid')
        """)

        # FTS5 triggers to keep index in sync with base tables
        await db.executescript("""
            CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
                INSERT INTO facts_fts(rowid, fact, user_id) VALUES (new.id, new.fact, new.user_id);
            END;
            CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
                INSERT INTO facts_fts(facts_fts, rowid, fact, user_id)
                    VALUES ('delete', old.id, old.fact, old.user_id);
            END;
            CREATE TRIGGER IF NOT EXISTS vault_ai AFTER INSERT ON vault BEGIN
                INSERT INTO vault_fts(rowid, title, content, user_id)
                    VALUES (new.rowid, new.title, new.content, new.user_id);
            END;
            CREATE TRIGGER IF NOT EXISTS vault_ad AFTER DELETE ON vault BEGIN
                INSERT INTO vault_fts(vault_fts, rowid, title, content, user_id)
                    VALUES ('delete', old.rowid, old.title, old.content, old.user_id);
            END;
        """)

        # Safe migrations for pre-existing databases
        for stmt in [
            "ALTER TABLE sessions ADD COLUMN pinned INTEGER DEFAULT 0",
            "ALTER TABLE sessions ADD COLUMN sort_order INTEGER DEFAULT 0",
            "ALTER TABLE sessions ADD COLUMN user_id TEXT",
            "ALTER TABLE facts ADD COLUMN user_id TEXT",
            "ALTER TABLE vault ADD COLUMN user_id TEXT",
            "ALTER TABLE analytics ADD COLUMN user_id TEXT",
            "ALTER TABLE custom_tools ADD COLUMN user_id TEXT",
        ]:
            try:
                await db.execute(stmt)
            except Exception:
                pass
        await db.commit()


# ── Users ──────────────────────────────────────────────────────────────────────

async def create_user(email: str, password_hash: str, name: str = "") -> dict:
    uid = str(uuid.uuid4())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (id, email, password_hash, name) VALUES (?, ?, ?, ?)",
            (uid, email.lower().strip(), password_hash, name),
        )
        await db.commit()
    return {"id": uid, "email": email.lower().strip(), "name": name}


async def get_user_by_email(email: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, email, password_hash, name, settings FROM users WHERE email=?",
            (email.lower().strip(),),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_user_by_id(user_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, email, name, settings, created_at FROM users WHERE id=?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_user_settings(user_id: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT settings, name FROM users WHERE id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return {}
    try:
        cfg = json.loads(row["settings"] or "{}")
    except Exception:
        cfg = {}
    if row["name"]:
        cfg.setdefault("user_name", row["name"])
    return cfg


async def save_user_settings(user_id: str, settings: dict) -> dict:
    """Merge new settings into existing user settings and persist."""
    existing = await get_user_settings(user_id)
    # Don't overwrite masked API key with masked value
    for key in ("openrouter_api_key",):
        new_val = settings.get(key, "")
        if new_val.startswith("sk-or-...") or new_val == "":
            settings.pop(key, None)
    merged = {**existing, **settings}
    # Keep user name in sync
    name = merged.get("user_name", "")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET settings=?, name=? WHERE id=?",
            (json.dumps(merged), name, user_id),
        )
        await db.commit()
    return merged


async def user_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            row = await cur.fetchone()
    return row[0] if row else 0


# ── Sessions ───────────────────────────────────────────────────────────────────

async def create_session(user_id: str = None) -> dict:
    sid = str(uuid.uuid4())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO sessions (id, user_id) VALUES (?, ?)", (sid, user_id)
        )
        await db.commit()
    return {"id": sid, "title": "New Chat", "created_at": datetime.utcnow().isoformat()}


async def get_sessions(user_id: str = None) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if user_id:
            async with db.execute(
                "SELECT id, title, created_at, updated_at, pinned, sort_order FROM sessions "
                "WHERE user_id=? ORDER BY pinned DESC, sort_order ASC, updated_at DESC",
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT id, title, created_at, updated_at, pinned, sort_order FROM sessions "
                "ORDER BY pinned DESC, sort_order ASC, updated_at DESC"
            ) as cur:
                rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def pin_session(session_id: str, pinned: bool, user_id: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if user_id:
            await db.execute(
                "UPDATE sessions SET pinned=? WHERE id=? AND user_id=?",
                (1 if pinned else 0, session_id, user_id),
            )
        else:
            await db.execute(
                "UPDATE sessions SET pinned=? WHERE id=?",
                (1 if pinned else 0, session_id),
            )
        await db.commit()


async def reorder_sessions(order: list, user_id: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        for idx, session_id in enumerate(order):
            if user_id:
                await db.execute(
                    "UPDATE sessions SET sort_order=? WHERE id=? AND user_id=?",
                    (idx, session_id, user_id),
                )
            else:
                await db.execute(
                    "UPDATE sessions SET sort_order=? WHERE id=?", (idx, session_id)
                )
        await db.commit()


async def update_session_title(session_id: str, title: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET title=?, updated_at=datetime('now') WHERE id=?",
            (title[:60], session_id),
        )
        await db.commit()


async def touch_session(session_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET updated_at=datetime('now') WHERE id=?", (session_id,)
        )
        await db.commit()


async def save_message(session_id: str, role: str, content: str = None,
                       tool_calls=None, tool_call_id: str = None, name: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO messages (session_id, role, content, tool_calls, tool_call_id, name)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                session_id, role, content,
                json.dumps(tool_calls) if tool_calls else None,
                tool_call_id, name,
            ),
        )
        await db.commit()


async def get_messages(session_id: str) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT role, content, tool_calls, tool_call_id, name, created_at
               FROM messages WHERE session_id=? ORDER BY id""",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
    result = []
    for r in rows:
        msg = {"role": r["role"]}
        if r["content"] is not None:
            msg["content"] = r["content"]
        if r["tool_calls"]:
            msg["tool_calls"] = json.loads(r["tool_calls"])
        if r["tool_call_id"]:
            msg["tool_call_id"] = r["tool_call_id"]
        if r["name"]:
            msg["name"] = r["name"]
        result.append(msg)
    return result


async def delete_session(session_id: str, user_id: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        if user_id:
            await db.execute(
                "DELETE FROM sessions WHERE id=? AND user_id=?", (session_id, user_id)
            )
        else:
            await db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        await db.commit()


# ── Facts (Memory) ─────────────────────────────────────────────────────────────

async def save_fact(fact: str, user_id: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO facts (fact, user_id) VALUES (?, ?)", (fact, user_id)
        )
        await db.commit()


async def search_facts(query: str, user_id: str = None) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Use FTS5 for ranked full-text search; fall back to LIKE on any error
        try:
            fts_query = " OR ".join(f'"{t}"' for t in query.split() if t)
            if user_id:
                sql = ("SELECT fact FROM facts_fts WHERE facts_fts MATCH ? "
                       "AND user_id IS ? ORDER BY rank LIMIT 20")
                params = [fts_query, user_id]
                else:
                sql = "SELECT fact FROM facts_fts WHERE facts_fts MATCH ? ORDER BY rank LIMIT 20"
                params = [fts_query]
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
            return [r["fact"] for r in rows]
        except Exception:
            # FTS5 fallback: plain LIKE search
            terms = query.lower().split()
            conditions = " AND ".join(["lower(fact) LIKE ?" for _ in terms])
            like_params = [f"%{t}%" for t in terms]
            if user_id:
                sql = f"SELECT fact FROM facts WHERE user_id=? AND {conditions} ORDER BY id DESC LIMIT 20"
                like_params = [user_id] + like_params
            else:
                sql = f"SELECT fact FROM facts WHERE {conditions} ORDER BY id DESC LIMIT 20"
            async with db.execute(sql, like_params) as cur:
                rows = await cur.fetchall()
            return [r["fact"] for r in rows]


async def get_all_facts(user_id: str = None) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if user_id:
            async with db.execute(
                "SELECT fact FROM facts WHERE user_id=? ORDER BY id DESC LIMIT 50", (user_id,)
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute("SELECT fact FROM facts ORDER BY id DESC LIMIT 50") as cur:
                rows = await cur.fetchall()
    return [r["fact"] for r in rows]


async def get_all_facts_with_ids(user_id: str = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if user_id:
            async with db.execute(
                "SELECT id, fact, created_at FROM facts WHERE user_id=? ORDER BY id DESC LIMIT 200",
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT id, fact, created_at FROM facts ORDER BY id DESC LIMIT 200"
            ) as cur:
                rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def delete_fact(fact_id: int, user_id: str = None) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        if user_id:
            cur = await db.execute(
                "DELETE FROM facts WHERE id=? AND user_id=?", (fact_id, user_id)
            )
        else:
            cur = await db.execute("DELETE FROM facts WHERE id=?", (fact_id,))
        await db.commit()
        return cur.rowcount > 0


# ── RAG Vault ─────────────────────────────────────────────────────────────────

async def vault_add(title: str, content: str, user_id: str = None) -> str:
    doc_id = str(uuid.uuid4())[:8]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO vault (id, user_id, title, content) VALUES (?, ?, ?, ?)",
            (doc_id, user_id, title[:200], content),
        )
        await db.commit()
    return doc_id


async def vault_search(query: str, user_id: str = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        try:
            fts_query = " OR ".join(f'"{t}"' for t in query.split() if t)
            if user_id:
                sql = ("SELECT v.id, v.title, v.content, v.created_at "
                       "FROM vault_fts f JOIN vault v ON v.rowid = f.rowid "
                       "WHERE vault_fts MATCH ? AND f.user_id IS ? ORDER BY rank LIMIT 10")
                params = [fts_query, user_id]
            else:
                sql = ("SELECT v.id, v.title, v.content, v.created_at "
                       "FROM vault_fts f JOIN vault v ON v.rowid = f.rowid "
                       "WHERE vault_fts MATCH ? ORDER BY rank LIMIT 10")
                params = [fts_query]
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
            return [dict(r) for r in rows]
        except Exception:
            # Fallback to LIKE
            terms = query.lower().split()[:5]
            conditions = " OR ".join(["lower(content) LIKE ? OR lower(title) LIKE ?" for _ in terms])
            like_params = [p for t in terms for p in (f"%{t}%", f"%{t}%")]
            if user_id:
                sql = (f"SELECT id, title, content, created_at FROM vault "
                       f"WHERE user_id=? AND ({conditions}) ORDER BY created_at DESC LIMIT 10")
                like_params = [user_id] + like_params
            else:
                sql = (f"SELECT id, title, content, created_at FROM vault "
                       f"WHERE {conditions} ORDER BY created_at DESC LIMIT 10")
            async with db.execute(sql, like_params) as cur:
                rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def vault_list(user_id: str = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if user_id:
            async with db.execute(
                "SELECT id, title, length(content) as chars, created_at FROM vault WHERE user_id=? ORDER BY created_at DESC",
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT id, title, length(content) as chars, created_at FROM vault ORDER BY created_at DESC"
            ) as cur:
                rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def vault_delete(doc_id: str, user_id: str = None) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        if user_id:
            cur = await db.execute(
                "DELETE FROM vault WHERE id=? AND user_id=?", (doc_id, user_id)
            )
        else:
            cur = await db.execute("DELETE FROM vault WHERE id=?", (doc_id,))
        await db.commit()
        return cur.rowcount > 0


# ── Analytics ─────────────────────────────────────────────────────────────────

async def track_event(event_type: str, tool_name: str = None, model: str = None,
                      tokens: int = 0, session_id: str = None, user_id: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO analytics (event_type, tool_name, model, tokens, session_id, user_id) VALUES (?,?,?,?,?,?)",
            (event_type, tool_name, model, tokens or 0, session_id, user_id),
        )
        await db.commit()


async def get_analytics(user_id: str = None) -> dict:
    uid_filter = "WHERE user_id=?" if user_id else ""
    uid_and    = "AND user_id=?" if user_id else ""
    p          = [user_id] if user_id else []

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute(
            f"SELECT tool_name, COUNT(*) as cnt FROM analytics {uid_filter} {'AND' if uid_filter else 'WHERE'} event_type='tool' AND tool_name IS NOT NULL GROUP BY tool_name ORDER BY cnt DESC LIMIT 15".replace("WHERE AND","WHERE"),
            p,
        ) as cur:
            top_tools = [{"tool": r["tool_name"], "count": r["cnt"]} for r in await cur.fetchall()]

        async with db.execute(
            f"SELECT model, COUNT(*) as cnt FROM analytics WHERE event_type='llm' AND model IS NOT NULL {uid_and} GROUP BY model ORDER BY cnt DESC",
            p,
        ) as cur:
            model_usage = [{"model": r["model"], "count": r["cnt"]} for r in await cur.fetchall()]

        async with db.execute(
            f"SELECT COALESCE(SUM(tokens),0) as total FROM analytics {uid_filter}",
            p,
        ) as cur:
            row = await cur.fetchone()
            total_tokens = row["total"] if row else 0

        async with db.execute(
            f"SELECT date(created_at) as day, COUNT(*) as cnt FROM analytics {uid_filter} GROUP BY day ORDER BY day DESC LIMIT 14",
            p,
        ) as cur:
            daily = [{"day": r["day"], "count": r["cnt"]} for r in await cur.fetchall()]

        sc_filter = f"WHERE user_id=?" if user_id else ""
        async with db.execute(f"SELECT COUNT(*) as cnt FROM sessions {sc_filter}", p) as cur:
            row = await cur.fetchone()
            session_count = row["cnt"] if row else 0

    return {
        "top_tools": top_tools,
        "model_usage": model_usage,
        "total_tokens": total_tokens,
        "daily_activity": daily,
        "session_count": session_count,
    }


# ── Personas (stored in user settings) ────────────────────────────────────────

async def get_personas(user_id: str = None) -> list[dict]:
    if user_id:
        cfg = await get_user_settings(user_id)
    else:
        cfg = settings_store.load()
    return cfg.get("personas", [])


async def save_persona(persona: dict, user_id: str = None) -> dict:
    new_persona = {
        "id": str(uuid.uuid4()),
        "name": persona.get("name", "Unnamed"),
        "description": persona.get("description", ""),
        "system_prompt": persona.get("system_prompt", ""),
        "builtin": False,
    }
    if user_id:
        cfg = await get_user_settings(user_id)
        personas = cfg.get("personas", [])
        personas.append(new_persona)
        await save_user_settings(user_id, {"personas": personas})
    else:
        cfg = settings_store.load()
        personas = cfg.get("personas", [])
        personas.append(new_persona)
        settings_store.save({"personas": personas})
    return new_persona


async def delete_persona(persona_id: str, user_id: str = None) -> bool:
    if user_id:
        cfg = await get_user_settings(user_id)
        personas = cfg.get("personas", [])
        original_len = len(personas)
        personas = [p for p in personas if p["id"] != persona_id or p.get("builtin", False)]
        if len(personas) == original_len:
            return False
        await save_user_settings(user_id, {"personas": personas})
    else:
        cfg = settings_store.load()
        personas = cfg.get("personas", [])
        original_len = len(personas)
        personas = [p for p in personas if p["id"] != persona_id or p.get("builtin", False)]
        if len(personas) == original_len:
            return False
        settings_store.save({"personas": personas})
    return True


# ── Custom Tools ───────────────────────────────────────────────────────────────

async def get_custom_tools(user_id: str = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if user_id:
            async with db.execute(
                "SELECT id, name, description, command, created_at FROM custom_tools WHERE user_id=? ORDER BY created_at DESC",
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT id, name, description, command, created_at FROM custom_tools ORDER BY created_at DESC"
            ) as cur:
                rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def save_custom_tool(tool: dict, user_id: str = None) -> dict:
    tool_id = str(uuid.uuid4())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO custom_tools (id, user_id, name, description, command) VALUES (?, ?, ?, ?, ?)",
            (tool_id, user_id, tool.get("name", ""), tool.get("description", ""), tool.get("command", "")),
        )
        await db.commit()
    return {"id": tool_id, "name": tool.get("name", ""), "description": tool.get("description", ""), "command": tool.get("command", "")}


async def delete_custom_tool(tool_id: str, user_id: str = None) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        if user_id:
            cur = await db.execute(
                "DELETE FROM custom_tools WHERE id=? AND user_id=?", (tool_id, user_id)
            )
        else:
            cur = await db.execute("DELETE FROM custom_tools WHERE id=?", (tool_id,))
        await db.commit()
        return cur.rowcount > 0


# ── Audit Log ─────────────────────────────────────────────────────────────────

async def log_audit(user_id: str, session_id: str, tool_name: str,
                    args_summary: str, result_summary: str, status: str = "info"):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO audit_log (user_id, session_id, tool_name, args_summary, result_summary, status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, session_id, tool_name,
             args_summary[:300] if args_summary else None,
             result_summary[:500] if result_summary else None,
             status),
        )
        await db.commit()


async def get_audit_log(user_id: str = None, limit: int = 100) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if user_id:
            async with db.execute(
                "SELECT * FROM audit_log WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ) as cur:
                rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ── Secrets Vault ─────────────────────────────────────────────────────────────

async def set_secret(user_id: str, name: str, value: str):
    encrypted = _encrypt(value)
    secret_id = str(uuid.uuid4())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO secrets (id, user_id, name, encrypted_value)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id, name) DO UPDATE SET encrypted_value=excluded.encrypted_value""",
            (secret_id, user_id, name.strip().lower(), encrypted),
        )
        await db.commit()


async def get_secret(user_id: str, name: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT encrypted_value FROM secrets WHERE user_id=? AND name=?",
            (user_id, name.strip().lower()),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    try:
        return _decrypt(row["encrypted_value"])
    except Exception:
        return None


async def list_secrets(user_id: str) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT name, created_at FROM secrets WHERE user_id=? ORDER BY name",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [{"name": r["name"], "created_at": r["created_at"]} for r in rows]


async def delete_secret(user_id: str, name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM secrets WHERE user_id=? AND name=?",
            (user_id, name.strip().lower()),
        )
        await db.commit()
        return cur.rowcount > 0


# ── Task Tracker ──────────────────────────────────────────────────────────────

async def create_task(user_id: str, session_id: str, title: str) -> str:
    task_id = str(uuid.uuid4())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO tasks (id, user_id, session_id, title) VALUES (?, ?, ?, ?)",
            (task_id, user_id, session_id, title[:200]),
        )
        await db.commit()
    return task_id


async def update_task(task_id: str, status: str, result: str = None,
                      error: str = None, tool_calls_count: int = 0):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE tasks SET status=?, result=?, error=?, tool_calls_count=?,
               updated_at=datetime('now') WHERE id=?""",
            (status, result[:500] if result else None,
             error[:300] if error else None, tool_calls_count, task_id),
        )
        await db.commit()


async def get_tasks(user_id: str = None, limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if user_id:
            async with db.execute(
                "SELECT * FROM tasks WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ) as cur:
                rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ── Webhooks ───────────────────────────────────────────────────────────────────

async def create_webhook(user_id: str, name: str, prompt_template: str) -> dict:
    wid = str(uuid.uuid4())
    token = str(uuid.uuid4()).replace("-", "")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO webhooks (id, user_id, name, token, prompt_template) VALUES (?,?,?,?,?)",
            (wid, user_id, name, token, prompt_template),
        )
        await db.commit()
    return {"id": wid, "token": token, "name": name, "prompt_template": prompt_template}


async def get_webhook_by_token(token: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM webhooks WHERE token=?", (token,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def list_webhooks(user_id: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM webhooks WHERE user_id=? ORDER BY created_at DESC",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def delete_webhook(user_id: str, webhook_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM webhooks WHERE id=? AND user_id=?", (webhook_id, user_id)
        )
        await db.commit()
        return cur.rowcount > 0


# ── Cost tracker ───────────────────────────────────────────────────────────────

# Approximate pricing per 1M tokens (input/output blended) for OpenRouter free models
_MODEL_COST_PER_1M: dict[str, float] = {
    # All free models — $0 cost (rate-limited on OpenRouter free tier)
}


async def log_cost(user_id: str, session_id: str, model: str,
                   prompt_tokens: int, completion_tokens: int):
    per_1m = _MODEL_COST_PER_1M.get(model, 0.0)
    total_tokens = prompt_tokens + completion_tokens
    cost_usd = (total_tokens / 1_000_000) * per_1m
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO cost_log (user_id, session_id, model, prompt_tokens,
               completion_tokens, cost_usd) VALUES (?,?,?,?,?,?)""",
            (user_id, session_id, model, prompt_tokens, completion_tokens, cost_usd),
        )
        await db.commit()


async def get_cost_summary(user_id: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT model,
                      SUM(prompt_tokens) as prompt_tokens,
                      SUM(completion_tokens) as completion_tokens,
                      SUM(cost_usd) as cost_usd,
                      COUNT(*) as calls
               FROM cost_log WHERE user_id=? GROUP BY model ORDER BY calls DESC""",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
        async with db.execute(
            "SELECT SUM(cost_usd) as total FROM cost_log WHERE user_id=?",
            (user_id,),
        ) as cur:
            total_row = await cur.fetchone()
    return {
        "total_usd": round((total_row["total"] or 0), 6),
        "by_model": [dict(r) for r in rows],
    }
