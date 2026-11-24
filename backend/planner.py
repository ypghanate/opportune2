"""The planner: a Claude agent that looks after a student's whole application plan.

The fixed loop in autopilot.py runs the same steps in the same order. The planner replaces that order with judgment:
each pass it reads a snapshot of the student's situation (blocks, deadlines, stages, calendar, unanswered check-ins,
practice notes, what it did lately), decides what to do, and calls tools to do it, reading each result before the next.

Claude chooses the actions. Code decides what is allowed: every action goes through `Ctx.run`, which refuses anything
outside the guardrails (interviews are never moved, no nudges at night, a block is moved at most twice a day, at most six
actions per pass, only strong open programmes are started without asking). A refusal goes back to Claude as the tool result,
so it can choose again. Every action is written to the agent log with Claude's reason.

A pass only happens when something changed (see `fingerprint`), plus once a day. If a pass fails, autopilot falls back to its
fixed steps, so the student is never left without a safety net.
"""
import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import calendar_api
import db
import runner
import autopilot

log = logging.getLogger("uvicorn.error")
LONDON = ZoneInfo("Europe/London")
ENABLED = os.environ.get("PLANNER", "1") != "0"
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
MAX_STEPS = 8                    # model turns per pass
MAX_ACTIONS = 6                  # things it may change per pass
MAX_PASSES_PER_DAY = int(os.environ.get("PLANNER_MAX_PER_DAY", "12"))
MOVES_PER_BLOCK_PER_DAY = 2      # stops it thrashing a block around
NUDGE_COOLDOWN_HOURS = 2
QUIET_FROM, QUIET_TO = 22, 8     # London hours with no nudges
STARTS_PER_DAY = 3               # programmes it may start without asking
MAX_OPEN_QUESTIONS = 2
MAX_REVIEWS_PER_PASS = 5
LOOKAHEAD_DAYS = 14
CLOSED = ("offer", "rejected", "withdrawn")
FIXED = ("interview", "assessment_centre")

_client = None


def client():
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic
        _client = AsyncAnthropic()
    return _client


# ---------------------------------------------------------------- what the planner sees
def _local(iso) -> str:
    d = autopilot._parse(iso)
    return d.astimezone(LONDON).strftime("%a %d %b %H:%M") if d else ""


def _bucket(minutes: float) -> int:
    return 0 if minutes < 30 else 1 if minutes < 240 else 2 if minutes < 720 else 3


def build_snapshot(user_id: int, now: datetime, events: list[dict]) -> tuple[dict, str]:
    """Returns (what the planner reads, a fingerprint that changes only when something it cares about changed)."""
    profile, _ = db.get_profile(user_id)
    apps = [a for a in db.get_applications(user_id) if a["stage"] not in CLOSED]
    due = db.blocks_with_app(user_id, ("due",))
    waiting = []
    for b in due:
        ended = autopilot._parse(b["end"] or b["start"]) or now
        waiting.append({"block_id": b["id"], "what": b["summary"], "company": b["company"], "kind": b["kind"], "ended": _local(b["end"] or b["start"]),
                        "minutes_waiting": int((now - ended).total_seconds() // 60),
                        "last_nudge": _local(db.last_log_time_for_block(user_id, "nudge", b["id"])) or None})
    applications = []
    for a in apps:
        blocks = [{"block_id": b["id"], "kind": b["kind"], "what": b["title"], "starts": _local(b["start_at"]), "ends": _local(b["end_at"]), "status": b["status"],
                   **({"note": b["note"]} if b.get("note") else {})}
                  for b in a["blocks"] if b["status"] in ("planned", "due") or (b["status"] == "done" and b.get("note"))]
        applications.append({"application_id": a["id"], "company": a["company"], "title": a["title"], "stage": a["stage"],
                             "application_deadline": _local(a["deadline"]) or None, "deadline_is_a_guess": bool(a.get("deadline_assumed")),
                             "stage_deadline": _local(a["stage_deadline"]) or None, "interview_or_centre_starts": _local(a["stage_start"]) or None, "blocks": blocks})
    opportunities = db.get_opportunities(user_id)
    pending = []
    for o in opportunities:
        if o["status"] == "pending" and not db.log_exists(user_id, "opportunity", f'"opportunity_id": {o["id"]},'):  # not yet suggested (or passed on) to the student
            d = json.loads(o["data"])
            pending.append({"opportunity_id": o["id"], "company": d.get("company"), "title": d.get("title"), "kind": d.get("type"), "field": d.get("field"),
                            "match": d.get("match"), "score": d.get("score"), "why_the_researcher_picked_it": d.get("why"),
                            "location": "Online" if d.get("online") else d.get("location"), "country": d.get("country"),
                            "open_to": d.get("open_to"), "open": d.get("open_status"), "deadline": _local(d.get("deadline")) or None,
                            "starts": (d.get("date") or "")[:10] or None})
    watching = [{"opportunity_id": w["id"], "company": json.loads(w["data"]).get("company") or json.loads(w["data"]).get("title"),
                 "last_checked": _local(json.loads(w["data"]).get("watch_checked_at")) or "never"} for w in db.get_watching(user_id)]
    others = [e for e in events if not e.get("opportune")]
    cal = [{"title": e["title"], "start": _local(e["start"]) if not e["all_day"] else e["start"], "end": _local(e["end"]) if not e["all_day"] else e["end"],
            "all_day": e["all_day"]} for e in others[:80]]
    recent = [{"when": _local(l["created_at"].replace(" ", "T") + "+00:00"), "did": l["message"]} for l in db.get_log(user_id, 12)]
    snap = {
        "now": now.astimezone(LONDON).strftime("%A %d %B %Y, %H:%M (UK time)"),
        "quiet_hours_now": now.astimezone(LONDON).hour >= QUIET_FROM or now.astimezone(LONDON).hour < QUIET_TO,
        "student": {k: profile.get(k) for k in ("name", "university", "degree", "graduation_year", "interests", "location") if profile.get(k)},
        "applications": applications,
        "blocks_waiting_for_an_answer": waiting,
        "programmes_to_review": pending[:10],
        "programmes_being_watched": watching,
        "other_calendar_events_next_14_days": cal,
        "questions_open_now": [{"question": q["message"]} for q in db.get_open_questions(user_id) if q["kind"] == "ask"],
        "answers_the_student_gave": db.recent_answers(user_id),
        "what_i_did_recently": recent,
    }
    parts = {
        "blocks": sorted((b["block_id"], b["starts"], b["status"]) for a in applications for b in a["blocks"]),
        "apps": sorted((a["application_id"], a["stage"], a["application_deadline"], a["stage_deadline"], a["interview_or_centre_starts"]) for a in applications),
        "waiting": sorted((w["block_id"], _bucket(w["minutes_waiting"])) for w in waiting),
        "events": sorted((e["title"], e["start"], e["end"]) for e in cal),
        "pending": sorted(p["opportunity_id"] for p in pending),
        "watch": sorted((w["opportunity_id"], w["last_checked"] == "never") for w in watching),
        "answers": sorted(str(a["at"]) for a in snap["answers_the_student_gave"]),
        "open_q": len(snap["questions_open_now"]),
        "day": now.astimezone(LONDON).date().isoformat(),
    }
    return snap, hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


# ---------------------------------------------------------------- the planner's instructions and tools
SYSTEM = """You are Opportune's planner. You look after one UK student's internship and spring week applications: you keep their
calendar plan sound, and you act for them when something changes. You read a snapshot of their situation and decide what, if
anything, to do. Then you call tools to do it.

How to think:
- Look at the whole picture first: deadlines, stages, what is waiting for an answer, new events in the calendar, what you did lately.
- Work out what matters most. An online assessment or interview soon outranks practice. An application deadline outranks everything for that company.
- Applying always comes before practising for the same company.
- Do nothing when nothing needs doing. Doing nothing is a good answer. Do not repeat what you already did recently.
- Be careful with the student's time. Never nudge in quiet hours. Do not nag: if you nudged a block recently, leave it.
- If you are unsure what the student wants (drop something? is a date right?), ask them with ask_student instead of guessing.
- You can only act through the tools. Never say you did something unless the tool said it worked. If a tool refuses, read why and choose something else, or leave it.
- Every action needs a short, honest reason written to the student in plain English, first person ("Your exams start Monday, so I moved...").
- Finish by calling finish with your assessment: two or three plain sentences on how their plan looks, and up to four concerns if there are any.

Tools: repair_clashes (fix blocks that clash with new calendar events, and tidy blocks the student deleted or dragged), move_block
(move one block, optionally into a time window), check_openings (read the pages of programmes you are waiting on),
review_programme (suggest a programme the Researcher found, or say why you would skip it; the student sees it as a pop-up at the top),
nudge (prompt the student about a block that has ended with no answer; you write the message), ask_student (ask a short
question with 2-3 answers), finish.
Reviewing programmes: "programmes_to_review" are programmes the Researcher found that the student has not seen yet. For each one call
review_programme. Say "recommend" when it is a good fit for this student: the right field, the right year, in the UK or online, open or
opening soon, with time before the deadline. Say "pass" when you would skip it, and give the real reason: it is only in a city or country the student
is not in (compare "location" with the student's location), it is for a different year of study, it is outside their interests, it has closed,
or it is a weak match. Always write the reason to the student in one or two plain sentences, first person, naming the facts, for example
"This is a quant trading spring week, the field you picked first, and it closes in nine days." or "This is only in Sheffield and you are in London."
The student decides: nothing is planned until they press the button. Review the best ones first, and at most five per pass.
Interviews and assessment centres are set by the employer and can never be moved."""

TOOLS = [
    {"name": "repair_clashes", "description": "Look at every calendar and fix the planned blocks: move any that clash with a new event, take off the plan any the student deleted, record any they dragged. Use it when the snapshot shows new events near planned blocks.",
     "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
    {"name": "move_block", "description": "Move one block (planned or ended) to a new free slot. Optionally limit where it may go. Use when you judge it should happen earlier or later, for example to make room for exams or an assessment.",
     "input_schema": {"type": "object", "properties": {"block_id": {"type": "integer"}, "reason": {"type": "string"},
                                                       "not_before": {"type": "string", "description": "ISO 8601, earliest the new slot may start"},
                                                       "not_after": {"type": "string", "description": "ISO 8601, latest the new slot may end"}}, "required": ["block_id", "reason"]}},
    {"name": "check_openings", "description": "Read the pages of the programmes being watched and book the application for any that have opened.",
     "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
    {"name": "review_programme", "description": "Tell the student about a programme from programmes_to_review: either recommend it, or say you would skip it and why. It appears as a pop-up at the top with buttons. Nothing is booked until the student presses a button.",
     "input_schema": {"type": "object", "properties": {"opportunity_id": {"type": "integer"}, "verdict": {"type": "string", "enum": ["recommend", "pass"]},
                                                       "reason": {"type": "string", "description": "One or two plain sentences to the student, first person, with the real facts."}}, "required": ["opportunity_id", "verdict", "reason"]}},
    {"name": "nudge", "description": "Prompt the student about a block that ended and has no answer. You write the message (one or two short sentences, friendly, specific). Not allowed in quiet hours or if nudged in the last two hours.",
     "input_schema": {"type": "object", "properties": {"block_id": {"type": "integer"}, "message": {"type": "string"}, "reason": {"type": "string"}}, "required": ["block_id", "message", "reason"]}},
    {"name": "ask_student", "description": "Ask the student a short question when you are unsure. Give 2 or 3 short answers. You will see their answer next time.",
     "input_schema": {"type": "object", "properties": {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 3},
                                                       "application_id": {"type": "integer"}, "reason": {"type": "string"}}, "required": ["question", "options", "reason"]}},
    {"name": "finish", "description": "End the pass. Say how the student's plan looks.",
     "input_schema": {"type": "object", "properties": {"assessment": {"type": "string"}, "concerns": {"type": "array", "items": {"type": "string"}, "maxItems": 4}}, "required": ["assessment"]}},
]


# ---------------------------------------------------------------- guardrails and execution
class Ctx:
    """One pass: runs the model's tool calls, refusing anything outside the rules."""

    def __init__(self, user_id: int, call, profile: dict, now: datetime):
        self.uid, self.call, self.profile, self.now = user_id, call, profile, now
        self.actions = 0
        self.asked = 0
        self.reviewed = 0
        self.did: dict[str, int] = {}
        self.finished: dict | None = None

    def count(self, key: str):
        self.did[key] = self.did.get(key, 0) + 1

    def _budget(self):
        if self.actions >= MAX_ACTIONS:
            return {"refused": f"You have already made {MAX_ACTIONS} changes in this pass. Call finish."}

    def _quiet(self) -> bool:
        h = self.now.astimezone(LONDON).hour
        return h >= QUIET_FROM or h < QUIET_TO

    def _block(self, block_id, statuses):
        return next((b for b in db.blocks_with_app(self.uid, statuses) if b["id"] == block_id), None)

    async def run(self, name: str, args: dict) -> dict:
        autopilot.set_activity(self.uid, self._describe(name, args))
        fn = getattr(self, f"t_{name}", None)
        if not fn:
            return {"refused": f"There is no tool called {name}."}
        try:
            return await fn(**args)
        except TypeError as e:
            return {"refused": f"Bad arguments: {e}"}
        except Exception as e:
            log.exception("Planner tool %s failed for user %s", name, self.uid)
            return {"error": runner.describe(e)}

    def _describe(self, name: str, args: dict) -> str:
        """What the bar at the top says while a tool runs."""
        if name == "review_programme":
            o = next((o for o in db.get_opportunities(self.uid) if o["id"] == args.get("opportunity_id")), None)
            d = json.loads(o["data"]) if o else {}
            return f"Planner Agent is reviewing {d.get('title') or d.get('company') or 'a new programme'} for you…"
        if name == "move_block":
            b = self._block(args.get("block_id"), ("planned", "due"))
            return f"Planner Agent is finding a better time for {b['summary'] if b else 'a session'}…"
        return {"repair_clashes": "Planner Agent is checking your calendar for clashes…",
                "check_openings": "Planner Agent is checking whether watched programmes have opened…",
                "nudge": "Planner Agent is writing you a reminder…",
                "ask_student": "Planner Agent is preparing a question for you…",
                "finish": "Planner Agent is writing up how your plan looks…"}.get(name, "Planner Agent is working on your plan…")

    async def _via_step(self, step, reason: str, empty: str, **kw) -> dict:
        refused = self._budget()
        if refused:
            return refused
        before = db.last_log_id(self.uid)
        summary: dict = {}
        await step(self.uid, self.call, summary, **kw)
        msgs = [l["message"] for l in db.log_since(self.uid, before) if l["kind"] not in ("discover_start", "problem")]
        if msgs:
            db.annotate_log(self.uid, before, reason)
            self.actions += 1
            for k, v in summary.items():
                self.did[k] = self.did.get(k, 0) + (v if isinstance(v, int) else 1)
        return {"changes": msgs or [empty]}

    async def t_repair_clashes(self, reason: str) -> dict:
        return await self._via_step(autopilot._clashes, reason, "Nothing clashes and nothing was deleted or dragged.")

    async def t_check_openings(self, reason: str) -> dict:
        out = await self._via_step(autopilot._watch, reason, "None of the watched programmes have opened yet.", force=True)
        return out

    async def t_search_for_new(self, reason: str) -> dict:
        if db.count_log_today(self.uid, "discover") >= 2:
            return {"refused": "You already searched twice today."}
        out = await self._via_step(lambda uid, call, summ, **kw: autopilot._discover(uid, self.profile, summ, **kw), reason, "The search found nothing new.", force=True, auto_plan=False)
        out["waiting_for_approval"] = [{"opportunity_id": o["id"], **{k: json.loads(o["data"]).get(k) for k in ("company", "title", "match", "open_status", "deadline")}}
                                       for o in db.get_opportunities(self.uid) if o["status"] == "pending"][:12]
        return out

    async def t_move_block(self, block_id: int, reason: str, not_before: str | None = None, not_after: str | None = None) -> dict:
        refused = self._budget()
        if refused:
            return refused
        b = self._block(block_id, ("planned", "due"))
        if not b:
            return {"refused": "That is not a block you can move (it must be planned or ended, and yours)."}
        if b["kind"] in FIXED:
            return {"refused": "That is an interview or assessment centre set by the employer. It cannot be moved."}
        key = hashlib.md5(f"{b['application_id']}|{b['kind']}|{b['summary']}".encode()).hexdigest()[:12]  # the same session keeps its key after a move
        if db.count_log_today(self.uid, "move", f'"key": "{key}"') >= MOVES_PER_BLOCK_PER_DAY:
            return {"refused": f"You already moved that block {MOVES_PER_BLOCK_PER_DAY} times today. Leave it."}
        args = {"blocks": [b], "move": b["status"] == "planned"}
        if not_before:
            args["not_before"] = not_before
        if not_after:
            args["not_after"] = not_after
        res = ((await self.call("rebook_missed_blocks", args)) or [{}])[0]
        act = (res.get("actions") or [{}])[0]
        if act.get("action") != "rebooked":
            why = {"fixed": "it is fixed by the employer", "not_needed": "it is not needed at this stage", "deadline_passed": "its deadline is too close"}.get(act.get("reason"), act.get("reason") or "no free slot")
            return {"failed": f"Could not move it: {why}."}
        db.update_block(self.uid, block_id, status="moved" if b["status"] == "planned" else "missed")
        db.add_blocks(self.uid, autopilot._new_blocks([act["new"]], b["application_id"]))
        db.log_action(self.uid, "move", f"Moved '{b['summary']}' from {autopilot._when(b['start'])} to {autopilot._when(act['new'].get('start'))}.",
                      b["application_id"], data={"block_id": block_id, "key": key, "reason": reason[:300]})
        self.actions += 1
        self.count("moved")
        return {"moved": True, "new_start": act["new"].get("start"), "new_end": act["new"].get("end")}

    async def t_start_application(self, opportunity_id: int, reason: str) -> dict:
        refused = self._budget()
        if refused:
            return refused
        row = next((o for o in db.get_opportunities(self.uid) if o["id"] == opportunity_id), None)
        if not row or row["status"] != "pending":
            return {"refused": "That programme is not waiting for approval."}
        d = json.loads(row["data"])
        if not (d.get("match") == "strong" and d.get("open_status") == "open" and d.get("deadline")):
            return {"refused": "Only strong matches that are open with a deadline can be started without asking. Use ask_student, or leave it for the student to approve."}
        if db.count_log_today(self.uid, "start") >= STARTS_PER_DAY:
            return {"refused": f"You already started {STARTS_PER_DAY} applications today."}
        _, booked_ids, apps = await autopilot.plan_opportunities(self.uid, [row])
        if not booked_ids:
            return {"failed": "No free slot was found for the Apply block before the deadline."}
        db.mark_approved(self.uid, booked_ids)
        app = apps[0]
        db.log_action(self.uid, "start", f"Started your application to {app['company']}: booked the Apply block and practice.", app["id"],
                      data={"opportunity_id": opportunity_id, "reason": reason[:300]})
        self.actions += 1
        self.count("started")
        return {"started": app["company"]}

    async def t_review_programme(self, opportunity_id: int, verdict: str, reason: str) -> dict:
        if verdict not in ("recommend", "pass"):
            return {"refused": "verdict must be recommend or pass."}
        reason = str(reason or "").strip()
        if len(reason) < 12:
            return {"refused": "Give the student a real reason in a sentence."}
        row = next((o for o in db.get_opportunities(self.uid) if o["id"] == opportunity_id), None)
        if not row or row["status"] != "pending":
            return {"refused": "That programme is not waiting for review."}
        if db.log_exists(self.uid, "opportunity", f'"opportunity_id": {opportunity_id},'):
            return {"refused": "The student has already been told about that one."}
        if self.reviewed >= MAX_REVIEWS_PER_PASS:
            return {"refused": f"You have already reviewed {MAX_REVIEWS_PER_PASS} programmes in this pass. The rest will wait for the next pass."}
        if sum(1 for q in db.get_open_questions(self.uid) if q["kind"] == "opportunity") >= autopilot.MAX_ANNOUNCE:
            return {"refused": "The student already has several suggestions waiting. Wait for them to answer."}
        d = json.loads(row["data"])
        name = d.get("title") or d.get("company") or "this programme"
        company = d.get("company") or ""
        when = autopilot._parse(d.get("deadline"))
        closes = f" It closes {when.astimezone(LONDON).strftime('%a %d %b')}." if when else ""
        head = f"I suggest {name}" if verdict == "recommend" else f"I would skip {name}"
        db.log_action(self.uid, "opportunity", f"{head}{' at ' + company if company and company not in name else ''}.{closes}", None, status="open",
                      data={"opportunity_id": opportunity_id, "company": company, "title": d.get("title"), "match": d.get("match"), "url": d.get("url"),
                            "verdict": verdict, "reason": reason[:300]})
        self.reviewed += 1
        self.count("suggested" if verdict == "recommend" else "passed_on")
        return {"shown_to_student": verdict}

    async def t_nudge(self, block_id: int, message: str, reason: str) -> dict:
        refused = self._budget()
        if refused:
            return refused
        if self._quiet():
            return {"refused": f"It is quiet hours ({QUIET_FROM}:00 to {QUIET_TO}:00). Do not nudge now."}
        b = self._block(block_id, ("due",))
        if not b:
            return {"refused": "That block is not waiting for an answer."}
        last = autopilot._parse(db.last_log_time_for_block(self.uid, "nudge", block_id))
        if last and self.now - last < timedelta(hours=NUDGE_COOLDOWN_HOURS):
            return {"refused": f"You nudged that block in the last {NUDGE_COOLDOWN_HOURS} hours."}
        db.log_action(self.uid, "nudge", message.strip()[:240], b["application_id"], data={"block_id": block_id, "reason": reason[:300]})
        self.actions += 1
        self.count("nudged")
        return {"nudged": True}

    async def t_ask_student(self, question: str, options: list[str], reason: str, application_id: int | None = None) -> dict:
        refused = self._budget()
        if refused:
            return refused
        opts = [str(o).strip()[:40] for o in options if str(o).strip()][:3]
        if len(opts) < 2:
            return {"refused": "Give two or three short answers."}
        if self.asked >= 2 or sum(1 for q in db.get_open_questions(self.uid) if q["kind"] == "ask") >= MAX_OPEN_QUESTIONS:
            return {"refused": "There are already open questions. Wait for the student to answer."}
        if application_id and not any(a["id"] == application_id for a in db.get_applications(self.uid)):
            application_id = None
        db.log_action(self.uid, "ask", question.strip()[:240], application_id, status="open", data={"options": opts, "reason": reason[:300]})
        self.asked += 1
        self.actions += 1
        self.count("asked")
        return {"asked": True}

    async def t_finish(self, assessment: str, concerns: list[str] | None = None) -> dict:
        self.finished = {"assessment": assessment.strip()[:600], "concerns": [str(c).strip()[:200] for c in (concerns or [])][:4]}
        return {"ok": True}


async def _think(ctx: Ctx, snap: dict) -> str:
    """The loop: Claude chooses a tool, the code runs it (or refuses), Claude reads the result, and so on."""
    messages = [{"role": "user", "content": "Here is the student's situation right now. Decide what to do, act, then call finish.\n\n" + json.dumps(snap, indent=1)}]
    last_text = ""
    for _ in range(MAX_STEPS):
        autopilot.set_activity(ctx.uid, "Planner Agent is thinking about your plan…")
        resp = await client().messages.create(model=MODEL, max_tokens=2048, system=SYSTEM, tools=TOOLS, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        last_text = " ".join(b.text for b in resp.content if b.type == "text" and getattr(b, "text", "")).strip() or last_text
        calls = [b for b in resp.content if b.type == "tool_use"]
        if not calls:
            break
        results = []
        for b in calls:
            out = await ctx.run(b.name, dict(b.input or {}))
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(out, default=str)[:6000]})
        messages.append({"role": "user", "content": results})
        if ctx.finished:
            break
    return last_text


# ---------------------------------------------------------------- one pass
async def _research(user_id: int, profile: dict, force: bool) -> None:
    """The Planner Agent asks the Researcher to search. It only really searches when the time is up (every AUTO_DISCOVERY_HOURS),
    or when `force` is set (two minutes after the student ran a search). Nothing is announced here: the Planner reviews what it finds."""
    before = db.last_log_id(user_id)
    try:
        await autopilot._discover(user_id, profile, {}, force=force, auto_plan=False, announce=False)
    except Exception:
        log.exception("Researcher failed for user %s", user_id)
    if db.last_log_id(user_id) != before:
        db.annotate_log(user_id, before, f"The Planner Agent asked the Researcher to search (it does this every {autopilot.DISCOVER_EVERY_HOURS:g} hours, and again after you search).")


async def run(user_id: int, force: bool = False, research_now: bool = False) -> dict:
    """One planner pass for one student. Returns {"skipped": why} or {"ran": True, "did": {...}}. Raises if it cannot look
    after the calendar, so autopilot can fall back to its fixed steps."""
    if not ENABLED:
        return {"skipped": "planner is off"}
    now = datetime.now(timezone.utc)
    profile, _ = db.get_profile(user_id)
    if research_now or profile.get("autopilot", True):
        await _research(user_id, profile, force=research_now)
    if not db.get_applications(user_id) and not db.get_watching(user_id) and not any(o["status"] == "pending" for o in db.get_opportunities(user_id)):
        return {"skipped": "nothing to plan yet"}
    try:
        events = await asyncio.to_thread(calendar_api.list_events, user_id, now, now + timedelta(days=LOOKAHEAD_DAYS))
    except PermissionError as e:
        raise RuntimeError(str(e))
    snap, fp = build_snapshot(user_id, now, events)
    state = db.get_planner_state(user_id)
    if not force and state.get("fingerprint") == fp:
        return {"skipped": "nothing changed"}
    if not force and db.count_log_today(user_id, "planner_run") >= MAX_PASSES_PER_DAY:
        return {"skipped": "daily limit reached"}
    profile, _ = db.get_profile(user_id)
    db.log_action(user_id, "planner_run", "pass")
    async with runner.tool_session(user_id) as call:
        ctx = Ctx(user_id, call, profile, now)
        text = await _think(ctx, snap)
    done = ctx.finished or {"assessment": text[:600] or "I looked over your plan and nothing needed changing.", "concerns": []}
    db.save_planner_state(user_id, assessment=done["assessment"], concerns=done["concerns"], mode="planner")
    after = datetime.now(timezone.utc)  # what it changed is not news: remember the state it left behind
    try:
        events = await asyncio.to_thread(calendar_api.list_events, user_id, after, after + timedelta(days=LOOKAHEAD_DAYS))
        db.save_planner_state(user_id, fingerprint=build_snapshot(user_id, after, events)[1])
    except Exception:
        db.save_planner_state(user_id, fingerprint=fp)
    return {"ran": True, "did": ctx.did, "assessment": done["assessment"]}
