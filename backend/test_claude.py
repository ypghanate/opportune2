"""Checks the Claude calls in server.py without a network: a mock HTTP transport stands in for api.anthropic.com."""
import asyncio, json, os, tempfile
from datetime import datetime, timedelta, timezone
from cryptography.fernet import Fernet
os.environ.update(ANTHROPIC_API_KEY="test-key", TAVILY_API_KEY="test-key", JWT_SECRET="x" * 40,
                  DATA_DIR=tempfile.mkdtemp(), TOKEN_ENC_KEY=Fernet.generate_key().decode())

try:  # newer anthropic SDKs ship their own HTTP library
    import httpx2 as httpx
except ImportError:
    import httpx
from anthropic import AsyncAnthropic

import server


def fake_claude(answers: list, seen: list):
    """A client whose 'API' replies with the next answer as a forced tool call, and records each request."""
    it = iter(answers)

    def handler(request: httpx.Request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "stop_reason": "tool_use", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1},
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "answer", "input": next(it)}],
        })

    return AsyncAnthropic(api_key="k", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def no_refs(node):
    text = json.dumps(node)
    return "$ref" not in text and "$defs" not in text


def test_ask_request_shape_and_nested_schema(monkeypatch):
    seen = []
    result = {"results": [{
        "title": "Spring Week", "url": "https://bank.com/sw", "grad": 2029, "eligibility": True, "type": "spring week",
        "date": "2027-03-29T09:00:00Z", "time_start": None, "duration": "P3D", "location": "London", "online": False,
        "deadline": "2026-12-15T23:59:00Z"}]}
    monkeypatch.setattr(server, "llm", fake_claude([result], seen))
    out = asyncio.run(server.ask([{"role": "system", "content": "SYS A"}, {"role": "system", "content": "SYS B"},
                                  {"role": "user", "content": "go"}], server.SearchResults))
    assert out.results[0].duration == timedelta(days=3) and out.results[0].time_start is None
    req = seen[0]
    assert req["model"] == "claude-sonnet-5-5" and req["system"] == "SYS A\n\nSYS B"
    assert req["messages"] == [{"role": "user", "content": "go"}]  # no system role inside messages
    assert req["tool_choice"] == {"type": "tool", "name": "answer"}
    schema = req["tools"][0]["input_schema"]
    assert schema["type"] == "object" and no_refs(schema)
    item = schema["properties"]["results"]["items"]["properties"]
    assert item["type"]["enum"][0] == "hackathons" and "deadline" in item  # the nested model was inlined


def test_fast_model_and_no_system(monkeypatch):
    seen = []
    monkeypatch.setattr(server, "llm", fake_claude([{"is_relevant": True, "category": "assessment", "reasoning": "x"}], seen))
    out = asyncio.run(server.classify_email_relevance("Subject: HackerRank test"))
    assert out.is_relevant and out.category == "assessment"
    assert seen[0]["model"] == "claude-haiku-5-5" and "system" not in seen[0]


def test_decide_dates_uses_main_model_and_received_dates(monkeypatch):
    seen = []
    answer = {"decisions": [{"index": 0, "needs_action": True, "title": "OA", "kind": "assessment", "date": "2030-05-01T12:00:00Z",
                             "date_basis": "stated", "evidence": "within 7 days", "duration_minutes": 90}]}
    monkeypatch.setattr(server, "llm", fake_claude([answer], seen))
    batch = [{"received": "Mon, 2 Nov 2026 10:00:00 +0000", "sender": "hr@bank.com", "subject": "Your assessment",
              "category": "assessment", "body": "Complete within 7 days"}]
    out = asyncio.run(server.decide_dates(batch))
    assert out[0].date == "2030-05-01T12:00:00Z" and out[0].duration_minutes == 90
    req = seen[0]
    assert req["model"] == "claude-sonnet-5-5" and "TODAY" in req["system"]
    sent = json.loads(req["messages"][0]["content"])
    assert sent[0]["received"].startswith("Mon, 2 Nov 2026") and "within 7 days" in sent[0]["body"]


def test_scan_keeps_only_actionable_future_emails(monkeypatch):
    import base64
    subjects = ["Stated", "Confirmation", "Old deadline", "No date", "Newsletter"]
    msgs = {str(i): {"id": str(i), "snippet": s, "payload": {"headers": [
        {"name": "Subject", "value": s}, {"name": "From", "value": "a@b.com"}, {"name": "Date", "value": "Mon, 2 Nov 2026 10:00:00 +0000"}],
        "mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"hi").decode()}}} for i, s in enumerate(subjects)}

    class Ex:
        def __init__(self, v): self.v = v
        def execute(self): return self.v

    class Svc:
        calls = {"list": [], "get": []}

        def users(self): return self
        def messages(self): return self

        def list(self, **k):  # two pages, to check paging
            self.calls["list"].append(k)
            ids = list(msgs)
            if k.get("pageToken") is None:
                return Ex({"messages": [{"id": i} for i in ids[:3]], "nextPageToken": "p2"})
            return Ex({"messages": [{"id": i} for i in ids[3:]]})

        def get(self, userId, id, format, metadataHeaders=None):
            self.calls["get"].append(format)
            return Ex(msgs[id])

        def new_batch_http_request(self, callback):
            class B:
                def __init__(b): b.reqs = []
                def add(b, req, request_id): b.reqs.append((request_id, req))
                def execute(b):
                    for rid, req in b.reqs:
                        callback(rid, req.execute(), None)
            return B()

    async def classify(text):
        rel = "Newsletter" not in text
        return server.EmailRelevance(is_relevant=rel, category="assessment" if rel else "noise", reasoning="")

    future = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()

    async def decide(batch):
        D = server.EmailDecision
        by = {e["subject"]: i for i, e in enumerate(batch)}
        return [
            D(index=by["Stated"], needs_action=True, title="Stated OA", kind="assessment", date=future, date_basis="stated", evidence="by Friday"),
            D(index=by["Confirmation"], needs_action=False, title="x", kind="other", date=None, date_basis="none", evidence="received"),
            D(index=by["Old deadline"], needs_action=True, title="Old", kind="assessment", date=past, date_basis="stated", evidence="x"),
            D(index=by["No date"], needs_action=True, title="Undated", kind="assessment", date=future, date_basis="suggested", evidence="no date in email"),
        ]

    monkeypatch.setattr(server, "get_gmail_service", lambda: Svc())
    monkeypatch.setattr(server, "classify_email_relevance", classify)
    monkeypatch.setattr(server, "decide_dates", decide)
    items = asyncio.run(server.scan_inbox_semantically.fn() if hasattr(server.scan_inbox_semantically, "fn") else server.scan_inbox_semantically())
    assert [i["title"] for i in items] == ["Stated OA", "Undated"]
    assert [c.get("pageToken") for c in Svc.calls["list"]] == [None, "p2"]  # followed the second page
    assert "metadata" in Svc.calls["get"] and "full" in Svc.calls["get"]  # headers for all, full text for relevant ones
    assert items[0]["deadline_assumed"] is False and items[1]["deadline_assumed"] is True
    assert items[1]["snippet"].startswith("No deadline in the email") and items[0]["source"] == "gmail"


class FakeCalendar:
    def __init__(self):
        self.inserted = []

    def events(self):
        outer = self

        class E:
            def list(self, **kw):
                return type("R", (), {"execute": staticmethod(lambda: {"items": []})})

            def insert(self, calendarId, body):
                outer.inserted.append(body)
                return type("R", (), {"execute": staticmethod(lambda: {"htmlLink": "https://cal/1"})})
        return E()


def test_scheduler_retry_loop_history(monkeypatch):
    now = datetime.now(timezone.utc)
    deadline = (now + timedelta(days=5)).replace(hour=12, minute=0, second=0, microsecond=0)
    good = (now + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
    cal, seen = FakeCalendar(), []
    monkeypatch.setattr(server, "get_calendar_service", lambda: cal)
    monkeypatch.setattr(server, "llm", fake_claude([
        {"suggestions": []},  # first try: nothing usable -> error message goes back to Claude
        {"suggestions": [{"opportunity_index": 0, "start_time": good.isoformat(), "duration_hours": 1.5, "reason": "free evening"}]},
    ], seen))
    out = asyncio.run(server.schedule_application_with_llm(
        [{"title": "Spring Week", "type": "spring week", "deadline": deadline.isoformat(), "url": "https://bank.com/sw"}]))
    assert out[0].startswith("Booked 'Spring Week'") and cal.inserted[0]["summary"] == "Apply: Spring Week"
    assert len(seen) == 2
    roles = [m["role"] for m in seen[1]["messages"]]
    assert roles == ["user", "assistant", "user"]  # Claude needs strictly alternating turns
    assert "error with scheduling try again to find slot" in seen[1]["messages"][2]["content"]
    assert "Spring Week" in seen[1]["messages"][2]["content"]
