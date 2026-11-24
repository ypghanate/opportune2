import asyncio
import base64
import json
import os
import re
import sys
import time as time_module
from collections import Counter
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import docx
import httpx
import pdfplumber
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from mcp.server.fastmcp import FastMCP
from anthropic import AsyncAnthropic, BadRequestError
from pydantic import BaseModel, Field
from tavily import TavilyClient

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
]

CALENDAR_ID = os.environ.get("CALENDAR_ID", "primary")
TOKEN_PATH = Path(__file__).parent / "token.json"

# Keys come from backend/.env (the API passes them to this process)
tavily_client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
llm = AsyncAnthropic()  # reads ANTHROPIC_API_KEY
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")  # search, extraction, scheduling
FAST_MODEL = os.environ.get("CLAUDE_FAST_MODEL", "claude-haiku-5-5")  # per-email classification
SCHEDULER_MODEL = os.environ.get("SCHEDULER_MODEL", FAST_MODEL)  # picks the calendar slots; the code checks every rule, so the fast model is enough
SCHEDULER_ATTEMPTS = int(os.environ.get("SCHEDULER_ATTEMPTS", "1"))  # model tries before the code finds the slot itself
SCHEDULER_TIMEOUT = float(os.environ.get("SCHEDULER_TIMEOUT", "12"))  # seconds the model gets for one try

mcp = FastMCP("searching_server")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class SearchQueryModel(BaseModel):
    reasoning: str = Field(description="Brief breakdown of what search parameters to combine.")
    new_search_query: str = Field(
        description="An optimized, highly specific search query combining interests, location and graduation year"
    )


class OutputForm(BaseModel):
    title: str = Field(description="The title of the event or position")
    url: str = Field(description="Source URL of the listing, copied verbatim from Tavily search results")
    grad: int = Field(description="Graduation year target")
    eligibility: bool = Field(description="Whether open to user or not")
    type: Literal[
        "hackathons",
        "talk",
        "networking",
        "spring week",
        "internship",
        "research",
        "other_competitions",
    ]
    date: datetime = Field(description="What date will the event start, ISO 8601, e.g. 2027-03-29T09:00:00Z")
    time_start: time | None = Field(default=None, description="When the event will start as HH:MM:SS, or null if unknown")
    duration: timedelta | None = Field(default=None, description="How long the event lasts as an ISO 8601 duration, e.g. PT2H or P3D, or null if unknown")
    location: str = Field(description="Location of the event (or 'Online' if virtual)")
    online: bool = Field(description="If event is online/virtual or not")
    gender_requirement: list[str] = Field(default=[], description="List of eligible genders if restricted")
    graduation_year_requirement: list[int] = Field(default=[], description="List of eligible grad years if restricted")
    deadline: datetime = Field(description="What is the last date to apply, ISO 8601, e.g. 2026-12-15T23:59:00Z")


class SearchResults(BaseModel):
    results: list[OutputForm]


class SlotSuggestion(BaseModel):
    opportunity_index: int = Field(description="The 'index' of the opportunity this slot is for")
    start_time: str = Field(description="ISO 8601 start time of the block WITH its UTC offset, in UK local time, e.g. 2026-11-23T18:00:00+00:00")
    duration_hours: float = Field(description="Length of the block in hours (0.5 to 3)")
    reason: str = Field(description="One sentence on why this slot was chosen")


class ScheduleSuggestions(BaseModel):
    suggestions: list[SlotSuggestion]


# ---------------------------------------------------------------------------
# Claude with a fixed answer shape
# ---------------------------------------------------------------------------
def _inline_refs(node, defs):
    """Claude's tool schemas are simplest without $ref, so copy each definition in place."""
    if isinstance(node, dict):
        if "$ref" in node:
            return _inline_refs(defs[node["$ref"].split("/")[-1]], defs)
        return {k: _inline_refs(v, defs) for k, v in node.items() if k != "title"}
    if isinstance(node, list):
        return [_inline_refs(v, defs) for v in node]
    return node


FORCE_TOOL = True  # flips to False if a model rejects a forced tool_choice


async def ask(messages: list[dict], schema: type[BaseModel], model: str | None = None, max_tokens: int = 8192):
    """Asks Claude and returns a validated `schema` object. Replaces OpenAI's `.parse(response_format=...)`:
    Claude answers through one tool whose input schema is the pydantic model.
    `messages` use the usual roles; "system" messages are moved to Claude's separate system prompt.
    Some models reject a forced tool_choice (400), so we then fall back to asking it to call the tool."""
    global FORCE_TOOL
    raw = schema.model_json_schema()
    input_schema = _inline_refs({k: v for k, v in raw.items() if k != "$defs"}, raw.get("$defs", {}))
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    chat = [m for m in messages if m["role"] != "system"]
    tools = [{"name": "answer", "description": "Give your answer in exactly this structure.", "input_schema": input_schema}]

    async def call(forced: bool):
        kwargs = {}
        sys_text = system
        if forced:
            kwargs["tool_choice"] = {"type": "tool", "name": "answer"}
        else:
            sys_text = (system + "\n\n" if system else "") + "You MUST reply by calling the `answer` tool exactly once. Do not write any other text."
        if sys_text:
            kwargs["system"] = sys_text
        return await llm.messages.create(model=model or MODEL, max_tokens=max_tokens, messages=chat, tools=tools, **kwargs)

    if FORCE_TOOL:
        try:
            response = await call(True)
        except BadRequestError as e:
            if "tool_choice" not in str(e):
                raise
            print("[ask] model rejects forced tool_choice, falling back to auto", file=sys.stderr)
            FORCE_TOOL = False
            response = await call(False)
    else:
        response = await call(False)

    block = next((b for b in response.content if b.type == "tool_use"), None)
    if block is None:  # auto mode can occasionally answer in text: one more try
        response = await call(False)
        block = next((b for b in response.content if b.type == "tool_use"), None)
    if block is None:
        raise ValueError(f"Claude did not return a structured answer (stop reason: {response.stop_reason})")
    return schema.model_validate(block.input)


# ---------------------------------------------------------------------------
# Google auth (single shared helper)
# ---------------------------------------------------------------------------
def get_credentials() -> Credentials:
    token_json = os.environ.get("GOOGLE_TOKEN_JSON")
    if token_json:  # per-user token injected by the FastAPI backend for this run
        creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
        if not creds.valid and creds.refresh_token:
            creds.refresh(Request())
        return creds

    creds = None  # local/dev fallback: token.json created by auth.py
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json())
    if not creds or not creds.valid:
        raise RuntimeError("Missing/invalid Google credentials.")
    return creds


def get_calendar_service():
    return build("calendar", "v3", credentials=get_credentials())


def get_gmail_service():
    return build("gmail", "v1", credentials=get_credentials())


# ---------------------------------------------------------------------------
# CV parsing
# ---------------------------------------------------------------------------
async def cv_to_text(cv_file: str) -> dict:
    path = os.path.expanduser(str(cv_file))
    root = os.environ.get("CV_ROOT")
    if root and Path(root).resolve() not in Path(path).resolve().parents:
        raise ValueError("CV path is outside the allowed folder")
    lower = path.lower()

    if lower.endswith(".pdf"):
        pages = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                if text.strip():
                    pages.append(text)
        full = "\n\n".join(pages)
        full = re.sub(r"[ \t]+", " ", full)
        full = re.sub(r"\n\s*\n", "\n\n", full).strip()
        return {"text": full, "format": "pdf"}

    if lower.endswith(".docx"):
        d = docx.Document(path)
        parts = [p.text.strip() for p in d.paragraphs if p.text.strip()]
        for table in d.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    parts.append(" | ".join(dict.fromkeys(cells)))
        return {"text": re.sub(r"[ \t]+", " ", "\n".join(parts)).strip(), "format": "docx"}

    raise ValueError(f"Unsupported file format or file not found at: {path}")


# ---------------------------------------------------------------------------
# Opportunity search
# ---------------------------------------------------------------------------
async def verify_url(url: str) -> bool:
    try:
        parsed = httpx.URL(url)
        if len(parsed.path) <= 1:
            return False
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            # 401/403/429 usually mean "bot blocked" not "broken", so allow them
            if response.status_code >= 400 and response.status_code not in (401, 403, 429):
                return False
            final = httpx.URL(str(response.url))
            if final.host == parsed.host and len(final.path) <= 1:
                return False
            return True
    except Exception:
        return False


_CACHE: dict = {}
SEARCH_CACHE_MIN = float(os.environ.get("SEARCH_CACHE_MIN", "30"))  # an identical web search is not repeated inside this many minutes; 0 turns the caches off
PAGE_CACHE_MIN = float(os.environ.get("PAGE_CACHE_MIN", "60"))  # a programme page is not read again inside this many minutes


def _cached(kind: str, key, minutes: float):
    if minutes <= 0:
        return None
    hit = _CACHE.get((kind, key))
    if hit and time_module.monotonic() - hit[0] < minutes * 60:
        return hit[1]
    return None


def _remember(kind: str, key, value, minutes: float):
    if minutes <= 0:
        return
    if len(_CACHE) > 4000:  # keep memory small: drop the oldest quarter
        for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:1000]:
            _CACHE.pop(k, None)
    _CACHE[(kind, key)] = (time_module.monotonic(), value)


async def tavily_search_opportunities(search_query: str, max_results: int = 10) -> list[dict]:
    hit = _cached("tavily", (search_query, max_results), SEARCH_CACHE_MIN)
    if hit is not None:
        return hit
    found = await _tavily_search(search_query, max_results)
    if found:
        _remember("tavily", (search_query, max_results), found, SEARCH_CACHE_MIN)
    return found


async def _tavily_search(search_query: str, max_results: int = 10) -> list[dict]:
    loop = asyncio.get_running_loop()
    response = await loop.run_in_executor(
        None,
        lambda: tavily_client.search(
            query=search_query,
            search_depth="basic",
            max_results=max_results,
            include_raw_content=False,
        ),
    )
    return response.get("results", [])


class SearchPlan(BaseModel):
    queries: list[str] = Field(description="10 to 12 different web search queries, each short (3 to 10 words)")
    student_country: str = Field(default="United Kingdom", description="The country the student's location is in, e.g. 'United Kingdom'")
    employers: list[str] = Field(default=[], description="25 to 30 real employers (company names only) known to run the wanted kind of programme for this student's stage, spread over their interests")


class Candidate(BaseModel):
    index: int = Field(description="The 'index' of the search result this came from")
    company: str = Field(description="The ONE employer running this programme, e.g. 'Goldman Sachs'. Never a job board or a list site")
    title: str = Field(description="Programme name, e.g. 'Spring into Banking 2027' or 'Software Engineering Summer Internship 2027'")
    type: Literal["spring week", "internship", "hackathons", "talk", "networking", "research", "other_competitions", "other"]
    page_is_this_company_only: bool = Field(
        description="true if the result's page is about this one company's programme (the company's careers page, its application page, "
        "or a job board page for this single role). false if the page lists several companies or roles"
    )
    location: str = Field(description="City or country, 'Online', or 'Unknown'")
    online: bool = False
    date: str | None = Field(default=None, description="Programme start date, ISO 8601, only if the page states it, else null")
    deadline: str | None = Field(default=None, description="Application deadline, ISO 8601, only if stated, else null (rolling or not given)")
    gender_requirement: list[str] = Field(default=[], description="Eligible genders, only if restricted")
    graduation_year_requirement: list[int] = Field(default=[], description="Eligible graduation years, only if restricted")
    programme_year: int | None = Field(default=None, description="The year the programme itself runs in, e.g. 2027 for 'Spring Week 2027' or 'Summer 2027 internship'. For an academic year like 2026/27 use the later year. Null only if the page truly does not say")
    field: str = Field(default="", description="The area of work the programme is in, as specifically as the page allows, e.g. 'software engineering', 'quantitative trading', 'investment banking: markets', 'management consulting', 'machine learning research'. Use the named division or stream when there is one")
    country: str = Field(default="Unknown", description="Country the programme takes place in, e.g. 'United Kingdom' or 'United States'. 'Online' if virtual, 'Multiple' if several countries, 'Unknown' if the page does not say")
    restricted_to: list[Literal["women", "men", "ethnic_minority", "lgbtq", "disability", "low_income_or_first_generation", "specific_universities", "citizens_or_residents_only"]] = Field(
        default=[], description="Groups the programme is limited to, as the page states it. 'Women in Trading' or 'women only' = women. Empty if open to everyone.")
    universities_allowed: list[str] = Field(default=[], description="If limited to specific universities, their names; otherwise empty")
    open_to: list[Literal["first_year", "penultimate_year", "final_year", "any_undergraduate", "postgraduate", "phd", "graduate"]] = Field(
        default=[],
        description="Who the programme is for, as the page states it. 'first_year' = first-year / freshers / 'first-year spring week'; "
        "'penultimate_year' = penultimate-year internships; 'final_year' = final-year students; 'any_undergraduate' = any undergraduate year; "
        "'postgraduate' = masters; 'phd' = PhD or doctoral; 'graduate' = full-time graduate roles. Empty if the page does not say.")


class Candidates(BaseModel):
    opportunities: list[Candidate]


class Score(BaseModel):
    index: int
    match: Literal["strong", "partial", "none"] = Field(description="How well the programme's field matches what the student wants")
    score: int = Field(description="0 to 10: how well this matches the student's interests, search request and wanted types")
    reason: str = Field(description="One short sentence on why")


class Scores(BaseModel):
    scores: list[Score]


SEARCH_BUDGET = float(os.environ.get("SEARCH_BUDGET", "75"))  # seconds for the whole search; each stage adapts to what is left
SEARCH_CONCURRENCY = 8  # Tavily searches at once
EXTRACT_BATCH = 10  # search results read per model call
MAX_SHOWN = 8  # most opportunities returned, best first
LOOSE_MIN_SCORE = 4  # the weakest adjacent match that may fill a thin list
MIN_WANTED = 5  # below this many results, looser matches are added so the list is not nearly empty
MAX_WATCH = 3  # programmes that are not open yet and are watched, on top of the open ones
MAX_RESOLVE = 20  # companies found on list pages that we look up on their own


async def _within(coros, seconds: float) -> list:
    """Runs coroutines together and returns the results of those that finish in time; the rest are cancelled."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    if not tasks:
        return []
    done, pending = await asyncio.wait(tasks, timeout=max(seconds, 0.5))
    for t in pending:
        t.cancel()
    return [t.result() for t in tasks if t in done and not t.cancelled() and t.exception() is None]
MIN_STRONG = 5  # with this many strong matches, partial ones are left out
PARTIAL_MIN_SCORE = 6  # a partial match needs at least this score to fill a gap
MIN_SCORE = 2  # only clearly wrong ones are dropped; the best MAX_SHOWN are kept whatever their score
NO_DEADLINE_DAYS_WEB = 14  # rolling or unknown deadline: apply within two weeks


def _student_stage(info: dict, now: datetime) -> tuple[str, set[str], str]:
    """Works out where the student is: (a plain description, the open_to values that apply to them, what to avoid)."""
    label = str(info.get("year_of_study") or "").lower()
    grad = info.get("graduation_year")
    academic_end = now.year + 1 if now.month >= 9 else now.year  # the year the current academic year ends
    left = (int(grad) - academic_end) if str(grad).isdigit() else None
    if "post" in label:
        return ("a postgraduate student", {"postgraduate", "phd"} if "phd" in str(info.get("degree", "")).lower() else {"postgraduate"},
                "undergraduate-only schemes and full-time graduate jobs")
    if left is not None and left <= 0:
        stage = "final_year"
    elif left == 1:
        stage = "penultimate_year"
    else:
        stage = "first_year"
    if label.startswith("first"):
        stage = "first_year"  # what they told us on their profile wins
    names = {"first_year": "an early-stage undergraduate (first or second year, with at least two years left)",
             "penultimate_year": "a penultimate-year undergraduate", "final_year": "a final-year undergraduate"}
    ok = {stage, "any_undergraduate"}
    avoid = {
        "first_year": "penultimate-year or final-year-only internships, graduate schemes, PhDs and masters programmes",
        "penultimate_year": "first-year-only programmes, final-year-only roles, graduate schemes, PhDs and masters programmes",
        "final_year": "first-year or penultimate-year-only programmes, PhDs and masters programmes",
    }[stage]
    return names[stage], ok, avoid


def _parse_dt(value):
    if not value:
        return None
    try:
        v = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v.astimezone(timezone.utc)
    except ValueError:
        return None


def _norm(text: str) -> str:
    text = re.sub(r"\b20\d\d\b|\(.*?\)", " ", (text or "").lower())
    text = re.sub(r"\b(plc|ltd|limited|llp|inc|group|the|uk|international|&|and)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", "", text)


async def _search_many(queries: list[str], per_query: int, seconds: float = 20) -> list[dict]:
    sem = asyncio.Semaphore(SEARCH_CONCURRENCY)

    async def one(q):
        try:
            async with sem:
                return await tavily_search_opportunities(q, max_results=per_query)
        except Exception as e:
            print(f"[search] '{q}' failed: {e}", file=sys.stderr)
            return []

    seen, results = set(), []
    for found in await _within([one(q) for q in queries], seconds):
        for r in found:
            url = r.get("url")
            if url and url not in seen:
                seen.add(url)
                results.append({"url": url, "title": r.get("title", ""), "content": (r.get("content") or "")[:900]})
    return results


async def _extract(results: list[dict], context: str, model: str = FAST_MODEL, seconds: float = 25) -> list[tuple[dict, Candidate]]:
    """Reads search results and pulls out one entry per company programme."""
    system = f"""You extract early-careers opportunities from web search results. {context}

Rules:
- One entry per company programme. If a page lists several companies (a 'top spring weeks' article, a job board list),
  make a SEPARATE entry for each company programme it names, and set page_is_this_company_only to false.
- company is always the employer, never the website (not Bright Network, Gradcracker, RateMyPlacement, LinkedIn, Indeed...).
- Skip graduate (full-time) jobs, programmes that have clearly closed, and anything for school pupils.
- Always fill programme_year. Pages about earlier years (2022, 2023, 2024...) are old: still report their programme_year honestly, and never present an old programme as current.
- Always fill field with the programme's area of work (its division or stream if the page names one), not just the industry of the company.
- Fill country, restricted_to and universities_allowed from what the page says about where it is and who may apply.
- Always fill open_to from what the page says about who can apply (year of study, level). Leave it empty only if the page really does not say.
- Only state dates the text actually gives. Never invent a deadline.
- Only use the index of the result the entry came from."""
    sem = asyncio.Semaphore(10)

    async def batch_call(batch):
        payload = [{"index": n, **r} for n, r in enumerate(batch)]
        try:
            async with sem:
                out = await ask([{"role": "system", "content": system},
                                 {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], Candidates, model=model)
            return [(batch[c.index], c) for c in out.opportunities if 0 <= c.index < len(batch) and c.company.strip()]
        except Exception as e:
            print(f"[search] extraction failed for {len(batch)} results: {e}", file=sys.stderr)
            return []

    key_of = lambda r: (r["url"], hash(r.get("content", "")), context)
    cached_pairs, todo = [], []
    for r in results:
        hit = _cached("extract", key_of(r), PAGE_CACHE_MIN)
        if hit is None:
            todo.append(r)
        else:
            cached_pairs += [(r, c) for c in hit]
    batches = [todo[i:i + EXTRACT_BATCH] for i in range(0, len(todo), EXTRACT_BATCH)]
    fresh = [pair for got in await _within([batch_call(b) for b in batches], seconds) for pair in got]
    for r in todo:  # remember what each page gave; a page that gave nothing is read again, since its batch may simply have timed out
        mine = [c for rr, c in fresh if rr is r]
        if mine:
            _remember("extract", key_of(r), mine, PAGE_CACHE_MIN)
    return cached_pairs + fresh


STAGE_WORDS = {"first_year": "first year", "penultimate_year": "penultimate year", "final_year": "final year"}


def _same_company(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    return bool(a) and bool(b) and (a == b or (len(min(a, b, key=len)) >= 5 and (a in b or b in a)))


async def _resolve_many(companies: list[Candidate], context: str, year: int, stage_word: str, type_words: str, seconds: float) -> list[tuple[dict, Candidate]]:
    """Companies found only on list pages: one search each for their own page, then ONE pass over everything found."""
    queries = [f"{c.company} {stage_word} {type_words} {year} apply".replace("  ", " ") for c in companies]
    pages = await _search_many(queries, per_query=4, seconds=seconds * 0.45)
    if not pages:
        return []
    names = ", ".join(sorted({c.company for c in companies}))
    found = await _extract(pages, context + f" Only extract programmes run by these companies: {names}.", seconds=seconds * 0.55)
    out = []
    for r, cand in found:
        if cand.page_is_this_company_only and any(_same_company(cand.company, c.company) for c in companies):
            out.append((r, cand))
    return out


def _keep_relevant(entries: list[dict]) -> list[dict]:
    """Strong matches (in the student's own fields) first. With enough of them, adjacent ones are left out; with too few,
    only decent adjacent ones fill the gap, so a quant student is not shown generic finance programmes."""
    strong = [e for e in entries if e["match"] == "strong"]
    if len(strong) >= MIN_STRONG:
        return strong
    return strong + [e for e in entries if e["match"] != "strong" and e["score"] >= PARTIAL_MIN_SCORE]


async def _rank(entries: list[dict], profile_text: str, seconds: float = 12) -> dict[int, Score]:
    compact = [{"index": n, "company": e["company"], "title": e["title"], "field": e.get("field", ""), "type": e["type"],
                "location": e["location"], "open_to": e.get("open_to")} for n, e in enumerate(entries)]
    system = f"""You decide which opportunities are relevant to this student, and how well.
{profile_text}

Step 1. Work out the student's TARGET FIELDS from the three things they gave:
- The search request, if it names a field, sector, role, company or city, is the target and it OVERRIDES their profile interests.
- If the request is generic (for example "find spring weeks and internships for me"), the INTERESTS are the target.
- If they gave no interests, use their degree.
- The CV never adds fields. It only tells you how well a programme in a target field fits this student (relevant projects, skills, modules), so use it to separate good fits from weaker ones inside the target fields.

Step 2. For every programme, set match by comparing the programme's field (and its title) with the target fields:
- strong: the programme is in a target field. A technology or engineering stream at a bank counts as software engineering; a quant stream at a hedge fund counts as quant.
- partial: a clearly adjacent field (for example data science or machine learning for a software engineer), or a general programme that has a stream in a target field.
- none: a different field, even from a famous employer. For a quant student, quant trading, quant research, quant development, systematic or algorithmic trading, market making and risk or pricing quant roles are strong; software engineering or data science at a trading firm is partial; investment banking, general markets, consulting, audit, law and generic graduate programmes are none. For a software engineering student, investment banking, markets, trading, consulting, audit, law and similar programmes with no engineering stream are none. For a finance student, pure software roles are none.

Step 3. Give a score from 0 to 10 for how good a fit it is overall (field first, then kind of programme, location, and how well-known it is). Make scores differ so the order is meaningful. Inside the target fields, add up to 2 points when the CV clearly supports the programme.
Every programme listed is already open to the student's year of study, so do not mark them down for that. Be strict about match: when in doubt between partial and none, ask whether the student would be glad to see it. Give a one-sentence reason that names the field."""
    scores = {}

    async def part(chunk):
        try:
            out = await ask([{"role": "system", "content": system},
                             {"role": "user", "content": json.dumps(chunk, ensure_ascii=False)}], Scores)
            return out.scores
        except Exception as e:
            print(f"[search] ranking failed: {e}", file=sys.stderr)
            return []

    for got in await _within([part(compact[i:i + 80]) for i in range(0, len(compact), 80)], seconds):
        scores.update({s.index: s for s in got})
    return scores


def _gender_key(value) -> str:
    v = re.sub(r"[^a-z]", "", str(value or "").lower())
    if v in ("female", "woman", "women", "f", "girl"):
        return "female"
    if v in ("male", "man", "men", "m", "boy"):
        return "male"
    if v in ("nonbinary", "enby"):
        return "nonbinary"
    return ""


_UK = {"uk", "unitedkingdom", "greatbritain", "britain", "england", "scotland", "wales", "northernireland"}
_US = {"us", "usa", "unitedstates", "unitedstatesofamerica", "america"}
_ANYWHERE = {"", "unknown", "online", "virtual", "remote", "multiple", "global", "worldwide", "international", "various", "europe", "emea"}


_ABROAD = re.compile(r"\b(china|chinese|beijing|shanghai|shenzhen|guangzhou|hong kong|singapore|india|mumbai|bangalore|bengaluru|delhi|japan|tokyo|korea|seoul|taiwan|dubai|uae|"
                     r"united states|usa|new york|chicago|boston|san francisco|silicon valley|canada|toronto|australia|sydney|melbourne|germany|frankfurt|berlin|munich|france|paris|"
                     r"switzerland|zurich|geneva|netherlands|amsterdam|ireland|dublin|brazil|mexico|south africa|johannesburg|emea\s+excluding)\b", re.I)


def _abroad(*texts) -> str:
    """A non-UK place named in the text, when the programme's country field was left 'Unknown' or 'Multiple'."""
    m = _ABROAD.search(" ".join(str(t or "") for t in texts))
    return m.group(1) if m else ""


def _country_key(value) -> str:
    v = re.sub(r"[^a-z]", "", str(value or "").lower())
    return "uk" if v in _UK else "us" if v in _US else v


GROUP_WORDS = {
    "ethnic_minority": "applicants from an ethnic minority background",
    "lgbtq": "LGBTQ+ applicants",
    "disability": "applicants with a disability",
    "low_income_or_first_generation": "low-income or first-generation students",
    "citizens_or_residents_only": "citizens or residents with the right to work there",
}


class OpenStatus(BaseModel):
    status: Literal["open", "not_yet_open", "closed", "unknown"] = Field(
        description="open = applications are open right now. not_yet_open = the page gives a future opening date, says 'coming soon' or 'register your interest'. "
        "closed = applications have closed, the deadline has passed, or the page is about a programme from an earlier year. unknown = the text does not say")
    opens_on: str | None = Field(default=None, description="Opening date, ISO 8601, only if the text states one")
    deadline: str | None = Field(default=None, description="Closing date, ISO 8601, only if the text states one")
    for_year: int | None = Field(default=None, description="The year the programme on this page runs in (for 2026/27 use 2027), if the text says")
    evidence: str = Field(default="", description="A short phrase (under 12 words) from the text that shows this")


_NOISE = re.compile(r"<(script|style|noscript|svg)\b.*?</\1>", re.S | re.I)
_KEYWORDS = re.compile(r"appl(?:y|ication|ications)|deadline|clos(?:e|es|ed|ing)|open(?:s|ed|ing)?\b|rolling|register|20[123]\d", re.I)


def _page_text(markup: str, limit: int = 3500) -> str:
    """The parts of a page that talk about applying, opening and closing."""
    import html as html_lib
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", _NOISE.sub(" ", markup)))
    text = re.sub(r"\s+", " ", text).strip()
    spans = []
    for m in _KEYWORDS.finditer(text):
        a, b = max(0, m.start() - 200), min(len(text), m.end() + 250)
        if spans and a <= spans[-1][1]:
            spans[-1][1] = b
        else:
            spans.append([a, b])
    joined = " ... ".join(text[a:b] for a, b in spans)
    return (joined or text)[:limit]


async def _fetch_page(url: str) -> tuple[bool, str]:
    """(is the link alive, the text about applying on that page). Sites that block bots count as alive with no text."""
    try:
        parsed = httpx.URL(url)
        if len(parsed.path) <= 1:
            return False, ""
        async with httpx.AsyncClient(timeout=7.0, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if response.status_code in (404, 410):
            return False, ""
        if response.status_code >= 400:  # blocked, rate limited or a server hiccup: the page may be fine, we just cannot read it
            return True, ""
        final = httpx.URL(str(response.url))
        if final.host == parsed.host and len(final.path) <= 1:
            return False, ""
        if response.status_code >= 400 or "html" not in response.headers.get("content-type", "html"):
            return True, ""
        return True, _page_text(response.text)
    except Exception:  # a slow or odd career site is not proof the link is dead; only a clear 404 or a redirect to the home page is
        return True, ""


async def _check_open(pool: list[dict], now: datetime, seconds: float) -> None:
    """Reads each programme's own page and records whether applications have actually opened."""
    sem = asyncio.Semaphore(10)

    keep = ("alive", "open_status", "open_evidence", "for_year", "deadline", "deadline_assumed", "opens_on")

    async def one(e):
        hit = _cached("open", e["url"], PAGE_CACHE_MIN)
        if hit:
            e.update(hit)
            return
        try:
            await one_fresh(e)
        finally:
            if e.get("open_status") and e.get("open_status") != "unknown":  # an unknown answer may just be a slow page: try again next time
                _remember("open", e["url"], {k: e[k] for k in keep if k in e}, PAGE_CACHE_MIN)

    async def one_fresh(e):
        async with sem:
            alive, text = await _fetch_page(e["url"])
            e["alive"] = alive
            text = text or e.get("_snippet", "")
            if not alive or not text:
                e["open_status"] = "unknown"
                return
            system = (f"Today is {now:%d %B %Y}. Below is text from a web page about '{e['title']}' at {e['company']}. "
                      "Decide whether applications for this programme are open RIGHT NOW. Use only what the text says; if it does not say, answer unknown. "
                      "Give dates only if the text states them. If the page is about a programme from a year that has already finished, the answer is closed.")
            try:
                out = await ask([{"role": "system", "content": system}, {"role": "user", "content": text}], OpenStatus, model=FAST_MODEL, max_tokens=600)
            except Exception as err:
                print(f"[search] could not check if {e['company']} is open: {err}", file=sys.stderr)
                e["open_status"] = "unknown"
                return
            e["open_status"], e["open_evidence"], e["for_year"] = out.status, out.evidence, out.for_year
            closing = _parse_dt(out.deadline)
            if out.status == "open" and closing and closing > now:
                e["deadline"], e["deadline_assumed"] = closing.isoformat(), False
            opening = _parse_dt(out.opens_on)
            if opening:
                e["opens_on"] = opening.isoformat()

    await _within([one(e) for e in pool], seconds)


@mcp.tool()
async def search_opportunities(
    cv_file_path: str,
    interests: str,
    eligibility_info: dict,
    query: str,
    types: list[str] | None = None,
    max_results: int = 8,
) -> list[dict]:
    """Searches the web widely for spring weeks, internships and other opportunities, one company programme per result,
    then keeps the ones that fit the student. types: the 'Types of opportunity wanted' from the message,
    e.g. ["spring week", "internship"]. Leave max_results at its default (the best 8 are returned)."""
    try:
        cv_object = await cv_to_text(cv_file_path)
    except Exception as e:
        return [{"error": f"Could not read CV: {e}"}]

    began = time_module.monotonic()

    def lap(text):
        print(f"[search] {text} ({time_module.monotonic() - began:.0f}s)", file=sys.stderr)

    now = datetime.now(timezone.utc)
    left = lambda reserve=0: SEARCH_BUDGET - (time_module.monotonic() - began) - reserve  # seconds remaining
    max_results = MAX_SHOWN  # always the best 8, whatever the agent passes in
    types = [t for t in (types or []) if t] or ["spring week", "internship"]
    grad = str(eligibility_info.get("graduation_year") or "")
    location = eligibility_info.get("location") or "United Kingdom"
    stage_text, stage_ok, stage_avoid = _student_stage(eligibility_info, now)
    profile_text = f"""Student: {stage_text} ({eligibility_info.get('year_of_study') or 'year not given'}) studying {eligibility_info.get('degree') or 'an unknown degree'} at {eligibility_info.get('university') or 'an unknown university'}, graduating {grad or 'unknown'}, based in {location}.
Interests: {interests or 'not given'}.
Types wanted: {', '.join(types)}.
Search request (the student's own words; it overrides the interests when it names a specific field, role, company or city): {query or 'none'}.
Programmes must be open to this student's stage. AVOID: {stage_avoid}.
CV excerpt: {cv_object['text'][:1200]}"""
    context = f"Today is {now:%Y-%m-%d}. The student graduates in {grad or 'unknown'} and is based in {location}."

    # 1. Many different searches, so we see lots of programmes
    student_country = "United Kingdom"
    plan_system = f"""You plan web searches to find as many early-careers opportunities as possible for this student.
Today is {now:%d %B %Y}.
{profile_text}

Write 10 to 12 different queries for the current application cycle (programmes running in {now.year + 1}).
Only target programmes the student can actually attend and apply to from their location; do not search other countries unless they are online or the search request asks for them.
Tailor every query to THIS student's stage, wording it the way employers do for them (e.g. "first year spring week", "insight programme first year undergraduate", "penultimate year summer internship", "final year" ...). Never write queries for stages they are not in, and never for PhDs, masters or graduate schemes unless that is their stage.
The student gave three things: a search request (their words just now), their interests, and their CV. Use them like this.
TARGET FIELDS: the field the search request names, if it names one; otherwise the student's interests. Every query and every employer must serve the target fields, and nothing else.
The CV decides which sub-areas, skills and employers inside the target fields suit them best (for a quant student with machine learning projects: quant research and systematic trading). Never write a query for a field the student did not choose just because their degree or CV could also fit it.
If the search request is specific (a sector, company, city or type), at least half of the queries must target exactly that.
- broad ones per wanted type and location, e.g. "spring week {now.year + 1} UK", "summer internship {now.year + 1} London penultimate year"
- several per interest field, e.g. "technology spring week {now.year + 1}", "quant trading internship {now.year + 1} UK"
- several naming specific well-known employers in their fields (one or two employers per query)
- first-year / diversity programmes where relevant, and anything in the search request
Do not repeat yourself; each query should find different programmes.

RELEVANCE IS STRICT. Work out the student's target fields: the search request if it names a field, sector, company or city; otherwise their interests; if neither is given, their degree and CV. Every query and every employer must serve those fields. For example, for a student interested in software engineering write queries such as "software engineering spring insight 2027" and list technology companies and the technology divisions of banks, and do NOT write finance, banking, consulting or trading queries or employers. For a quant or finance student, do the opposite.

Also list 25 to 30 real employers, names only, that run the wanted programme types for a student at this stage (only employers whose programmes are in the TARGET FIELDS above, for example trading firms, market makers and quant hedge funds for a quant student; do not pad the list with banks, consultancies, law firms or tech companies that run nothing in those fields), most relevant first. Only companies you are confident exist and recruit in the UK or the student's location. These are looked up on the web afterwards, so leave out any you are unsure about."""
    try:
        plan = await asyncio.wait_for(ask([{"role": "system", "content": plan_system},
                                           {"role": "user", "content": query or "Find opportunities for me"}], SearchPlan, model=FAST_MODEL), 10)
        queries = [q.strip() for q in plan.queries if q.strip()][:12]
        employers = list(dict.fromkeys(e.strip() for e in plan.employers if e.strip()))[:30]
        student_country = plan.student_country or student_country
    except Exception as e:
        print(f"[search] planning failed, using simple queries: {e}", file=sys.stderr)
        queries, employers = [], []
    if not queries:
        queries = [f"{t} {now.year + 1} {location}" for t in types] + [f"{i.strip()} {types[0]} {now.year + 1}" for i in interests.split(",")[:6]]
    for q in queries:
        print(f"[search] query: {q}", file=sys.stderr)

    wanted = types[0] if types[0] in Candidate.model_fields["type"].annotation.__args__ else "internship"
    stage_word = STAGE_WORDS.get(next(iter(stage_ok - {"any_undergraduate"}), ""), "")
    stubs = [Candidate(index=0, company=n, title="", type=wanted, page_is_this_company_only=False, location="Unknown") for n in employers]
    print(f"[search] {len(stubs)} employers to look up directly", file=sys.stderr)
    employer_job = asyncio.ensure_future(_resolve_many(stubs, context, now.year + 1, stage_word, " or ".join(types), min(34, left(40)))) if stubs else None

    results = await _search_many(queries, per_query=10, seconds=min(16, left(50)))
    lap(f"{len(results)} unique pages from {len(queries)} searches")

    # 2. One entry per company programme
    found = await _extract(results, context, seconds=min(20, left(30))) if results else []
    lap(f"{len(found)} company programmes mentioned")

    # 3. Companies only seen on list pages: look up each one's own page, all together
    direct = [(r, c) for r, c in found if c.page_is_this_company_only]
    have = {_norm(c.company) for _, c in direct} | {_norm(n) for n in employers}
    popularity = Counter(_norm(c.company) for _, c in found if not c.page_is_this_company_only)
    to_resolve, queued = [], set()
    for _, c in sorted(found, key=lambda rc: -popularity[_norm(rc[1].company)]):  # companies on many lists first
        key = _norm(c.company)
        if not c.page_is_this_company_only and key not in have and key not in queued:
            queued.add(key)
            to_resolve.append(c)
    try:
        resolved = await _resolve_many(to_resolve[:20], context, now.year + 1, stage_word, " or ".join(types), min(14, left(24))) if (to_resolve and left(24) > 6) else []
    except Exception as e:
        print(f"[search] list-page lookups failed: {e}", file=sys.stderr)
        resolved = []
    from_employers = []
    if employer_job:
        try:
            from_employers = await asyncio.wait_for(asyncio.shield(employer_job), max(left(14), 0.5))
        except Exception as e:
            employer_job.cancel()
            print(f"[search] employer lookups did not finish: {e}", file=sys.stderr)
    lap(f"{len(direct)} programmes found on their own pages; {len(resolved)} from list-page companies; {len(from_employers)} from {len(stubs)} employers looked up")

    # 4. Tidy up: one per company programme, nothing closed, eligibility rules
    user_gender = _gender_key(eligibility_info.get("gender"))
    first_year = now.year + 1 if now.month >= 9 else now.year  # the earliest intake that can still be open
    my_country = _country_key(student_country)
    my_uni = _norm(eligibility_info.get("university") or "")
    try:
        known = set(json.loads(os.environ.get("KNOWN_OPPS", "[]")))  # programmes this student already has (any status)
    except ValueError:
        known = set()
    entries, seen, dropped = [], set(), Counter()
    for r, c in direct + resolved + from_employers:
        key = (_norm(c.company), c.type)  # one entry per company and kind of programme
        if key in seen:
            dropped["same company twice"] += 1
            continue
        years_named = [int(y) for y in re.findall(r"(?<!\d)(20[12]\d)(?!\d)", f"{c.title} {r['url']}")]
        if (c.programme_year and c.programme_year < first_year) or (years_named and max(years_named) < first_year and not c.programme_year):
            dropped[f"from before {first_year}"] += 1
            continue
        if c.type not in types:  # only the kinds of programme the student asked for
            dropped["not a wanted type"] += 1
            continue
        if f"web|{_norm(c.company)}|{c.type}" in known:
            dropped["already in your opportunities"] += 1
            continue
        start, deadline = _parse_dt(c.date), _parse_dt(c.deadline)
        if (deadline and deadline < now) or (start and start < now):
            dropped["already closed or started"] += 1
            continue
        wanted_genders = {_gender_key(g) for g in c.gender_requirement} - {""}
        if "women" in c.restricted_to:
            wanted_genders.add("female")
        if "men" in c.restricted_to:
            wanted_genders.add("male")
        if wanted_genders and user_gender not in wanted_genders:  # also hides them when no gender is set on the profile
            dropped["restricted to a gender you are not"] += 1
            continue
        country = _country_key(c.country)
        if not c.online and country not in _ANYWHERE and country != my_country:
            dropped[f"outside {student_country}"] += 1
            continue
        if not c.online and my_country == "uk" and country in _ANYWHERE and not re.search(r"\b(uk|london|united kingdom|england|scotland|wales|manchester|edinburgh)\b", str(c.location), re.I) \
                and _abroad(c.location):
            dropped[f"outside {student_country}"] += 1  # the country field was vague, the location names a place abroad
            continue
        if "specific_universities" in c.restricted_to and c.universities_allowed and my_uni and \
                not any(_norm(u) and (_norm(u) in my_uni or my_uni in _norm(u)) for u in c.universities_allowed):
            dropped["limited to other universities"] += 1
            continue
        if c.graduation_year_requirement and grad.isdigit() and int(grad) not in c.graduation_year_requirement:
            dropped["other graduation year"] += 1
            continue
        if c.open_to and not (set(c.open_to) & stage_ok):  # e.g. a PhD or penultimate-year scheme for a first year
            dropped["not open to your year of study"] += 1
            continue
        seen.add(key)
        assumed = deadline is None
        if assumed:
            deadline = now + timedelta(days=NO_DEADLINE_DAYS_WEB)
        title = c.title if _norm(c.company) in _norm(c.title) else f"{c.company} {c.title}"
        entries.append({
            "title": title,
            "company": c.company,
            "url": r["url"],
            "type": c.type,
            "date": start.isoformat() if start else None,
            "deadline": deadline.isoformat(),
            "deadline_assumed": assumed,
            "location": c.location,
            "online": c.online,
            "gender_requirement": c.gender_requirement,
            "graduation_year_requirement": c.graduation_year_requirement,
            "open_to": c.open_to,
            "restricted_to": c.restricted_to,
            "country": c.country,
            "field": c.field,
            "programme_year": c.programme_year,
            "_snippet": r.get("content", ""),
            "source": "web",
        })
    lap(f"{len(entries)} distinct open programmes" + (f"  (dropped: {dict(dropped)})" if dropped else ""))

    # 5. Narrow down to what fits the student and their search
    scores = await _rank(entries, profile_text, seconds=min(22, left(8)))
    for n, e in enumerate(entries):
        s = scores.get(n)
        e["score"], e["why"] = (s.score, s.reason) if s else (3, "")
    for n, e in enumerate(entries):
        e["match"] = scores[n].match if n in scores else "partial"  # the scorer ran out of time: it can only fill a gap, never push out a strong match
    for e in entries:
        if e["match"] == "none" or e["score"] < MIN_SCORE:
            print(f"[search] dropped, not relevant to your interests or search ({e['match']}, {e['score']}/10): {e['company']} - {e.get('field') or e['title'][:50]}", file=sys.stderr)
    entries = [e for e in entries if e["match"] != "none" and e["score"] >= MIN_SCORE]
    entries.sort(key=lambda e: (e["match"] != "strong", -e["score"], e["deadline"]))  # strong matches first
    kept_entries = _keep_relevant(entries)
    reserve = [e for e in entries if e not in kept_entries and e["score"] >= LOOSE_MIN_SCORE]  # only used if too few survive the checks
    entries = kept_entries

    # 6. Read each programme's own page: is the link alive, and have applications actually opened?
    pool = entries[: max_results * 4]  # spares, since closed, not-yet-open and dead ones are dropped here
    await _check_open(pool, now, min(24, max(left(6), 2)))
    usable, upcoming = [], []
    for e in pool:
        status = e.get("open_status", "unknown")
        if e.get("alive") is False:
            print(f"[search] dropped, link not working: {e['company']} {e['url']}", file=sys.stderr)
        elif e.get("for_year") and e["for_year"] < first_year:
            print(f"[search] dropped, the page is for {e['for_year']}: {e['company']} {e['url']}", file=sys.stderr)
        elif status == "not_yet_open":
            upcoming.append(e)  # not open yet: the agent watches it and books the application when it opens
        elif status == "closed":
            print(f"[search] dropped, applications closed: {e['company']} ({e.get('open_evidence', '')})", file=sys.stderr)
        else:
            usable.append(e)
    if len(usable) < MIN_WANTED and reserve and left(4) > 4:  # too few: check the looser matches too and add the ones that are open
        extra = [e for e in reserve if e not in pool][: MIN_WANTED * 3]
        await _check_open(extra, now, min(14, max(left(3), 2)))
        for e in extra:
            if e.get("alive") is False or e.get("open_status") == "closed" or (e.get("for_year") and e["for_year"] < first_year):
                continue
            if e.get("open_status") == "not_yet_open":
                upcoming.append(e)
            else:
                e["loose"] = True
                usable.append(e)
        lap(f"only a few close matches, so {len([e for e in usable if e.get('loose')])} looser ones were added")
    # confirmed-open programmes first, then the ones we could not confirm
    usable.sort(key=lambda e: (e["match"] != "strong", e.get("open_status") != "open", -e["score"], e["deadline"]))
    kept = usable[:max_results]
    for e in kept:
        notes = []
        if e.get("open_status") == "open":
            notes.append("Applications are open now.")
        else:
            notes.append("Could not confirm applications are open, so check the page.")
        if e.get("restricted_to"):
            groups = [GROUP_WORDS[g] for g in e["restricted_to"] if g in GROUP_WORDS]
            if groups:
                notes.append("Check you qualify: for " + ", ".join(groups) + ".")
        if e.get("loose"):
            notes.append("A looser match for your interests.")
        e["note"] = " ".join(notes)
        e.pop("_snippet", None)
        print(f"[search] returned ({e['match']}, {e['score']}/10, {e.get('open_status', 'unknown')}): {e['company']} - {e['title'][:50]} [{e.get('field', '')}]", file=sys.stderr)
    for e in upcoming[:MAX_WATCH]:
        opens = (e.get("opens_on") or "")[:10]
        e.update(watch=True, deadline=None, deadline_assumed=False,
                 note=(f"Applications open on {opens}. " if opens else "Applications are not open yet. ") + "The agent will watch the page and book your application when it opens.")
        e.pop("_snippet", None)
        kept.append(e)
        print(f"[search] watching ({e['match']}, {e['score']}/10): {e['company']} - {e['title'][:50]} opens {opens or '?'}", file=sys.stderr)
    lap(f"{len(kept)} returned (after relevance, eligibility and open-for-applications checks)")
    if not kept:
        if not results and not from_employers:
            return [{"error": "The web search returned nothing. Check your TAVILY_API_KEY and try again."}]
        return [{"error": "No open programmes matched your interests and search. Try broader interests on your profile, or a broader search."}]
    return kept


# ---------------------------------------------------------------------------
# Calendar scheduling: the slot finder, the application plan and stage changes
# ---------------------------------------------------------------------------
def _utc(v):
    if isinstance(v, str):
        v = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(timezone.utc)


LONDON = ZoneInfo("Europe/London")


def _london(v, end_of_day: bool = False):
    """Parses a time typed by the student. No offset means UK time; a bare date means 23:00 that day if end_of_day."""
    if isinstance(v, str):
        v = v.strip()
        bare = len(v) == 10
        v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if bare and end_of_day:
            v = v.replace(hour=23)
    if v.tzinfo is None:
        v = v.replace(tzinfo=LONDON)
    return v.astimezone(timezone.utc)


DAY_START_H, DAY_END_H = 8, 22  # blocks sit between 08:00 and 22:00 UK time
BUFFER = timedelta(minutes=15)  # breathing space kept around every busy event
IMPORTANT_GUARD = timedelta(hours=24)  # no block in the 24 hours before an exam, test, interview...
MIN_BEFORE_DEADLINE = timedelta(hours=1)
MAX_PER_DAY = 2  # at most this many new blocks on one day
IMPORTANT_WORDS = re.compile(
    r"\b(exams?|tests?|quiz|viva|interviews?|assessments?|midterms?|finals?|presentation|oral|deadline|submission|"
    r"coursework|hand[- ]?in|dissertation|thesis)\b",
    re.I,
)

# What each block in a company's plan is called in the calendar
LABELS = {
    "apply": "Apply", "practice": "Practice", "oa": "OA", "test": "Test",
    "interview_prep": "Interview prep", "interview": "Interview",
    "ac_prep": "AC prep", "assessment_centre": "Assessment centre", "chase": "Check",
}
OWN_PREFIXES = tuple(f"{label}: " for label in LABELS.values())  # titles of the blocks this app creates
FIXED_KINDS = {"interview", "assessment_centre"}  # booked at the time the employer set, never moved
STAGES = ["to_apply", "applied", "oa", "interview", "assessment_centre", "offer", "rejected", "withdrawn"]
PRACTICE_MINUTES = 45
PRACTICE_COUNT = 2
PRACTICE_WINDOW_DAYS = 10  # practice before an OA that has not arrived yet sits in the next ten days


def _company_key(text: str) -> str:
    """Same idea as company_norm in db.py: 'Goldman Sachs International' and 'Goldman Sachs' match."""
    text = re.sub(r"\b20\d\d\b|\(.*?\)", " ", (text or "").lower())
    text = re.sub(r"\b(plc|ltd|limited|llp|inc|group|the|uk|international|&|and)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", "", text)


MAX_CALENDARS = 20


def _calendar_ids(service) -> list[str]:
    """Every calendar the student has switched on, not just the main one: a timetable, a society or a work calendar
    each shows up in its own colour in Google Calendar, and each can hold events that must not be double-booked."""
    ids: list[str] = []
    try:
        items = service.calendarList().list().execute().get("items", [])
    except Exception:
        items = []
    primary_seen = False
    for c in items:
        if c.get("selected") is False or c.get("deleted"):
            continue
        primary_seen = primary_seen or bool(c.get("primary"))
        ids.append("primary" if c.get("primary") else c["id"])
    if not primary_seen:
        ids.insert(0, CALENDAR_ID)
    return ids[:MAX_CALENDARS] or [CALENDAR_ID]


def _all_events(service, start, end) -> list[dict]:
    """The events of every calendar between two times, recurring ones expanded, oldest first."""
    found: dict[str, dict] = {}
    for cid in _calendar_ids(service):
        try:
            page = None
            while True:
                resp = service.events().list(calendarId=cid, timeMin=start.isoformat(), timeMax=end.isoformat(),
                                             singleEvents=True, orderBy='startTime', maxResults=250, pageToken=page).execute()
                for e in resp.get("items", []):
                    found.setdefault(f"{cid}|{e.get('id')}", e)
                page = resp.get("nextPageToken")
                if not page or len(found) > 1500:
                    break
        except Exception as err:  # one unreadable calendar must not stop the others
            print(f"[calendar] could not read calendar {cid}: {err}", file=sys.stderr)
    return sorted(found.values(), key=lambda e: (e.get("start", {}).get("dateTime") or e.get("start", {}).get("date") or ""))


def _is_free(e: dict) -> bool:
    """Only an all-day event that is not marked busy leaves the time free (a birthday, a holiday, a note).
    A timed event always blocks the time, whatever its colour, calendar or free/busy setting."""
    return "dateTime" not in e.get("start", {}) and e.get("transparency") == "transparent"


def _slot_problem(start, end, *, now, deadline, busy, important, owner, per_day,
                  earliest=None, company_days=None, fixed=False) -> str | None:
    """Why a proposed block is not allowed, or None if it is fine. The rules live here, not just in the prompt."""
    ls, le = start.astimezone(LONDON), end.astimezone(LONDON)
    if start < now:
        return "starts in the past"
    if earliest is not None and start < earliest:
        return f"starts before {earliest.isoformat()}, the earliest this block may start"
    if start > now + timedelta(days=30):
        return "is more than 30 days away"
    if end > deadline - MIN_BEFORE_DEADLINE:
        return f"ends less than an hour before the deadline {deadline.isoformat()}"
    if ls.date() != le.date() or ls.hour < DAY_START_H or (le.hour, le.minute) > (DAY_END_H, 0):
        return f"is outside {DAY_START_H:02d}:00 to {DAY_END_H:02d}:00 UK time"
    for b0, b1 in busy:
        if start < b1 + BUFFER and end > b0 - BUFFER:
            return f"clashes with, or is right next to, a busy event ({b0.isoformat()} to {b1.isoformat()}); keep 15 minutes free around events"
    for s0, e0, own, title in important:
        if own != owner and start < e0 and end > s0 - IMPORTANT_GUARD:
            return f"is within 24 hours before (or during) '{title}' at {s0.isoformat()}"
    if per_day[ls.date()] >= MAX_PER_DAY:
        return f"would be block number {MAX_PER_DAY + 1} on {ls.date()}; use a different day"
    if company_days is not None and company_days[(owner, ls.date())] >= 1:
        return f"is on a day that already has a block for the same company ({ls.date()}); use a different day"
    return None


def _kind_of(item: dict) -> str:
    return item.get("block_kind") or ("test" if item.get("source") == "gmail" else "apply")


def _summary_of(item: dict) -> str:
    return item.get("summary") or f"{LABELS.get(_kind_of(item), 'Apply')}: {item.get('title')}"


async def _schedule_blocks(items: list[dict]) -> list[dict]:
    """Finds a slot for every block (see _find_slots), then puts all the new events into Google Calendar in one batch request."""
    queued: list = []
    results = await _find_slots(items, queued)
    await _flush_inserts(queued, results)
    return results


async def _flush_inserts(queued: list, results: list[dict]) -> None:
    if not queued:
        return
    service = queued[0][2]
    by_index = {r["index"]: r for r in results}

    def fail(i, why):
        by_index[i].update(status="failed", reason=f"Google Calendar did not accept it: {why}", event_id=None, link=None)

    def done(i, event):
        by_index[i].update(event_id=event.get("id"), link=event.get("htmlLink"))

    def run():
        if hasattr(service, "new_batch_http_request"):  # one round trip for up to 50 events, instead of one each
            for n in range(0, len(queued), 50):
                batch = service.new_batch_http_request()
                for i, body, _ in queued[n:n + 50]:
                    batch.add(service.events().insert(calendarId=CALENDAR_ID, body=body),
                              callback=lambda _rid, resp, exc, i=i: fail(i, exc) if exc else done(i, resp))
                batch.execute()
        else:
            for i, body, _ in queued:
                try:
                    done(i, service.events().insert(calendarId=CALENDAR_ID, body=body).execute())
                except Exception as exc:
                    fail(i, exc)

    await asyncio.get_running_loop().run_in_executor(None, run)


async def _find_slots(items: list[dict], queued: list) -> list[dict]:
    """The slot finder. Every item is one block: the model proposes a slot, the code enforces the rules.
    Item fields: title, deadline (the block must end an hour before it), block_kind, company, summary, earliest,
    exact_minutes, fixed_start (booked as is), colour_id, url, details, application_id (handed back untouched).
    Returns one dict per item: status booked / already / failed / skipped, plus start, end, event_id, link."""
    now = datetime.now(timezone.utc)
    out: dict[int, dict] = {}

    def result(i, status, **extra):
        it = items[i]
        out[i] = {"index": i, "status": status, "kind": _kind_of(it), "company": it.get("company") or it.get("title"),
                  "title": _summary_of(it), "application_id": it.get("application_id"), "ref": it.get("ref"), **extra}

    todo = []  # (index, deadline)
    for i, it in enumerate(items):
        raw = it.get("fixed_start") or it.get("deadline")
        if not raw:
            result(i, "skipped", reason="no date")
        elif it.get("type") == "application_update":
            result(i, "skipped", reason="status email, nothing to schedule")
        else:
            try:
                d = _utc(raw)
            except Exception:
                result(i, "skipped", reason=f"'{raw}' is not a valid date")
                continue
            if d <= now:
                result(i, "skipped", reason="date has already passed")
            else:
                todo.append((i, d))
    if not todo:
        return [out[i] for i in sorted(out)]

    service = get_calendar_service()
    horizon = min(max(d for _, d in todo), now + timedelta(days=30)) + timedelta(days=2)
    existing = _all_events(service, now, horizon)

    def _range(e):
        s, en = e['start'], e['end']
        return _utc(s.get('dateTime') or s.get('date')), _utc(en.get('dateTime') or en.get('date'))

    busy, important, already = [], [], {}
    per_day = Counter()
    company_days = Counter()
    for e in existing:
        if e.get("status") == "cancelled":
            continue
        me = next((a for a in e.get("attendees", []) if a.get("self")), None)
        if me and me.get("responseStatus") == "declined":
            continue
        start_e, end_e = _range(e)
        title = e.get("summary", "")
        props = (e.get("extendedProperties") or {}).get("private") or {}
        ours = title.startswith(OWN_PREFIXES) or props.get("opportune") == "1"
        if ours:
            per_day[start_e.astimezone(LONDON).date()] += 1
            owner = props.get("company") or ""
            if props.get("kind") not in FIXED_KINDS:  # prep may sit on the same day as the interview itself
                company_days[(owner, start_e.astimezone(LONDON).date())] += 1
            already[title.lower()] = (start_e, e.get("htmlLink"), e.get("id"))
            if props.get("kind") in FIXED_KINDS:
                important.append((start_e, end_e, owner, title))
        is_important = (not ours) and bool(IMPORTANT_WORDS.search(title))
        if is_important:
            important.append((start_e, end_e, None, title))
        if _is_free(e) and not is_important:
            continue  # only an all-day note that is not marked busy leaves the time free
        busy.append((start_e, end_e))

    owner_of = lambda i: _company_key(items[i].get("company") or items[i].get("title") or "") or f"item{i}"
    deadlines = dict(todo)
    pending: set[int] = set()

    # Fixed-time blocks (an interview or assessment centre the employer set): importance for every OTHER company's blocks
    for i, d in todo:
        it = items[i]
        if _kind_of(it) in FIXED_KINDS and it.get("fixed_start"):
            stop = d + timedelta(minutes=it.get("exact_minutes") or 60)
            important.append((d, stop, owner_of(i), _summary_of(it)))
            busy.append((d, stop))

    local = lambda dt: dt.astimezone(LONDON).isoformat()

    def book_event(i, start, end, reason):
        it = items[i]
        kind = _kind_of(it)
        body = {
            'summary': _summary_of(it),
            'description': (f"{it['details']}\n\nOpen the practice: {it['resource_url']}" if it.get("resource_url")
                            else f"URL/link: {it.get('url') or it.get('link') or 'N/A'}\n{reason}"),
            'location': it.get('location', 'Online'),
            'start': {'dateTime': start.astimezone(LONDON).isoformat(), 'timeZone': 'Europe/London'},
            'end': {'dateTime': end.astimezone(LONDON).isoformat(), 'timeZone': 'Europe/London'},
            'extendedProperties': {"private": {"opportune": "1", "kind": kind, "company": owner_of(i),
                                               "company_name": str(it.get("company") or it.get("title") or "")[:100]}},
        }
        if it.get("colour_id"):
            body["colorId"] = str(it["colour_id"])
        if it.get("resource_url"):  # Google Calendar shows this as a link on the event
            body["source"] = {"title": it.get("resource_label") or "Practice", "url": it["resource_url"]}
        queued.append((i, body, service))  # inserted together at the end, in one batch
        busy.append((start, end))
        per_day[start.astimezone(LONDON).date()] += 1
        if kind == "apply":
            apply_end[owner_of(i)] = end
        if kind not in FIXED_KINDS:
            company_days[(owner_of(i), start.astimezone(LONDON).date())] += 1
        pending.discard(i)
        result(i, "booked", start=start.isoformat(), end=end.isoformat(), event_id=None,
               link=None, reason=reason, colour_id=it.get("colour_id"),
               resource_url=it.get("resource_url"), resource_label=it.get("resource_label"))

    # Blocks already in the calendar from an earlier run are not booked twice; fixed-time blocks go in as they are
    for i, d in todo:
        key = _summary_of(items[i]).lower()
        if key in already and not items[i].get("force_new"):  # force_new: book a new one even though the old event still stands
            when, link, event_id = already[key]
            result(i, "already", start=when.isoformat(), event_id=event_id, link=link, reason="already in your calendar")
        elif _kind_of(items[i]) in FIXED_KINDS and items[i].get("fixed_start"):
            stop = d + timedelta(minutes=items[i].get("exact_minutes") or 60)
            book_event(i, d, stop, "the time the employer set")
        else:
            pending.add(i)
    # Apply comes first: practice that follows an Apply block is never booked unless that Apply block is (or already was) booked
    apply_of = {owner_of(i): i for i in range(len(items)) if _kind_of(items[i]) == "apply"}

    def blocked(i):
        j = apply_of.get(owner_of(i))
        return bool(items[i].get("after_apply")) and j is not None and out.get(j, {}).get("status") not in ("booked", "already")

    for i in sorted(pending):
        j = apply_of.get(owner_of(i))
        if blocked(i) and j in out:  # the Apply block was already ruled out (no date, or the deadline has passed)
            pending.discard(i)
            result(i, "skipped", reason="the Apply block could not be scheduled, so practice waits")
    if not pending:
        return [out[i] for i in sorted(out)]

    apply_end: dict[str, datetime] = {}  # company -> when its Apply block (booked in this batch) ends

    def earliest_of(i):
        e = items[i].get("earliest")
        found = _utc(e) if e else None
        if items[i].get("after_apply") and owner_of(i) in apply_end:
            found = max(found, apply_end[owner_of(i)]) if found else apply_end[owner_of(i)]
        return found

    order = lambda i: (0 if _kind_of(items[i]) == "apply" else 1, deadlines[i])  # applications first, then the rest by deadline

    def problem(i, start, end):
        return _slot_problem(start, end, now=now, deadline=deadlines[i], busy=busy, important=important,
                             owner=owner_of(i), per_day=per_day, earliest=earliest_of(i), company_days=company_days)

    def hours_for(i, wanted=1.5):
        exact = items[i].get("exact_minutes")
        if exact:
            return exact / 60
        minimum = max(0.5, (items[i].get("duration_minutes") or 0) / 60)
        return min(max(wanted, minimum), max(3, minimum))

    system_prompt = f"""
    You schedule time blocks in a student's Google Calendar for their internship and spring week applications.
    NOW: {local(now)} (UK time). All times below are UK local time with their UTC offset; return start_time the same way.
    Each block in 'blocks' has a kind:
    - apply: time to fill in the application form before its deadline.
    - practice: practice for an online assessment that has not arrived yet (numerical, verbal, situational judgement). It has no real deadline; spread the sessions over different days soon. A practice block with after_apply must come AFTER the same company's apply block: apply first, practice after.
    - oa: time to sit a real online assessment, before its deadline. If it has minutes, give it at least that long.
    - chase: a 15-minute reminder to check the inbox and spam folder for an update from that company. Any free slot soon will do.
    - interview_prep, ac_prep: preparation before an interview or assessment centre ('deadline' is that event itself). A prep block may sit right before it.
    Schedule the blocks around each other and around 'existing_events'.
    HARD RULES (every one is checked in code, and a slot that breaks one is rejected):
    1. Start and finish between {DAY_START_H:02d}:00 and {DAY_END_H:02d}:00 UK time, on the same day.
    2. No overlap with any existing event or your other blocks, and keep 15 minutes free before and after every event.
    3. Never within 24 hours BEFORE an exam, test, viva, interview or similar: see 'important_events', where
       'no_blocks_between' is the forbidden window. (A prep block for that same company's interview may go right before it.)
    4. Start after now and finish at least 1 hour before that block's deadline; only within the next 30 days. If a block has 'earliest', start no sooner than that.
    5. 0.5 to 3 hours long, unless it has 'minutes': then use exactly that many.
    6. At most {MAX_PER_DAY} blocks on any one day, and at most ONE block per company on any one day.
    PREFERENCES: spread blocks over different days; avoid packed days; book apply blocks as early as the rules allow, because many
    programmes fill on a rolling basis and applying early helps; then practice blocks; book as close to today as the rules allow.
    Return one suggestion per block, using its 'index'.
    """
    user_prompt = json.dumps({
        "blocks": [{"index": i, "kind": _kind_of(items[i]), "company": items[i].get("company"), "title": items[i].get("title"),
                    "deadline": local(deadlines[i]), "earliest": items[i].get("earliest") and local(earliest_of(i)),
                    "after_apply": bool(items[i].get("after_apply")),
                    "minutes": items[i].get("exact_minutes") or items[i].get("duration_minutes"),
                    "details": items[i].get("details") or items[i].get("snippet")} for i in sorted(pending)],
        "existing_events": [{"title": e.get('summary', 'Busy'), "start": local(_range(e)[0]), "end": local(_range(e)[1])}
                            for e in existing if e.get("status") != "cancelled"],
        "important_events": [{"title": t, "starts": local(s0), "no_blocks_between": [local(s0 - IMPORTANT_GUARD), local(e0)]}
                             for s0, e0, _, t in important],
    }, indent=2)

    # Eval loop: the model proposes, the code checks every rule, and what failed goes back to the model with the reason.
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]
    max_attempts = SCHEDULER_ATTEMPTS
    for attempt in range(1, max_attempts + 1):
        if not pending:
            break
        try:  # a slow or failing model must not hold up the booking: the code finds the slots below
            parsed = await asyncio.wait_for(ask(messages, ScheduleSuggestions, model=SCHEDULER_MODEL), SCHEDULER_TIMEOUT)
        except Exception as err:
            print(f"[schedule] model attempt {attempt} skipped: {err}", file=sys.stderr)
            break
        messages.append({"role": "assistant", "content": parsed.model_dump_json()})
        by_index = {s.opportunity_index: s for s in parsed.suggestions}

        failed = []
        for i in sorted(pending, key=order):
            title, s = _summary_of(items[i]), by_index.get(i)
            if blocked(i):
                failed.append(f"index {i} '{title}': its Apply block is not booked yet, so this waits")
                continue
            if s is None:
                failed.append(f"index {i} '{title}': no slot was suggested")
                continue
            try:
                start = _utc(s.start_time)
                end = start + timedelta(hours=hours_for(i, s.duration_hours))
            except Exception:
                failed.append(f"index {i} '{title}': start_time '{s.start_time}' is not a valid ISO 8601 time")
                continue
            why = problem(i, start, end)
            if why:
                failed.append(f"index {i} '{title}': slot {s.start_time} rejected, it {why}")
                continue
            book_event(i, start, end, s.reason)

        if pending and attempt < max_attempts:
            messages.append({"role": "user", "content": (
                "error with scheduling try again to find slot. These blocks could not be booked: "
                + "; ".join(failed)
                + ". Return suggestions ONLY for these indexes. The other blocks you suggested are now booked, "
                  "so avoid overlapping them too and respect every hard rule."
            )})

    # Last resort, in code. A block is never left unbooked just because the calendar is busy: the soft limits give way one at a time,
    # and the hard rules (no clashes, 15 minutes around events, 24 hours before an exam or interview, 08:00 to 22:00, within 30 days) never do.
    #   tier 0: all rules      tier 1: lift the two-blocks-a-day limit      tier 2: practice may go beyond its ten-day window
    #   tier 3: allow a second block for the same company on one day       tier 4: shorten the block to 30 minutes
    def tier_problem(i, start, end, tier):
        deadline, per, company = deadlines[i], per_day, company_days
        if tier >= 1:
            per = Counter()
        if tier >= 2 and _kind_of(items[i]) not in ("apply",) + tuple(FIXED_KINDS):
            deadline = max(deadline, now + timedelta(days=30))
        if tier >= 3:
            company = None
        return _slot_problem(start, end, now=now, deadline=deadline, busy=busy, important=important, owner=owner_of(i),
                             per_day=per, earliest=earliest_of(i), company_days=company)

    TIER_NOTE = ["earliest free slot that keeps clear of your other events",
                 "earliest free slot (your calendar was busy, so this is on a day that already has two blocks)",
                 "earliest free slot (your calendar was busy, so this is later than usual)",
                 "earliest free slot (your calendar was busy, so this shares a day with another block for the same company)",
                 "earliest free slot (your calendar was busy, so this one is a short 30-minute block)"]
    for i in sorted(pending, key=order):
        if blocked(i):
            continue
        found = None
        for tier in range(5):
            length = timedelta(hours=hours_for(i, 1.0))
            if tier >= 4:
                length = min(length, timedelta(minutes=30))
            first_day = now.astimezone(LONDON).date()
            for offset in range(31):
                day = first_day + timedelta(days=offset)
                for minutes in range(DAY_START_H * 60, DAY_END_H * 60 + 1, 30):
                    start = datetime.combine(day, time(0, 0), tzinfo=LONDON).astimezone(timezone.utc) + timedelta(minutes=minutes)
                    if not tier_problem(i, start, start + length, tier):
                        found = (start, start + length, tier)
                        break
                if found:
                    break
            if found:
                break
        if found:
            book_event(i, found[0], found[1], TIER_NOTE[found[2]])

    for i in sorted(pending):
        if blocked(i):
            result(i, "skipped", reason="no slot was found for the Apply block, so practice was not booked")
        else:
            result(i, "failed", reason="your calendar has no free time in the next 30 days that keeps clear of your events")
    return [out[i] for i in sorted(out)]


@mcp.tool()
async def schedule_application_with_llm(items: list[dict]) -> list[dict]:
    """Finds and books calendar slots for blocks of work: applications, practice, online assessments, interview prep.
    The model proposes slots; the code enforces the rules (no clashes, nothing in the 24h before an important event).
    Each item needs a title and a deadline; it may also carry block_kind, summary, company, earliest, exact_minutes,
    fixed_start, colour_id, application_id. Returns one dict per item: status booked / already / failed / skipped,
    with start, end, event_id and link. Used to book a block again when the student did not get to it."""
    return await _schedule_blocks(items)


# ---------------------------------------------------------------------------
# The application plan: one company, many blocks
# ---------------------------------------------------------------------------
# Practice for an online assessment links to the matching SimplyTK drill (the pages listed on https://simplytk.com/oa-prep).
SIMPLYTK = "https://simplytk.com"
PRACTICE_TESTS = {
    "numerical": {"label": "Numerical reasoning", "url": f"{SIMPLYTK}/numerical-reasoning",
                  "about": "SHL Verify, Cappfinity and Talent Q style: 12 table questions in 18 minutes"},
    "data_statements": {"label": "Data statements (Aon-style numerical)", "url": f"{SIMPLYTK}/data-statements",
                        "about": "six tabs of company data, true / false / cannot say, 12 minutes"},
    "verbal": {"label": "Verbal statements", "url": f"{SIMPLYTK}/verbal-statements",
               "about": "Aon-style verbal: six tabs of text, true / false / cannot say"},
    "situational": {"label": "Situational strengths (behavioural)", "url": f"{SIMPLYTK}/situational-strengths",
                    "about": "the behavioural, judgement-style questions"},
    "inductive": {"label": "Figure series (inductive reasoning)", "url": f"{SIMPLYTK}/figure-series",
                  "about": "four figures in a row, pick the fifth: 15 items in 18 minutes"},
    "logical": {"label": "Logical statements", "url": f"{SIMPLYTK}/logical-statements",
                "about": "deductive logic under time pressure"},
}
# Firms SimplyTK says send each test, from the drill pages. Checked first, so the practice matches the firm's real test.
FIRM_TESTS = [
    (r"nomura|morgan stanley|bnp", ["data_statements", "situational"]),
    (r"barclays|hsbc|lloyds|citi|\bubs\b|deutsche|jefferies", ["numerical", "situational"]),
]
# Otherwise pick from the field, title and name. First match wins; the order is the order of the two practice sessions.
TEST_MIX = [
    (r"software|engineer|developer|technology|tech\b|data|machine learning|\bai\b|computer|cyber|devops", ["inductive", "numerical"]),
    (r"quant|trading|trader|markets|sales and trading|hedge|asset management|investment|banking|bank\b|finance|financial|insurance|private equity|accounting|audit|actuar", ["numerical", "situational"]),
    (r"consult|strategy|advisory", ["numerical", "situational"]),
    (r"law|legal|solicitor|barrister", ["verbal", "situational"]),
]
DEFAULT_MIX = ["numerical", "situational"]


def _tests_for(item: dict) -> list[str]:
    name = str(item.get("company") or "").lower()
    for pattern, mix in FIRM_TESTS:
        if re.search(pattern, name):
            return mix
    text = " ".join(str(item.get(k) or "") for k in ("field", "title", "company")).lower()
    for pattern, mix in TEST_MIX:
        if re.search(pattern, text):
            return mix
    return DEFAULT_MIX


def _practice_blocks(company: str, base: dict, count: int, start_number: int = 1, total: int | None = None,
                     tests: list[str] | None = None) -> list[dict]:
    now = datetime.now(timezone.utc)
    total = total or count
    tests = tests or DEFAULT_MIX
    blocks = []
    for n in range(count):
        number = start_number + n
        test = PRACTICE_TESTS[tests[(number - 1) % len(tests)]]
        blocks.append({
            **base, "block_kind": "practice", "company": company, "title": f"{company}: {test['label']} practice",
            "summary": f"Practice: {company} · {test['label']} ({number}/{total})", "exact_minutes": PRACTICE_MINUTES,
            "deadline": (now + timedelta(days=PRACTICE_WINDOW_DAYS)).isoformat(),
            "after_apply": True,  # practice starts only once the same company's Apply block is done
            "resource_url": test["url"], "resource_label": f"{test['label']} on SimplyTK",
            "details": f"Practice for {company}'s online assessment while it has not arrived.\n{test['label']} ({test['about']}):\n{test['url']}",
        })
    return blocks


def _application_blocks(it: dict) -> list[dict]:
    """The first blocks of one application: an Apply block before the deadline, then the practice sessions."""
    company = it.get("company") or it.get("title") or "Company"
    base = {"company": company, "url": it.get("url") or it.get("link"), "colour_id": it.get("colour_id"),
            "application_id": it.get("application_id"), "ref": it.get("ref")}
    return [{**base, "block_kind": "apply", "title": it.get("title") or company, "deadline": it.get("deadline"),
             "summary": f"Apply: {company}"}] + _practice_blocks(company, base, PRACTICE_COUNT, tests=_tests_for(it))


@mcp.tool()
async def plan_application(items: list[dict]) -> list[dict]:
    """Plans each company's first blocks and books them: an Apply block before the deadline, and, because no online
    assessment has arrived yet, two 45-minute practice sessions. Each item is an opportunity (company, title, type, url,
    deadline) plus its application_id and colour_id. Returns one dict per block with status, start, end, event_id, link."""
    blocks = []
    for it in items:
        blocks += _application_blocks(it)
    return await _schedule_blocks(blocks)


# ---------------------------------------------------------------------------
# Stage changes
# ---------------------------------------------------------------------------
class EmailFacts(BaseModel):
    is_about_this_company: bool = Field(description="False if the screenshot is clearly from a different company or is not a recruitment email")
    kind: Literal["oa", "interview", "assessment_centre", "offer", "rejection", "other"] = Field(
        description="oa = online assessment, test, game or video interview to complete in your own time; interview = a live interview; assessment_centre = a day of assessments or an in-person/virtual assessment centre")
    deadline: str | None = Field(default=None, description="ISO 8601 with UK offset: the last moment to complete an online assessment, or to accept or book a slot. Null if not stated")
    start_time: str | None = Field(default=None, description="ISO 8601 with UK offset: when a live interview or assessment centre starts. Null if not yet booked or not stated")
    duration_minutes: int | None = Field(default=None, description="How long the test, interview or assessment centre takes, if stated")
    summary: str = Field(description="One short sentence: what the email asks the student to do")


async def _read_screenshot(path: str, company: str, title: str) -> EmailFacts:
    root = os.environ.get("SHOT_ROOT")
    p = Path(path).resolve()
    if root and Path(root).resolve() not in p.parents:
        raise ValueError("That screenshot is outside the allowed folder")
    media = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}.get(p.suffix.lower())
    if not media or not p.is_file():
        raise ValueError("The screenshot must be a PNG, JPG, WEBP or GIF image")
    data = p.read_bytes()
    if len(data) > 5 * 1024 * 1024:
        raise ValueError("The screenshot must be under 5 MB")
    now = datetime.now(LONDON)
    text = (f"This is a screenshot of an email a student received about their application to {company} ({title}). "
            f"Today is {now:%A %d %B %Y}, UK time. Read it and fill in the answer. Use UK time for every time. "
            "If a date has no year, use the next one that has not passed.")
    content = [{"type": "image", "source": {"type": "base64", "media_type": media, "data": base64.b64encode(data).decode()}},
               {"type": "text", "text": text}]
    return await ask([{"role": "user", "content": content}], EmailFacts, model=MODEL, max_tokens=1024)


def _cancel_events(event_ids: list[str]) -> set[str]:
    """Deletes events from the calendar. A missing event counts as gone. Returns the ids that are now gone."""
    from googleapiclient.errors import HttpError
    service = get_calendar_service()
    gone = set()
    for event_id in event_ids:
        try:
            service.events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute()
            gone.add(event_id)
        except HttpError as e:
            if getattr(e.resp, "status", None) in (404, 410):
                gone.add(event_id)
    return gone


@mcp.tool()
async def update_stage(application: dict, new_stage: str, deadline: str | None = None, start_time: str | None = None,
                       duration_minutes: int | None = None, screenshot_path: str | None = None,
                       blocks: list[dict] | None = None) -> dict:
    """Moves one company to a new stage and re-plans its calendar blocks.
    application: {id, company, title, url, colour_id}. new_stage: applied, oa, interview, assessment_centre, offer,
    rejected or withdrawn. deadline / start_time / duration_minutes: what the student typed (UK time). screenshot_path:
    an image of the email, read to fill in what was not typed. blocks: the company's current blocks
    ({id, kind, status, start, event_id}). Future planned blocks that no longer fit are removed from the calendar and the new
    stage's blocks are booked. Returns {stage, cancelled: [block ids], blocks: [...], extracted, notes}."""
    if new_stage not in STAGES:
        return {"error": f"Unknown stage '{new_stage}'"}
    company = application.get("company") or application.get("title") or "Company"
    title = application.get("title") or company
    notes: list[str] = []
    now = datetime.now(timezone.utc)

    if new_stage == "oa":
        if not screenshot_path:
            return {"error": "Add a screenshot of the employer's email. I read the deadline from it, so there is nothing to type."}
        deadline = start_time = duration_minutes = None  # an online assessment takes its details from the email only

    facts = None
    if screenshot_path:
        try:
            facts = await _read_screenshot(screenshot_path, company, title)
        except Exception as e:
            return {"error": f"Could not read the screenshot: {e}"}
        if not facts.is_about_this_company:
            return {"error": f"That screenshot does not look like it is from {company}. Nothing was changed."}
        notes.append(facts.summary)
        wanted = {"oa": "oa", "interview": "interview", "assessment_centre": "assessment_centre"}
        if facts.kind in wanted and wanted[facts.kind] != new_stage and new_stage in wanted.values():
            notes.append(f"The email looks like a{'n' if facts.kind[0] in 'aeiou' else ''} {facts.kind.replace('_', ' ')}, but you chose {new_stage.replace('_', ' ')}. Used your choice.")

    when_deadline = deadline or (facts.deadline if facts else None)
    when_start = start_time or (facts.start_time if facts else None)
    minutes = duration_minutes or (facts.duration_minutes if facts else None)

    mine = blocks or []
    is_future = lambda b: b.get("status") == "planned" and b.get("start") and _utc(b["start"]) > now
    future = [b for b in mine if is_future(b)]
    kept_practice = [b for b in future if b.get("kind") == "practice"]

    if new_stage == "to_apply":
        drop = []
    elif new_stage == "applied":
        drop = [b for b in future if b.get("kind") == "apply"]
    else:
        drop = list(future)  # every later stage replaces whatever was still planned

    gone = _cancel_events([b["event_id"] for b in drop if b.get("event_id")])
    cancelled = [b["id"] for b in drop if not b.get("event_id") or b["event_id"] in gone]

    base = {"company": company, "url": application.get("url"), "colour_id": application.get("colour_id"),
            "application_id": application.get("id")}
    new_items: list[dict] = []
    assumed = False

    if new_stage == "applied":
        have = len([b for b in kept_practice if b["id"] not in cancelled])
        if have < PRACTICE_COUNT:
            new_items += _practice_blocks(company, base, PRACTICE_COUNT - have, start_number=have + 1, total=PRACTICE_COUNT,
                                          tests=_tests_for(application))
            notes.append("Waiting for an online assessment, so practice sessions are kept in your calendar.")
    elif new_stage == "oa":
        try:
            d = _london(when_deadline, end_of_day=True) if when_deadline else None
        except Exception:
            return {"error": f"'{when_deadline}' is not a valid deadline"}
        if d is None:
            d = now + timedelta(days=7)
            assumed = True
            notes.append("I could not find a deadline in that screenshot, so I assumed 7 days from now. Upload a clearer screenshot to fix it.")
        new_items.append({**base, "block_kind": "oa", "title": f"{company}: online assessment", "summary": f"OA: {company}",
                          "deadline": d.isoformat(), "exact_minutes": minutes or 90,
                          "details": (facts.summary if facts else "Online assessment")})
    elif new_stage in ("interview", "assessment_centre"):
        prep_kind = "interview_prep" if new_stage == "interview" else "ac_prep"
        label = LABELS[new_stage]
        try:
            s = _london(when_start) if when_start else None
        except Exception:
            return {"error": f"'{when_start}' is not a valid date and time"}
        if s is not None and s <= now:
            return {"error": "That interview time has already passed"}
        if s is not None:
            new_items.append({**base, "block_kind": new_stage, "title": f"{company}: {label.lower()}", "summary": f"{label}: {company}",
                              "fixed_start": s.isoformat(), "exact_minutes": minutes or (60 if new_stage == "interview" else 240),
                              "details": facts.summary if facts else label})
            target = s
        else:
            target = now + timedelta(days=5)
            assumed = True
            notes.append(f"No {label.lower()} time yet, so I booked preparation for the next few days. Add the time when you have it.")
        for n in range(2):
            new_items.append({**base, "block_kind": prep_kind, "title": f"{company}: {label.lower()} preparation",
                              "summary": f"{LABELS[prep_kind]}: {company} ({n + 1}/2)", "deadline": target.isoformat(),
                              "exact_minutes": 60, "details": f"Prepare for your {label.lower()} with {company}."})
    elif new_stage == "offer":
        notes.append("Congratulations! Remaining blocks for this company were removed.")

    results = await _schedule_blocks(new_items) if new_items else []
    return {
        "stage": new_stage,
        "cancelled": cancelled,
        "blocks": results,
        "extracted": facts.model_dump() if facts else None,
        "deadline_assumed": assumed,
        "notes": notes,
    }



# ---------------------------------------------------------------------------
# Autopilot tools: the agent calls these on its own, without anyone pressing a button
# ---------------------------------------------------------------------------
# Which stage an application must be in for a missed block of each kind to be worth booking again
RESCHEDULE_STAGES = {"apply": ("to_apply",), "practice": ("to_apply", "applied"), "oa": ("oa",),
                     "interview_prep": ("interview",), "ac_prep": ("assessment_centre",), "chase": ("applied",)}
CHASE_AFTER_DAYS = 6  # an application with no news for this long is chased
CHASE_REPEAT_DAYS = 4  # and again after this long if it is still quiet


def _when(v) -> datetime | None:
    """A stored time (ISO, or SQLite's 'YYYY-MM-DD HH:MM:SS' in UTC) as an aware datetime, or None."""
    if not v:
        return None
    try:
        return _utc(str(v).replace(" ", "T", 1) if len(str(v)) == 19 and " " in str(v) else str(v))
    except Exception:
        return None


def _rebook_item(b: dict, now: datetime) -> tuple[dict | None, str | None]:
    """The scheduling item to book a block again, or (None, why not). Fixed-time events are never moved."""
    kind = b.get("kind")
    if kind in FIXED_KINDS:
        return None, "fixed"
    if b.get("stage") not in RESCHEDULE_STAGES.get(kind, ()):
        return None, "not_needed"
    deadline = {"apply": _when(b.get("app_deadline")), "oa": _when(b.get("stage_deadline")),
                "practice": now + timedelta(days=PRACTICE_WINDOW_DAYS), "chase": now + timedelta(days=3),
                "interview_prep": _when(b.get("stage_start")), "ac_prep": _when(b.get("stage_start"))}.get(kind)
    if deadline is None or deadline <= now + timedelta(hours=2):
        return None, "deadline_passed"
    start, end = _when(b.get("start")), _when(b.get("end"))
    minutes = max(15, int((end - start).total_seconds() // 60)) if start and end else 60
    return {
        "block_kind": kind, "title": b.get("application_title") or b.get("company"), "summary": b.get("summary"),
        "company": b.get("company"), "deadline": deadline.isoformat(), "exact_minutes": minutes,
        "colour_id": b.get("colour_id"), "application_id": b.get("application_id"), "ref": {"block_id": b.get("id")},
        "resource_url": b.get("resource_url"), "resource_label": b.get("resource_label"),
        "details": "Rebooked: the first slot did not work out." + (f"\n{b['resource_url']}" if b.get("resource_url") else ""),
    }, None


def _slot_result(r: dict) -> dict:
    return {k: r.get(k) for k in ("status", "kind", "title", "start", "end", "event_id", "link", "reason", "colour_id", "resource_url", "resource_label", "application_id")}


@mcp.tool()
async def repair_clashes(blocks: list[dict]) -> dict:
    """Looks at every calendar the student has switched on and repairs the plan. blocks: their future planned blocks
    ({id, kind, summary, start, end, event_id, company, colour_id, application_id, application_title, stage, app_deadline,
    stage_deadline, stage_start, resource_url, resource_label}). For each block: if its event was deleted, reports 'removed';
    if the student dragged it to another time, reports 'retimed'; if a new event overlaps it, books a new slot, deletes the
    old one and reports 'moved'. An interview or assessment centre cannot be moved ('unmovable'). Returns {actions, checked}."""
    now = datetime.now(timezone.utc)
    mine = [b for b in blocks if b.get("event_id") and b.get("start") and _when(b["start"]) and now < _when(b["start"]) < now + timedelta(days=29)]
    if not mine:
        return {"actions": [], "checked": 0}
    service = get_calendar_service()
    horizon = min(max(_when(b.get("end") or b["start"]) for b in mine), now + timedelta(days=30)) + timedelta(days=1)
    events = _all_events(service, now, horizon)
    complete = len(events) < 1500  # if the list was cut short, a missing event proves nothing
    by_id = {e["id"]: e for e in events if e.get("id")}
    own_ids = {b["event_id"] for b in mine}

    def span(e):
        s, en = e["start"], e["end"]
        return _utc(s.get("dateTime") or s.get("date")), _utc(en.get("dateTime") or en.get("date"))

    def is_own(e):
        props = (e.get("extendedProperties") or {}).get("private") or {}
        return e.get("id") in own_ids or props.get("opportune") == "1" or str(e.get("summary", "")).startswith(OWN_PREFIXES)

    others = []
    for e in events:
        me = next((a for a in e.get("attendees", []) if a.get("self")), None)
        if e.get("status") == "cancelled" or is_own(e) or _is_free(e) or (me and me.get("responseStatus") == "declined"):
            continue
        others.append((*span(e), e.get("summary") or "an event"))

    actions, to_move = [], []
    for b in mine:
        e = by_id.get(b["event_id"])
        if e is None:
            if complete:
                actions.append({"action": "removed", "block_id": b["id"], "summary": b.get("summary")})
            continue
        s, en = span(e)
        old_s, old_e = _when(b["start"]), _when(b.get("end") or b["start"])
        if abs((s - old_s).total_seconds()) > 60 or abs((en - old_e).total_seconds()) > 60:
            actions.append({"action": "retimed", "block_id": b["id"], "summary": b.get("summary"), "start": s.isoformat(), "end": en.isoformat()})
        clash = next((o for o in others if o[0] < en and o[1] > s), None)
        if not clash:
            continue
        if b.get("kind") in FIXED_KINDS:
            actions.append({"action": "unmovable", "block_id": b["id"], "summary": b.get("summary"), "clashed_with": clash[2], "start": s.isoformat()})
        else:
            to_move.append(({**b, "start": s.isoformat(), "end": en.isoformat()}, clash))

    items, owners = [], []
    for b, clash in to_move:
        item, why = _rebook_item(b, now)
        if item is None:
            actions.append({"action": "stuck", "block_id": b["id"], "summary": b.get("summary"), "clashed_with": clash[2], "reason": why})
            continue
        item["force_new"] = True  # the old event is still in the calendar until the new one is booked
        items.append(item)
        owners.append((b, clash))
    results = await _schedule_blocks(items) if items else []
    for (b, clash), r in zip(owners, results):
        if r["status"] == "booked":
            _cancel_events([b["event_id"]])
            actions.append({"action": "moved", "block_id": b["id"], "summary": b.get("summary"), "old_start": b["start"],
                            "clashed_with": clash[2], "new": _slot_result(r)})
        else:
            actions.append({"action": "stuck", "block_id": b["id"], "summary": b.get("summary"), "clashed_with": clash[2], "reason": "no free slot"})
    return {"actions": actions, "checked": len(mine)}


@mcp.tool()
async def rebook_missed_blocks(blocks: list[dict], move: bool = False, not_before: str | None = None, not_after: str | None = None) -> dict:
    """Books blocks again: ones the student did not get to (they said so, or did not answer the check-in), or, with
    move=true, ones that are still planned but should go somewhere else (the old event is deleted once the new one is
    booked). blocks have the same fields as for repair_clashes. not_before / not_after (ISO 8601) limit where the new slot
    may be. Each block gets 'rebooked' (with its new slot), 'dropped' (an interview that passed, a deadline that passed, or
    a stage that moved on) or 'failed' (no free slot). Returns {actions}."""
    now = datetime.now(timezone.utc)
    earliest, latest = _when(not_before), _when(not_after)
    actions, items, owners = [], [], []
    for b in blocks:
        item, why = _rebook_item(b, now)
        if item is None:
            actions.append({"action": "dropped", "block_id": b["id"], "summary": b.get("summary"), "reason": why})
            continue
        if move:
            item["force_new"] = True  # the old event is still in the calendar until the new one is booked
        if earliest:
            item["earliest"] = earliest.isoformat()
        if latest:
            item["deadline"] = min(_when(item["deadline"]), latest).isoformat()
            if _when(item["deadline"]) <= now + timedelta(hours=2):
                actions.append({"action": "failed", "block_id": b["id"], "summary": b.get("summary"), "reason": "the time limit is too close"})
                continue
        items.append(item)
        owners.append(b)
    results = await _schedule_blocks(items) if items else []
    for b, r in zip(owners, results):
        if r["status"] in ("booked", "already"):
            if move and r["status"] == "booked" and b.get("event_id"):
                _cancel_events([b["event_id"]])
            actions.append({"action": "rebooked", "block_id": b["id"], "summary": b.get("summary"), "new": _slot_result(r)})
        else:
            actions.append({"action": "failed", "block_id": b["id"], "summary": b.get("summary"), "reason": r.get("reason") or "no free slot"})
    return {"actions": actions}


@mcp.tool()
async def watch_openings(watches: list[dict]) -> dict:
    """Checks programmes that had not opened yet. Each watch: {watch_id, company, title, url, type, field, colour_id, plan}.
    It reads each programme's own page. If applications are now open and plan is true, it books the Apply block and
    practice sessions straight away. Returns {results}: per watch, status (open, not_yet_open, closed, unknown), opens_on,
    deadline, deadline_assumed, evidence, and the blocks it booked."""
    now = datetime.now(timezone.utc)
    first_year = now.year + 1 if now.month >= 9 else now.year
    pool = [{"url": w["url"], "title": w.get("title") or w["company"], "company": w["company"], "_snippet": ""} for w in watches]
    await _check_open(pool, now, 40)
    out, blocks = [], []
    for w, e in zip(watches, pool):
        status = e.get("open_status", "unknown")
        if e.get("alive") is False:
            status = "unknown"
        if e.get("for_year") and e["for_year"] < first_year:
            status = "closed"
        row = {"watch_id": w["watch_id"], "company": w["company"], "status": status, "opens_on": e.get("opens_on"),
               "deadline": e.get("deadline"), "deadline_assumed": False, "evidence": e.get("open_evidence", ""), "blocks": []}
        if status == "open":
            closing = _when(e.get("deadline")) or now + timedelta(days=NO_DEADLINE_DAYS_WEB)
            row["deadline_assumed"] = not e.get("deadline")
            row["deadline"] = closing.isoformat()
            if closing <= now + timedelta(hours=2):
                row["status"] = "closed"
            elif w.get("plan"):
                blocks += _application_blocks({**w, "deadline": closing.isoformat(), "application_id": None, "ref": {"watch_id": w["watch_id"]}})
        out.append(row)
    for r in (await _schedule_blocks(blocks) if blocks else []):
        row = next(x for x in out if x["watch_id"] == r["ref"]["watch_id"])
        row["blocks"].append(_slot_result(r))
    return {"results": out}


@mcp.tool()
async def chase_applications(applications: list[dict]) -> dict:
    """Finds applications that have gone quiet and books a 15-minute 'Check: Company' reminder for each, to look in the
    inbox and spam for news. Each application: {application_id, company, stage, stage_updated_at, last_chase_at, colour_id}.
    Only a company at 'applied' for 6 days or more, and not chased in the last 4, is chased. Returns {results}."""
    now = datetime.now(timezone.utc)
    items, due = [], []
    for a in applications:
        since, last = _when(a.get("stage_updated_at")), _when(a.get("last_chase_at"))
        if a.get("stage") != "applied" or since is None or now - since < timedelta(days=CHASE_AFTER_DAYS):
            continue
        if last is not None and now - last < timedelta(days=CHASE_REPEAT_DAYS):
            continue
        days = (now - since).days
        due.append({"application_id": a["application_id"], "company": a["company"], "days_waiting": days})
        items.append({"block_kind": "chase", "title": f"{a['company']}: check for an update", "summary": f"Check: {a['company']}",
                      "company": a["company"], "deadline": (now + timedelta(days=3)).isoformat(), "exact_minutes": 15,
                      "colour_id": a.get("colour_id"), "application_id": a["application_id"],
                      "details": f"It has been {days} days since you applied to {a['company']}. Look in your inbox and spam folder for an update."})
    results = await _schedule_blocks(items) if items else []
    for d, r in zip(due, results):
        d["block"] = _slot_result(r)
    return {"results": due}


if __name__ == "__main__":
    mcp.run()