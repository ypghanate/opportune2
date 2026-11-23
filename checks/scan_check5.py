import asyncio, base64, os, sys, threading
os.environ.update(TAVILY_API_KEY="x", ANTHROPIC_API_KEY="x")
sys.path.insert(0, "/home/claude/opp2/backend")
from datetime import datetime, timedelta, timezone
import inbox_scan as server
server.time_module.sleep = lambda s: None
now = datetime.now(timezone.utc)
ms = lambda d: str(int((now - timedelta(days=d)).timestamp() * 1000))
def msg(i, subj, days_ago, body="hello"):
    return {"id": str(i), "internalDate": ms(days_ago), "snippet": subj, "payload": {"headers": [{"name": "Subject", "value": subj}, {"name": "From", "value": "a@b.com"}, {"name": "Date", "value": "x"}], "mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}}}
msgs = {"0": msg(0, "Your SHL test", 2, "complete your online assessment"), "1": msg(1, "Newsletter", 1), "2": msg(2, "Old HireVue", 20, "video interview"), "3": msg(3, "Your next step", 3, "Please click the link"), "4": msg(4, "Receipt", 4)}
formats, threads = [], set()
class Ex:
    def __init__(s, v): s.v = v
    def execute(s, **kw): return s.v
class Svc:
    def users(s): return s
    def messages(s): return s
    def list(s, **k):
        ids = ["0", "1", "3", "4"] if "-category:promotions" in k["q"] else ["0", "2"]
        return Ex({"messages": [{"id": i} for i in ids]})
    def get(s, userId, id, format="full", **k): formats.append((id, format)); threads.add(threading.get_ident()); return Ex(msgs[id])
server.get_gmail_service = lambda: Svc()
async def flag(batch): return [i for i, e in enumerate(batch) if e["subject"] == "Your next step"]
seen = []
async def decide(batch):
    seen.extend(e["subject"] for e in batch)
    return [server.EmailDecision(index=i, needs_action=True, title=e["subject"], kind="assessment", date=None, date_basis="none", evidence="x") for i, e in enumerate(batch)]
server.flag_candidates = flag; server.decide_dates = decide
items = asyncio.run(server.scan_inbox_semantically.fn() if hasattr(server.scan_inbox_semantically, "fn") else server.scan_inbox_semantically())
print("fetches:", sorted(formats))
print("model saw:", sorted(seen), "| threads used:", len(threads))
print([ (i["title"], i["deadline"][:10]) for i in items])
assert sorted(seen) == ["Old HireVue", "Your SHL test", "Your next step"]          # newsletter and receipt never reach the big model
assert ("0", "full") in formats and ("1", "metadata") in formats and ("3", "metadata") in formats and ("3", "full") in formats
assert ("1", "full") not in formats and ("4", "full") not in formats                 # unflagged ones are never downloaded in full
assert [i["title"] for i in items] == ["Your SHL test", "Your next step"]           # Old HireVue: sent 20d ago + 7d is past
print("SPEED-PATH CHECKS PASSED")
