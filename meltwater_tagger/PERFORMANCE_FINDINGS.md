# Why the tagger is slow, and why these issues keep coming back

Analysis of `main` @ 1796465, 2026-10-08. Written after Ritu reported "1 min to tag 1 post".

---

## TL;DR

**~30–60 seconds per post is the current DESIGN, not a bug.** Extended thinking was
deliberately turned on to improve judgment. The only thing that makes a batch fast is
how many posts run **at the same time** — and that number is small.

So the batch time is roughly:

```
total ≈ (posts ÷ posts_in_flight) × 30-60s
```

116 posts with 8 in flight ≈ **15 minutes**. That is exactly what everyone is seeing.

Three things to check/do, in order of effort:

1. **Confirm the running container actually has PR #45** (merged Oct 5). If production
   wasn't rebuilt since, Ritu is still on the old sequential code and nothing we merged
   is live. Cheapest possible win — verify before anything else.
2. **Back-port the Bentley latency guards to the sentiment path** (§1). Sentiment has
   *no timeout and no thinking cap*; Bentley already fixed this and it was never copied over.
3. **Turn on prompt caching** (§3). Nothing in the codebase uses it today.

---

## 1. The sentiment path has NO latency ceiling  ← most likely cause of "1 min per post"

The two classify paths were built at different times and never converged:

| | Sentiment (Kaseya / N-Able / Ninja) | Taxonomy (Bentley) |
|---|---|---|
| client | `AsyncAnthropic()` — **no timeout** | `Anthropic(timeout=150)` |
| thinking | `{"type": "adaptive"}` — **unbounded** | `budget = 2000` tokens |
| max_tokens | **16000** | 8000 |
| on a stuck call | blocks (SDK default is **600s**, ×2 retries) | fails at 150s → row flagged for review |

`webapp/classify_web.py:154-165` vs `brands/bentley/classify_bentley.py:38-41,548-554`.

Adaptive thinking scales against `max_tokens`, so a 16000 ceiling lets the model think for
a long time on what is a one-line decision. And because a chunk finishes only when its
**slowest** post finishes, one deep-thinking post paces the other seven.

**Bentley already solved this.** The guards simply never made it back to the older path.

---

## 2. Per-call latency is intentional — so only concurrency moves the needle

`classify_bentley.py:33-39` says it outright:

> *"Extended thinking makes the model WORK THROUGH the protocol/rules instead of
> pattern-matching on keywords — the single biggest lever on judgment adherence. It was
> disabled earlier purely for speed; a bounded budget keeps each call to ~30-60s."*

That was a deliberate accuracy-over-speed trade. It means **you cannot make a single post
fast without giving back judgment quality.** The lever is how many run concurrently:

- `MELTWATER_CLASSIFY_CHUNK_SIZE` (8) — posts per request
- `MELTWATER_CLASSIFY_PARALLEL` (3) — requests in flight  *(added in #45)*
- `MELTWATER_CLASSIFY_CONCURRENCY` (8) — posts in flight per request

Chunk size is capped by **nginx's `proxy_read_timeout`** (default **60s**) — which is why
chunks are only 8, and why they're already brushing that limit. Raising chunk size without
raising the nginx timeout produces 502/504s.

---

## 3. Prompt caching is not used anywhere

Every post re-sends the full system prompt, uncached:

- Kaseya: **5,944 chars** (~1,500 tokens) per post
- Bentley: **29,198 chars** (~7,300 tokens) per article

The system prompt is identical for every post in a run, so it is the textbook case for a
cache breakpoint — it cuts input latency and input cost on every call after the first.
The cost doc already flagged this as "worth implementing once volume grows". Volume grew.

---

## 4. The structural reason these issues keep recurring

Every incident in this project traces back to the same root:

> **All heavy work runs inside a synchronous HTTP request.**

Classification batches, Playwright apply, SSO login with SMS OTP — all of it happens
inside a Flask request, behind nginx's 60s timeout, on a single gunicorn worker.

Everything downstream is a workaround for that one choice:

| Symptom we hit | Actually caused by |
|---|---|
| "Unexpected token '<'" / 502 | request outlived the gateway timeout |
| chunking into 8s | working around that timeout |
| 15-minute batches | those chunks then ran sequentially |
| site unusable while tagging | long job occupied the only worker |
| `Lock is bound to a different event loop` | a fresh event loop per request |
| "it may still be processing — retry" | no job state, so the client can't tell |

**The fix for the whole class is a background job queue**: submit → get a job id → poll for
progress. That removes the chunking hack, the timeout risk, worker starvation, the
partial-results complexity, and gives Ritu a real progress bar instead of a spinner.

---

## 5. Failures are silent — which is why they surface days later via Ritu

The system degrades quietly instead of failing loudly:

- `APIFY_TOKEN` missing → silently falls back to Reddit RSS, which is **~200× slower**
  (one WARNING line, mid-log). This is what happened on Jagrati's machine.
- An LLM error → the row becomes `action: "review"` — indistinguishable from a genuine
  judgment call.
- The Meltwater tagging API returns **202 Accepted and silently drops** the tag if
  `matchSentence`/`keywords` are missing, the document id is quoted, or the token is the
  wrong one.
- An expired Meltwater session surfaces as a generic failure.

Nothing alerts. The first signal is an analyst saying "it's slow" or "it didn't tag",
days later, with no way to tell which stage failed.

#45 added per-stage timings and a slow-run warning. That's a start, not a monitoring story.

---

## 6. No CI at all

There is **no `.github/workflows`**. Nothing compiles, imports or tests the app on push.
A syntax error from a bad merge reached `main` and took production down for a day
(`_classify_bentley_urls` lost its body, docstring left unterminated).

One job running `python -m compileall` + importing the app would have blocked it.

---

## 7. Divergent duplicate paths

- **two** classify pipelines (sentiment, taxonomy)
- **three** apply paths (document-ID API, browser feed-walk, session injection)
- **two** fetch stacks (Reddit: apify/RSS/OAuth/cookie/CDP — and Bentley: trafilatura/playwright)

Fixes land in one and not the other. §1 is exactly that: the timeout and thinking-budget
guards exist in Bentley only. This is why bugs feel like they "come back" — they were only
ever fixed on one side.

---

## Suggested order

| # | Action | Effort | Expected |
|---|---|---|---|
| 0 | Verify prod is running #45 (rebuild if not) | minutes | up to 3× if it was never deployed |
| 1 | Back-port timeout + thinking budget to sentiment (§1) | small | caps worst-case per post |
| 2 | Prompt caching (§3) | small | lower latency + cost on every call |
| 3 | Raise nginx `proxy_read_timeout`, then raise chunk size (§2) | small | fewer round trips & Apify runs |
| 4 | CI: compile + import on push (§6) | ~30 min | prevents another day-long outage |
| 5 | Background job queue (§4) | large | removes the whole class of timeout/starvation bugs |

1–4 are a day's work and address the symptoms. **5 is the one that stops this recurring.**
