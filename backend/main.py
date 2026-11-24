import config  # noqa: F401  (loads .env first)

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlparse
from email.utils import parseaddr

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
import json
from typing import Literal

_bg: set = set()  # keeps follow-up tasks alive until they finish

import autopilot
import planner
import calendar_api
import db
import google_oauth
import rules
import uk_data
import runner
import security
from security import current_user

CATEGORIES = ["internship", "spring_week"]
MAX_CV_BYTES = 8 * 1024 * 1024  # the frontend says 8 MB


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    loop = asyncio.create_task(autopilot.run_forever())  # the agent working in the background
    yield
    loop.cancel()


app = FastAPI(title="Opportune", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in config.CORS_ORIGINS.split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type"],
)


# ---------------------------------------------------------------- auth
class _Checked(BaseModel):
    """name / university / degree rules, shared by sign-up and the profile form."""

    @field_validator("name", check_fields=False)
    @classmethod
    def _name(cls, v):
        return rules.full_name(v)

    @field_validator("university", check_fields=False)
    @classmethod
    def _uni(cls, v):
        return rules.university(v)

    @field_validator("degree", check_fields=False)
    @classmethod
    def _degree(cls, v):
        return rules.degree(v)


class RegisterIn(_Checked):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=200)
    password: str = Field(min_length=10, max_length=72)
    name: str
    university: str
    degree: str


class LoginIn(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(min_length=1, max_length=72)


@app.get("/api/meta")
def meta():
    """The dropdown lists, so the frontend and the API always agree."""
    return {"universities": uk_data.UNIVERSITIES, "degrees": uk_data.DEGREES}


@app.post("/api/auth/register")
def register(body: RegisterIn):
    email = body.email.strip().lower()
    if db.get_user_by_email(email):
        raise HTTPException(409, "An account with this email already exists")
    user_id = db.create_user(email, security.hash_password(body.password))
    db.save_profile(user_id, {"name": body.name, "university": body.university, "degree": body.degree})
    return {"access_token": security.make_session(user_id), "token_type": "bearer"}


@app.post("/api/auth/login")
def login(body: LoginIn):
    user = db.get_user_by_email(body.email.strip().lower())
    if not user or not security.check_password(body.password, user["password_hash"]):
        raise HTTPException(401, "Wrong email or password")
    return {"access_token": security.make_session(user["id"]), "token_type": "bearer"}


# ---------------------------------------------------------------- profile
class ProfileIn(_Checked):
    name: str
    university: str
    degree: str
    year: str = Field(default="", max_length=30)
    graduation_year: int = Field(ge=2020, le=2040)
    location: str = Field(default="", max_length=100)
    gender: Literal["", "Female", "Male", "Non-binary", "Prefer not to say"] = ""  # only used to hide schemes you can't apply to
    interests: list[str] = Field(default=[], max_length=20)
    autopilot: bool = True  # lets the agent move, rebook and book things on its own


@app.get("/api/profile")
def get_profile(user=Depends(current_user)):
    profile, cv_path = db.get_profile(user["id"])
    has_cv = bool(cv_path and Path(cv_path).exists())
    return {
        **profile,
        "email": user["email"],
        "cv_name": db.get_cv_name(user["id"]) if has_cv else None,
        "google_connected": db.get_google_token(user["id"]) is not None,
    }


@app.put("/api/profile")
def save_profile(body: ProfileIn, user=Depends(current_user)):
    data = body.model_dump()
    data["interests"] = [i.strip()[:60] for i in data["interests"] if i.strip()]
    db.save_profile(user["id"], data)
    return {"ok": True}


@app.post("/api/profile/cv")
async def upload_cv(file: UploadFile = File(...), user=Depends(current_user)):
    name = (file.filename or "").lower()
    ext = ".pdf" if name.endswith(".pdf") else ".docx" if name.endswith(".docx") else None
    if not ext:
        raise HTTPException(400, "Upload a PDF or Word (.docx) file")
    data = await file.read(MAX_CV_BYTES + 1)
    if len(data) > MAX_CV_BYTES:
        raise HTTPException(413, "The CV must be under 8 MB")
    if not (data.startswith(b"%PDF") if ext == ".pdf" else data.startswith(b"PK")):
        raise HTTPException(400, "That file does not look like a real PDF or Word document")

    folder = config.CV_ROOT / str(user["id"])
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("cv.*"):
        old.unlink()
    path = folder / f"cv{ext}"
    path.write_bytes(data)
    original = Path(file.filename).name[:120]
    db.set_cv(user["id"], str(path), original)
    return {"ok": True, "filename": original}


# ---------------------------------------------------------------- google
@app.get("/api/google/connect")
def google_connect(user=Depends(current_user)):
    return {"url": google_oauth.start(user["id"])}


@app.get("/api/google/callback")
def google_callback(request: Request):
    # Google redirects the browser here, so there is no Authorization header: the state value identifies the user.
    params = request.query_params
    back = f"{config.FRONTEND_URL}/#/profile"
    if params.get("error"):
        return RedirectResponse(f"{back}?google=error&reason=Google+sign-in+was+cancelled")
    try:
        google_oauth.finish(params.get("state", ""), params.get("code", ""))
    except Exception as e:
        return RedirectResponse(f"{back}?google=error&reason={quote_plus(str(e))}")
    return RedirectResponse(f"{back}?google=connected")


@app.delete("/api/google")
def google_disconnect(user=Depends(current_user)):
    google_oauth.disconnect(user["id"])
    return {"ok": True}


# ---------------------------------------------------------------- opportunities
CATEGORY_OF = {"spring week": "spring_week", "hackathons": "hackathon", "other_competitions": "other"}


def view(row: dict) -> dict:
    """Turns a stored agent item into the shape the Opportune UI shows."""
    d = json.loads(row["data"])
    kind = d.get("type") or "other"
    url = d.get("url") or d.get("link")
    if d.get("source") == "gmail":
        sender = parseaddr(d.get("sender") or "")
        company = sender[0] or (sender[1].split("@")[-1] if "@" in sender[1] else "")
        description = d.get("snippet") or d.get("subject") or ""
        location = ""
    else:
        company = d.get("company") or (urlparse(url or "").hostname or "").removeprefix("www.")
        start = (d.get("date") or "")[:10]
        opens = (d.get("opens_on") or "")[:10]
        description = " ".join(x for x in (
            f"Applications open {opens}." if d.get("watch") and opens else "",
            f"Starts {start}." if start else "",
            d.get("note") or "",
            "No closing date found, so this is a rough reminder date." if d.get("deadline_assumed") else "",
            d.get("why") or "",
        ) if x)
        location = "Online" if d.get("online") else d.get("location") or ""
    return {
        "id": row["id"],
        "title": d.get("title"),
        "company": company,
        "category": CATEGORY_OF.get(kind, kind),
        "location": location,
        "description": description,
        "url": url,
        "deadline": d.get("deadline"),
        "status": row["status"],
    }


class SearchIn(BaseModel):
    query: str = Field(default="", max_length=500)
    categories: list[str] = Field(default=["internship", "spring_week"])


@app.post("/api/opportunities/search")
async def search(body: SearchIn, user=Depends(current_user)):
    profile, cv_path = db.get_profile(user["id"])
    if not all(profile.get(k) for k in ("name", "university", "degree", "graduation_year")):
        raise HTTPException(400, "Complete your profile first")
    if not cv_path or not Path(cv_path).exists():
        raise HTTPException(400, "Upload your CV on the Profile page first")
    if not db.get_google_token(user["id"]):
        raise HTTPException(400, "Connect your Google account on the Profile page first")
    cats = [c for c in body.categories if c in CATEGORIES]
    if not cats:
        raise HTTPException(422, "Pick at least one type of opportunity")
    missing = [k for k in ("ANTHROPIC_API_KEY", "TAVILY_API_KEY") if not os.environ.get(k)]
    if missing:
        raise HTTPException(503, f"The server is missing {' and '.join(missing)} in backend/.env")
    if runner.active_run_for(user["id"]):
        raise HTTPException(409, "A search is already running. Wait for it to finish")
    if db.runs_today(user["id"]) >= config.MAX_RUNS_PER_DAY:
        raise HTTPException(429, f"You can run the agent {config.MAX_RUNS_PER_DAY} times per day. Try again tomorrow")

    if body.query.strip():
        db.log_action(user["id"], "search_prompt", body.query.strip()[:300])  # the background searches reuse it
    try:
        result = await runner.search(user["id"], profile, cv_path, body.query.strip(), cats)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    known = {r["key"] for r in db.get_opportunities(user["id"])}
    fresh = [it for it in result["items"] if db.item_key(it) not in known]
    ids = db.upsert_opportunities(user["id"], result["items"])  # refreshes details of old ones; their status is kept
    shown = {db.item_key(it) for it in fresh}
    rows = [r for r in db.get_opportunities(user["id"], ids) if r["key"] in shown]
    message = result["message"]
    repeats = len(result["items"]) - len(fresh)
    if repeats:
        message += f"\n\n{repeats} of what the agent found {'was' if repeats == 1 else 'were'} already in My opportunities, so not shown again."
    if autopilot.note_search(user["id"]) and autopilot.FOLLOW_UP_MIN > 0:  # only the first search of a session
        _bg.add(t := asyncio.create_task(autopilot.follow_up(user["id"])))  # looks again in a couple of minutes
        t.add_done_callback(_bg.discard)
    return {"items": [view(r) for r in rows], "message": message}


@app.get("/api/opportunities")
def list_opportunities(user=Depends(current_user)):
    return [view(r) for r in db.get_opportunities(user["id"])]


@app.delete("/api/opportunities")
def delete_all_opportunities(user=Depends(current_user)):
    """Clears My opportunities. Events already booked in Google Calendar are left alone."""
    return {"deleted": db.delete_opportunities(user["id"])}


@app.post("/api/opportunities/approve")
async def approve(ids: list[int], user=Depends(current_user)):
    """Approving an opportunity starts an application: its apply block and, until an online assessment arrives, practice sessions."""
    rows = [r for r in db.get_opportunities(user["id"], ids) if r["status"] == "pending"]
    if not rows:
        raise HTTPException(400, "Nothing to approve")
    if len(rows) > 25:
        raise HTTPException(400, "Approve at most 25 at a time")
    try:
        results, booked, _ = await autopilot.plan_opportunities(user["id"], rows)
    except Exception as e:
        runner.log.exception("Planning failed")
        raise HTTPException(502, f"Could not plan these in your calendar: {runner.describe(e)}")
    db.mark_approved(user["id"], booked)
    report = [f"{r['title']}: {r['status']}" + (f" ({r['reason']})" if r.get("status") in ("failed", "skipped") and r.get("reason") else "")
              for r in results]
    if not booked:
        raise HTTPException(422, "No free slot was found for these before their deadlines. " + " ".join(report))
    return {"ok": True, "booked": booked, "report": report}


# ---------------------------------------------------------------- applications
SHOT_TYPES = {b"\x89PNG": ".png", b"\xff\xd8\xff": ".jpg", b"RIFF": ".webp", b"GIF8": ".gif"}
MAX_SHOT_BYTES = 5 * 1024 * 1024


def _blocks_for_tool(app_row: dict) -> list[dict]:
    return [{"id": b["id"], "kind": b["kind"], "status": b["status"], "start": b["start_at"], "event_id": b["event_id"]}
            for b in app_row["blocks"]]


class CheckinIn(BaseModel):
    done: bool
    note: str = Field(default="", max_length=300)  # e.g. "numerical 9/12"


@app.get("/api/checkins")
def checkins(user=Depends(current_user)):
    """Blocks that have finished and are waiting for 'did you do it?'."""
    return db.get_checkins(user["id"])


@app.post("/api/blocks/{block_id}/checkin")
async def checkin(block_id: int, body: CheckinIn, user=Depends(current_user)):
    """Yes: the block is done. No: the agent books it again (an interview or assessment centre is only marked missed)."""
    b = db.get_block(user["id"], block_id)
    if not b:
        raise HTTPException(404, "That block is not waiting for an answer")
    note = body.note.strip() or None
    if body.done:
        db.finish_block(user["id"], block_id, "done", note)
        return {"status": "done", "message": "Marked as done."}

    due = next((x for x in db.blocks_with_app(user["id"], ("due",)) if x["id"] == block_id), None)
    if not due:
        raise HTTPException(404, "That block is not waiting for an answer")
    try:
        async with runner.tool_session(user["id"]) as call:
            res = (await call("rebook_missed_blocks", {"blocks": [due]}) or [{}])[0]
    except Exception as e:
        runner.log.exception("Reschedule failed")
        raise HTTPException(502, f"Could not rebook it: {runner.describe(e)}")
    action = (res.get("actions") or [{}])[0]
    if action.get("action") == "failed":
        raise HTTPException(422, "No free slot was found before the deadline. It stays on your list, so try again after freeing some time.")
    if note:
        db.update_block(user["id"], block_id, note=note)
    saved = autopilot.save_rebooks(user["id"], [action], {block_id: due}, silent=False)[0]
    return {"status": "rescheduled" if action.get("action") == "rebooked" else "missed", "message": saved["message"]}


# ---------------------------------------------------------------- the agent's activity
@app.get("/api/agent/log")
def agent_log(user=Depends(current_user)):
    """What the agent did on its own, newest first."""
    return db.get_log(user["id"], 40)


@app.get("/api/agent/status")
def agent_status(user=Depends(current_user)):
    """What the agent is doing for the student right now, one line per behaviour."""
    return autopilot.status(user["id"])


@app.get("/api/agent/pulse")
def agent_pulse(user=Depends(current_user)):
    """Small and cheap: the 'agent is awake' bar polls this every 20 seconds."""
    return autopilot.pulse(user["id"])


@app.get("/api/agent/questions")
def agent_questions(user=Depends(current_user)):
    """Things the agent is waiting for an answer on, e.g. 'Have you heard anything from Barclays?'."""
    out = []
    for q in db.get_open_questions(user["id"]):
        if q["kind"] == "opportunity":  # the student already dealt with it elsewhere, or the planner started it: close the question
            opp_id = json.loads(q["data"] or "{}").get("opportunity_id")
            rows = db.get_opportunities(user["id"], [opp_id]) if opp_id else []
            if not rows or rows[0]["status"] != "pending":
                db.answer_log(user["id"], q["id"], "done", "handled")
                continue
        out.append(q)
    return out


@app.post("/api/opportunities/{opp_id}/skip")
def skip_opportunity(opp_id: int, user=Depends(current_user)):
    """'Not for me': keeps it in My opportunities, marked skipped, and the agent stops asking about it."""
    rows = db.get_opportunities(user["id"], [opp_id])
    if not rows or rows[0]["status"] != "pending":
        raise HTTPException(404, "That opportunity is not waiting for approval.")
    db.update_opportunity(user["id"], opp_id, status="skipped")
    return {"ok": True}


class AnswerIn(BaseModel):
    answer: str = Field(default="", max_length=60)


@app.get("/api/agent/thinking")
def agent_thinking(user=Depends(current_user)):
    """How the planner currently sees the student's plan: its assessment and concerns, and whether it is running."""
    s = db.get_planner_state(user["id"])
    return {"assessment": s.get("assessment") or "", "concerns": s.get("concerns") or [], "at": s.get("ran_at"),
            "mode": s.get("mode") or ("planner" if planner.ENABLED else "rules")}


@app.post("/api/agent/log/{log_id}/answer")
def answer_question(log_id: int, body: AnswerIn | None = None, user=Depends(current_user)):
    if not db.answer_log(user["id"], log_id, "done", (body.answer.strip() if body else "") or None):
        raise HTTPException(404, "That question is not open")
    return {"ok": True}


_last_manual: dict[int, float] = {}


@app.post("/api/agent/run")
async def run_agent_now(user=Depends(current_user)):
    """Runs the autopilot once for the signed-in student, instead of waiting for the next pass."""
    import time
    if time.time() - _last_manual.get(user["id"], 0) < 60:
        raise HTTPException(429, "Wait a minute before running it again")
    _last_manual[user["id"]] = time.time()
    if not db.get_google_token(user["id"]):
        raise HTTPException(400, "Connect your Google account on the Profile page first")
    db.log_action(user["id"], "search_prompt", db.last_search_prompt(user["id"]) or "Find new spring weeks and internships for me.")  # running the agent counts as a search
    done = await autopilot.tick(user["id"], force=True)
    return {"done": done}


@app.get("/api/applications")
def list_applications(user=Depends(current_user)):
    return db.get_applications(user["id"])


@app.post("/api/applications/{application_id}/stage")
async def change_stage(
    application_id: int,
    stage: str = Form(...),
    deadline: str = Form(default=""),
    start_time: str = Form(default=""),
    duration_minutes: int | None = Form(default=None),
    screenshot: UploadFile | None = File(default=None),
    user=Depends(current_user),
):
    """Moves one application to a new stage. A screenshot of the employer's email fills in the deadline or interview time."""
    found = db.get_applications(user["id"], application_id)
    if not found:
        raise HTTPException(404, "Application not found")
    current = found[0]
    if stage not in db.STAGES:
        raise HTTPException(422, "Unknown stage")
    if duration_minutes is not None and not 5 <= duration_minutes <= 600:
        raise HTTPException(422, "Duration must be between 5 and 600 minutes")
    if not db.get_google_token(user["id"]):
        raise HTTPException(400, "Connect your Google account on the Profile page first")

    if stage == "oa" and not (screenshot is not None and screenshot.filename):
        raise HTTPException(422, "Add a screenshot of the employer's email. I read the deadline from it.")
    if stage == "oa":
        deadline = start_time = ""  # typed details are ignored for an online assessment
        duration_minutes = None

    shot_path = None
    if screenshot is not None and screenshot.filename:
        data = await screenshot.read(MAX_SHOT_BYTES + 1)
        if len(data) > MAX_SHOT_BYTES:
            raise HTTPException(413, "The screenshot must be under 5 MB")
        ext = next((e for magic, e in SHOT_TYPES.items() if data.startswith(magic)), None)
        if not ext or (ext == ".webp" and data[8:12] != b"WEBP"):
            raise HTTPException(400, "The screenshot must be a PNG, JPG, WEBP or GIF image")
        folder = config.SHOT_ROOT / str(user["id"])
        folder.mkdir(parents=True, exist_ok=True)
        shot_path = folder / f"{uuid.uuid4().hex}{ext}"
        shot_path.write_bytes(data)

    args = {
        "application": {"id": current["id"], "company": current["company"], "title": current["title"], "url": current["url"],
                        "colour_id": current["colour_id"], "field": current.get("field")},
        "new_stage": stage,
        "blocks": _blocks_for_tool(current),
    }
    if deadline.strip():
        args["deadline"] = deadline.strip()
    if start_time.strip():
        args["start_time"] = start_time.strip()
    if duration_minutes:
        args["duration_minutes"] = duration_minutes
    if shot_path:
        args["screenshot_path"] = str(shot_path)
    try:
        result = await runner.change_stage(user["id"], args)
    except Exception as e:
        runner.log.exception("Stage change failed")
        raise HTTPException(502, f"Could not update the stage: {runner.describe(e)}")
    finally:
        if shot_path:
            shot_path.unlink(missing_ok=True)  # the screenshot is only needed to read it

    if result.get("error"):
        raise HTTPException(422, result["error"])
    db.cancel_blocks(user["id"], result.get("cancelled") or [])
    db.add_blocks(user["id"], result.get("blocks") or [])
    facts = result.get("extracted") or {}
    db.set_stage(
        user["id"], application_id, stage,
        deadline=deadline.strip() or facts.get("deadline"), start=start_time.strip() or facts.get("start_time"),
        assumed=bool(result.get("deadline_assumed")), note=" ".join(result.get("notes") or []) or None,
    )
    failed = [b for b in result.get("blocks") or [] if b.get("status") == "failed"]
    out = db.get_applications(user["id"], application_id)[0]
    out["warnings"] = [f"No free slot was found for '{b['title']}' before its deadline." for b in failed]
    return out


# ---------------------------------------------------------------- calendar
@app.get("/api/calendar")
async def calendar_events(start: str, end: str, user=Depends(current_user)):
    """The signed-in user's Google Calendar events between two ISO 8601 times (at most 62 days apart)."""
    def parse(v: str) -> datetime:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d

    try:
        s, e = parse(start), parse(end)
    except ValueError:
        raise HTTPException(422, "start and end must be ISO 8601 dates")
    if not s < e or e - s > timedelta(days=62):
        raise HTTPException(422, "Ask for between 1 and 62 days at a time")
    if not db.get_google_token(user["id"]):
        raise HTTPException(400, "Connect your Google account on the Profile page first")

    loop = asyncio.get_running_loop()
    try:
        events = await loop.run_in_executor(None, calendar_api.list_events, user["id"], s, e)
    except PermissionError as err:
        raise HTTPException(400, str(err))
    except Exception as err:
        runner.log.exception("Calendar read failed")
        raise HTTPException(502, f"Could not read your Google Calendar: {runner.describe(err)}")
    return {"events": events}


# Serve the built frontend in production (cd frontend && npm run build)
DIST = Path(__file__).parent.parent / "frontend" / "dist"
if DIST.exists():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="frontend")