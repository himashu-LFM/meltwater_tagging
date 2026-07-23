# Deploy to Hugging Face Spaces (free, 16 GB RAM, no credit card)

Best free host for this app: 16 GB RAM (Chromium login never OOMs), a persistent
container (long tagging jobs finish), a secrets manager, and the Playwright base
image ships all of Chromium's system libraries. Free Spaces sleep after ~48 h of
inactivity and wake on the next visit.

## 1. Create the Space
1. Sign up / log in at https://huggingface.co
2. Top-right **+ New** → **Space**.
3. Name it (e.g. `meltwater-tagger`), **License**: any, **SDK**: choose **Docker**
   → **Blank**. Set visibility to **Private** (recommended — it holds logins).
4. Create the Space. It gives you a git repo URL.

## 2. Add the Space metadata
Hugging Face reads a YAML header at the top of the Space's `README.md`. Create a
`README.md` in the Space repo (separate from this project's README) starting with:

```
---
title: Meltwater Tagger
emoji: 🏷️
colorFrom: yellow
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
---
```

`app_port: 7860` must match the Dockerfile's `EXPOSE`/`PORT`.

## 3. Push the app
Put the **contents of the `meltwater_tagger/` folder** at the **root** of the Space
repo (so `Dockerfile`, `requirements.txt`, `webapp/`, etc. sit at the top level):

```bash
git clone https://huggingface.co/spaces/<you>/meltwater-tagger
cd meltwater-tagger
# copy everything from this project's meltwater_tagger/ into here,
# including the Dockerfile and the README.md header above
git add .
git commit -m "Deploy Meltwater tagger"
git push
```

The Space auto-builds from the Dockerfile.

## 4. Set secrets (NOT in the repo)
In the Space: **Settings → Variables and secrets → New secret**. Add each as a
**Secret** (they become env vars in the container):

- `ANTHROPIC_API_KEY`
- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`
- `SUPABASE_SERVICE_ROLE_KEY`
- `MELTWATER_ALLOW_CDP` = `false`   (no local Chrome on a server)
- `MELTWATER_USE_API` = `true`      (memory-safe API apply path)

After adding secrets, **Restart** the Space (Settings → Factory reboot) so they load.

## 5. Verify
- Open the Space URL → the login page should render.
- Log in, classify a few URLs (no browser needed for classify).
- Run **Apply to Meltwater** on one post; watch **Logs** in the Space for
  `apply-api: tagged … -> …`. The browser only opens briefly for login, then
  everything is HTTP.

## Notes
- Same Dockerfile works on Fly.io / Google Cloud Run / Railway / a VM if you ever
  move — only the "how to set env vars + which port" differs per host.
- If you keep Render too: it may now fit in 512 MB thanks to the API apply path
  (feed is no longer rendered). Keep `MELTWATER_USE_API=true` and 1 gunicorn
  worker. If Render's build log shows "Chromium did NOT launch" or you still see
  OOM/502 during Apply, use this Space instead.
