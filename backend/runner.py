"""Runs the agent for one user: search until the approval step, and book approved items later."""
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.types import Command

import config
import db
import google_oauth
import progress
from agent import build_graph, content_texts, extract_items

log = logging.getLogger("uvicorn.error")  # shows up in the uvicorn terminal

SERVER = Path(__file__).parent / "server.py"
APPROVAL_TIMEOUT = 30 * 60

RUNS: dict[str, "Run"] = {}


def describe(e: BaseException) -> str:
    """A readable reason. Tool-server failures arrive wrapped in an ExceptionGroup, so unwrap to the real error."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    text = str(e).strip()
    return f"{type(e).__name__}: {text}" if text else type(e).__name__

TOOL_LABELS = {
    "search_opportunities": "Searching the web for opportunities",
}


class Run:
    def __init__(self, user_id: int):
        self.id = uuid.uuid4().hex
        self.user_id = user_id
        self.status = "running"  # running | awaiting_approval | done | error
        self.events: list[dict] = []
        self.cond = asyncio.Condition()
        self.approval: asyncio.Future | None = None
        self.finished_at: float | None = None
        self.task: asyncio.Task | None = None

    async def emit(self, **event):
        async with self.cond:
            event["seq"] = len(self.events)
            self.events.append(event)
            self.cond.notify_all()

    async def stream(self):
        i = 0
        while True:
            timed_out = False
            async with self.cond:
                if i >= len(self.events):
                    try:
                        await asyncio.wait_for(self.cond.wait(), timeout=15)
                    except asyncio.TimeoutError:
                        timed_out = True
            if timed_out:
                yield ": keepalive\n\n"
                continue
            while i < len(self.events):
                event = self.events[i]
                i += 1
                yield f"data: {json.dumps(event)}\n\n"
                if event["type"] in ("done", "error"):
                    return

    def approve(self, selected: list[int]) -> bool:
        if self.status != "awaiting_approval" or not self.approval or self.approval.done():
            return False
        self.approval.set_result(selected)
        return True


def get(run_id: str, user_id: int) -> Run | None:
    run = RUNS.get(run_id)
    return run if run and run.user_id == user_id else None


def active_run_for(user_id: int) -> Run | None:
    for run in RUNS.values():
        if run.user_id == user_id and run.status in ("running", "awaiting_approval"):
            return run
    return None


def start(user_id: int, profile: dict, cv_path: str, query: str, categories: list[str]) -> Run:
    now = time.time()
    for rid in [r.id for r in RUNS.values() if r.finished_at and now - r.finished_at > 3600]:
        del RUNS[rid]
    run = Run(user_id)
    RUNS[run.id] = run
    db.create_run(run.id, user_id)
    run.task = asyncio.create_task(_execute(run, profile, cv_path, query, categories))
    return run


TYPE_WORDS = {"spring_week": "spring week", "hackathon": "hackathons"}


def build_prompt(profile: dict, cv_path: str, query: str, categories: list[str]) -> str:
    eligibility = {
        "graduation_year": profile["graduation_year"],
        "location": profile["location"],
        "year_of_study": profile.get("year", ""),
        "degree": profile.get("degree", ""),
        "university": profile.get("university", ""),
        "gender": profile.get("gender", ""),
    }
    types = ", ".join(TYPE_WORDS.get(c, c.replace("_", " ")) for c in categories)
    interests = ", ".join(profile.get("interests") or []) or "not given"
    return f"""{query or 'Find spring weeks and internships for me.'}

User Profile Details:
- Name: {profile['name']}
- University: {profile['university']}
- Degree: {profile.get('degree', '')} ({profile.get('year', '')})
- CV File Path: {cv_path}
- Interests: {interests}
- Types of opportunity wanted: {types}
- Eligibility Info: {json.dumps(eligibility)}
"""


async def _narrate(run: Run, node: str, update):
    if node.startswith("__") or not isinstance(update, dict):
        return
    messages = update.get("messages") or []
    if node == "Researcher":
        for m in messages:
            calls = getattr(m, "tool_calls", None)
            if calls:
                for call in calls:
                    await run.emit(type="status", message=TOOL_LABELS.get(call["name"], call["name"]))
            else:
                await run.emit(type="status", message="Summarising what was found")
    elif node == "tools":
        for m in messages:
            if not isinstance(m, ToolMessage):
                continue
            texts = content_texts(m.content)
            found = extract_items(m.content)
            errors = [f["error"] for f in found if f.get("error")]
            good = [f for f in found if not f.get("error")]
            if errors:
                await run.emit(type="warning", message=errors[0])
            elif any(t.startswith("Error") for t in texts):
                await run.emit(type="warning", message=texts[0][:200])
            elif m.name == "search_opportunities":
                await run.emit(type="status", message=f"Found {len(good)} matching opportunities")


async def _drive(run: Run, app, graph_input, cfg):
    """Streams the graph, pauses for the user's approval, resumes, and emits the final result."""
    loop = asyncio.get_running_loop()
    while True:
        async for chunk in app.astream(graph_input, cfg, stream_mode="updates"):
            for node, update in chunk.items():
                await _narrate(run, node, update)

        snapshot = await app.aget_state(cfg)
        pending = [i for t in snapshot.tasks for i in t.interrupts]
        if not pending:
            break

        run.approval = loop.create_future()
        await run.emit(type="approval", items=pending[0].value["items"])
        run.status = "awaiting_approval"  # set after the event exists, so a poller can rely on it
        try:
            selected = await asyncio.wait_for(run.approval, APPROVAL_TIMEOUT)
        except asyncio.TimeoutError:
            raise RuntimeError("Approval timed out. Start a new run")
        run.status = "running"
        await run.emit(
            type="status",
            message="Adding to your calendar" if selected else "Skipping the calendar",
        )
        graph_input = Command(resume=selected)

    values = (await app.aget_state(cfg)).values
    summary = values.get("summary") or ("".join(content_texts(values["messages"][-1].content)).strip() if values.get("messages") else "")
    run.status = "done"
    await run.emit(type="done", summary=summary, booked=values.get("booking_report", []))


@asynccontextmanager
async def _mcp_tools(user_id: int):
    """Starts a tool server for this user with their own Google token, and yields its tools."""
    loop = asyncio.get_running_loop()
    try:
        token_json = await loop.run_in_executor(None, google_oauth.fresh_token_json, user_id)
    except Exception:
        raise RuntimeError("Google access expired or was removed. Reconnect Google on your Profile page")
    if not token_json:
        raise RuntimeError("Connect your Google account on your Profile page first")

    # The tool server only gets what it needs, not our app secrets.
    env = {k: os.environ[k] for k in ("ANTHROPIC_API_KEY", "TAVILY_API_KEY", "CLAUDE_MODEL", "CLAUDE_FAST_MODEL") if k in os.environ}
    known = [r["key"] for r in db.get_opportunities(user_id) if r["key"].startswith("web|")]  # the search skips what you already have
    env.update(PROGRESS_USER=str(user_id), PROGRESS_DIR=str(progress.DIR), KNOWN_OPPS=json.dumps(known), GOOGLE_TOKEN_JSON=token_json, CALENDAR_ID="primary", CV_ROOT=str(config.CV_ROOT),
               SHOT_ROOT=str(config.SHOT_ROOT))
    client = MultiServerMCPClient(
        {"opportunities": {"transport": "stdio", "command": sys.executable, "args": [str(SERVER)], "env": env}}
    )
    async with client.session("opportunities") as session:
        yield await load_mcp_tools(session)


async def _execute(run: Run, profile: dict, cv_path: str, query: str, categories: list[str]):
    try:
        await run.emit(type="status", message="Starting your search")
        async with _mcp_tools(run.user_id) as all_tools:
            tools = [t for t in all_tools if t.name == "search_opportunities"]  # the agent only searches; the plan is made after approval
            plan_tool = next(t for t in all_tools if t.name == "plan_application")
            app = build_graph(tools, plan_tool)

            cfg = {"configurable": {"thread_id": run.id}, "recursion_limit": 25}
            graph_input = {
                "messages": [HumanMessage(content=build_prompt(profile, cv_path, query, categories))],
                "scheduled_events": [],
                "summary": "",
                "booking_report": [],
            }
            await _drive(run, app, graph_input, cfg)
    except Exception as e:
        log.exception("Agent run failed")  # full traceback in the terminal
        run.status = "error"
        # Our own RuntimeErrors are already written for the user; anything else gets its type shown.
        await run.emit(type="error", message=str(e) if type(e) is RuntimeError else describe(e))
    finally:
        run.finished_at = time.time()
        db.finish_run(run.id, run.status)


SEARCH_TIMEOUT = 3 * 60


async def search(user_id: int, profile: dict, cv_path: str, query: str, categories: list[str]) -> dict:
    """Runs the search tool directly (no language-model turns around it, which used to add two slow calls) and hands back the list.

    Nothing is booked here. The user approves later, and book() does the booking.
    Returns {"items": [raw item dicts], "message": str}.
    """
    if active_run_for(user_id):
        raise RuntimeError("A search is already running. Wait for it to finish")
    run = Run(user_id)
    RUNS[run.id] = run
    db.create_run(run.id, user_id)
    progress.clear(user_id, "search")
    try:
        eligibility = {
            "graduation_year": profile["graduation_year"], "location": profile["location"],
            "year_of_study": profile.get("year", ""), "degree": profile.get("degree", ""),
            "university": profile.get("university", ""), "gender": profile.get("gender", ""),
        }
        args = {
            "cv_file_path": cv_path,
            "interests": ", ".join(profile.get("interests") or []) or "not given",
            "eligibility_info": eligibility,
            "query": query or "Find spring weeks and internships for me.",
            "types": [TYPE_WORDS.get(c, c.replace("_", " ")) for c in categories],
        }
        log.info("[search] user %s: starting", user_id)
        import server as tools  # runs in this process: no new program to start for each search, which took many seconds on the small free server

        progress.write_for(user_id, "search", {"stage": "Reading your request", "items": [], "notes": [f"Reading your request: \"{args['query'][:120]}\""], "queries": [], "employers": [], "done": False})
        known = [r["key"] for r in db.get_opportunities(user_id) if r["key"].startswith("web|")]
        t1, t2 = progress.USER.set(user_id), tools.KNOWN.set(known)
        try:
            found = await asyncio.wait_for(tools.search_opportunities(**args), SEARCH_TIMEOUT)
        finally:
            progress.USER.reset(t1)
            tools.KNOWN.reset(t2)
        diag = next((f["diag"] for f in found if f.get("diag")), "")
        errors = [f["error"] for f in found if f.get("error")]
        items = [f for f in found if f.get("title") and not f.get("error") and (f.get("deadline") or f.get("watch"))]
        if errors and not items:
            raise RuntimeError(errors[0])
        if not items:
            message = "I searched widely but found nothing open that fits you right now."
        else:
            names = ", ".join(i["title"] for i in items[:3])
            message = f"Found {len(items)} opportunities that fit you, including {names}."
        run.status = "done"
        return {"items": items, "message": message}
    except asyncio.TimeoutError:
        run.status = "error"
        raise RuntimeError("The search took too long. Try a narrower request")
    except Exception:
        run.status = "error"
        raise
    finally:
        run.finished_at = time.time()
        db.finish_run(run.id, run.status)


async def _call(user_id: int, name: str, args: dict):
    """Starts the tool server for this user, calls one tool and returns its result."""
    async with _mcp_tools(user_id) as all_tools:
        tool = next(t for t in all_tools if t.name == name)
        return await tool.ainvoke(args)


async def plan(user_id: int, items: list[dict]) -> list[dict]:
    """plan_application: books each company's apply block and its practice sessions. One dict per block.
    Runs inside this process (starting a separate tool program for each plan cost many seconds on the small free server)."""
    t0 = time.time()
    log.info("[plan] user %s: planning %d opportunities", user_id, len(items))
    import server as tools
    loop = asyncio.get_running_loop()
    try:
        token_json = await loop.run_in_executor(None, google_oauth.fresh_token_json, user_id)
    except Exception:
        raise RuntimeError("Google access expired or was removed. Reconnect Google on your Profile page")
    if not token_json:
        raise RuntimeError("Connect your Google account on your Profile page first")
    log.info("[plan] Google token ready (%.1fs)", time.time() - t0)
    t1, t2 = progress.USER.set(user_id), tools.TOKEN.set(token_json)
    try:
        out = await tools.plan_application(items)
    finally:
        progress.USER.reset(t1)
        tools.TOKEN.reset(t2)
    log.info("[plan] finished (%.1fs in total)", time.time() - t0)
    return out


async def change_stage(user_id: int, args: dict) -> dict:
    """update_stage: moves a company to a new stage and re-plans its blocks. Returns the tool's answer."""
    found = extract_items(await _call(user_id, "update_stage", args))
    if not found:
        raise RuntimeError("The stage change returned nothing")
    return found[0]


async def reschedule(user_id: int, items: list[dict]) -> list[dict]:
    """schedule_application_with_llm: books blocks again, for work the student did not get to."""
    return extract_items(await _call(user_id, "schedule_application_with_llm", {"items": items}))


@asynccontextmanager
async def tool_session(user_id: int):
    """One tool server for several calls: `await call(name, args)` returns the tool's answer as a list of dicts."""
    async with _mcp_tools(user_id) as all_tools:
        by_name = {t.name: t for t in all_tools}

        async def call(name: str, args: dict) -> list[dict]:
            return extract_items(await by_name[name].ainvoke(args))

        yield call