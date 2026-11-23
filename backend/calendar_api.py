"""Reads the signed-in user's Google Calendar for the Calendar page."""
import json
from datetime import datetime

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import db
import google_oauth

MAX_EVENTS = 1000


def _shape(e: dict) -> dict:
    """Turns a Google Calendar event into what the page needs."""
    start, end = e.get("start", {}), e.get("end", {})
    title = e.get("summary") or "(No title)"
    props = (e.get("extendedProperties") or {}).get("private") or {}
    ours = props.get("opportune") == "1" or title.startswith(("Apply: ", "Test: "))  # blocks this app booked
    colour_id = int(e["colorId"]) if str(e.get("colorId", "")).isdigit() else None
    return {
        "id": e["id"],
        "title": title,
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "all_day": "dateTime" not in start,  # all-day events only have a date
        "location": e.get("location") or "",
        "link": e.get("htmlLink"),
        "opportune": ours,
        "kind": props.get("kind"),
        "company": props.get("company_name") if ours else None,
        "practice_url": (e.get("source") or {}).get("url") if ours else None,
        "practice_label": (e.get("source") or {}).get("title") if ours else None,
        "colour": db.HEX_OF.get(colour_id) if ours else None,
    }


def list_events(user_id: int, start: datetime, end: datetime) -> list[dict]:
    """Events in [start, end) from the user's primary calendar, recurring events expanded, oldest first."""
    try:
        token = google_oauth.fresh_token_json(user_id)
    except Exception:  # the refresh token was revoked or has expired
        raise PermissionError("Google access expired or was removed. Reconnect Google on your Profile page")
    if not token:
        raise PermissionError("Connect your Google account on the Profile page first")
    creds = Credentials.from_authorized_user_info(json.loads(token), google_oauth.SCOPES)
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)

    # Every calendar the student has switched on (a timetable, a society, work), not just the main one
    try:
        listed = service.calendarList().list().execute().get("items", [])
    except Exception:
        listed = []
    ids = [("primary" if c.get("primary") else c["id"]) for c in listed if c.get("selected") is not False and not c.get("deleted")] or ["primary"]
    if not any(c.get("primary") for c in listed):
        ids.insert(0, "primary")
    events = []
    for cid in ids[:20]:
        page = None
        try:
            while len(events) < MAX_EVENTS:
                resp = service.events().list(
                    calendarId=cid,
                    timeMin=start.isoformat(),
                    timeMax=end.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=250,
                    pageToken=page,
                ).execute()
                events += [_shape(e) for e in resp.get("items", []) if e.get("status") != "cancelled"]
                page = resp.get("nextPageToken")
                if not page:
                    break
        except Exception:
            if cid == "primary":
                raise  # the main calendar must work; a broken extra calendar is skipped
    events.sort(key=lambda e: e["start"] or "")
    return events
