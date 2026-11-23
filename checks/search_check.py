import asyncio, os, sys
os.environ.update(TAVILY_API_KEY="x", ANTHROPIC_API_KEY="x")
sys.path.insert(0, "/home/claude/opp2/backend")
from datetime import datetime, timedelta, timezone
import server
fut = lambda d: (datetime.now(timezone.utc) + timedelta(days=d)).isoformat()
async def cv(p): return {"text": "cv"}
server.cv_to_text = cv
async def ok(u): return True
server.verify_url = ok
async def fp(u): return True, ""
server._fetch_page = fp
calls = []
async def tav(q, max_results=10):
    calls.append(q)
    if "apply" in q and "Barclays" in q: return [{"url": "https://search.barclays.com/spring", "title": "Barclays spring", "content": "Barclays spring week"}]
    return [{"url": f"https://list.com/{len(calls)}", "title": "Top spring weeks", "content": "Goldman Sachs, Barclays and JP Morgan"}]
server.tavily_search_opportunities = tav
C = server.Candidate
async def ask(messages, schema, model=None, max_tokens=8192):
    if schema is server.SearchPlan: return server.SearchPlan(queries=["spring week 2027 UK", "tech spring week 2027"])
    if schema is server.Candidates:
        if "run by these companies" in messages[0]["content"]:
            return server.Candidates(opportunities=[C(index=0, company="Barclays", title="Spring Week 2027", type="spring week", page_is_this_company_only=True, location="London", deadline=fut(20))])
        return server.Candidates(opportunities=[
            C(index=0, company="Goldman Sachs", title="Spring into Banking", type="spring week", page_is_this_company_only=True, location="London", deadline=fut(10)),
            C(index=0, company="Barclays", title="Spring Week", type="spring week", page_is_this_company_only=False, location="London"),
            C(index=0, company="Old Co", title="Spring Week", type="spring week", page_is_this_company_only=True, location="London", deadline=fut(-3)),
            C(index=1, company="Goldman Sachs", title="Spring into Banking", type="spring week", page_is_this_company_only=True, location="London", deadline=fut(10)),
            C(index=1, company="Bakery", title="Baking intern", type="internship", page_is_this_company_only=True, location="Leeds")])
    if schema is server.Scores:
        import json; items = json.loads(messages[1]["content"])
        return server.Scores(scores=[server.Score(index=i["index"], match="none" if i["company"] == "Bakery" else "strong", score=1 if i["company"] == "Bakery" else 9, reason="fit") for i in items])
server.ask = ask
out = asyncio.run(server.search_opportunities(cv_file_path="x", interests="finance, tech", eligibility_info={"graduation_year": 2028, "location": "London"}, query="", types=["spring week"]))
for o in out: print(o["company"], "|", o["title"], "|", o["url"], "|", o["score"], o["deadline_assumed"])
assert [o["company"] for o in out] == ["Goldman Sachs", "Barclays"], out
assert out[1]["url"] == "https://search.barclays.com/spring"
print("SEARCH CHECKS PASSED")
