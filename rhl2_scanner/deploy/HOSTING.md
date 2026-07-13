# No-terminal hosting — run the scanner 24/7 from a website

Both options below deploy straight from GitHub with a web UI. No SSH, no server
logins, no commands. They read the Docker image in this repo, so there's nothing
to configure except your two Telegram values. Expect ~$5/month (there's no truly
free always-on tier that can reach arbitrary RPC/DexScreener).

The scanner starts in **paper (calibration) mode** and posts a nightly digest to
your channel. To switch to live alerts later, change the start command's `paper`
to `run` in the host's dashboard.

---

## Option A — Railway (smoothest, trial credit)

1. Go to **railway.app** → **Login with GitHub**.
2. **New Project** → **Deploy from GitHub repo** → authorize Railway to see your
   repos → pick **vida-fork**.
3. Railway detects `railway.json` and builds from the Docker image automatically.
4. Open the service → **Variables** tab → **New Variable**, add these two:
   - `TELEGRAM_BOT_TOKEN` = your bot token
   - `TELEGRAM_ALERT_CHAT_ID` = `-1004299219898`
5. **Settings** → **Branch** = `claude/rh-l2-memecoin-scanner-7sj2qa` (if not already).
6. It redeploys. Open **Deploy Logs** — you'll see the scan cycles, and the first
   message/digest lands in your channel.

To go live: **Settings → Deploy → Custom Start Command** →
`python -m rhl2_scanner run --config rhl2_scanner/config/robinhood.example.yaml`

---

## Option B — Render (Blueprint form)

1. Go to **render.com** → sign up / **Login with GitHub**.
2. **New +** → **Blueprint** → connect your **vida-fork** repo.
3. Render reads `render.yaml` and shows a form → paste the two values it asks for
   (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALERT_CHAT_ID` = `-1004299219898`) → **Apply**.
4. It builds the Docker image and starts the worker. Watch **Logs**.

To go live: the worker's **Settings** → **Docker Command** →
`python -m rhl2_scanner run --config rhl2_scanner/config/robinhood.example.yaml`

---

## Notes
- **Private repo?** Both hosts ask GitHub for access when you connect — approve it
  and private works fine.
- **Secrets** live only in the host's dashboard, never in the repo.
- **Database** (seen tokens / paper trades) resets on redeploy unless you attach a
  persistent volume/disk — fine for calibration; add a disk later if you want the
  history to survive deploys.
- **Calibration report:** in the host's shell/console tab (both have a web one),
  run `python -m rhl2_scanner paper-report --config rhl2_scanner/config/robinhood.example.yaml`.
