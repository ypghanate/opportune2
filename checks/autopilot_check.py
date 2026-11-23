"""The autopilot against a fake Google account with several calendars: clash repair, auto-rebook after silence,
chasing and the opening watcher. The tools are called exactly as the MCP server would, and the model is faked."""
import asyncio, os, sys, json, tempfile, sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
tmp = tempfile.mkdtemp()
os.environ.update(TAVILY_API_KEY="x", ANTHROPIC_API_KEY="x", JWT_SECRET="j" * 32, TOKEN_ENC_KEY="Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyMTI=", DATA_DIR=tmp, AGENT_INTERVAL_MIN="0")
sys.path.insert(0, "/home/claude/opp2/backend")
import db, server, runner, autopilot

UTC = timezone.utc
now = datetime.now(UTC)
iso = lambda d: d.isoformat()
at = lambda days, h=12, m=0: (now + timedelta(days=days)).replace(hour=h, minute=m, second=0, microsecond=0)

class Ex:
    def __init__(s, v): s.v = v
    def execute(s): return s.v
class Cal:
    def __init__(s): s.ev = {}; s.n = 0; s.mode = "events"
    def events(s): s.mode = "events"; return s
    def calendarList(s): s.mode = "cals"; return s
    def list(s, calendarId="primary", **k):
        if s.mode == "cals": return Ex({"items": [{"id": "me", "primary": True, "selected": True}, {"id": "uni", "selected": True}]})
        return Ex({"items": [e for e in s.ev.values() if e["_cal"] == calendarId]})
    def insert(s, calendarId, body):
        s.n += 1; e = {**body, "id": f"e{s.n}", "htmlLink": f"https://cal/e{s.n}", "_cal": "primary"}; s.ev[e["id"]] = e; return Ex(e)
    def delete(s, calendarId, eventId): s.ev.pop(eventId); return Ex(None)
    def add(s, title, start, end, cal="uni", **kw):
        s.n += 1; e = {"id": f"x{s.n}", "summary": title, "start": {"dateTime": iso(start)}, "end": {"dateTime": iso(end)}, "_cal": cal, **kw}; s.ev[e["id"]] = e; return e
cal = Cal(); server.get_calendar_service = lambda: cal

async def fake_ask(messages, schema, **kw): return server.ScheduleSuggestions(suggestions=[])  # the code finds the slots
server.ask = fake_ask

@asynccontextmanager
async def fake_session(user_id):
    async def call(name, args):
        out = await getattr(server, name)(**args)
        return [out] if isinstance(out, dict) else out
    yield call
runner.tool_session = fake_session
async def fake_plan(uid, items): return await server.plan_application(items)
runner.plan = fake_plan

db.init()
uid = db.create_user("a@b.co", "x"); db.save_google_token(uid, "tok")
db.save_profile(uid, {"name": "Test Student", "university": "UCL", "degree": "CS", "graduation_year": 2028, "autopilot": True})
sql = lambda q, *a: (lambda con: (con.execute(q, a), con.commit(), con.close()))(sqlite3.connect(db.DB_PATH))
def opp(company, title, field, days, **extra):
    d = {"company": company, "title": title, "type": "spring week", "field": field, "deadline": iso(at(days)) if days else None, "url": f"https://x/{company}", "source": "web", **extra}
    ids = db.upsert_opportunities(uid, [d]); return db.get_opportunities(uid, ids)[0]
def tick(**kw): return asyncio.run(autopilot.tick(uid, **kw))
def planned(app_id, *kinds): return sorted((b for b in db.get_applications(uid, app_id)[0]["blocks"] if b["status"] == "planned" and (not kinds or b["kind"] in kinds)), key=lambda b: b["start_at"])
def log_kinds(): return [l["kind"] for l in db.get_log(uid, 100)]

rows = [opp("Barclays", "Barclays Spring Week", "banking", 20), opp("Optiver", "Optiver Internship", "trading", 25)]
results, booked, apps = asyncio.run(autopilot.plan_opportunities(uid, rows))
barc, opt = apps[0]["id"], apps[1]["id"]
assert len(booked) == 2
print("planned:", len(cal.ev), "events")

# ---- 1. nothing changed: the agent does nothing
assert tick() == {}; print("1. quiet when nothing changed")

# ---- 2. clash repair: a new event on ANOTHER (colour-coded) calendar lands on a practice block
prac = planned(barc, "practice")[0]
s, e = datetime.fromisoformat(prac["start_at"]), datetime.fromisoformat(prac["end_at"])
cal.add("Dentist", s + timedelta(minutes=10), e - timedelta(minutes=5), cal="uni", colorId="5")
out = tick(); print("2.", out, "|", db.get_log(uid, 1)[0]["message"])
assert out.get("moved") == 1
new = planned(barc, "practice"); assert len(new) == 2 and prac["id"] not in [b["id"] for b in new]
assert prac["event_id"] not in cal.ev, "the old calendar event is gone"
moved_to = [b for b in new if b["title"] == prac["title"]][0]
ms, me = datetime.fromisoformat(moved_to["start_at"]), datetime.fromisoformat(moved_to["end_at"])
assert not (ms < e - timedelta(minutes=5) and me > s + timedelta(minutes=10)), "no longer overlaps the dentist"
assert moved_to["resource_url"].startswith("https://simplytk.com/"), "keeps its practice link"
assert tick() == {}, "does not move it again"

# ---- 3. the student deletes a block in Google Calendar; another one is dragged to a new time
gone, dragged = planned(opt, "practice")[0], planned(opt, "practice")[1]
cal.ev.pop(gone["event_id"])
ev = cal.ev[dragged["event_id"]]; shift = timedelta(hours=2)
new_s = datetime.fromisoformat(ev["start"]["dateTime"]) + shift
ev["start"]["dateTime"], ev["end"]["dateTime"] = iso(new_s), iso(datetime.fromisoformat(ev["end"]["dateTime"]) + shift)
tick(); msgs = [l["message"] for l in db.get_log(uid, 5)]; print("3.", msgs[:2])
left = {b["id"]: b for b in planned(opt, "practice")}
assert gone["id"] not in left and datetime.fromisoformat(left[dragged["id"]]["start_at"]) == new_s

# ---- 4. an interview (fixed time) that clashes cannot be moved: warned once, not every pass
sql("UPDATE applications SET stage = 'interview' WHERE id = ?", barc)
itv = asyncio.run(server._schedule_blocks([{"block_kind": "interview", "title": "Barclays: interview", "summary": "Interview: Barclays", "company": "Barclays", "fixed_start": iso(at(6, 15)), "exact_minutes": 60, "application_id": barc, "colour_id": apps[0]["colour_id"]}]))
db.add_blocks(uid, itv)
cal.add("Society social", at(6, 15, 20), at(6, 17), cal="uni")
tick(); tick()
assert log_kinds().count("clash_warning") == 1, log_kinds(); print("4. interview clash warned once")
sql("UPDATE applications SET stage = 'applied' WHERE id = ?", barc)

# ---- 5. silence: a block ended yesterday and nobody answered -> rebooked automatically; a recent one is left alone
def age(block, hours):
    end = now - timedelta(hours=hours); cal.ev.pop(block["event_id"], None)
    sql("UPDATE blocks SET start_at = ?, end_at = ? WHERE id = ?", iso(end - timedelta(minutes=45)), iso(end), block["id"])
old, recent = planned(opt, "apply")[0], planned(opt, "practice")[0]
sql("UPDATE applications SET stage = 'to_apply' WHERE id = ?", opt)
age(old, 30); age(recent, 3)
out = tick(); print("5.", out, "|", [l["message"] for l in db.get_log(uid, 3)][0])
assert out.get("rebooked") == 1
due = [b["id"] for b in db.get_checkins(uid)]; assert recent["id"] in due and old["id"] not in due, "the recent one still waits for the student"
assert [b for b in planned(opt, "apply")] and planned(opt, "apply")[0]["id"] != old["id"]
assert "You did not answer" in [l["message"] for l in db.get_log(uid, 5) if l["kind"] == "rebook"][0]

# ---- 6. chasing: applied a week ago and no news
sql("UPDATE applications SET stage = 'applied', updated_at = datetime('now', '-7 days') WHERE id = ?", barc)
out = tick(); print("6.", out, "|", [l["message"] for l in db.get_log(uid, 3) if l["kind"] == "chase"][0][:90])
assert out.get("chased") == 1
chase_blocks = planned(barc, "chase"); assert len(chase_blocks) == 1
assert datetime.fromisoformat(chase_blocks[0]["end_at"]) - datetime.fromisoformat(chase_blocks[0]["start_at"]) == timedelta(minutes=15)
assert chase_blocks[0]["title"] == "Check: Barclays"
q = db.get_open_questions(uid); assert len(q) == 1 and q[0]["application_id"] == barc
assert tick().get("chased") is None, "not chased again straight away"
sql("UPDATE blocks SET start_at = ?, end_at = ? WHERE id = ?", iso(now - timedelta(hours=5)), iso(now - timedelta(hours=4, minutes=45)), chase_blocks[0]["id"])
assert chase_blocks[0]["id"] not in [b["id"] for b in db.get_checkins(uid)], "a reminder is never asked about"
assert db.answer_log(uid, q[0]["id"]) and db.get_open_questions(uid) == []

# ---- 7. opening watcher
async def fake_check(pool, now_, seconds):
    for e in pool:
        if e["company"] == "Jane Street": e.update(open_status="open", alive=True, deadline=iso(at(12)), open_evidence="Applications are now open")
        elif e["company"] == "Citadel": e.update(open_status="closed", alive=True, open_evidence="closed")
        else: e.update(open_status="not_yet_open", alive=True, opens_on=iso(at(30)))
server._check_open = fake_check
w1 = opp("Jane Street", "Jane Street Insight Programme", "quant trading", None, watch=True, opens_on=iso(at(-1)))
w2 = opp("Citadel", "Citadel Spring Week", "quant", None, watch=True)
w3 = opp("Goldman Sachs", "GS Spring Week", "banking", None, watch=True)
assert {w["status"] for w in (w1, w2, w3)} == {"watching"}
out = tick(); print("7.", out, "|", [l["message"] for l in db.get_log(uid, 4) if l["kind"] == "watch"])
assert out.get("opened") == 1
js = [a for a in db.get_applications(uid) if a["company"] == "Jane Street"][0]
assert js["stage"] == "to_apply" and sorted(b["kind"] for b in js["blocks"]) == ["apply", "practice", "practice"]
assert len({a["colour_id"] for a in db.get_applications(uid)}) == len(db.get_applications(uid)), "every company has its own colour"
st = {o["id"]: o["status"] for o in db.get_opportunities(uid)}
assert st[w1["id"]] == "approved" and st[w2["id"]] == "expired" and st[w3["id"]] == "watching"
assert tick() == {}, "the watch on Goldman Sachs is not re-checked for 20 hours"

# ---- 8. autopilot switched off in the profile: it leaves everything alone (unless asked)
db.save_profile(uid, {**db.get_profile(uid)[0], "autopilot": False})
cal.add("Another clash", datetime.fromisoformat(planned(js["id"], "apply")[0]["start_at"]), datetime.fromisoformat(planned(js["id"], "apply")[0]["end_at"]))
assert tick() == {"skipped": "autopilot is off"}
assert tick(force=True).get("moved") == 1; print("8. off means off; 'run now' still works")
db.save_profile(uid, {**db.get_profile(uid)[0], "autopilot": True})

# ---- 9. weekly discovery: new, clearly good, open programmes are booked on their own (at most three)
cv = os.path.join(tmp, "cv.pdf"); open(cv, "wb").write(b"%PDF-1.4"); db.set_cv(uid, cv, "cv.pdf")
def hit(name, **kw): return {"company": name, "title": f"{name} Internship", "type": "internship", "field": "software", "deadline": iso(at(15)), "url": f"https://x/{name}", "match": "strong", "open_status": "open", "score": 9, "source": "web", **kw}
found = [hit(n) for n in ("Alpha", "Beta", "Gamma", "Delta")] + [hit("Epsilon", match="partial"), hit("Zeta", watch=True, deadline=None, open_status="not_yet_open")]
async def fake_search(*a, **k): return {"items": found, "message": ""}
runner.search = fake_search; runner.active_run_for = lambda u: None
out = tick(); msg = db.get_log(uid, 1)[0]["message"]; print("9.", msg)
st = {json.loads(o["data"])["company"]: o["status"] for o in db.get_opportunities(uid)}
assert [c for c in ("Alpha", "Beta", "Gamma", "Delta") if st[c] == "approved"] == ["Alpha", "Beta", "Gamma"]
assert st["Delta"] == "pending" and st["Epsilon"] == "pending" and st["Zeta"] == "watching"
assert tick().get("discovered") is None, "it searches once a week, not every pass"
print("ALL AUTOPILOT CHECKS PASSED")
