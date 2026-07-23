# Deploy to Google Cloud Run (free tier, ~2 GB RAM, runs the Dockerfile as-is)

Cloud Run runs your container, gives 2 GB RAM (Chromium login is easy there),
scales to zero (so $0 when idle), and the free tier covers an internal tool's
usage. A credit card is required for account verification; you won't be charged
within the free limits, and `--min-instances 0` + `--max-instances 1` keeps it
from ever scaling into paid territory.

## 1. Google Cloud account + project
1. Go to https://console.cloud.google.com and sign in / sign up.
2. Accept the free trial (asks for a card — verification only).
3. Top bar → project dropdown → **New Project** → name it `meltwater-tagger` → Create.
   Select that project once it's made.

## 2. Install the gcloud CLI (run from your own PC where the code lives)
- Windows installer: https://cloud.google.com/sdk/docs/install
- After install, open a NEW terminal and run:
  ```bash
  gcloud auth login
  gcloud config set project <YOUR_PROJECT_ID>
  ```
  (`<YOUR_PROJECT_ID>` is shown in the console project dropdown — often like
  `meltwater-tagger-123456`.)

## 3. Deploy (builds your Dockerfile in the cloud, no local Docker needed)
```bash
cd /c/Users/himan/OneDrive/Desktop/tagging_skill/meltwater_tagger

gcloud run deploy meltwater-tagger \
  --source . \
  --region asia-south1 \
  --memory 2Gi \
  --cpu 2 \
  --timeout 3600 \
  --min-instances 0 \
  --max-instances 1 \
  --allow-unauthenticated
```
- First run: it will offer to enable the Cloud Run, Cloud Build and Artifact
  Registry APIs — type **y**.
- `--region asia-south1` is Mumbai; pick the region closest to you.
- `--allow-unauthenticated` makes the URL reachable — the app still has its own
  Supabase login, exactly like on Render.
- When it finishes it prints a **Service URL** (https://meltwater-tagger-xxxxx.run.app).

## 4. Set the environment variables / secrets
Easiest via the console (avoids shell quoting issues):
1. Console → **Cloud Run** → click **meltwater-tagger** → **Edit & deploy new revision**.
2. Scroll to **Variables & Secrets** → **+ Add variable**, add each:
   - `ANTHROPIC_API_KEY`
   - `SUPABASE_URL`
   - `SUPABASE_ANON_KEY`
   - `SUPABASE_SERVICE_ROLE_KEY`
   - `MELTWATER_ALLOW_CDP` = `false`
   - `MELTWATER_USE_API` = `true`
3. Click **Deploy** (creates a new revision with the vars).

(The Dockerfile already listens on `$PORT`, which Cloud Run sets automatically.)

## 5. Test
1. Open the Service URL → login page loads.
2. Log in, classify 2-3 URLs (no browser needed for classify).
3. Run **Apply to Meltwater** on one post. In the console **Logs** tab you should
   see `apply-api: tagged … -> …` and no OOM.

## Redeploying after code changes
Just re-run the same `gcloud run deploy … --source .` command; env vars persist
across revisions.

## Cost guardrails
- `--min-instances 0` → the container fully stops when idle (you pay nothing while
  no one is using it).
- `--max-instances 1` → it can never fan out into many paid instances.
- Cloud Run free tier (per month): 2M requests, 360k GB-seconds, 180k vCPU-seconds
  — far more than an internal tagging tool uses.
