# Deploy on AWS EC2 (t2.medium) — beginner step-by-step

Approved plan: run the app (Docker) on an EC2 **t2.medium** (2 vCPU, 4 GB RAM —
enough for the browser login step). Keep using **Supabase** for the database +
login (do NOT migrate to RDS — the app's authentication is built on Supabase).

You'll do this once; redeploys later are just 3 commands (see the end).

---

## Phase A — Launch the server (AWS Console, all clicks)
1. Sign in at https://console.aws.amazon.com → search **EC2** → open it.
2. Top-right: set your **Region** to the one closest to you (e.g. **Mumbai / ap-south-1**).
3. Click **Launch instance**.
4. **Name:** `meltwater-tagger`.
5. **OS image (AMI):** choose **Ubuntu Server 22.04 LTS** (free-tier eligible label is fine).
6. **Instance type:** pick **t2.medium**. (Search it in the box.)
7. **Key pair:** click **Create new key pair** → name `meltwater-key` → type **RSA**,
   format **.pem** → **Create**. A `meltwater-key.pem` file downloads — **save it safely,
   you can't re-download it.**
8. **Network settings** → **Edit**:
   - **Allow SSH (port 22)** → source **My IP** (so only you can log in).
   - Click **Add security group rule** → Type **HTTP (port 80)** → source **Anywhere (0.0.0.0/0)**
     (the app has its own login, so this is fine).
9. **Configure storage:** change the disk from 8 to **30 GB** (the Playwright image needs room).
10. Click **Launch instance** → then **View all instances**. Wait until **Instance state =
    Running**, then click it and copy its **Public IPv4 address** (e.g. `13.234.x.x`).

---

## Phase B — Connect to the server
Open **Git Bash** on your PC, go to where the `.pem` downloaded (usually Downloads):

```bash
cd ~/Downloads
chmod 400 meltwater-key.pem
ssh -i meltwater-key.pem ubuntu@<PUBLIC_IP>
```
Type **yes** if asked to trust the host. You're now on the server (prompt shows `ubuntu@...`).

---

## Phase C — Install Docker (on the server)
```bash
sudo apt-get update
sudo apt-get install -y docker.io git
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
```
Then log out and back in so Docker works without sudo:
```bash
exit
ssh -i meltwater-key.pem ubuntu@<PUBLIC_IP>
docker --version    # should print a version
```

---

## Phase D — Get the code and build
Clone your GitHub repo (use your repo's HTTPS URL from GitHub → green **Code** button).
If the repo is **private**, when git asks for a password, paste a GitHub **Personal
Access Token** (GitHub → Settings → Developer settings → Tokens), not your password.

```bash
git clone <YOUR_REPO_URL>
cd */meltwater_tagger     # or: cd <repo-folder>/meltwater_tagger
docker build -t meltwater-tagger .
```
The build takes a few minutes (it downloads the Playwright image).

---

## Phase E — Add your secrets and run
Create the env file (same values you used on Render/Supabase):
```bash
nano app.env
```
Paste this, filling in your real values, then save (Ctrl+O, Enter, Ctrl+X):
```
ANTHROPIC_API_KEY=sk-ant-...
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_ANON_KEY=...
SUPABASE_SERVICE_ROLE_KEY=...
MELTWATER_ALLOW_CDP=false
MELTWATER_USE_API=true
```
Lock it down and run the container (host port 80 → app):
```bash
chmod 600 app.env
docker run -d --name tagger --restart unless-stopped --env-file app.env -p 80:7860 meltwater-tagger
```

---

## Phase F — Test
Open in your browser:  `http://<PUBLIC_IP>`
Log in, classify a couple of URLs, run one **Apply to Meltwater**.

Check logs any time:
```bash
docker logs -f tagger
```

The `--restart unless-stopped` flag means the app comes back automatically if the
server reboots.

---

## Redeploying after code changes (later)
```bash
ssh -i meltwater-key.pem ubuntu@<PUBLIC_IP>
cd */meltwater_tagger
git pull
docker build -t meltwater-tagger .
docker rm -f tagger
docker run -d --name tagger --restart unless-stopped --env-file app.env -p 80:7860 meltwater-tagger
```

---

## Optional later: custom domain + HTTPS
Point a domain's A-record at the Public IP, then put Caddy or Nginx in front for a
free Let's Encrypt certificate. Not required for the app to work — do this only if
management wants a branded `https://tagger.yourcompany.com` URL.

## Note: keep the Public IP stable
A stopped/started EC2 instance gets a NEW public IP. To keep a fixed address,
allocate an **Elastic IP** (EC2 → Elastic IPs → Allocate → Associate to the
instance). Free while associated to a running instance.
