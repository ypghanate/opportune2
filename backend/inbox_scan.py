"""Inbox scan (Gmail), kept for a later optional upgrade. NOT an agent tool and not imported by the app.

It needs the restricted gmail.readonly scope, which is why the product no longer depends on it.
To use it again: register `scan_inbox_semantically` with @mcp.tool() in server.py (or call it from a button).
"""
import asyncio
import base64
import json
import os
import re
import sys
import threading
import time as time_module
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from server import FAST_MODEL, ask, get_gmail_service


class EmailDecision(BaseModel):
    index: int = Field(description="The 'index' of the email this decision is for")
    needs_action: bool = Field(description="true ONLY if the email asks the student to complete or attend an online assessment or test (e.g. SHL, HireVue, Codility, HackerRank, aptitude, psychometric, numerical or verbal reasoning, coding or game-based test), a video interview, a phone or live interview, or an assessment centre. false for everything else: club RSVPs, social or alumni events, ID verification, security or billing notices, newsletters, support hubs or webinars, tutoring or admin tasks, application-received confirmations, rejections, or anything already done")
    title: str = Field(description="Short name of the thing to do, e.g. 'Deutsche Bank online assessment'")
    kind: Literal["assessment", "interview", "other"]
    date: str | None = Field(default=None, description="The deadline or appointment time STATED in the email, ISO 8601 UTC e.g. 2026-12-03T17:00:00Z (relative deadlines like 'within 7 days' worked out from the email's Received date). null if the email gives no deadline or time, or if needs_action is false")
    date_basis: Literal["stated", "none"] = Field(description="'stated' if the email gives a deadline or time; 'none' if it does not, or if needs_action is false")
    evidence: str = Field(description="The words in the email that support the date, or 'no date in email'")
    is_fixed_time: bool = Field(default=False, description="true ONLY for an appointment the student must attend at a set time, e.g. a scheduled interview; false for deadlines")
    duration_minutes: int | None = Field(default=None, description="How long it takes, if the email says, else null")
    link: str | None = Field(default=None, description="URL to start or book it, or null")


class EmailDecisions(BaseModel):
    decisions: list[EmailDecision]


class Flagged(BaseModel):
    indexes: list[int] = Field(description="The 'index' of EVERY email that could be an invitation or reminder to take an online assessment or test, or to attend an interview or assessment centre. Include an email if unsure. Empty list if none.")


class EmailRelevance(BaseModel):
    is_relevant: bool
    category: Literal["assessment", "interview", "application_update", "noise"]
    reasoning: str



# ---------------------------------------------------------------------------
# Gmail scanning
# ---------------------------------------------------------------------------
async def classify_email_relevance(text: str) -> EmailRelevance:
    prompt = (
        "Analyze the following email and determine if it is a genuine job/internship "
        "assessment invitation, interview invitation, or direct application status update.\n\n"
        "CRITICAL: Exclude medical appointments, retail bookings, newsletters, "
        "marketing, or personal emails.\n"
        "You may only see the subject, sender and a short snippet. If it could plausibly be an assessment, "
        "interview, task to submit or application update from an employer or recruiter, mark it relevant: "
        "a second step reads the full email and decides.\n\n"
        f"Email Text:\n{text}"
    )
    return await ask([{"role": "user", "content": prompt}], EmailRelevance, model=FAST_MODEL, max_tokens=1024)


def get_email_body(msg_data: dict) -> str:
    def walk(part):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime in ("text/plain", "text/html"):
            text = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="ignore")
            return [(mime, text)]
        found = []
        for p in part.get("parts") or []:
            found.extend(walk(p))
        return found

    found = walk(msg_data.get("payload", {}))
    for mime, text in found:
        if mime == "text/plain":
            return text
    if found:
        return re.sub(r"<[^>]+>", " ", found[0][1])
    return ""


DECIDE_BATCH = 10  # emails per main-model call
NO_DEADLINE_DAYS = 7  # an assessment/interview email with no stated deadline is due this many days after it was sent


async def decide_dates(batch: list[dict]) -> list[EmailDecision]:
    """One main-model call over several relevant emails: what must the student do, and by when?"""
    now = datetime.now(timezone.utc)
    system = f"""You read emails that a UK university student received and pick out ONLY the ones that ask them to take an online assessment, test or interview.
TODAY: {now.strftime('%A %Y-%m-%d %H:%M')} UTC.
For EVERY email give one decision, using its 'index'. For emails that are not relevant, set needs_action false,
kind "other", date null, date_basis "none", and keep the other fields short.
RULES:
- needs_action is true ONLY if the email asks the student to complete or attend: an online assessment or test (SHL, HireVue,
  Codility, HackerRank, aptitude, psychometric, numerical or verbal reasoning, coding or game-based test), a video interview,
  a phone or live interview, or an assessment centre.
- needs_action is FALSE for everything else, even if the student must act: club or society RSVPs (Spond etc.), social or alumni
  events, ID or identity verification, account, security, banking or billing notices, newsletters, support hubs, webinars or
  info sessions, tutoring or university admin, application-received confirmations, rejections, and anything already completed.
- date is the deadline or appointment time the email STATES. Work relative deadlines ("within 7 days", "by Friday") out from
  THAT email's Received date, not from today. A date with no year means the next occurrence after the Received date. A time
  with no timezone is UK time: convert it to UTC. If the email states no deadline or time, date is null: never invent one.
- kind is "assessment" for a test or online assessment, "interview" for any interview or assessment centre.
- evidence: quote the few words that justify the decision and the date."""
    user = json.dumps(
        [
            {
                "index": i,
                "received": e["received"],
                "from": e["sender"],
                "subject": e["subject"],
                "body": e["body"],
            }
            for i, e in enumerate(batch)
        ],
        indent=1,
    )
    parsed = await ask([{"role": "system", "content": system}, {"role": "user", "content": user}], EmailDecisions, max_tokens=4096)
    return parsed.decisions


# "(in:inbox OR -in:sent)" drops mail you only SENT to other people but keeps mail you sent to yourself (a test email), which carries both labels.
SCAN_BASE = "newer_than:30d -in:drafts -in:spam -in:trash (in:inbox OR -in:sent)"
NOT_PRIMARY = "-category:promotions -category:social -category:updates -category:forums"
SEARCH_TERMS = ('{assessment interview test hirevue shl codility hackerrank codesignal pymetrics cognitive aptitude psychometric '
                '"numerical reasoning" "verbal reasoning" "logical reasoning" "coding challenge" "assessment centre"}')  # { } means OR in Gmail
SCAN_QUERIES = [
    ("every email in your Primary tab", f"{SCAN_BASE} {NOT_PRIMARY}"),  # also catches inbox mail Gmail left uncategorised
    ("emails in any other tab that mention an assessment, interview or test", f"{SCAN_BASE} {SEARCH_TERMS}"),
]
SCAN_LIMIT = 500  # per search, newest first. Promotions and social mail is NOT counted, so this reaches back the full 30 days
BODY_CHARS = 2500  # text sent to the model when the email mentions an assessment/interview/test
OTHER_BODY_CHARS = 1200  # for every other email (saves time and cost)

# Emails that mention one of these get more of their text shown to the model. Nothing is filtered out by it.
ASSESSMENT_WORDS = re.compile(
    r"\b(online assessments?|assessments?|interviews?|tests?|hirevue|shl|codility|hackerrank|codesignal|pymetrics|cognitive|aptitude|"
    r"psychometric|numerical reasoning|verbal reasoning|logical reasoning|coding challenge|assessment cent(?:re|er))\b",
    re.I,
)


def _list_ids(service, query: str, limit: int) -> list[str]:
    ids, token = [], None
    while len(ids) < limit:
        resp = service.users().messages().list(
            userId="me", q=query, maxResults=min(100, limit - len(ids)), pageToken=token
        ).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        token = resp.get("nextPageToken")
        if not token:
            break
    return ids


GMAIL_WORKERS = 1  # Gmail refuses parallel bursts (403 rateLimitExceeded) on this account, so fetch one at a time. Raise only if you never see those warnings.


GMAIL_PAUSE = 0.15  # seconds between requests, so we stay well under Gmail's per-second limit
GMAIL_ATTEMPTS = 7


def _fetch_many(ids: list[str], workers: int = GMAIL_WORKERS, **get_args) -> list[dict]:
    """Gets Gmail messages at a steady pace (each worker thread has its own Gmail client, as the client is not thread-safe).
    When Gmail says slow down (403 rateLimitExceeded), everything pauses and the wait doubles each time: 2, 4, 8, 16, 32, 60 seconds."""
    local = threading.local()
    last_error = [None]
    done = [0]
    cooldown_until = [0.0]
    lock = threading.Lock()

    def one(mid):
        if not hasattr(local, "service"):
            local.service = get_gmail_service()
        result = None
        for attempt in range(GMAIL_ATTEMPTS):
            wait = cooldown_until[0] - time_module.monotonic()
            if wait > 0:
                time_module.sleep(wait)
            try:
                result = local.service.users().messages().get(userId="me", id=mid, **get_args).execute(num_retries=0)
                break
            except Exception as e:
                last_error[0] = e
                pause = min(60, 2 ** (attempt + 1))
                with lock:
                    cooldown_until[0] = max(cooldown_until[0], time_module.monotonic() + pause)
                if attempt == 0:
                    print(f"[scan] Gmail asked us to slow down, pausing ({str(e)[:90]})", file=sys.stderr)
        time_module.sleep(GMAIL_PAUSE)
        done[0] += 1
        if done[0] % 25 == 0:
            print(f"[scan] fetched {done[0]} of {len(ids)} emails", file=sys.stderr)
        return result

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, ids))
    got = [r for r in results if r]
    if len(got) < len(ids):
        print(f"[scan] WARNING: could not read {len(ids) - len(got)} of {len(ids)} emails ({last_error[0]})", file=sys.stderr)
    return got


FLAG_BATCH = 40  # short emails (subject and snippet only) screened per call


async def flag_candidates(batch: list[dict]) -> list[int]:
    """Quick screen of subject + snippet only: which emails could be an assessment, test or interview?"""
    system = """You screen a UK university student's emails. For each email (index, sender, subject, snippet) decide whether it
could be an invitation or reminder to take an online assessment or test (SHL, HireVue, Codility, HackerRank, aptitude,
psychometric, numerical or verbal reasoning, coding or game-based test), to attend an interview or assessment centre, or
a job/internship application step like that. Return the index of every email that could be. If unsure, include it.
Leave out newsletters, receipts, security alerts, club or social mail, and plain application-received confirmations."""
    user = json.dumps(
        [{"index": i, "from": e["sender"], "subject": e["subject"], "snippet": e["snippet"][:200]} for i, e in enumerate(batch)],
        indent=1,
    )
    parsed = await ask([{"role": "system", "content": system}, {"role": "user", "content": user}], Flagged, max_tokens=1024)
    return [i for i in parsed.indexes if 0 <= i < len(batch)]


def _headers(m: dict) -> dict:
    return {h["name"]: h["value"] for h in m["payload"]["headers"]}


# ---------------------------------------------------------------------------
# Scan cache: every email is only read and judged once
# ---------------------------------------------------------------------------
# Bump this if SEARCH_TERMS or the prompts change, so old verdicts are thrown away.
CACHE_VERSION = 1
CACHE_DAYS = 40  # a little longer than the 30-day scan window


def _cache_path():
    path = os.environ.get("SCAN_CACHE", "").strip()
    return Path(path) if path else None


def _load_cache() -> dict:
    """{email id: {"t": sent time in ms, "state": "skip" | "decided", "email": {...}, "decision": {...}}}"""
    path = _cache_path()
    if not path or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if data.get("version") == CACHE_VERSION and isinstance(data.get("emails"), dict):
            return data["emails"]
    except Exception as e:
        print(f"[scan] cache unreadable, starting fresh: {e}", file=sys.stderr)
    return {}


def _save_cache(emails: dict) -> None:
    path = _cache_path()
    if not path:
        return
    try:
        cutoff = (time_module.time() - CACHE_DAYS * 86400) * 1000
        kept = {i: v for i, v in emails.items() if v.get("t", 0) >= cutoff}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": CACHE_VERSION, "emails": kept}))
        os.replace(tmp, path)  # never leaves a half-written file behind
    except Exception as e:
        print(f"[scan] could not save the cache: {e}", file=sys.stderr)


async def scan_inbox_semantically() -> list[dict]:
    """Looks through the last 30 days of Gmail for assessment, test and interview emails and has the model decide what to schedule, and when."""
    try:
        service = get_gmail_service()
        loop = asyncio.get_running_loop()
        began = time_module.monotonic()

        def lap(text):
            print(f"[scan] {text} ({time_module.monotonic() - began:.0f}s)", file=sys.stderr)

        def list_all():
            return [_list_ids(service, q, SCAN_LIMIT) for _, q in SCAN_QUERIES]

        primary, keyword = await loop.run_in_executor(None, list_all)
        for (label, _), found in zip(SCAN_QUERIES, (primary, keyword)):
            print(f"[scan] {len(found)} emails: {label}" + ("  (hit the limit!)" if len(found) >= SCAN_LIMIT else ""), file=sys.stderr)
        keyword_set = set(keyword)
        others = [i for i in primary if i not in keyword_set]  # Primary emails that never mention an assessment word
        lap(f"listed {len(keyword)} emails that mention an assessment/interview/test, plus {len(others)} other Primary emails")

        cache = _load_cache()
        listed = set(primary) | keyword_set
        keyword = [i for i in keyword if i not in cache]  # emails we have already judged are not read again
        others = [i for i in others if i not in cache]
        lap(f"{len(cache)} emails already in the cache, {len(keyword) + len(others)} new ones to read")
        pending = [i for i, v in cache.items() if v.get("state") == "flagged" and i in listed]  # flagged earlier, never downloaded
        if pending:
            print(f"[scan] {len(pending)} emails flagged on an earlier scan still need reading", file=sys.stderr)

        now = datetime.now(timezone.utc)
        sem = asyncio.Semaphore(6)  # a few Claude calls at a time

        # 1. Emails that mention an assessment, interview or test: download in full
        messages = await loop.run_in_executor(None, lambda: _fetch_many(keyword, format="full"))
        lap(f"read {len(messages)} of those in full")

        # 2. Other Primary emails: download only subject and snippet, and let the model flag the ones that could matter
        light = await loop.run_in_executor(
            None, lambda: _fetch_many(others, format="metadata", metadataHeaders=["Subject", "From", "Date"])
        )
        short = [{"id": m["id"], "sender": _headers(m).get("From", ""), "subject": _headers(m).get("Subject", ""),
                  "snippet": m.get("snippet", "")} for m in light]

        async def screen(batch):
            try:
                async with sem:
                    return batch, await flag_candidates(batch)
            except Exception as e:
                print(f"[scan] screening failed for {len(batch)} emails: {e}", file=sys.stderr)
                return batch, list(range(len(batch)))  # if the screen fails, keep them all

        flagged_ids = []
        screened_ok = set()
        for batch, picked in await asyncio.gather(*(screen(short[i:i + FLAG_BATCH]) for i in range(0, len(short), FLAG_BATCH))):
            for n, e in enumerate(batch):
                if n in picked:
                    flagged_ids.append(e["id"])
                else:
                    screened_ok.add(e["id"])
                    print(f"[scan] skipped (no assessment/interview/test): {e['subject'][:70]}", file=sys.stderr)
        lap(f"screened {len(short)} other Primary emails, {len(flagged_ids)} could matter")
        sent_at = {m["id"]: int(m.get("internalDate", 0)) for m in light}
        for i in screened_ok:  # the model said these are irrelevant, so never look at them again
            cache[i] = {"t": sent_at.get(i, 0), "state": "skip"}
        for i in flagged_ids:  # remembered until the full email has been read, in case Gmail refuses
            cache[i] = {"t": sent_at.get(i, 0), "state": "flagged"}
        _save_cache(cache)  # progress is kept even if the scan is stopped now
        flagged_ids = flagged_ids + [i for i in pending if i not in flagged_ids]

        if flagged_ids:
            messages += await loop.run_in_executor(None, lambda: _fetch_many(flagged_ids, format="full"))

        messages.sort(key=lambda m: int(m.get("internalDate", 0)), reverse=True)
        if messages:
            dates = [datetime.fromtimestamp(int(m.get("internalDate", 0)) / 1000, tz=timezone.utc) for m in messages]
            print(f"[scan] {len(messages)} emails go to the model, from {min(dates):%d %b} to {max(dates):%d %b}", file=sys.stderr)

        relevant = []
        for m in messages:
            headers = _headers(m)
            subject, snippet = headers.get("Subject", ""), m.get("snippet", "")
            full_text = get_email_body(m)
            try:  # when it was sent: Gmail's own timestamp is more reliable than the Date header
                received_at = datetime.fromtimestamp(int(m["internalDate"]) / 1000, tz=timezone.utc)
            except (KeyError, ValueError):
                received_at = now
            relevant.append({
                "id": m["id"],
                "t": int(m.get("internalDate", 0)),
                "subject": subject,
                "sender": headers.get("From", ""),
                "received": headers.get("Date", "unknown"),
                "received_at": received_at,
                "body": full_text[:BODY_CHARS] or snippet,
                "snippet": snippet[:300],
            })

        # 3. The main model reads each of those in full and decides what to schedule and when
        async def decide(batch):
            for attempt in (1, 2):
                try:
                    async with sem:
                        return batch, await decide_dates(batch)
                except Exception as e:
                    print(f"[scan] date extraction failed for a batch of {len(batch)} (try {attempt}): {e}", file=sys.stderr)
            return batch, []

        batches = [relevant[i:i + DECIDE_BATCH] for i in range(0, len(relevant), DECIDE_BATCH)]
        verdicts = []  # (email, decision) for new emails and cached ones alike
        for batch, decisions in await asyncio.gather(*(decide(b) for b in batches)):
            judged = set()
            for d in decisions:
                if not 0 <= d.index < len(batch) or d.index in judged:
                    continue
                judged.add(d.index)
                e = batch[d.index]
                print(f"[scan] {e['subject'][:55]} | action={d.needs_action} | date={d.date} | {d.date_basis} | {d.evidence[:70]}", file=sys.stderr)
                verdicts.append((e, d))
                cache[e["id"]] = {
                    "t": e["t"], "state": "decided", "decision": d.model_dump(),
                    "email": {"sender": e["sender"], "subject": e["subject"], "snippet": e["snippet"], "received_at": e["received_at"].isoformat()},
                }
            # emails the model gave no verdict for are left out of the cache, so the next scan tries them again
        _save_cache(cache)

        fresh = {e["id"] for e, _ in verdicts}
        for i, v in cache.items():  # emails judged on an earlier scan
            if v.get("state") != "decided" or i not in listed or i in fresh:
                continue
            try:
                mail = dict(v["email"])
                mail["received_at"] = datetime.fromisoformat(mail["received_at"])
                verdicts.append((mail, EmailDecision(**v["decision"])))
            except Exception as err:
                print(f"[scan] ignoring a damaged cache entry: {err}", file=sys.stderr)
        lap(f"{len(verdicts)} judged emails in total ({len(relevant)} new)")

        items = []
        for e, d in verdicts:
            if not d.needs_action:
                continue
            when, suggested = None, False
            if d.date:
                try:
                    when = datetime.fromisoformat(d.date.replace("Z", "+00:00"))
                    when = when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when.astimezone(timezone.utc)
                except ValueError:
                    when = None
            if when is None:  # no deadline in the email: due one week after it was sent
                when, suggested = e["received_at"] + timedelta(days=NO_DEADLINE_DAYS), True
                print(f"[scan]   -> no deadline in the email, due set to {NO_DEADLINE_DAYS} days after it was sent ({when:%d %b})", file=sys.stderr)
            if when < now:
                print("[scan]   -> dropped, deadline already passed", file=sys.stderr)
                continue
            items.append({
                "title": d.title or e["subject"],
                "deadline": when.isoformat(),
                "deadline_assumed": suggested,
                "type": d.kind,
                "fixed_time": d.is_fixed_time,
                "duration_minutes": d.duration_minutes if d.duration_minutes and d.duration_minutes > 0 else None,
                "link": d.link,
                "sender": e["sender"],
                "subject": e["subject"],
                "snippet": f"Due {when:%a %d %b %Y}" + (" (no deadline in the email, set to one week after it was sent). " if suggested else ". ") + e["snippet"],
                "source": "gmail",
            })
        lap(f"{len(items)} to schedule")
        return items
    except Exception as e:
        return [{"error": f"Inbox scan failed: {e}"}]


