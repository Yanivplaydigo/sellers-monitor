# sellers.json monitor

Checks the sellers.json of 33 SSPs and demand partners once a day. When sellers are **added**, **removed** or **changed** (name, domain, seller type, confidential flag), it posts to Slack and updates a **web dashboard**.

Partners: Magnite (Rubicon Project), Xandr (AppNexus), TripleLift, OpenX, Equativ, PubMatic, Media.net, FreeWheel, Zeta Global, LoopMe, Epsilon (Conversant), Adform, Nexxen, Consumable, Criteo, Sonobi, Bright Mountain Media, Sovrn, OneTag, Sharethrough, Redmob, Verve, SmartyAds, CAS.ai, E-Planning, Improve Digital, InMobi, Ogury, Eskimi, Adsyield, Adagio, 33Across, Apester. Edit `config.json` to add or remove SSPs.

## What you get
- **Dashboard** (`docs/index.html`, served by GitHub Pages) shows:
  - each SSP's status, seller count, and last change
  - a searchable change log you can filter by SSP, change type, seller type and date
  - CSV download
  - a red banner for anything on your watch list
- **Slack message** after each run that found changes or problems, with a link to the dashboard.
- **Full log** in `reports/changes.csv`.

## Safety guards
- **First run** saves a baseline only (no flood of alerts).
- **Download fails** → the previous snapshot is kept and the SSP is marked "Download failed".
- **Seller count drops by more than 50%** → treated as a broken file and ignored.
- **Watch list** (`watch_domains` / `watch_names` in config.json: playdigo.com, "playdigo", "yaggdigo") → pinned at the top of Slack and the dashboard.

## Setup (about 15 minutes)

### 1. Create the repo
GitHub → New repository → name it `sellers-monitor`. Upload all files from this folder, including the hidden `.github` folder. Dragging and dropping the folder contents onto the "uploading an existing file" page works.

**Public or private?** GitHub Pages (the dashboard) is free only for **public** repos. Everything shown on the dashboard comes from public sellers.json files, and your Slack webhook stays an encrypted secret either way. If you'd rather keep the repo private, you can still log in to GitHub and open `reports/changes.csv` in the repo; GitHub shows it as a searchable table.

### 2. Slack
1. Go to api.slack.com/apps → **Create New App** → From scratch → pick your workspace.
2. Open **Incoming Webhooks** → turn it on → **Add New Webhook to Workspace** → choose the channel (e.g. #sellers-monitor).
3. Copy the webhook URL (`https://hooks.slack.com/services/...`).
4. In the repo, go to Settings → Secrets and variables → Actions → **New repository secret**. Name it `SLACK_WEBHOOK_URL` and paste the URL.

### 3. Turn on the dashboard
1. Repo → Settings → **Pages** → Source: "Deploy from a branch" → Branch `main`, folder **`/docs`** → Save.
2. After a minute it shows your address, e.g. `https://<your-user>.github.io/sellers-monitor/`.
3. Settings → Secrets and variables → Actions → **Variables** tab → New variable `DASHBOARD_URL` = that address. This adds an "Open dashboard" link to the Slack message.

### 4. Allow the job to save its log
Settings → Actions → General → Workflow permissions → **Read and write permissions** → Save.

### 5. First run
Actions tab → "sellers.json monitor" → **Run workflow**. The first run saves the baselines, and you'll get a Slack status message. After that it runs daily at 11:47 Bangkok time. You can also click Run workflow any time.

## Fixing an SSP that shows "Download failed"
Find the SSP's correct sellers.json link and put it first in that SSP's `urls` list in `config.json` (edit it right in GitHub).

## Running locally (optional)
```
python3 sellers_monitor.py                 # all SSPs
python3 sellers_monitor.py --only pubmatic # one SSP
python3 sellers_monitor.py --dry-run       # test without saving or notifying
```
