import os, sys, base64
os.environ.update(TAVILY_API_KEY="x", ANTHROPIC_API_KEY="x")
sys.path.insert(0, "/home/claude/opp2/backend")
import server
sleeps = []
server.time_module.sleep = lambda s: sleeps.append(s)
fails = {}
class Req:
    def __init__(s, mid): s.mid = mid
    def execute(s, num_retries=0):
        n = fails.get(s.mid, 0)
        if n > 0:
            fails[s.mid] = n - 1
            raise RuntimeError("403 rateLimitExceeded")
        return {"id": s.mid}
class Svc:
    def users(s): return s
    def messages(s): return s
    def get(s, userId, id, **k): return Req(id)
ids = [str(i) for i in range(120)]
fails.update({"3": 2, "70": 1, "99": 9})   # 99 never works
out = server._fetch_many(Svc(), ids, format="full")
print(len(out), "of", len(ids), "| order kept:", [m["id"] for m in out][:4], "| backoff waits:", [x for x in sleeps if x >= 1][:6])
assert len(out) == 119 and "99" not in [m["id"] for m in out] and "3" in [m["id"] for m in out] and "70" in [m["id"] for m in out]
print("FETCH CHECKS PASSED")
