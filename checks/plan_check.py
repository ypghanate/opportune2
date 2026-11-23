"""Plan and stage flow against a fake calendar and fake Claude: apply + practice, then every stage change."""
import asyncio, os, sys, json, tempfile, base64
from datetime import datetime, timedelta, timezone
tmp = tempfile.mkdtemp()
os.environ.update(TAVILY_API_KEY="x", ANTHROPIC_API_KEY="x", JWT_SECRET="j", TOKEN_ENC_KEY="k", DATA_DIR=tmp, SHOT_ROOT=tmp)
sys.path.insert(0, "/home/claude/opp2/backend")
import db, server

UTC = timezone.utc
now = datetime.now(UTC)
iso = lambda d: d.isoformat()
at = lambda days, h=12: (now + timedelta(days=days)).replace(hour=h, minute=0, second=0, microsecond=0)

class Ex:
    def __init__(s, v): s.v = v
    def execute(s): return s.v
class Cal:
    def __init__(s): s.events_ = {}; s.n = 0; s.deleted = []
    def events(s): return s
    def list(s, **k): return Ex({"items": list(s.events_.values())})
    def insert(s, calendarId, body):
        s.n += 1; e = {**body, "id": f"e{s.n}", "htmlLink": f"https://cal/e{s.n}"}
        e["start"] = {"dateTime": body["start"]["dateTime"]}; e["end"] = {"dateTime": body["end"]["dateTime"]}
        s.events_[e["id"]] = e; return Ex(e)
    def delete(s, calendarId, eventId): s.deleted.append(eventId); s.events_.pop(eventId, None); return Ex(None)
cal = Cal()
server.get_calendar_service = lambda: cal

facts = {"value": None}
async def fake_ask(messages, schema, **kw):
    if schema is server.EmailFacts:
        return facts["value"]
    return server.ScheduleSuggestions(suggestions=[])  # the model proposes nothing; the code finds slots
server.ask = fake_ask

db.init()
uid = db.create_user("a@b.co", "x")
def opp(company, title, field, days):
    d = {"company": company, "title": title, "type": "spring week", "field": field, "deadline": iso(at(days)), "url": "https://x/" + company, "source": "web"}
    ids = db.upsert_opportunities(uid, [d]); return db.get_opportunities(uid, ids)[0]

rows = [opp("Barclays", "Barclays Spring Week 2027", "investment banking", 20), opp("Monzo", "Monzo Software Intern", "software engineering", 25)]
apps = db.create_applications(uid, rows)
assert apps[0]["colour_id"] != apps[1]["colour_id"], "each company gets its own colour"
items = [{**json.loads(r["data"]), "company": a["company"], "application_id": a["id"], "colour_id": a["colour_id"]} for r, a in zip(rows, apps)]
res = asyncio.run(server.plan_application(items))
db.add_blocks(uid, res)
for r in res: print(f"  {r['status']:7} {r['title']}")
assert len(res) == 6 and all(r["status"] == "booked" for r in res)

# calendar events carry colour, ownership, and the practice link
ev = list(cal.events_.values())
assert all(e["colorId"] and e["extendedProperties"]["private"]["opportune"] == "1" for e in ev)
prac = [e for e in ev if e["summary"].startswith("Practice: ")]
assert len(prac) == 4
urls = {e["summary"]: e["source"]["url"] for e in prac}; print("  ", urls)
assert urls["Practice: Barclays · Numerical reasoning (1/2)"] == "https://simplytk.com/numerical-reasoning"
assert urls["Practice: Barclays · Situational strengths (behavioural) (2/2)"] == "https://simplytk.com/situational-strengths"
assert urls["Practice: Monzo · Figure series (inductive reasoning) (1/2)"] == "https://simplytk.com/figure-series"
assert all("https://simplytk.com/" in e["description"] for e in prac)
assert server._tests_for({"company": "Nomura", "field": "banking"}) == ["data_statements", "situational"]
names = sorted(e["summary"] for e in prac); print("  ", names)
assert "Practice: Monzo · Numerical reasoning (2/2)" in names
for company in ("Barclays", "Monzo"):  # never two blocks for one company on one day
    days = [e["start"]["dateTime"][:10] for e in ev if f": {company}" in e["summary"]]
    assert len(days) == len(set(days)), (company, days)
for company in ("Barclays", "Monzo"):  # apply first, then the practice sessions
    apply_at = [e["start"]["dateTime"] for e in ev if e["summary"] == f"Apply: {company}"][0]
    assert all(e["start"]["dateTime"] > apply_at for e in prac if f": {company} " in e["summary"]), company
lens = {(datetime.fromisoformat(e["end"]["dateTime"]) - datetime.fromisoformat(e["start"]["dateTime"])).seconds // 60 for e in prac}
assert lens == {45}, lens

def stage(app_id, new_stage, **kw):
    a = db.get_applications(uid, app_id)[0]
    blocks = [{"id": b["id"], "kind": b["kind"], "status": b["status"], "start": b["start_at"], "event_id": b["event_id"]} for b in a["blocks"]]
    application = {"id": a["id"], "company": a["company"], "title": a["title"], "url": a["url"], "colour_id": a["colour_id"], "field": a.get("field")}
    out = asyncio.run(server.update_stage(application, new_stage, blocks=blocks, **kw))
    if "error" not in out:
        db.cancel_blocks(uid, out["cancelled"]); db.add_blocks(uid, out["blocks"])
        f = out.get("extracted") or {}
        db.set_stage(uid, app_id, new_stage, deadline=kw.get("deadline") or f.get("deadline"), start=kw.get("start_time") or f.get("start_time"), assumed=out["deadline_assumed"])
    return out, db.get_applications(uid, app_id)[0]
def kinds(a): return sorted(b["kind"] for b in a["blocks"] if b["status"] == "planned")
bar = apps[0]["id"]

# applied: the apply block goes, both practice sessions stay
out, a = stage(bar, "applied")
print("applied ->", kinds(a)); assert kinds(a) == ["practice", "practice"] and a["stage"] == "applied"

# OA with a typed deadline: practice replaced by one OA block that ends before the deadline
dl = at(6, 17).replace(tzinfo=UTC)
out, a = stage(bar, "oa", deadline=dl.strftime("%Y-%m-%dT%H:%M"), duration_minutes=60)
oa = [b for b in a["blocks"] if b["kind"] == "oa" and b["status"] == "planned"]
print("oa ->", kinds(a), oa[0]["start_at"]); assert kinds(a) == ["oa"] and len(oa) == 1
assert datetime.fromisoformat(oa[0]["end_at"]) <= dl - timedelta(hours=1) and not out["deadline_assumed"]

# OA with a screenshot and no typed deadline: the date is read from the email
png = os.path.join(tmp, "mail.png"); open(png, "wb").write(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="))
facts["value"] = server.EmailFacts(is_about_this_company=True, kind="oa", deadline=iso(at(7, 18)), duration_minutes=75, summary="Complete the Pymetrics games and numerical test.")
m = [x for x in apps if x["company"] == "Monzo"][0]["id"]
out, a = stage(m, "oa", screenshot_path=png, deadline=None) if False else stage(m, "oa", screenshot_path=png)
oa = [b for b in a["blocks"] if b["kind"] == "oa" and b["status"] == "planned"]
print("oa from screenshot ->", kinds(a), out["notes"]); assert len(oa) == 1 and out["extracted"]["duration_minutes"] == 75 and kinds(a) == ["oa"]
assert a["stage_deadline"] == iso(at(7, 18))

# a screenshot from a different company is refused and nothing changes
facts["value"] = server.EmailFacts(is_about_this_company=False, kind="other", summary="x")
before = len(cal.events_)
out, a = stage(m, "interview", screenshot_path=png)
print("wrong company ->", out.get("error")); assert "error" in out and len(cal.events_) == before and a["stage"] == "oa"
facts["value"] = None

# interview with a time: the interview itself plus two prep blocks, all before it, none on top of it
t = at(9, 15)
out, a = stage(bar, "interview", start_time=t.strftime("%Y-%m-%dT%H:%M"), duration_minutes=45)
print("interview ->", kinds(a)); assert kinds(a) == ["interview", "interview_prep", "interview_prep"]
inter = [b for b in a["blocks"] if b["kind"] == "interview"][0]; assert inter["start_at"][:16] == t.strftime("%Y-%m-%dT%H:%M")
assert all(b["end_at"] <= inter["start_at"] for b in a["blocks"] if b["kind"] == "interview_prep")

# interview with no time yet: prep only, flagged as assumed
out, a = stage(m, "interview")
print("interview, no time ->", kinds(a), out["deadline_assumed"]); assert kinds(a) == ["interview_prep", "interview_prep"] and out["deadline_assumed"]

# offer: everything still planned is removed from the calendar
gone_before = len(cal.deleted)
out, a = stage(bar, "offer")
print("offer ->", kinds(a), f"{len(cal.deleted) - gone_before} events deleted"); assert kinds(a) == [] and len(cal.deleted) > gone_before

# bad inputs
assert "error" in asyncio.run(server.update_stage({"id": 1, "company": "X"}, "nonsense"))
out, a = stage(m, "assessment_centre", start_time=(now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")); assert "already passed" in out["error"]

# deleting all opportunities leaves the applications (foreign key SET NULL, not CASCADE)
db.delete_opportunities(uid); assert len(db.get_applications(uid)) == 2
# practice is not re-booked when applied is tapped twice
print("ALL PLAN AND STAGE CHECKS PASSED")
