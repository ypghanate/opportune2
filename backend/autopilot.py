"""The autopilot: the agent working in the background, without anyone pressing a button.

Every few minutes it looks at each connected student. The planner (planner.py, a Claude agent) reads the whole situation and
decides what to do. If it fails, or is switched off (PLANNER=0), the fixed steps below do the work instead:
  repair_clashes        a new event overlaps a planned block, or a block was deleted or dragged: move it, or tidy the plan
  rebook_missed_blocks  a block finished and nobody answered "did you do it?" for a day: book it again
  nudge                 a block finished and nobody has said whether they did it: prompt them now, and again every few hours
  chase_applications    an application has gone quiet for 6 days: book a reminder and ask
  watch_openings        a programme was not open yet: read its page, and when it opens book the application
Everything it does is written to the agent log, so the student can see what happened and why.
"""
import asyncio
import json
import re
import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
import db
import runner

log = logging.getLogger("uvicorn.error")
FIXED_KINDS_UI = ("interview", "assessment_centre")
LONDON = ZoneInfo("Europe/London")
INTERVAL_MIN = int(os.environ.get("AGENT_INTERVAL_MIN", "5"))  # 0 turns the background loop off
SILENCE_HOURS = 24  # no answer to "did you do it?" for this long counts as "no"
WATCH_EVERY_HOURS = float(os.environ.get("WATCH_EVERY_HOURS", "1"))  # how often a programme that has not opened yet is looked at
NUDGE_AFTER_MIN = 30  # a finished block with no answer gets its first nudge after this long
NUDGE_EVERY_HOURS = 4  # and another this often, until the silence rule rebooks it
CHASE_AFTER_DAYS = 6
DISCOVER_EVERY_HOURS = float(os.environ.get("AUTO_DISCOVERY_HOURS", "2"))  # how often it searches for new programmes; 0 turns automatic searching off
FOLLOW_UP_MIN = float(os.environ.get("SEARCH_FOLLOW_UP_MIN", "2"))  # after a student runs a search, look again this many minutes later; 0 = off
MAX_AUTO_PLANS = 3  # new programmes the agent may book on its own per search

_locks: dict[int, asyncio.Lock] = {}
activity: dict[int, dict] = {}  # what the Planner Agent (or the Researcher it asked) is doing this second: shown in the bar at the top


def set_activity(user_id: int, text: str, who: str = "planner"):
    activity[user_id] = {"text": text, "who": who, "since": datetime.now(timezone.utc).isoformat()}


def clear_activity(user_id: int):
    activity.pop(user_id, None)


loop_beat: dict = {}  # when the background loop last went round (proof it is awake)
last_pass: dict[int, str] = {}  # when each student's last pass finished (in memory: the Applications page shows it)


def _when(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(LONDON).strftime("%a %d %b, %H:%M")
    except (TypeError, ValueError):
        return "a later time"


def _parse(v) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(v).replace(" ", "T", 1).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _first(found: list[dict]) -> dict:
    return found[0] if found else {}


def _new_blocks(results: list[dict], application_id: int | None = None) -> list[dict]:
    return [{**r, "application_id": application_id or r.get("application_id")} for r in results if r.get("status") in ("booked", "already")]


# ---------------------------------------------------------------- planning (shared with the approve button)
async def plan_opportunities(user_id: int, rows: list[dict]) -> tuple[list[dict], list[int], list[dict]]:
    """Starts an application for each opportunity row and books its Apply block and practice sessions.
    Returns (block results, ids of the opportunities that got an Apply block, the applications)."""
    apps = db.create_applications(user_id, rows)
    app_of = {a["opportunity_id"]: a for a in apps}
    items = [{**json.loads(r["data"]), "company": app_of[r["id"]]["company"], "application_id": app_of[r["id"]]["id"],
              "colour_id": app_of[r["id"]]["colour_id"]} for r in rows]
    results = await runner.plan(user_id, items)
    db.add_blocks(user_id, results)
    got_apply = {r["application_id"] for r in results if r.get("kind") == "apply" and r.get("status") in ("booked", "already")}
    return results, [r["id"] for r in rows if app_of[r["id"]]["id"] in got_apply], apps


# ---------------------------------------------------------------- rebooking (shared with the check-in button)
def save_rebooks(user_id: int, actions: list[dict], by_id: dict[int, dict], *, silent: bool) -> list[dict]:
    """Stores what rebook_missed_blocks did. `silent` means the student never answered, so the log says so."""
    out = []
    for a in actions:
        b = by_id.get(a["block_id"], {})
        name, app_id = a.get("summary") or b.get("summary") or "a block", b.get("application_id")
        why = "You did not answer, so I assumed it did not happen. " if silent else ""
        if a["action"] == "rebooked":
            db.update_block(user_id, a["block_id"], status="missed")
            db.add_blocks(user_id, _new_blocks([a["new"]], app_id))
            msg = f"{why}Rebooked '{name}' for {_when(a['new'].get('start'))}."
            db.log_action(user_id, "rebook", msg, app_id, data={"block_id": a["block_id"]})
        elif a["action"] == "dropped":
            db.update_block(user_id, a["block_id"], status="missed")
            reason = {"fixed": "It was a fixed-time event, so it cannot be rebooked.", "not_needed": "It is not needed at this stage any more.",
                      "deadline_passed": "The deadline has passed, so there is no time left to book it."}.get(a.get("reason"), "")
            msg = f"{why}Marked '{name}' as missed. {reason}".strip()
            db.log_action(user_id, "rebook", msg, app_id, data={"block_id": a["block_id"]})
        else:  # failed: no free slot. It stays on the list; say so once
            msg = f"I could not find a free slot to rebook '{name}'. It stays on your list."
            if not db.log_exists(user_id, "rebook_failed", f'"block_id": {a["block_id"]}'):
                db.log_action(user_id, "rebook_failed", msg, app_id, data={"block_id": a["block_id"]})
        out.append({**a, "message": msg})
    return out


# ---------------------------------------------------------------- the four behaviours
async def _clashes(user_id: int, call, summary: dict):
    now = datetime.now(timezone.utc).isoformat()
    planned = [b for b in db.blocks_with_app(user_id, ("planned",)) if b["event_id"] and b["start"] > now]
    if not planned:
        return
    by_id = {b["id"]: b for b in planned}
    res = _first(await call("repair_clashes", {"blocks": planned}))
    for a in res.get("actions", []):
        b = by_id.get(a["block_id"], {})
        name, app_id, bid = a.get("summary") or b.get("summary"), b.get("application_id"), a["block_id"]
        if a["action"] == "removed":
            db.update_block(user_id, bid, status="cancelled")
            db.log_action(user_id, "sync", f"You removed '{name}' from your calendar, so I took it off your plan.", app_id)
            summary["removed"] = summary.get("removed", 0) + 1
        elif a["action"] == "retimed":
            db.update_block(user_id, bid, start_at=a["start"], end_at=a["end"])
            db.log_action(user_id, "sync", f"You moved '{name}' to {_when(a['start'])}, so I updated your plan.", app_id)
        elif a["action"] == "moved":
            db.update_block(user_id, bid, status="moved")
            db.add_blocks(user_id, _new_blocks([a["new"]], app_id))
            db.log_action(user_id, "clash", f"Moved '{name}' from {_when(a['old_start'])} to {_when(a['new'].get('start'))} "
                          f"because '{a['clashed_with']}' now clashes with it.", app_id, data={"block_id": bid})
            summary["moved"] = summary.get("moved", 0) + 1
        elif a["action"] in ("unmovable", "stuck"):
            if not db.log_exists(user_id, "clash_warning", f'"block_id": {bid}'):
                why = "It is at a time the employer set, so I cannot move it." if a["action"] == "unmovable" else "I could not find a free slot to move it to."
                db.log_action(user_id, "clash_warning", f"'{name}' clashes with '{a['clashed_with']}'. {why}", app_id, data={"block_id": bid})


async def _silence(user_id: int, call, summary: dict):
    stale = db.stale_due_blocks(user_id, SILENCE_HOURS)
    if not stale:
        return
    res = _first(await call("rebook_missed_blocks", {"blocks": stale}))
    done = save_rebooks(user_id, res.get("actions", []), {b["id"]: b for b in stale}, silent=True)
    summary["rebooked"] = summary.get("rebooked", 0) + sum(1 for a in done if a["action"] == "rebooked")


async def _nudge(user_id: int, summary: dict):
    """A block ended and nobody said whether they did it: prompt them. Needs no tool, only the database.
    The first nudge comes shortly after the block ends, then one every few hours; after a day the silence rule takes over."""
    now = datetime.now(timezone.utc)
    for b in db.blocks_with_app(user_id, ("due",)):
        ended = _parse(b["end"] or b["start"])
        if not ended or now - ended < timedelta(minutes=NUDGE_AFTER_MIN):
            continue
        last = _parse(db.last_log_time_for_block(user_id, "nudge", b["id"]))
        if last and now - last < timedelta(hours=NUDGE_EVERY_HOURS):
            continue
        hours = int((now - ended).total_seconds() // 3600)
        ago = f"{hours} hours ago" if hours >= 2 else "a little while ago"
        what = {"apply": "applying to", "practice": "your practice for", "oa": "your online assessment for", "test": "your test for"}.get(b["kind"], b["summary"].split(":")[0].lower() + " for")
        if b["kind"] in FIXED_KINDS_UI:
            msg = f"Did your {b['summary'].split(':')[0].lower()} with {b['company']} happen? It ended {ago}. Tell me so I can keep your plan right."
        else:
            msg = f"You haven't told me if you finished {what} {b['company']}. It ended {ago}. Tap Yes, or I will book it again."
        db.log_action(user_id, "nudge", msg, b["application_id"], data={"block_id": b["id"]})
        summary["nudged"] = summary.get("nudged", 0) + 1


async def _chase(user_id: int, call, summary: dict):
    cutoff = datetime.now(timezone.utc) - timedelta(days=CHASE_AFTER_DAYS)
    quiet = [a for a in db.applied_applications(user_id) if (_parse(a["updated_at"]) or datetime.now(timezone.utc)) <= cutoff]
    if not quiet:
        return
    apps = [{"application_id": a["id"], "company": a["company"], "stage": a["stage"], "stage_updated_at": a["updated_at"],
             "last_chase_at": db.last_log_time(user_id, "chase", a["id"]), "colour_id": a["colour_id"]} for a in quiet]
    res = _first(await call("chase_applications", {"applications": apps}))
    for r in res.get("results", []):
        for q in db.get_open_questions(user_id):  # an old unanswered question is replaced by the new one
            if q["application_id"] == r["application_id"] and q["kind"] == "chase":
                db.answer_log(user_id, q["id"], "done")
        if r.get("block", {}).get("status") == "booked":
            db.add_blocks(user_id, _new_blocks([r["block"]], r["application_id"]))
        when = f" I put a 15-minute reminder in your calendar for {_when(r['block'].get('start'))}." if r.get("block", {}).get("status") == "booked" else ""
        db.log_action(user_id, "chase", f"It has been {r['days_waiting']} days since you applied to {r['company']}. Have you heard anything?{when}",
                      r["application_id"], status="open", data={"company": r["company"]})
        summary["chased"] = summary.get("chased", 0) + 1


async def _watch(user_id: int, call, summary: dict, force: bool = False):
    now = datetime.now(timezone.utc)
    rows, watches, used = [], [], set()
    for row in db.get_watching(user_id):
        data = json.loads(row["data"])
        checked = _parse(data.get("watch_checked_at"))
        if checked and now - checked < timedelta(hours=WATCH_EVERY_HOURS) and not force:
            continue
        colour = db.pick_colour(user_id, data.get("company") or data.get("title") or "", avoid=used)
        used.add(colour)
        rows.append((row, data, colour))
        watches.append({"watch_id": row["id"], "company": data.get("company") or data.get("title"), "title": data.get("title"),
                        "url": data.get("url"), "type": data.get("type"), "field": data.get("field"), "colour_id": colour, "plan": True})
    if not watches:
        return
    res = _first(await call("watch_openings", {"watches": watches}))
    by_watch = {r["watch_id"]: r for r in res.get("results", [])}
    for row, data, colour in rows:
        r = by_watch.get(row["id"])
        if not r:
            continue
        company = data.get("company") or data.get("title")
        patch = {"watch_checked_at": now.isoformat()}
        if r.get("opens_on"):
            patch["opens_on"] = r["opens_on"]
        booked = [b for b in r.get("blocks", []) if b.get("status") in ("booked", "already")]
        if r["status"] == "open" and booked and any(b["kind"] == "apply" for b in booked):
            patch.update(deadline=r["deadline"], deadline_assumed=r["deadline_assumed"], watch=False, open_status="open")
            db.update_opportunity(user_id, row["id"], patch=patch)
            fresh = json.loads(next(x for x in db.get_opportunities(user_id, [row["id"]]))["data"])
            app = db.create_applications(user_id, [{"id": row["id"], "key": row["key"], "data": json.dumps(fresh), "colour_id": colour}])[0]
            db.add_blocks(user_id, _new_blocks(booked, app["id"]))
            db.update_opportunity(user_id, row["id"], status="approved")
            apply_block = next(b for b in booked if b["kind"] == "apply")
            db.log_action(user_id, "watch", f"{company} has opened applications. I booked '{apply_block['title']}' for {_when(apply_block.get('start'))}"
                          f" and {len(booked) - 1} practice sessions.", app["id"])
            summary["opened"] = summary.get("opened", 0) + 1
        elif r["status"] == "open":  # open, but nothing could be booked: keep watching and say so once
            db.update_opportunity(user_id, row["id"], patch=patch)
            if not db.log_exists(user_id, "watch_failed", f'"opp": {row["id"]}'):
                db.log_action(user_id, "watch_failed", f"{company} has opened applications, but I could not find a free slot before its deadline. Check your calendar.",
                              data={"opp": row["id"]})
        elif r["status"] == "closed":
            db.update_opportunity(user_id, row["id"], status="expired", patch=patch)
            db.log_action(user_id, "watch", f"{company} closed applications, so I stopped watching it.")
        else:
            db.update_opportunity(user_id, row["id"], patch=patch)


MAX_ANNOUNCE = 5  # most "new opportunity" questions open at once, so a big search does not flood the banner


def _announce_stale(user_id: int, minutes: int = 30) -> int:
    """Safety net for planner mode: a programme the Planner Agent has not got to within half an hour is announced the plain way."""
    now = datetime.now(timezone.utc)
    old = []
    for o in db.get_opportunities(user_id):
        made = _parse(str(o.get("created_at") or "").replace(" ", "T") + "+00:00") if o.get("created_at") else None
        if o["status"] == "pending" and made and now - made > timedelta(minutes=minutes):
            old.append(o)
    return _announce(user_id, old) if old else 0


def _announce(user_id: int, rows: list[dict]) -> int:
    """Asks the student about each new programme still waiting for approval: "New opportunity detected. Want me to plan it?"
    One open question per programme, never asked twice. The banner shows it with Plan it / Not for me buttons."""
    open_now = sum(1 for q in db.get_open_questions(user_id) if q["kind"] == "opportunity")
    made = 0
    for r in rows:
        if open_now + made >= MAX_ANNOUNCE:
            break
        d = json.loads(r["data"])
        if r["status"] != "pending" or db.log_exists(user_id, "opportunity", f'"opportunity_id": {r["id"]},'):
            continue
        place = f'{d.get("country") or ""} {d.get("location") or ""}'
        if re.search(r"\b(china|beijing|shanghai|shenzhen|hong kong|singapore|india|japan|tokyo|korea|dubai|new york|usa|united states|canada|australia|germany|france|switzerland)\b", place, re.I) \
                and not re.search(r"\b(uk|united kingdom|london|england|scotland|wales|online|remote)\b", place, re.I):
            continue  # abroad: never ask the student about it
        when = _parse(d.get("deadline"))
        closes = f" It closes {when.astimezone(LONDON).strftime('%a %d %b')}." if when else ""
        db.log_action(user_id, "opportunity", f"New opportunity detected: {d.get('title') or d.get('company')}.{closes} Want me to plan it?", None, status="open",
                      data={"opportunity_id": r["id"], "company": d.get("company"), "title": d.get("title"), "match": d.get("match"), "url": d.get("url")})
        made += 1
    return made


NEEDS_SEARCH = os.environ.get("DISCOVER_NEEDS_SEARCH", "1") != "0"  # the background search only runs once the student has run the agent themselves


def searched_lately(user_id: int) -> bool:
    """True when the student has run the agent within the last SESSION_GAP_HOURS. Until then nothing searches on its own."""
    if not NEEDS_SEARCH:
        return True
    last = _parse(db.last_log_time(user_id, "search_prompt"))
    return bool(last) and datetime.now(timezone.utc) - last < timedelta(hours=SESSION_GAP_HOURS)


async def _discover(user_id: int, profile: dict, summary: dict, force: bool = False, auto_plan: bool = True, announce: bool = True):
    """Search again, save what is new, and (when auto_plan) book the clearly good, open ones. Called by the Planner Agent every few hours."""
    import planner
    if not searched_lately(user_id):
        return  # a new visit starts quiet: the agent does not search until you have run it
    if not force and (not DISCOVER_EVERY_HOURS or not profile.get("autopilot", True)):
        return
    last = _parse(db.last_log_time(user_id, "discover"))
    if not force and last and datetime.now(timezone.utc) - last < timedelta(hours=DISCOVER_EVERY_HOURS):
        return
    started = _parse(db.last_log_time(user_id, "discover_start"))
    if not force and started and (not last or started > last) and datetime.now(timezone.utc) - started < timedelta(hours=6):
        return  # the last attempt did not finish: wait a few hours before trying again
    _, cv_path = db.get_profile(user_id)
    if not cv_path or not os.path.exists(cv_path) or runner.active_run_for(user_id) or db.runs_today(user_id) >= config.MAX_RUNS_PER_DAY:
        return
    if not all(profile.get(k) for k in ("name", "university", "degree", "graduation_year")):
        return
    set_activity(user_id, "Planner Agent asked the Researcher to search for new programmes…" if planner.ENABLED else "Researcher is searching for new programmes…", "researcher")
    db.log_action(user_id, "discover_start", "Searching for new programmes.")  # written first, so a failure is not retried every tick
    try:
        result = await runner.search(user_id, profile, cv_path, db.last_search_prompt(user_id) or "Find new spring weeks and internships for me.", ["internship", "spring_week"])
    except Exception as e:
        log.exception("Weekly search failed for user %s", user_id)
        db.log_action(user_id, "discover_failed", f"The search did not finish: {runner.describe(e)} I will try again in a few hours.")
        return
    known = {r["key"] for r in db.get_opportunities(user_id)}
    fresh = [it for it in result["items"] if db.item_key(it) not in known]
    ids = db.upsert_opportunities(user_id, fresh)
    rows = sorted(db.get_opportunities(user_id, ids), key=lambda r: ids.index(r["id"]))  # keep the search's best-first order
    strong = [r for r in rows if r["status"] == "pending" and (lambda d: d.get("match") == "strong" and d.get("open_status") == "open" and d.get("deadline"))(json.loads(r["data"]))]
    auto = strong[:MAX_AUTO_PLANS] if auto_plan else []
    names = []
    if auto:
        _, booked_ids, apps = await plan_opportunities(user_id, auto)
        db.mark_approved(user_id, booked_ids)
        names = [a["company"] for a in apps if a["opportunity_id"] in booked_ids]
    waiting = len([r for r in rows if r["status"] == "pending"]) - len(names)
    watching = len([r for r in rows if r["status"] == "watching"])
    text = f"Searched for new programmes and found {len(fresh)} new." if fresh else "Searched for new programmes and found nothing new."
    if names:
        text += f" I booked applications for {', '.join(names)}."
    if waiting > 0:
        text += f" {waiting} {'are' if not names else 'more are'} waiting for your approval."
    if watching:
        text += f" I am watching {watching} that have not opened yet."
    db.log_action(user_id, "discover", text)
    summary["discovered"] = len(fresh)
    if announce:  # in planner mode the Planner Agent reviews each new programme itself and says why it suggests it or not
        asked = _announce(user_id, sorted(db.get_opportunities(user_id, ids), key=lambda r: ids.index(r["id"])))  # best first; the booked ones are no longer pending
        if asked:
            summary["announced"] = asked


# ---------------------------------------------------------------- what the agent is doing right now (for the Applications page)
def status(user_id: int) -> list[dict]:
    """One line per behaviour, saying what the agent is doing for this student at this moment. Reads the database only."""
    profile, _ = db.get_profile(user_id)
    now = datetime.now(timezone.utc)
    clock = lambda iso: (_parse(iso) or now).astimezone(LONDON).strftime("%H:%M")
    if not db.get_google_token(user_id):
        return [{"key": "off", "on": False, "title": "Not connected", "text": "Connect your Google account on the Profile page so I can look after your calendar."}]
    if not profile.get("autopilot", True):
        return [{"key": "off", "on": False, "title": "Autopilot is off", "text": "Turn it on in your Profile and I will start watching again."}]
    import planner
    checked = last_pass.get(user_id)
    seen = f" Last checked at {clock(checked)}." if checked else " I check every few minutes."
    out = []
    if planner.ENABLED:
        st = db.get_planner_state(user_id)
        live = activity.get(user_id)
        if st.get("mode") == "rules":
            text = "The planner hit a problem, so I am using simple rules for now."
        elif live and (_parse(live["since"]) or now) > now - timedelta(minutes=10):
            text = live["text"]  # what it is doing this second
        elif st.get("assessment"):
            said = st["assessment"].strip()
            said = said if len(said) <= 220 else said[:217].rsplit(" ", 1)[0] + "…"
            worries = f" {len(st['concerns'])} thing{'s' * (len(st['concerns']) != 1)} I am keeping an eye on." if st.get("concerns") else ""
            text = f"{said}{worries} (Looked at {clock(st['ran_at'])}.)"  # its own words from the last time it looked
        else:
            text = "I have not looked at your plan yet. I will as soon as something changes."
        out.append({"key": "planner", "on": st.get("mode") != "rules", "title": "Planner Agent", "text": text})

    ahead = [b for b in db.blocks_with_app(user_id, ("planned",)) if b["event_id"] and b["start"] > now.isoformat()]
    out.append({"key": "clash", "on": bool(ahead), "title": "Clash repair",
                "text": (f"Ready to reschedule if anything clashes with your {len(ahead)} upcoming block{'s' * (len(ahead) != 1)}." + seen) if ahead
                else "Nothing to protect yet. Once you have blocks in your calendar, I will move them if something clashes."})

    watching = db.get_watching(user_id)
    names = [(json.loads(w["data"]).get("company") or json.loads(w["data"]).get("title") or "a programme") for w in watching]
    last_watch = max((c for c in (_parse(json.loads(w["data"]).get("watch_checked_at")) for w in watching) if c), default=None)
    out.append({"key": "watch", "on": bool(watching), "title": "Opening watcher",
                "text": (f"Waiting for {len(watching)} programme{'s' * (len(watching) != 1)} that {'has' if len(watching) == 1 else 'have'}n't opened yet: {', '.join(names[:3])}"
                         f"{' and more' if len(names) > 3 else ''}. I book your application as soon as {'it opens' if len(watching) == 1 else 'they open'}."
                         + (f" Last looked at {clock(last_watch.isoformat())}." if last_watch else "")) if watching
                else "No programmes waiting to open. When a search finds one that is not open yet, I watch it for you."})

    if DISCOVER_EVERY_HOURS:
        last = _parse(db.last_log_time(user_id, "discover"))
        nxt = (last + timedelta(hours=DISCOVER_EVERY_HOURS)).astimezone(LONDON).strftime("%H:%M") if last else "soon"
        every = f"{DISCOVER_EVERY_HOURS:g} hour{'s' * (DISCOVER_EVERY_HOURS != 1)}"
        out.append({"key": "discover", "on": True, "title": "New programmes",
                    "text": f"The Planner Agent asks the Researcher to search every {every} using your interests. It then tells you which ones it suggests, and why, or why it would skip one. Next search: {nxt}."})

    due = db.blocks_with_app(user_id, ("due",))
    out.append({"key": "nudge", "on": True, "title": "Check-ins",
                "text": (f"Waiting for you to tell me if you finished {len(due)} block{'s' * (len(due) != 1)}. I will keep nudging you, then rebook {'it' if len(due) == 1 else 'them'} after a day.") if due
                else "Nothing waiting. When a block ends I will ask whether you did it, nudge you if you don't answer, and rebook it if you still don't."})

    applied = db.applied_applications(user_id)
    out.append({"key": "chase", "on": bool(applied), "title": "Replies",
                "text": (f"Waiting to hear back from {len(applied)} application{'s' * (len(applied) != 1)}. I will ask you if there is no news after {CHASE_AFTER_DAYS} days.") if applied
                else "Nothing sent yet. After you apply, I will ask whether you have heard back."})
    return out


# ---------------------------------------------------------------- the loop
async def tick(user_id: int, force: bool = False, follow_up: bool = False) -> dict:
    """One pass for one student. Returns what it did, e.g. {'moved': 1}. Safe to call at any time."""
    import planner  # here, not at the top: planner imports this module
    profile, _ = db.get_profile(user_id)
    if not force and not profile.get("autopilot", True):
        return {"skipped": "autopilot is off"}
    lock = _locks.setdefault(user_id, asyncio.Lock())
    if lock.locked():
        return {"skipped": "already running"}
    summary: dict = {}
    async with lock:
        now = datetime.now(timezone.utc)
        planned_by_ai = False
        if planner.ENABLED:  # the planner decides what to do; the fixed steps below are only its safety net
            try:
                r = await planner.run(user_id, force=force, research_now=follow_up)
                planned_by_ai = True
                for k, v in (r.get("did") or {}).items():
                    summary[k] = summary.get(k, 0) + v
                if r.get("ran"):
                    summary["planner"] = 1
            except Exception as e:
                log.exception("Planner failed for user %s: falling back to the fixed steps", user_id)
                db.save_planner_state(user_id, assessment=db.get_planner_state(user_id).get("assessment") or "", concerns=[], mode="rules")
                summary["planner_failed"] = str(e)[:120]
        steps = (_silence, _chase, _watch) if planned_by_ai else (_clashes, _silence, _chase, _watch)
        if follow_up:  # the look two minutes after a student's own search: watched programmes are checked now, whatever their timer says
            async def _watch_now(uid, call, summ):
                await _watch(uid, call, summ, force=True)
            steps = tuple(_watch_now if st is _watch else st for st in steps)
        has_future = any(b["event_id"] and b["start"] > now.isoformat() for b in db.blocks_with_app(user_id, ("planned",)))
        due = bool(db.stale_due_blocks(user_id, SILENCE_HOURS))
        quiet = any((_parse(a["updated_at"]) or now) <= now - timedelta(days=CHASE_AFTER_DAYS) for a in db.applied_applications(user_id))
        watching = any(not (c := _parse(json.loads(w["data"]).get("watch_checked_at"))) or now - c >= timedelta(hours=WATCH_EVERY_HOURS)
                       for w in db.get_watching(user_id))
        if (due or quiet or watching or follow_up) if planned_by_ai else (has_future or due or quiet or watching or follow_up):
            try:
                async with runner.tool_session(user_id) as call:
                    for step in steps:  # clashes first: a moved block must not be rebooked twice
                        try:
                            await step(user_id, call, summary)
                        except Exception:
                            log.exception("Autopilot step %s failed for user %s", step.__name__, user_id)
            except RuntimeError as e:  # e.g. Google access expired
                recent = db.last_log_time(user_id, "problem")
                if not recent or (_parse(recent) or now) < now - timedelta(days=1):
                    db.log_action(user_id, "problem", f"I could not look after your calendar: {e}")
                summary["problem"] = str(e)
            except Exception:
                log.exception("Autopilot failed for user %s", user_id)
        if not planned_by_ai:
            try:
                await _nudge(user_id, summary)
            except Exception:
                log.exception("Autopilot nudge failed for user %s", user_id)
        if planned_by_ai:  # the Planner Agent already asked the Researcher (every few hours, and after a student's search)
            try:
                if _announce_stale(user_id):
                    summary["announced"] = summary.get("announced", 0) + 1
            except Exception:
                log.exception("Autopilot announce failed for user %s", user_id)
        else:
            try:
                await _discover(user_id, profile, summary, force=follow_up, auto_plan=True)
            except Exception:
                log.exception("Autopilot search failed for user %s", user_id)
        clear_activity(user_id)
        last_pass[user_id] = datetime.now(timezone.utc).isoformat()
    if summary:
        log.info("Autopilot user %s: %s", user_id, summary)
    return summary


def pulse(user_id: int) -> dict:
    """What the 'agent is awake' bar shows: is the loop alive, when it last looked, when it looks next, and what it is doing."""
    import planner
    now = datetime.now(timezone.utc)
    beat = _parse(loop_beat.get("at"))
    awake = bool(INTERVAL_MIN) and bool(beat) and (now - beat) < timedelta(minutes=INTERVAL_MIN * 2 + 2)
    last = _parse(last_pass.get(user_id))
    lock = _locks.get(user_id)
    from runner import active_run_for
    doing = "searching" if active_run_for(user_id) else "thinking" if (lock and lock.locked()) else "idle"
    act = activity.get(user_id)
    started = _parse(act["since"]) if act else None
    if act and started and now - started > timedelta(minutes=10):  # a pass that died: do not show a stale line forever
        act = None
    if not act and doing == "searching":
        act = {"text": "Researcher is searching for new opportunities…", "who": "researcher", "since": now.isoformat()}
    watching = len(db.get_watching(user_id))
    last_s = _parse(db.last_log_time(user_id, "discover"))
    last_w = max((c for c in (_parse(json.loads(w["data"]).get("watch_checked_at")) for w in db.get_watching(user_id)) if c), default=None)
    def nxt(t, hours):
        return (t + timedelta(hours=hours)).isoformat() if t and hours else None
    return {"awake": awake, "doing": doing, "activity": act, "watching": watching, "planner": bool(planner.ENABLED),
            "every_min": INTERVAL_MIN, "last_look": last.isoformat() if last else None,
            "next_look": (last + timedelta(minutes=INTERVAL_MIN)).isoformat() if last and INTERVAL_MIN else None,
            "next_search": nxt(last_s, DISCOVER_EVERY_HOURS) if searched_lately(user_id) else None, "next_watch": nxt(last_w, WATCH_EVERY_HOURS), "now": now.isoformat()}


SESSION_GAP_HOURS = float(os.environ.get("SESSION_GAP_HOURS", "6"))  # a search after this long without one starts a "new session"
_last_search: dict[int, datetime] = {}


def note_search(user_id: int) -> bool:
    """Called each time the student runs a search. True only for the first search of a new session, which is the only one
    that gets the 2-minute follow-up. Later searches in the same session do not schedule another."""
    now = datetime.now(timezone.utc)
    last = _last_search.get(user_id)
    _last_search[user_id] = now
    return not last or now - last > timedelta(hours=SESSION_GAP_HOURS)


async def follow_up(user_id: int):
    """Two minutes after a student runs a search, look again: new programmes and the ones being watched."""
    if FOLLOW_UP_MIN <= 0:
        return
    await asyncio.sleep(FOLLOW_UP_MIN * 60)
    for _ in range(5):  # a pass may be running: try again shortly rather than skip
        r = await tick(user_id, follow_up=True)
        if r.get("skipped") != "already running":
            return
        await asyncio.sleep(30)


async def run_forever():
    """Started with the app. Waits a minute so start-up is quick, then goes round every connected student."""
    if INTERVAL_MIN <= 0:
        log.info("Autopilot loop is off (AGENT_INTERVAL_MIN=0)")
        return
    loop_beat["at"] = datetime.now(timezone.utc).isoformat()
    await asyncio.sleep(60)
    while True:
        loop_beat["at"] = datetime.now(timezone.utc).isoformat()
        for user_id in db.users_for_autopilot():
            try:
                await tick(user_id)
            except Exception:
                log.exception("Autopilot tick failed for user %s", user_id)
        loop_beat["at"] = datetime.now(timezone.utc).isoformat()
        await asyncio.sleep(INTERVAL_MIN * 60)