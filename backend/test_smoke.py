import json, os, tempfile
from contextlib import asynccontextmanager
from cryptography.fernet import Fernet
os.environ.update(ANTHROPIC_API_KEY="test-key", TAVILY_API_KEY="test-key", JWT_SECRET="x" * 40, DATA_DIR=tempfile.mkdtemp(), TOKEN_ENC_KEY=Fernet.generate_key().decode())

from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool

import agent, db, main, runner


GOOD = {"name": "Yash Ghanate", "university": "UCL (University College London)", "degree": "Computer Science"}


def signup(c, email="a@b.co", **over):
    r = c.post("/api/auth/register", json={"email": email, "password": "hunter2hunter2", **GOOD, **over})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_signup_rules_and_meta():
    with TestClient(main.app) as c:
        meta = c.get("/api/meta").json()  # public: the sign-up form needs it before login
        assert "University of Oxford" in meta["universities"] and "Computer Science" in meta["degrees"]
        assert "Other UK institution" in meta["universities"] and meta["degrees"][-1] == "Other"
        assert len(meta["universities"]) > 100
        for field, bad in [("name", "Yash"), ("name", "  "), ("name", "Yash G"), ("name", "Yash 99"),
                           ("university", "Hogwarts"), ("university", "oxford"), ("degree", "Wizardry"), ("degree", "")]:
            r = c.post("/api/auth/register", json={"email": "r@r.co", "password": "hunter2hunter2", **GOOD, field: bad})
            assert r.status_code == 422, (field, bad, r.text)
        short = c.post("/api/auth/register", json={"email": "r@r.co", "password": "short", **GOOD})
        assert short.status_code == 422  # password needs 10+
        assert c.post("/api/auth/register", json={"email": "r@r.co", "password": "hunter2hunter2", **GOOD, "name": "  Mary-Jane   O'Neil "}).status_code == 200
        user = db.get_user_by_email("r@r.co")
        assert db.get_profile(user["id"])[0]["name"] == "Mary-Jane O'Neil"  # saved with single spaces


def test_auth_and_profile():
    with TestClient(main.app) as c:
        assert c.get("/api/profile").status_code == 401
        assert c.get("/api/profile", headers={"Authorization": "Bearer junk"}).status_code == 401
        h = signup(c)
        base = {"email": "a@b.co", "password": "hunter2hunter2", **GOOD}
        assert c.post("/api/auth/register", json=base).status_code == 409
        assert c.post("/api/auth/register", json={**base, "email": "bad"}).status_code == 422
        bad = c.post("/api/auth/login", json={"email": "a@b.co", "password": "wrongwrong"})
        assert bad.status_code == 401 and bad.json()["detail"] == "Wrong email or password"
        assert "access_token" in c.post("/api/auth/login", json={"email": "a@b.co", "password": "hunter2hunter2"}).json()

        p = c.get("/api/profile", headers=h).json()  # sign-up already saved name, university and degree
        assert p["name"] == "Yash Ghanate" and p["degree"] == "Computer Science"
        assert p["cv_name"] is None and p["google_connected"] is False
        prof = dict(name="Yash Ghanate", university="University of Oxford", degree="Mathematics", year="Second year",
                    graduation_year=2029, location="London", interests=["quant", " ml "])
        assert c.put("/api/profile", json=prof, headers=h).status_code == 200
        assert c.put("/api/profile", json={**prof, "graduation_year": 1999}, headers=h).status_code == 422
        for bad in ({"name": "Yash"}, {"name": "Y G"}, {"name": "Yash 123"}, {"university": "Hogwarts"}, {"university": ""}, {"degree": "Wizardry"}):
            assert c.put("/api/profile", json={**prof, **bad}, headers=h).status_code == 422, bad
        assert c.post("/api/profile/cv", files={"file": ("cv.txt", b"hi")}, headers=h).status_code == 400
        assert c.post("/api/profile/cv", files={"file": ("cv.pdf", b"nope")}, headers=h).status_code == 400
        r = c.post("/api/profile/cv", files={"file": ("My CV.pdf", b"%PDF-1.4 x")}, headers=h)
        assert r.json() == {"ok": True, "filename": "My CV.pdf"}
        p = c.get("/api/profile", headers=h).json()
        assert p["name"] == "Yash Ghanate" and p["university"] == "University of Oxford" and p["interests"] == ["quant", "ml"] and p["cv_name"] == "My CV.pdf"
        # search needs Google connected
        assert c.post("/api/opportunities/search", json={"query": "", "categories": ["internship"]}, headers=h).status_code == 400
        assert c.get("/api/opportunities", headers=h).json() == []
        # the Google callback finds the user by state, no auth header
        assert "google=error" in c.get("/api/google/callback?state=bogus&code=x", follow_redirects=False).headers["location"]


class Fake(GenericFakeChatModel):
    def bind_tools(self, tools, **kw):
        return self


def test_search_then_approve(monkeypatch):
    calls = iter([
        AIMessage(content="", tool_calls=[{"name": "search_opportunities", "args": {}, "id": "1"},
                                           {"name": "scan_inbox_semantically", "args": {}, "id": "2"}]),
        AIMessage(content=[{"type": "text", "text": "Found stuff."}]),  # Claude replies in content blocks
    ])
    monkeypatch.setattr(agent, "init_chat_model", lambda *a, **k: Fake(messages=calls))

    async def search_tool():
        return json.dumps([{"title": "Spring Week, Markets", "url": "https://www.bank.com/sw", "type": "spring week",
                            "date": "2030-02-01T00:00:00Z", "location": "London", "online": False,
                            "deadline": "2030-01-01T00:00:00Z"}])

    async def scan_tool():
        return json.dumps([{"title": "HackerRank OA", "type": "assessment", "source": "gmail",
                            "sender": "Bank Recruiting <jobs@bank.com>", "snippet": "Complete within 7 days",
                            "deadline": "2030-01-02T00:00:00Z"}])

    booked_with = {}

    async def sched(items: list[dict]):
        booked_with["items"] = items
        # pretend the calendar had no room for the spring week
        return [f"Booked '{i['title']}' for 2030 https://cal/{n}" if i.get("source") == "gmail"
                else f"Could not find a valid slot for '{i['title']}' after 3 attempts" for n, i in enumerate(items)]

    tools = [StructuredTool.from_function(coroutine=search_tool, name="search_opportunities", description="s"),
             StructuredTool.from_function(coroutine=scan_tool, name="scan_inbox_semantically", description="s"),
             StructuredTool.from_function(coroutine=sched, name="schedule_application_with_llm", description="s")]

    @asynccontextmanager
    async def fake_mcp(user_id):
        yield tools

    monkeypatch.setattr(runner, "_mcp_tools", fake_mcp)

    with TestClient(main.app) as c:
        h = signup(c, "z@z.co")
        prof = dict(name="Yash Ghanate", university="UCL (University College London)", degree="Computer Science",
                    graduation_year=2029, location="London", interests=["quant"])
        assert c.put("/api/profile", json=prof, headers=h).status_code == 200
        c.post("/api/profile/cv", files={"file": ("cv.pdf", b"%PDF-1.4 x")}, headers=h)
        uid = db.get_user_by_email("z@z.co")["id"]
        db.save_google_token(uid, "enc")

        r = c.post("/api/opportunities/search", json={"query": "", "categories": []}, headers=h)
        assert r.status_code == 422
        r = c.post("/api/opportunities/search", json={"query": "quant", "categories": ["internship", "spring_week"]}, headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["message"] == "Found stuff."
        by_title = {i["title"]: i for i in body["items"]}
        sw, oa = by_title["Spring Week, Markets"], by_title["HackerRank OA"]
        assert sw["category"] == "spring_week" and sw["company"] == "bank.com" and sw["status"] == "pending"
        assert oa["company"] == "Bank Recruiting" and oa["description"] == "Complete within 7 days"
        assert not runner.active_run_for(uid) and "items" not in booked_with  # nothing booked by searching

        assert len(c.get("/api/opportunities", headers=h).json()) == 2
        # another user sees nothing and cannot approve these
        h2 = signup(c, "other@z.co")
        assert c.get("/api/opportunities", headers=h2).json() == []
        assert c.post("/api/opportunities/approve", json=[sw["id"]], headers=h2).status_code == 400

        # approve both: only the OA gets a slot, the spring week stays pending
        r = c.post("/api/opportunities/approve", json=[sw["id"], oa["id"]], headers=h)
        assert r.status_code == 200 and r.json()["booked"] == [oa["id"]], r.text
        assert len(booked_with["items"]) == 2
        status = {i["title"]: i["status"] for i in c.get("/api/opportunities", headers=h).json()}
        assert status == {"Spring Week, Markets": "pending", "HackerRank OA": "approved"}

        # nothing booked at all -> a clear error and the item stays pending
        r = c.post("/api/opportunities/approve", json=[sw["id"]], headers=h)
        assert r.status_code == 422 and "No free slot" in r.json()["detail"]
        assert c.post("/api/opportunities/approve", json=[oa["id"]], headers=h).status_code == 400  # already approved


def test_database_recreated_if_data_folder_deleted():
    import shutil
    with TestClient(main.app) as c:
        h = signup(c, "gone@z.co")
        shutil.rmtree(db.DB_PATH.parent)  # what happens if you delete or replace backend/data while the server runs
        assert c.get("/api/profile", headers=h).status_code == 401  # old login no longer exists: sign in again, no 500
        assert db.DB_PATH.exists()
        assert signup(c, "gone@z.co")  # and sign-up works again


def test_agent_failure_is_reported_with_its_real_reason(monkeypatch, caplog):
    @asynccontextmanager
    async def boom(user_id):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [KeyError("TAVILY_API_KEY")])
        yield

    monkeypatch.setattr(runner, "_mcp_tools", boom)
    with TestClient(main.app) as c:
        h = signup(c, "fail@z.co")
        c.put("/api/profile", json=dict(name="Yash Ghanate", university="University of Oxford", degree="Mathematics",
                                        graduation_year=2029, location="London", interests=[]), headers=h)
        c.post("/api/profile/cv", files={"file": ("cv.pdf", b"%PDF-1.4 x")}, headers=h)
        db.save_google_token(db.get_user_by_email("fail@z.co")["id"], "enc")
        r = c.post("/api/opportunities/search", json={"query": "", "categories": ["internship"]}, headers=h)
        assert r.status_code == 502
        assert "KeyError" in r.json()["detail"] and "TAVILY_API_KEY" in r.json()["detail"], r.json()
        assert "Agent run failed" in caplog.text  # and the traceback is logged for the terminal


def test_search_needs_api_keys(monkeypatch):
    with TestClient(main.app) as c:
        h = signup(c, "keys@z.co")
        c.put("/api/profile", json=dict(name="Yash Ghanate", university="University of Oxford", degree="Mathematics",
                                        graduation_year=2029, location="London", interests=[]), headers=h)
        c.post("/api/profile/cv", files={"file": ("cv.pdf", b"%PDF-1.4 x")}, headers=h)
        db.save_google_token(db.get_user_by_email("keys@z.co")["id"], "enc")
        monkeypatch.delenv("ANTHROPIC_API_KEY")
        r = c.post("/api/opportunities/search", json={"query": "", "categories": ["internship"]}, headers=h)
        assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]
