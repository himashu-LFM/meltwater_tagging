"""Regression tests for the Apify fetch path — the stage that is 85-93% of a run.

No network, no API keys, no Apify account: the actor is stubbed, so this runs
anywhere in about two seconds.

    python tests/test_apify_fetch.py        (from meltwater_tagger/)

The stub models the behaviour actually observed in the Apify console, which is
the whole reason this code is shaped the way it is:

  * the search-based actor returns NOTHING for a comment permalink
  * a parent-thread scrape returns the post plus every comment in the thread

On a real 8-URL batch that meant two direct runs taking 59s and 86s and
returning 0 and 1 record(s), followed by three thread scrapes that did all the
real work. Hence: comments go straight to their thread, grouped and concurrent.

If you change fetch_via_apify, run this first.
"""
import asyncio
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("APIFY_TOKEN", "test-token")
import config                                        # noqa: E402

config.APIFY_TOKEN = "test-token"
config.APIFY_RETRY_DELAY = 0
import classify                                      # noqa: E402

# ---------------------------------------------------------------- stub actor

runs = []                     # [{"kind": "direct"|"thread", "urls": [...]}]
peak = inflight = 0
THREAD_COMMENTS = {}          # post_id -> [comment_id, ...]
THREAD_OMITS = set()          # comment ids a thread scrape "loses"
FAIL_URLS = set()
DIRECT_RESOLVES_COMMENTS = False
THROTTLE_ONCE = {"n": 0}


class FakeResp:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._p, self.text = status, payload or [], text

    def json(self):
        return self._p


class FakeClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        global inflight, peak
        urls = json["urls"]
        kind = "thread" if json.get("scrapeComments") else "direct"
        runs.append({"kind": kind, "urls": list(urls)})
        inflight += 1
        peak = max(peak, inflight)
        try:
            # direct runs are the slow ones in production; keep that ordering
            await asyncio.sleep(0.9 if kind == "direct" else 0.3)
            if any(u in FAIL_URLS for u in urls):
                return FakeResp(500, text="boom")
            if THROTTLE_ONCE["n"] > 0:
                THROTTLE_ONCE["n"] -= 1
                return FakeResp(429, text="slow down")
            if kind == "direct":
                out = []
                for u in urls:
                    pid, cid = classify.reddit_ids(u)
                    if cid and not DIRECT_RESOLVES_COMMENTS:
                        continue                      # the real actor's behaviour
                    rec = {"query": u, "kind": "comment" if cid else "post",
                           "id": cid or pid, "title": f"TITLE {pid}"}
                    rec["body" if cid else "selftext"] = (
                        f"CBODY {cid}" if cid else f"BODY {pid}")
                    out.append(rec)
                return FakeResp(200, out)
            pid, _ = classify.reddit_ids(urls[0] + "/x/")
            pid = pid or urls[0].rstrip("/").split("/")[-1]
            recs = [{"kind": "post", "id": pid, "title": f"TITLE {pid}",
                     "selftext": f"BODY {pid}"}]
            recs += [{"kind": "comment", "id": c, "body": f"CBODY {c}"}
                     for c in THREAD_COMMENTS.get(pid, []) if c not in THREAD_OMITS]
            return FakeResp(200, recs)
        finally:
            inflight -= 1


classify.httpx = types.SimpleNamespace(AsyncClient=FakeClient)

# ------------------------------------------------------------------ helpers

FAILURES = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"   <- {extra}"))
    if not cond:
        FAILURES.append(name)


def reset():
    global runs, peak, inflight
    runs.clear()
    peak = inflight = 0
    THREAD_COMMENTS.clear()


def comment_urls(spec):
    """{post_id: [comment_id, ...]} -> mention dicts in the modern URL form."""
    out = []
    for pid, cids in spec.items():
        THREAD_COMMENTS[pid] = list(cids)
        out += [{"permalink": f"https://www.reddit.com/r/sysadmin/comments/{pid}"
                              f"/some_title/comment/{cid}/", "excerpt": ""}
                for cid in cids]
    return out


def post_urls(n, prefix="q"):
    return [{"permalink": f"https://www.reddit.com/r/s/comments/{prefix}{i}/title/",
             "excerpt": ""} for i in range(n)]


def run(posts):
    return asyncio.run(classify.fetch_via_apify(posts))


def resolved(posts):
    return sum(1 for p in posts if p.get("text"))


# -------------------------------------------------------------------- tests

config.APIFY_RUN_SIZE, config.APIFY_CONCURRENCY = 4, 8
config.APIFY_COMMENTS_VIA_THREAD = True

print("\n--- comment URLs are not sent to the doomed direct lookup ---")
reset()
posts = comment_urls({"p1": ["c1", "c2", "c3"], "p2": ["c4", "c5"],
                      "p3": ["c6", "c7", "c8"]})
t0 = time.monotonic()
out = run(posts)
elapsed = time.monotonic() - t0
check("no wasted direct runs", all(r["kind"] == "thread" for r in runs), runs)
check("8 mentions cost 3 scrapes, not 8", len(runs) == 3, len(runs))
check("all 8 resolved", resolved(out) == 8, [p.get("text") for p in out])
check("scrapes ran concurrently", peak == 3, f"peak={peak}")
check("wall clock is one scrape, not three", elapsed < 0.6, f"{elapsed:.2f}s")
check("judged on the COMMENT body", out[0]["comment_text"] == "CBODY c1",
      out[0].get("comment_text"))
check("parent post kept as context only", out[0]["post_text"] == "TITLE p1\n\nBODY p1",
      out[0].get("post_text"))
check("typed as a comment", all(p["content_type"] == "comment" for p in out))
check("upload order preserved", out[0]["permalink"].endswith("c1/")
      and out[-1]["permalink"].endswith("c8/"))

print("\n--- many mentions in one thread share a single scrape ---")
reset()
out = run(comment_urls({"pz": [f"c{i}" for i in range(8)]}))
check("8 mentions -> 1 scrape", len(runs) == 1, len(runs))
check("all 8 resolved from it", resolved(out) == 8)

print("\n--- post URLs keep using the direct path ---")
reset()
out = run(post_urls(6))
check("posts are never thread-scraped", all(r["kind"] == "direct" for r in runs), runs)
check("6 posts -> 2 runs (4 + 2)", sorted(len(r["urls"]) for r in runs) == [2, 4],
      [len(r["urls"]) for r in runs])
check("all posts resolved", resolved(out) == 6)
check("post text is title + body", out[0]["post_text"] == "TITLE q0\n\nBODY q0",
      out[0].get("post_text"))

print("\n--- mixed batches route each URL independently ---")
reset()
out = run(comment_urls({"m1": ["x1", "x2"]}) + post_urls(1, prefix="m9"))
check("one direct run and one thread scrape",
      sorted(r["kind"] for r in runs) == ["direct", "thread"], runs)
check("all 3 resolved", resolved(out) == 3)

print("\n--- a comment missing from its thread falls back to direct ---")
reset()
THREAD_OMITS.add("lost1")
DIRECT_RESOLVES_COMMENTS = True
out = run(comment_urls({"pp": ["ok1", "lost1"]}))
check("second pass tries the other route", any(r["kind"] == "direct" for r in runs), runs)
check("the missing comment is recovered", resolved(out) == 2,
      [p.get("text") for p in out])
THREAD_OMITS.clear()
DIRECT_RESOLVES_COMMENTS = False

print("\n--- nothing hangs when a mention cannot be resolved at all ---")
reset()
THREAD_OMITS.add("gone")
out = run(comment_urls({"pg": ["gone"]}))
check("returns cleanly, left unresolved", not out[0].get("text"), out[0].get("text"))
THREAD_OMITS.clear()

print("\n--- legacy /comments/<pid>/<slug>/<cid>/ URLs ---")
legacy = "https://www.reddit.com/r/sysadmin/comments/pold/some_title_here/cold9/"
check("thread URL cut right after the post id",
      classify._apify_thread_url(legacy)
      == "https://www.reddit.com/r/sysadmin/comments/pold",
      classify._apify_thread_url(legacy))
check("query string stripped",
      classify._apify_thread_url(
          "https://www.reddit.com/r/s/comments/pq/t/comment/cq/?utm=1")
      == "https://www.reddit.com/r/s/comments/pq")
reset()
THREAD_COMMENTS["pold"] = ["cold9"]
out = run([{"permalink": legacy, "excerpt": ""}])
check("legacy URL resolves via its thread", out[0].get("comment_text") == "CBODY cold9",
      out[0].get("comment_text"))

print("\n--- one failed run never takes the batch down with it ---")
reset()
posts = comment_urls({"f1": ["a1", "a2"], "f2": ["b1", "b2"]})
FAIL_URLS.add("https://www.reddit.com/r/sysadmin/comments/f1")
out = run(posts)
check("the other thread still resolves", resolved(out) == 2,
      [p.get("text") for p in out])
FAIL_URLS.clear()

print("\n--- throttling is retried once, not dropped ---")
reset()
THROTTLE_ONCE["n"] = 1
out = run(post_urls(4, prefix="th"))
check("all 4 resolved after the retry", resolved(out) == 4)
check("a second call was actually made", len(runs) == 2, len(runs))

print("\n--- concurrency cap is honoured ---")
reset()
config.APIFY_RUN_SIZE, config.APIFY_CONCURRENCY = 1, 3
out = run(post_urls(9, prefix="cc"))
check("never more than 3 runs in flight", peak == 3, f"peak={peak}")
check("all 9 still resolved", resolved(out) == 9)
config.APIFY_RUN_SIZE, config.APIFY_CONCURRENCY = 4, 8

print("\n--- APIFY_COMMENTS_VIA_THREAD=0 restores the old order ---")
config.APIFY_COMMENTS_VIA_THREAD = False
reset()
out = run(comment_urls({"k1": ["z1", "z2"]}))
check("direct is tried first again", runs[0]["kind"] == "direct", runs)
check("thread fallback still recovers them", resolved(out) == 2)
config.APIFY_COMMENTS_VIA_THREAD = True

print("\n--- no token / non-Reddit URLs are passed straight through ---")
reset()
config.APIFY_TOKEN = ""
out = run(post_urls(2, prefix="nt"))
check("no token -> untouched, no runs", not any(p.get("text") for p in out) and not runs)
config.APIFY_TOKEN = "test-token"
reset()
out = run([{"permalink": "https://example.com/news/1", "excerpt": ""}])
check("non-Reddit URL makes no actor run", not runs, runs)

print()
if FAILURES:
    print(f"FAILED: {FAILURES}")
    sys.exit(1)
print("ALL PASSED")
