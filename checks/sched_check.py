import asyncio, os, sys
os.environ.update(TAVILY_API_KEY="x", ANTHROPIC_API_KEY="x")
sys.path.insert(0, "/home/claude/opp2/backend")
from datetime import datetime, timedelta, timezone
import server

UTC = timezone.utc
now = datetime.now(UTC)
def at(days, h, m=0):  # a UTC time on a given future day (winter: UK == UTC)
    d = (now + timedelta(days=days)).replace(hour=h, minute=m, second=0, microsecond=0)
    return d
iso = lambda d: d.isoformat()

class Ex:
    def __init__(s, v): s.v = v
    def execute(s): return s.v
class Cal:
    """A fake Google Calendar with three calendars: the main one, a colour-coded 'uni' one, and one the student has switched off."""
    def __init__(s, existing): s.existing = existing; s.inserted = []; s.mode = "events"
    def events(s): s.mode = "events"; return s
    def calendarList(s): s.mode = "calendars"; return s
    def list(s, calendarId="primary", **k):
        if s.mode == "calendars":
            return Ex({"items": [{"id": "me@x.com", "primary": True, "selected": True}, {"id": "uni", "selected": True}, {"id": "off", "selected": False}]})
        return Ex({"items": [e for e in s.existing if e.get("_cal", "primary") == calendarId]})
    def insert(s, calendarId, body): s.inserted.append(body); return Ex({"id": f"evt{len(s.inserted)}", "htmlLink": "https://cal/x"})
def ev(title, start, end, **kw): return {"id": title, "summary": title, "start": {"dateTime": iso(start)}, "end": {"dateTime": iso(end)}, **kw}

def report(items, results):
    """One line per item, as the tool used to answer."""
    lines = []
    for r in results:
        title = items[r["index"]].get("title")
        when = datetime.fromisoformat(r["start"]).astimezone(server.LONDON) if r.get("start") else None
        if r["status"] == "booked":
            lines.append(f"Booked '{title}' for {when:%Y-%m-%d %H:%M %Z} ({r.get('reason', '')}) {r.get('link') or ''}".strip())
        elif r["status"] == "already":
            lines.append(f"Booked '{title}' (already in your calendar on {when:%Y-%m-%d %H:%M}) {r.get('link') or ''}".strip())
        elif r["status"] == "failed":
            lines.append(f"Could not find a valid slot for '{title}' before its deadline")
    return lines or ["No opportunities or tests with a future deadline to schedule."]

def run(existing, items, proposals):
    cal = Cal(existing)
    server.get_calendar_service = lambda: cal
    it = iter(proposals)
    async def fake_ask(messages, schema, **kw):
        sugg = next(it, [])
        return server.ScheduleSuggestions(suggestions=[server.SlotSuggestion(opportunity_index=i, start_time=iso(t), duration_hours=h, reason="r") for i, t, h in sugg])
    server.ask = fake_ask
    res = report(items, asyncio.run(server.schedule_application_with_llm(items)))
    return cal, res

def start_of(b): return datetime.fromisoformat(b["start"]["dateTime"]).astimezone(UTC)
item = lambda title, dl, **kw: {"title": title, "deadline": iso(dl), "type": "internship", "source": "web", **kw}

# 1. exam on day+3 10:00; model first proposes the afternoon before it (inside the 24h guard) and a clash, then a valid slot
exam = ev("Maths Exam", at(3,10), at(3,12))
busy = ev("Gym", at(2,18), at(2,19))
cal, res = run([exam, busy], [item("Spring Week", at(10,12))],
               [[(0, at(2,15), 1.5)], [(0, at(2,18,10), 1.0)], [(0, at(5,16), 1.5)]])
print("1:", [r[:70] for r in res]); print("   booked", [start_of(b) for b in cal.inserted])
assert len(cal.inserted)==1 and start_of(cal.inserted[0])==at(5,16)

# 2. model never gets it right -> code fallback finds a valid slot that respects the guard, buffer, hours
cal, res = run([exam, busy], [item("Spring Week", at(10,12))], [[(0, at(2,15), 1.5)]]*3)
b = start_of(cal.inserted[0]); print("2:", res[0][:90])
assert b.hour>=8 and not (at(2,10) - timedelta(hours=2) <= b <= at(3,12)), b  # nowhere near the exam's 24h guard

# 3. outside hours (23:00) is rejected, fallback used
cal, res = run([], [item("Role", at(6,12))], [[(0, at(1,23), 1.0)]]*3)
print("3:", res[0][:90]); assert 8 <= start_of(cal.inserted[0]).hour < 22

# 4. max two blocks a day: 3 items proposed on the same day, third must move
items3 = [item(f"Role {k}", at(8+k,12)) for k in range(3)]
cal, res = run([], items3, [[(0, at(2,9), 1.0), (1, at(2,12), 1.0), (2, at(2,15), 1.0)], [(2, at(3,9), 1.0)]])
days = [start_of(b).date() for b in cal.inserted]; print("4:", days)
assert days.count(at(2,9).date())==2 and len(cal.inserted)==3

# 5. already in calendar -> not booked twice, but reported as booked
cal, res = run([ev("Apply: Spring Week", at(2,10), at(2,11))], [item("Spring Week", at(9,12))], [])
print("5:", res); assert not cal.inserted and res[0].startswith("Booked 'Spring Week'")

# 6. interview (fixed time) day+4 14:00: it is booked as is, its prep block may go right before it,
#    and another company's block may not sit in the 24 hours before it
interview = {"title": "Bank interview", "summary": "Interview: Bank", "company": "Bank", "block_kind": "interview", "fixed_start": iso(at(4,14)), "exact_minutes": 60}
prep = {"title": "Bank prep", "summary": "Interview prep: Bank", "company": "Bank", "block_kind": "interview_prep", "deadline": iso(at(4,14)), "exact_minutes": 60}
other = item("Other role", at(9,12), company="Other")
cal, res = run([], [interview, prep, other], [[(1, at(4,11), 1.0), (2, at(3,16), 1.0)], [(2, at(6,16), 1.0)]])
titles = {b["summary"]: start_of(b) for b in cal.inserted}; print("6:", titles)
assert titles["Interview: Bank"] == at(4,14) and titles["Interview prep: Bank"] == at(4,11) and titles["Apply: Other role"] == at(6,16)

# 7. all-day events marked free, and declined ones, do not block
allday_free = {"id":"b","summary":"Birthday","transparency":"transparent","start":{"date": at(2,0).strftime("%Y-%m-%d")},"end":{"date": at(3,0).strftime("%Y-%m-%d")}}
declined = ev("Meeting", at(2,14), at(2,15), attendees=[{"self": True, "responseStatus": "declined"}])
cal, res = run([allday_free, declined], [item("Role", at(8,12))], [[(0, at(2,12,30), 1.0)]])
print("7:", res[0][:80]); assert start_of(cal.inserted[0]) == at(2,12,30)

# 8. EVERY timed event blocks, whatever its calendar, colour or free/busy flag. An all-day event marked busy blocks the day.
uni = ev("Lecture", at(2,10), at(2,12), _cal="uni", colorId="7")            # another, colour-coded calendar
maybe = ev("Maybe lunch", at(2,12,30), at(2,13,30), transparency="transparent", colorId="11")  # marked "free"
off = ev("Hidden calendar thing", at(2,15), at(2,16), _cal="off")           # a calendar the student switched off
allday_busy = {"id":"ab","summary":"Away","start":{"date": at(3,0).strftime("%Y-%m-%d")},"end":{"date": at(4,0).strftime("%Y-%m-%d")}}
world = [uni, maybe, off, allday_busy]
for label, bad, good in [("other calendar", at(2,11), at(2,16,30)), ("marked free", at(2,13), at(2,16,30)), ("all-day busy", at(3,10), at(2,16,30))]:
    cal, res = run(world, [item("Role", at(8,12))], [[(0, bad, 1.0)], [(0, good, 1.0)]])
    assert start_of(cal.inserted[0]) == good, (label, start_of(cal.inserted[0]))
    print("8:", label, "respected")
cal, res = run(world, [item("Role", at(8,12))], [[(0, at(2,15), 1.0)]])   # a calendar switched off in Google is not read
assert start_of(cal.inserted[0]) == at(2,15); print("8: switched-off calendar ignored")
print("ALL SCHEDULER CHECKS PASSED")
