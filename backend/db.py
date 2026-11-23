import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import config

DB_PATH = config.DATA_DIR / "app.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS profiles (
    user_id INTEGER PRIMARY KEY,
    data TEXT NOT NULL DEFAULT '{}',
    cv_path TEXT,
    cv_name TEXT
);
CREATE TABLE IF NOT EXISTS google_tokens (
    user_id INTEGER PRIMARY KEY,
    token_enc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    data TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, key)
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_runs_user ON runs(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_opps_user ON opportunities(user_id);

-- One row per programme the student is applying to. Its stage drives the dashboard.
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    opportunity_id INTEGER REFERENCES opportunities(id) ON DELETE SET NULL,
    key TEXT NOT NULL,
    company TEXT NOT NULL,
    company_key TEXT NOT NULL,
    title TEXT NOT NULL,
    kind TEXT,
    field TEXT,
    url TEXT,
    deadline TEXT,
    colour_id INTEGER NOT NULL,
    stage TEXT NOT NULL DEFAULT 'to_apply',
    stage_deadline TEXT,
    stage_start TEXT,
    deadline_assumed INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, key)
);
CREATE INDEX IF NOT EXISTS idx_apps_user ON applications(user_id, stage);

-- Every calendar block of an application: apply, practice, OA, interview prep, interview...
CREATE TABLE IF NOT EXISTS blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT,
    status TEXT NOT NULL DEFAULT 'planned',
    event_id TEXT,
    link TEXT,
    resource_url TEXT,
    resource_label TEXT,
    note TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
-- What the agent did on its own, and questions it is waiting on (status 'open').
CREATE TABLE IF NOT EXISTS agent_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    application_id INTEGER REFERENCES applications(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'info',
    data TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_log_user ON agent_log(user_id, id);
-- The planner's memory: what it last looked at, and what it concluded.
CREATE TABLE IF NOT EXISTS planner_state (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    fingerprint TEXT,
    ran_at TEXT,
    assessment TEXT,
    concerns TEXT,
    mode TEXT
);
CREATE INDEX IF NOT EXISTS idx_blocks_app ON blocks(application_id, status);
CREATE INDEX IF NOT EXISTS idx_blocks_user_start ON blocks(user_id, start_at);
"""


# Columns added after the first release: a database created earlier gets them on the next start.
NEW_COLUMNS = {"blocks": [("resource_url", "TEXT"), ("resource_label", "TEXT"), ("note", "TEXT")], "applications": [("field", "TEXT")]}


def _add_missing_columns(c):
    for table, columns in NEW_COLUMNS.items():
        have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns:
            if have and name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")


def init():
    """Creates the data folder, the database file and the tables if they are missing."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.CV_ROOT.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    try:
        c.execute("PRAGMA journal_mode=WAL")  # readers do not block the writer, which matters with many users
        c.executescript(SCHEMA)
        _add_missing_columns(c)
        c.commit()
    finally:
        c.close()


@contextmanager
def conn():
    if not DB_PATH.exists():  # e.g. the data folder was deleted while the server was running
        init()
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


# ---- users ----
def create_user(email: str, password_hash: str) -> int:
    with conn() as c:
        return c.execute("INSERT INTO users(email, password_hash) VALUES (?, ?)", (email, password_hash)).lastrowid


def get_user_by_email(email: str):
    with conn() as c:
        return c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


def get_user(user_id: int):
    with conn() as c:
        return c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


# ---- profile ----
def get_profile(user_id: int) -> tuple[dict, str | None]:
    with conn() as c:
        row = c.execute("SELECT data, cv_path FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return {}, None
    return json.loads(row["data"]), row["cv_path"]


def get_cv_name(user_id: int) -> str | None:
    with conn() as c:
        row = c.execute("SELECT cv_name FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
    return row["cv_name"] if row else None


def save_profile(user_id: int, data: dict):
    with conn() as c:
        c.execute(
            "INSERT INTO profiles(user_id, data) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET data = excluded.data",
            (user_id, json.dumps(data)),
        )


def set_cv(user_id: int, path: str, name: str):
    with conn() as c:
        c.execute(
            "INSERT INTO profiles(user_id, data, cv_path, cv_name) VALUES (?, '{}', ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET cv_path = excluded.cv_path, cv_name = excluded.cv_name",
            (user_id, path, name),
        )


# ---- google ----
def save_google_token(user_id: int, token_enc: str):
    with conn() as c:
        c.execute(
            "INSERT INTO google_tokens(user_id, token_enc) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET token_enc = excluded.token_enc",
            (user_id, token_enc),
        )


def get_google_token(user_id: int) -> str | None:
    with conn() as c:
        row = c.execute("SELECT token_enc FROM google_tokens WHERE user_id = ?", (user_id,)).fetchone()
    return row["token_enc"] if row else None


def delete_google_token(user_id: int):
    with conn() as c:
        c.execute("DELETE FROM google_tokens WHERE user_id = ?", (user_id,))


# ---- runs ----
def create_run(run_id: str, user_id: int):
    with conn() as c:
        c.execute("INSERT INTO runs(id, user_id, status) VALUES (?, ?, 'running')", (run_id, user_id))


def finish_run(run_id: str, status: str):
    with conn() as c:
        c.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))


def runs_today(user_id: int) -> int:
    with conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE user_id = ? AND status != 'error' AND created_at >= datetime('now', '-1 day')",
            (user_id,),
        ).fetchone()
    return row["n"]


# ---- opportunities ----
def company_norm(text: str) -> str:
    """Company name boiled down so 'Goldman Sachs International' and 'Goldman Sachs' match. Keep in step with _norm in server.py."""
    text = re.sub(r"\b20\d\d\b|\(.*?\)", " ", (text or "").lower())
    text = re.sub(r"\b(plc|ltd|limited|llp|inc|group|the|uk|international|&|and)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def web_key(company: str, kind: str) -> str:
    return f"web|{company_norm(company)}|{kind}"


def item_key(item: dict) -> str:
    """Who an opportunity is: a web programme is its company and kind (not the wording of its title), an email is its title and link."""
    if item.get("source") != "gmail" and item.get("company") and item.get("type"):
        return web_key(item["company"], item["type"])
    return f"{(item.get('title') or '').strip().lower()}|{item.get('url') or item.get('link') or ''}"


_key = item_key


def upsert_opportunities(user_id: int, items: list[dict]) -> list[int]:
    """Saves found items; one that was already found before keeps its status. Returns the row ids."""
    ids = []
    with conn() as c:
        for it in items:
            c.execute(
                "INSERT INTO opportunities(user_id, key, data, status) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET data = excluded.data",
                (user_id, _key(it), json.dumps(it), "watching" if it.get("watch") else "pending"),
            )
            ids.append(
                c.execute("SELECT id FROM opportunities WHERE user_id = ? AND key = ?", (user_id, _key(it))).fetchone()["id"]
            )
    return ids


def get_opportunities(user_id: int, ids: list[int] | None = None) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM opportunities WHERE user_id = ? ORDER BY id DESC", (user_id,)).fetchall()
    return [dict(r) for r in rows if ids is None or r["id"] in ids]


def delete_opportunities(user_id: int) -> int:
    """Removes every saved opportunity for this user. Returns how many were removed."""
    with conn() as c:
        return c.execute("DELETE FROM opportunities WHERE user_id = ?", (user_id,)).rowcount


def mark_approved(user_id: int, ids: list[int]):
    with conn() as c:
        c.executemany(
            "UPDATE opportunities SET status = 'approved' WHERE user_id = ? AND id = ?", [(user_id, i) for i in ids]
        )


# ---- applications and their calendar blocks ----
# (calendar colour id, hex for the dark UI). The id is Google Calendar's own colorId, so events carry the same colour.
PALETTE = [(7, "#4fc3f7"), (6, "#ff8a65"), (2, "#66d19e"), (3, "#ba68c8"), (5, "#f6d04d"),
           (4, "#f48fb1"), (1, "#9fa8da"), (9, "#7986f5"), (10, "#81c784"), (11, "#ef6b6b")]
HEX_OF = dict(PALETTE)
STAGES = ["to_apply", "applied", "oa", "interview", "assessment_centre", "offer", "rejected", "withdrawn"]
ACTIVE_STAGES = ("to_apply", "applied", "oa", "interview", "assessment_centre")


def _pick_colour(c, user_id: int, company_key: str) -> int:
    """The same company always has the same colour; a new one takes the first colour that is not yet used."""
    same = c.execute("SELECT colour_id FROM applications WHERE user_id = ? AND company_key = ? LIMIT 1", (user_id, company_key)).fetchone()
    if same:
        return same["colour_id"]
    used = {r["colour_id"] for r in c.execute("SELECT colour_id FROM applications WHERE user_id = ?", (user_id,))}
    free = [cid for cid, _ in PALETTE if cid not in used]
    return free[0] if free else PALETTE[len(used) % len(PALETTE)][0]


def pick_colour(user_id: int, company: str, avoid: set[int] | None = None) -> int:
    """The colour a company has, or will get. `avoid` holds colours already handed out in the same batch."""
    with conn() as c:
        ck = company_norm(company) or company.lower()
        same = c.execute("SELECT colour_id FROM applications WHERE user_id = ? AND company_key = ? LIMIT 1", (user_id, ck)).fetchone()
        if same:
            return same["colour_id"]
        used = {r["colour_id"] for r in c.execute("SELECT colour_id FROM applications WHERE user_id = ?", (user_id,))} | (avoid or set())
        free = [cid for cid, _ in PALETTE if cid not in used]
        return free[0] if free else PALETTE[len(used) % len(PALETTE)][0]


def create_applications(user_id: int, rows: list[dict]) -> list[dict]:
    """Starts an application for each approved opportunity row ({id, key, data}). One that exists already is reused."""
    made = []
    with conn() as c:
        for row in rows:
            d = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
            company = (d.get("company") or d.get("title") or "Company").strip()
            ck = company_norm(company) or company.lower()
            c.execute(
                "INSERT INTO applications(user_id, opportunity_id, key, company, company_key, title, kind, field, url, deadline, colour_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id, key) DO NOTHING",
                (user_id, row["id"], row["key"], company, ck, d.get("title") or company, d.get("type"), d.get("field"),
                 d.get("url") or d.get("link"), d.get("deadline"), row.get("colour_id") or _pick_colour(c, user_id, ck)),
            )
            made.append(dict(c.execute("SELECT * FROM applications WHERE user_id = ? AND key = ?", (user_id, row["key"])).fetchone()))
    return made


def _mark_finished_blocks(c, user_id: int):
    """A planned block whose end has passed is 'due': the student is asked whether they did it.
    Times are stored as UTC ISO strings, so they compare as text."""
    now = datetime.now(timezone.utc).isoformat()
    c.execute("UPDATE blocks SET status = 'done' WHERE user_id = ? AND status = 'planned' AND kind = 'chase' AND COALESCE(end_at, start_at) < ?", (user_id, now))
    c.execute("UPDATE blocks SET status = 'due' WHERE user_id = ? AND status = 'planned' AND COALESCE(end_at, start_at) < ?", (user_id, now))


def _shape_application(a: sqlite3.Row, blocks: list[sqlite3.Row]) -> dict:
    out = dict(a)
    out["colour"] = HEX_OF.get(a["colour_id"], "#66d19e")
    out["blocks"] = [dict(b) for b in blocks]
    now = datetime.now(timezone.utc).isoformat()
    upcoming = [b for b in blocks if b["status"] == "planned" and b["start_at"] >= now]
    out["next_block"] = dict(min(upcoming, key=lambda b: b["start_at"])) if upcoming else None
    return out


def get_applications(user_id: int, application_id: int | None = None) -> list[dict]:
    with conn() as c:
        _mark_finished_blocks(c, user_id)
        q = "SELECT * FROM applications WHERE user_id = ?" + (" AND id = ?" if application_id else "") + " ORDER BY updated_at DESC, id DESC"
        apps = c.execute(q, (user_id, application_id) if application_id else (user_id,)).fetchall()
        blocks = c.execute(
            "SELECT * FROM blocks WHERE user_id = ? AND status NOT IN ('cancelled', 'moved') ORDER BY start_at", (user_id,)
        ).fetchall()
    by_app: dict[int, list] = {}
    for b in blocks:
        by_app.setdefault(b["application_id"], []).append(b)
    return [_shape_application(a, by_app.get(a["id"], [])) for a in apps]


def add_blocks(user_id: int, results: list[dict]) -> int:
    """Stores the blocks the tool booked (status booked or already). A block with a known event id is not stored twice."""
    added = 0
    with conn() as c:
        for r in results:
            if r.get("status") not in ("booked", "already") or not r.get("application_id") or not r.get("start"):
                continue
            if r.get("event_id") and c.execute("SELECT 1 FROM blocks WHERE user_id = ? AND event_id = ? AND status != 'cancelled'", (user_id, r["event_id"])).fetchone():
                continue
            c.execute(
                "INSERT INTO blocks(application_id, user_id, kind, title, start_at, end_at, event_id, link, resource_url, resource_label) "
                "SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM applications WHERE id = ? AND user_id = ?)",
                (r["application_id"], user_id, r["kind"], r["title"], r["start"], r.get("end"), r.get("event_id"), r.get("link"),
                 r.get("resource_url"), r.get("resource_label"), r["application_id"], user_id),
            )
            added += 1
    return added


def cancel_blocks(user_id: int, block_ids: list[int]):
    with conn() as c:
        c.executemany("UPDATE blocks SET status = 'cancelled' WHERE user_id = ? AND id = ?", [(user_id, i) for i in block_ids])


def set_stage(user_id: int, application_id: int, stage: str, *, deadline: str | None = None, start: str | None = None,
              assumed: bool = False, note: str | None = None):
    with conn() as c:
        c.execute(
            "UPDATE applications SET stage = ?, stage_deadline = ?, stage_start = ?, deadline_assumed = ?, note = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
            (stage, deadline, start, int(assumed), note, application_id, user_id),
        )
        if stage != "applied":  # they have news, so the "have you heard anything?" question is answered
            c.execute("UPDATE agent_log SET status = 'done' WHERE application_id = ? AND user_id = ? AND kind = 'chase' AND status = 'open'", (application_id, user_id))
        if stage == "applied":  # the apply block is finished business whether or not its slot has passed
            c.execute("UPDATE blocks SET status = 'done' WHERE application_id = ? AND user_id = ? AND kind = 'apply' AND status IN ('planned', 'due')", (application_id, user_id))
        if stage in ("offer", "rejected", "withdrawn"):  # nothing left to ask about
            c.execute("UPDATE blocks SET status = 'cancelled' WHERE application_id = ? AND user_id = ? AND status IN ('planned', 'due')", (application_id, user_id))



def get_checkins(user_id: int) -> list[dict]:
    """Blocks that have ended and are waiting for 'did you do it?', oldest first, with their application."""
    with conn() as c:
        _mark_finished_blocks(c, user_id)
        rows = c.execute(
            "SELECT b.*, a.company, a.title AS app_title, a.colour_id, a.stage, a.deadline AS app_deadline, "
            "a.stage_deadline, a.stage_start FROM blocks b JOIN applications a ON a.id = b.application_id "
            "WHERE b.user_id = ? AND b.status = 'due' ORDER BY COALESCE(b.end_at, b.start_at)", (user_id,)).fetchall()
    return [{**dict(r), "colour": HEX_OF.get(r["colour_id"], "#66d19e")} for r in rows]


def get_block(user_id: int, block_id: int) -> dict | None:
    rows = [b for b in get_checkins(user_id) if b["id"] == block_id]
    return rows[0] if rows else None


def finish_block(user_id: int, block_id: int, status: str, note: str | None = None):
    with conn() as c:
        c.execute("UPDATE blocks SET status = ?, note = COALESCE(?, note) WHERE id = ? AND user_id = ?", (status, note, block_id, user_id))


# ---- autopilot ----
def log_action(user_id: int, kind: str, message: str, application_id: int | None = None, status: str = "info", data: dict | None = None) -> int:
    with conn() as c:
        return c.execute(
            "INSERT INTO agent_log(user_id, application_id, kind, message, status, data) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, application_id, kind, message, status, json.dumps(data) if data else None),
        ).lastrowid


def get_log(user_id: int, limit: int = 40) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT l.*, a.company, a.colour_id FROM agent_log l LEFT JOIN applications a ON a.id = l.application_id "
            "WHERE l.user_id = ? AND l.kind != 'planner_run' ORDER BY l.id DESC LIMIT ?", (user_id, limit)).fetchall()
    return [{**dict(r), "colour": HEX_OF.get(r["colour_id"])} for r in rows]


def annotate_log(user_id: int, after_id: int, reason: str):
    """Attaches the planner's reason to every entry written since `after_id`, so the student can see why."""
    with conn() as c:
        for r in c.execute("SELECT id, data FROM agent_log WHERE user_id = ? AND id > ?", (user_id, after_id)).fetchall():
            data = json.loads(r["data"]) if r["data"] else {}
            data["reason"] = reason[:300]
            c.execute("UPDATE agent_log SET data = ? WHERE id = ?", (json.dumps(data), r["id"]))


def last_log_id(user_id: int) -> int:
    with conn() as c:
        return c.execute("SELECT COALESCE(MAX(id), 0) AS n FROM agent_log WHERE user_id = ?", (user_id,)).fetchone()["n"]


def log_since(user_id: int, after_id: int) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT id, kind, message FROM agent_log WHERE user_id = ? AND id > ? ORDER BY id", (user_id, after_id))]


def get_open_questions(user_id: int) -> list[dict]:
    """Questions the agent is waiting on (a quiet application it chased)."""
    return [r for r in get_log(user_id, 200) if r["status"] == "open"][::-1]


def answer_log(user_id: int, log_id: int, status: str = "done", answer: str | None = None) -> bool:
    """Closes an open question. `answer` (what the student chose) is kept with it so the planner can read it next time."""
    with conn() as c:
        row = c.execute("SELECT data FROM agent_log WHERE id = ? AND user_id = ? AND status = 'open'", (log_id, user_id)).fetchone()
        if not row:
            return False
        data = json.loads(row["data"]) if row["data"] else {}
        if answer:
            data["answer"] = answer[:200]
        c.execute("UPDATE agent_log SET status = ?, data = ? WHERE id = ? AND user_id = ?", (status, json.dumps(data) if data else None, log_id, user_id))
        return True


# ---- planner ----
def get_planner_state(user_id: int) -> dict:
    with conn() as c:
        r = c.execute("SELECT * FROM planner_state WHERE user_id = ?", (user_id,)).fetchone()
    if not r:
        return {}
    out = dict(r)
    out["concerns"] = json.loads(out["concerns"]) if out.get("concerns") else []
    return out


def save_planner_state(user_id: int, *, fingerprint: str | None = None, assessment: str | None = None, concerns: list[str] | None = None, mode: str | None = None):
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO planner_state(user_id) VALUES (?)", (user_id,))
        if fingerprint is not None:
            c.execute("UPDATE planner_state SET fingerprint = ? WHERE user_id = ?", (fingerprint, user_id))
        if assessment is not None:
            c.execute("UPDATE planner_state SET assessment = ?, concerns = ?, ran_at = ?, mode = ? WHERE user_id = ?",
                      (assessment, json.dumps(concerns or []), datetime.now(timezone.utc).isoformat(), mode or "planner", user_id))


def count_log_today(user_id: int, kind: str, needle: str | None = None) -> int:
    q, args = "SELECT COUNT(*) AS n FROM agent_log WHERE user_id = ? AND kind = ? AND created_at >= datetime('now', '-1 day')", [user_id, kind]
    if needle:
        q += " AND data LIKE ?"
        args.append(f"%{needle}%")
    with conn() as c:
        return c.execute(q, args).fetchone()["n"]


def recent_answers(user_id: int, days: int = 3) -> list[dict]:
    """Questions the student answered lately, so the planner remembers what they said."""
    with conn() as c:
        rows = c.execute("SELECT message, data, created_at FROM agent_log WHERE user_id = ? AND kind = 'ask' AND status = 'done' "
                         "AND data LIKE '%\"answer\"%' AND created_at >= datetime('now', ?) ORDER BY id DESC LIMIT 10", (user_id, f"-{days} days")).fetchall()
    return [{"question": r["message"], "answer": json.loads(r["data"]).get("answer"), "at": r["created_at"]} for r in rows]


def last_log_time(user_id: int, kind: str, application_id: int | None = None) -> str | None:
    q, args = "SELECT MAX(created_at) AS t FROM agent_log WHERE user_id = ? AND kind = ?", [user_id, kind]
    if application_id is not None:
        q += " AND application_id = ?"
        args.append(application_id)
    with conn() as c:
        return c.execute(q, args).fetchone()["t"]


def last_log_time_for_block(user_id: int, kind: str, block_id: int) -> str | None:
    with conn() as c:
        r = c.execute("SELECT created_at FROM agent_log WHERE user_id = ? AND kind = ? AND (data LIKE ? OR data LIKE ?) ORDER BY id DESC LIMIT 1",
                      (user_id, kind, f'%"block_id": {block_id},%', f'%"block_id": {block_id}}}%')).fetchone()
    return r["created_at"] if r else None


def count_log_since(user_id: int, kind: str, days: int) -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM agent_log WHERE user_id = ? AND kind = ? AND created_at >= datetime('now', ?)",
                         (user_id, kind, f"-{days} days")).fetchone()["n"]


def users_for_autopilot() -> list[int]:
    """Everyone with Google connected: the agent watches their calendar."""
    with conn() as c:
        return [r["user_id"] for r in c.execute("SELECT user_id FROM google_tokens")]


def blocks_with_app(user_id: int, statuses: tuple[str, ...]) -> list[dict]:
    """Blocks with the fields the autopilot tools need, oldest first."""
    marks = ",".join("?" * len(statuses))
    with conn() as c:
        _mark_finished_blocks(c, user_id)
        rows = c.execute(
            f"SELECT b.id, b.kind, b.title AS summary, b.start_at AS start, b.end_at AS end, b.event_id, b.status, b.resource_url, b.resource_label, "
            f"b.application_id, a.company, a.title AS application_title, a.colour_id, a.stage, a.deadline AS app_deadline, "
            f"a.stage_deadline, a.stage_start FROM blocks b JOIN applications a ON a.id = b.application_id "
            f"WHERE b.user_id = ? AND b.status IN ({marks}) ORDER BY b.start_at", (user_id, *statuses)).fetchall()
    return [dict(r) for r in rows]


def stale_due_blocks(user_id: int, hours: int = 24) -> list[dict]:
    """Blocks that finished more than `hours` ago and nobody answered 'did you do it?'."""
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    return [b for b in blocks_with_app(user_id, ("due",))
            if datetime.fromisoformat(b["end"] or b["start"]).timestamp() < cutoff]


def update_block(user_id: int, block_id: int, **fields):
    allowed = {"start_at", "end_at", "status", "note"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if sets:
        with conn() as c:
            c.execute(f"UPDATE blocks SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ? AND user_id = ?", (*sets.values(), block_id, user_id))


def applied_applications(user_id: int) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT id, company, colour_id, stage, updated_at FROM applications WHERE user_id = ? AND stage = 'applied'", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def get_watching(user_id: int) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM opportunities WHERE user_id = ? AND status = 'watching'", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def update_opportunity(user_id: int, opp_id: int, status: str | None = None, patch: dict | None = None):
    with conn() as c:
        if patch:
            row = c.execute("SELECT data FROM opportunities WHERE id = ? AND user_id = ?", (opp_id, user_id)).fetchone()
            if row:
                c.execute("UPDATE opportunities SET data = ? WHERE id = ? AND user_id = ?", (json.dumps({**json.loads(row["data"]), **patch}), opp_id, user_id))
        if status:
            c.execute("UPDATE opportunities SET status = ? WHERE id = ? AND user_id = ?", (status, opp_id, user_id))


def log_exists(user_id: int, kind: str, needle: str) -> bool:
    """True if an entry of this kind already mentions `needle` in its data: stops the agent repeating itself."""
    with conn() as c:
        return c.execute("SELECT 1 FROM agent_log WHERE user_id = ? AND kind = ? AND data LIKE ? LIMIT 1", (user_id, kind, f"%{needle}%")).fetchone() is not None
