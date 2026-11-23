"""The HTTP layer: approve -> applications -> stage change with a screenshot upload, using the fake calendar and Claude."""
import asyncio, os, sys, json, tempfile, base64, types
from datetime import datetime, timedelta, timezone
tmp = tempfile.mkdtemp()
os.environ.update(TAVILY_API_KEY="x", ANTHROPIC_API_KEY="x", JWT_SECRET="j" * 32, TOKEN_ENC_KEY="Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyMTI=", DATA_DIR=tmp)
sys.path.insert(0, "/home/claude/opp2/backend")
import db, server, runner
from fastapi.testclient import TestClient
import main

UTC = timezone.utc
now = datetime.now(UTC)
iso = lambda d: d.isoformat()
at = lambda days, h=12: (now + timedelta(days=days)).replace(hour=h, minute=0, second=0, microsecond=0)

class Ex:
    def __init__(s, v): s.v = v
    def execute(s): return s.v
class Cal:
    def __init__(s): s.ev = {}; s.n = 0
    def events(s): return s
    def list(s, **k): return Ex({"items": list(s.ev.values())})
    def insert(s, calendarId, body):
        s.n += 1; e = {**body, "id": f"e{s.n}", "htmlLink": "https://cal/x"}; s.ev[e["id"]] = e; return Ex(e)
    def delete(s, calendarId, eventId): s.ev.pop(eventId, None); return Ex(None)
cal = Cal(); server.get_calendar_service = lambda: cal
facts = {}
async def fake_ask(messages, schema, **kw):
    if schema is server.EmailFacts:
        img = messages[0]["content"][0]; assert img["type"] == "image" and img["source"]["media_type"] == "image/png"
        return server.EmailFacts(is_about_this_company=True, kind="oa", deadline=iso(at(6, 17)), duration_minutes=60, summary="Do the test.")
    return server.ScheduleSuggestions(suggestions=[])
server.ask = fake_ask

async def fake_plan(user_id, items): return await server.plan_application(items)
from contextlib import asynccontextmanager
@asynccontextmanager
async def fake_session(user_id):
    async def call(name, args):
        out = await getattr(server, name)(**args)
        return [out] if isinstance(out, dict) else out
    yield call
runner.tool_session = fake_session
async def fake_change(user_id, args):
    assert os.path.exists(args["screenshot_path"]) if "screenshot_path" in args else True
    return await server.update_stage(**args)
runner.plan, runner.change_stage = fake_plan, fake_change

c = TestClient(main.app)
import uk_data
with c:
    r = c.post("/api/auth/register", json={"email": "t@x.co", "password": "longenoughpw1", "name": "Test Student", "university": uk_data.UNIVERSITIES[0], "degree": uk_data.DEGREES[0]})
    assert r.status_code == 200, r.text
    H = {"Authorization": "Bearer " + r.json()["access_token"]}
    uid = db.get_user_by_email("t@x.co")["id"]; db.save_google_token(uid, "tok")
    d = {"company": "Barclays", "title": "Barclays Spring Week", "type": "spring week", "field": "banking", "deadline": iso(at(20)), "url": "https://b", "source": "web"}
    db.upsert_opportunities(uid, [d])
    opp = c.get("/api/opportunities", headers=H).json()[0]

    r = c.post("/api/opportunities/approve", json=[opp["id"]], headers=H); assert r.status_code == 200, r.text
    print("approve:", r.json()["report"])
    assert c.get("/api/opportunities", headers=H).json()[0]["status"] == "approved"
    apps = c.get("/api/applications", headers=H).json()
    a = apps[0]; print("app:", a["company"], a["stage"], a["colour"], len(a["blocks"]), "blocks; next:", a["next_block"]["kind"])
    assert len(apps) == 1 and a["stage"] == "to_apply" and len(a["blocks"]) == 3 and a["colour"].startswith("#")
    assert any(b["resource_url"] == "https://simplytk.com/numerical-reasoning" for b in a["blocks"])
    assert c.post("/api/opportunities/approve", json=[opp["id"]], headers=H).status_code == 400  # not approved twice

    # a plain tap
    r = c.post(f"/api/applications/{a['id']}/stage", data={"stage": "applied"}, headers=H); assert r.status_code == 200, r.text
    assert r.json()["stage"] == "applied" and sorted(b["kind"] for b in r.json()["blocks"] if b["status"] == "planned") == ["practice", "practice"]

    # OA with a screenshot
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
    r = c.post(f"/api/applications/{a['id']}/stage", data={"stage": "oa"}, files={"screenshot": ("mail.png", png, "image/png")}, headers=H)
    assert r.status_code == 200, r.text
    j = r.json(); print("oa:", j["stage"], j["stage_deadline"], [b["kind"] for b in j["blocks"] if b["status"] == "planned"])
    assert j["stage_deadline"] == iso(at(6, 17)) and [b["kind"] for b in j["blocks"] if b["status"] == "planned"] == ["oa"]
    assert not any(os.scandir(os.path.join(tmp, "screenshots", str(uid)))), "screenshot is deleted after reading"

    # rejects
    bad = c.post(f"/api/applications/{a['id']}/stage", data={"stage": "oa"}, files={"screenshot": ("x.png", b"not an image", "image/png")}, headers=H)
    assert bad.status_code == 400, bad.text
    assert c.post(f"/api/applications/{a['id']}/stage", data={"stage": "bogus"}, headers=H).status_code == 422
    assert c.post("/api/applications/999/stage", data={"stage": "applied"}, headers=H).status_code == 404
    assert c.get("/api/applications").status_code in (401, 403)
    # another user cannot touch it
    r2 = c.post("/api/auth/register", json={"email": "o@x.co", "password": "longenoughpw1", "name": "Other Person", "university": uk_data.UNIVERSITIES[0], "degree": uk_data.DEGREES[0]})
    H2 = {"Authorization": "Bearer " + r2.json()["access_token"]}
    assert c.get("/api/applications", headers=H2).json() == [] and c.post(f"/api/applications/{a['id']}/stage", data={"stage": "applied"}, headers=H2).status_code == 404

    # ---- check-ins: a finished block asks 'did you do it?'
    import sqlite3
    def age_block(block_id, hours=3):
        con = sqlite3.connect(db.DB_PATH); end = now - timedelta(hours=hours)
        cal.ev.pop(con.execute("SELECT event_id FROM blocks WHERE id = ?", (block_id,)).fetchone()[0], None)  # it is in the past now
        con.execute("UPDATE blocks SET start_at = ?, end_at = ? WHERE id = ?", (iso(end - timedelta(minutes=45)), iso(end), block_id)); con.commit(); con.close()
    # a fresh application for these: Barclays is at stage oa already
    d2 = {"company": "Optiver", "title": "Optiver Internship", "type": "internship", "field": "trading", "deadline": iso(at(20)), "url": "https://o", "source": "web"}
    db.upsert_opportunities(uid, [d2]); o2 = [o for o in c.get("/api/opportunities", headers=H).json() if o["company"] == "Optiver"][0]
    assert c.post("/api/opportunities/approve", json=[o2["id"]], headers=H).status_code == 200
    ap = [x for x in c.get("/api/applications", headers=H).json() if x["company"] == "Optiver"][0]
    assert c.get("/api/checkins", headers=H).json() == [], "nothing is due yet"
    prac = [b for b in ap["blocks"] if b["kind"] == "practice"]
    apply_b = [b for b in ap["blocks"] if b["kind"] == "apply"][0]
    age_block(prac[0]["id"]); age_block(prac[1]["id"], 2); age_block(apply_b["id"], 4)
    due = c.get("/api/checkins", headers=H).json(); print("due:", [b["title"] for b in due])
    assert len(due) == 3 and due[0]["kind"] == "apply" and due[0]["colour"].startswith("#")   # oldest first
    # yes, with a score
    r = c.post(f"/api/blocks/{prac[0]['id']}/checkin", json={"done": True, "note": "numerical 9/12"}, headers=H); assert r.json()["status"] == "done", r.text
    # no: the agent books it again at a later time
    n_before = len(cal.ev)
    r = c.post(f"/api/blocks/{prac[1]['id']}/checkin", json={"done": False}, headers=H); print("not done:", r.json()); assert r.json()["status"] == "rescheduled", r.text
    assert len(cal.ev) == n_before + 1
    ap2 = c.get("/api/applications", headers=H).json(); ap2 = [x for x in ap2 if x["company"] == "Optiver"][0]
    new = [b for b in ap2["blocks"] if b["status"] == "planned" and b["kind"] == "practice"]
    assert len(new) == 1 and new[0]["start_at"] > iso(now) and new[0]["resource_url"].startswith("https://simplytk.com/")
    assert new[0]["title"] == prac[1]["title"]
    # apply block missed: rebooked before its deadline, the first slot
    r = c.post(f"/api/blocks/{apply_b['id']}/checkin", json={"done": False}, headers=H); assert r.json()["status"] == "rescheduled", r.text
    assert c.get("/api/checkins", headers=H).json() == []
    # answering twice, or someone else's block, is refused
    assert c.post(f"/api/blocks/{prac[0]['id']}/checkin", json={"done": True}, headers=H).status_code == 404
    # a practice block that is no longer needed (the company is now at the interview stage) is just marked missed
    ap3 = [x for x in c.get("/api/applications", headers=H).json() if x["company"] == "Optiver"][0]
    pr = [b for b in ap3["blocks"] if b["kind"] == "practice" and b["status"] == "planned"][0]
    r = c.post(f"/api/applications/{ap3['id']}/stage", data={"stage": "interview"}, headers=H); assert r.status_code == 200, r.text
    # the stage change removed planned blocks, and an offer closes everything
    assert [b for b in r.json()["blocks"] if b["kind"] == "practice" and b["status"] == "planned"] == []
    # ---- the agent's activity
    log = c.get("/api/agent/log", headers=H).json(); print("log:", [l["kind"] for l in log][:5]); assert log and "rebook" in [l["kind"] for l in log]
    assert c.get("/api/agent/questions", headers=H).json() == []
    qid = db.log_action(uid, "chase", "Heard anything from X?", ap2["id"], status="open")
    q = c.get("/api/agent/questions", headers=H).json(); assert len(q) == 1 and q[0]["id"] == qid
    assert c.post(f"/api/agent/log/{qid}/answer", headers=H).status_code == 200 and c.get("/api/agent/questions", headers=H).json() == []
    assert c.post(f"/api/agent/log/{qid}/answer", headers=H).status_code == 404
    r = c.post("/api/agent/run", headers=H); assert r.status_code == 200, r.text; assert c.post("/api/agent/run", headers=H).status_code == 429
    assert c.get("/api/agent/log", headers=H2).json() == [], "another student sees nothing of it"
    prof = c.get("/api/profile", headers=H).json(); assert prof.get("autopilot", True) is True
print("ALL API CHECKS PASSED")
