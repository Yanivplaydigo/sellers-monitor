#!/usr/bin/env python3
"""
sellers.json change monitor.

For every SSP in config.json:
  1. download its sellers.json (tries each candidate URL until one is valid)
  2. compare with the last saved snapshot
  3. report sellers that were ADDED, REMOVED, or CHANGED (name / domain / type / confidential)
  4. send the report to Slack and/or email, and append every change to reports/changes.csv

The first run for an SSP only saves a baseline (no alert flood).
If a download fails, or the seller count drops by more than max_drop_ratio,
the old snapshot is kept and the report shows a warning instead of a fake "mass removal".

No third-party packages needed (Python 3.9+).

Notifications (all optional, set as environment variables):
  SLACK_WEBHOOK_URL                       -> Slack incoming webhook
  SMTP_HOST, SMTP_PORT, SMTP_USER,
  SMTP_PASS, EMAIL_FROM, EMAIL_TO         -> email (EMAIL_TO may be comma-separated)

Usage:
  python3 sellers_monitor.py                 # run all SSPs
  python3 sellers_monitor.py --only pubmatic openx
  python3 sellers_monitor.py --dry-run       # don't save snapshots / don't notify
"""

import argparse
import csv
import datetime as dt
import gzip
import json
import os
import smtplib
import sys
import time
import urllib.request
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
REPORT_DIR = ROOT / "reports"
FIELDS = ("name", "domain", "seller_type", "is_confidential")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 sellers-json-monitor")


# ---------------------------------------------------------------- fetching
def fetch_json(url, timeout=240):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json,*" "/*", "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
    data = json.loads(raw.decode("utf-8-sig", errors="replace"))
    if not isinstance(data, dict) or not isinstance(data.get("sellers"), list):
        raise ValueError("no 'sellers' array")
    return data


def fetch_ssp(ssp, preferred=None):
    urls = list(ssp["urls"])
    if preferred and preferred in urls:          # last URL that worked goes first
        urls.remove(preferred)
        urls.insert(0, preferred)
    errors = []
    for attempt in range(2):                     # second pass retries transient failures
        if attempt:
            time.sleep(8)
            errors = []
        for url in urls:
            try:
                return url, fetch_json(url), errors
            except Exception as e:               # noqa: BLE001
                errors.append(f"{url}: {type(e).__name__}: {e}"[:300])
    return None, None, errors


def normalize(data):
    """seller_id -> sorted list of unique records (some files repeat an id)."""
    out = {}
    for s in data["sellers"]:
        if not isinstance(s, dict) or s.get("seller_id") in (None, ""):
            continue
        sid = str(s["seller_id"]).strip()
        rec = [
            str(s.get("name") or "").strip(),
            str(s.get("domain") or "").strip().lower(),
            str(s.get("seller_type") or "").strip().upper(),
            1 if s.get("is_confidential") in (1, True, "1") else 0,
        ]
        out.setdefault(sid, [])
        if rec not in out[sid]:
            out[sid].append(rec)
    for sid in out:
        out[sid].sort()
    return out


# ---------------------------------------------------------------- state
def state_path(key):
    return STATE_DIR / f"{key}.json.gz"


def load_state(key):
    p = state_path(key)
    if not p.exists():
        return None
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return json.load(f)


def save_state(key, url, sellers):
    STATE_DIR.mkdir(exist_ok=True)
    with gzip.open(state_path(key), "wt", encoding="utf-8") as f:
        json.dump({"url": url, "fetched_at": now_iso(), "sellers": sellers}, f,
                  separators=(",", ":"))


# ---------------------------------------------------------------- diff
def diff(old, new):
    added = [(sid, new[sid]) for sid in new.keys() - old.keys()]
    removed = [(sid, old[sid]) for sid in old.keys() - new.keys()]
    changed = []
    for sid in old.keys() & new.keys():
        if old[sid] != new[sid]:
            changed.append((sid, old[sid], new[sid]))
    key = lambda t: t[0]
    return sorted(added, key=key), sorted(removed, key=key), sorted(changed, key=key)


def describe(recs):
    r = recs[0]
    conf = " (confidential)" if r[3] else ""
    extra = f" +{len(recs) - 1} more" if len(recs) > 1 else ""
    return f"{r[0] or '—'} | {r[1] or '—'} | {r[2] or '—'}{conf}{extra}"


def change_details(old, new):
    if len(old) == 1 and len(new) == 1:
        parts = []
        for i, f in enumerate(FIELDS):
            if old[0][i] != new[0][i]:
                parts.append(f"{f}: {old[0][i]!r} → {new[0][i]!r}")
        return "; ".join(parts)
    return f"{describe(old)}  →  {describe(new)}"


def is_watched(recs, cfg):
    doms = [d.lower() for d in cfg.get("watch_domains", [])]
    names = [n.lower() for n in cfg.get("watch_names", [])]
    for r in recs:
        if any(r[1] == d or r[1].endswith("." + d) for d in doms):
            return True
        if any(n in r[0].lower() for n in names):
            return True
    return False


def cap_for_log(items, cfg, recs_of):
    """Keep every watch-list row plus the first N others (huge files like Google churn a lot)."""
    n = cfg.get("max_logged_per_run", 500)
    mine = [t for t in items if is_watched(recs_of(t), cfg)]
    rest = [t for t in items if not is_watched(recs_of(t), cfg)]
    return mine + rest[:n]


def find_ours(sellers, cfg):
    """Our own entries (watch list) in an SSP's sellers.json."""
    out = []
    for sid, recs in sellers.items():
        for rec in recs:
            if is_watched([rec], cfg):
                out.append({"id": sid, "name": rec[0], "domain": rec[1], "type": rec[2]})
    return sorted(out, key=lambda o: o["id"])


# ---------------------------------------------------------------- reporting
def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_report(results, cfg):
    limit = cfg.get("max_items_per_list", 15)
    today = dt.date.today().isoformat()
    lines, summary_rows, watched = [], [], []
    any_change = any_warning = False

    for r in results:
        name = r["ssp"]["name"]
        if r["status"] == "error":
            any_warning = True
            summary_rows.append(f"⚠️ {name}: could not download sellers.json")
            lines.append(f"\n### ⚠️ {name}\nDownload failed — kept previous snapshot.\n"
                         + "\n".join(f"- {e}" for e in r["errors"]))
            continue
        if r["status"] == "anomaly":
            any_warning = True
            summary_rows.append(f"⚠️ {name}: seller count dropped {r['old_count']:,} → "
                                f"{r['new_count']:,} — ignored, looks like a bad file")
            continue
        if r["status"] == "baseline":
            summary_rows.append(f"🆕 {name}: baseline saved ({r['new_count']:,} sellers) — {r['url']}")
            continue

        a, rm, ch = r["added"], r["removed"], r["changed"]
        if not (a or rm or ch):
            summary_rows.append(f"✅ {name}: no changes ({r['new_count']:,} sellers)")
            continue
        any_change = True
        summary_rows.append(f"🔔 {name}: +{len(a)} added, −{len(rm)} removed, "
                            f"~{len(ch)} changed ({r['old_count']:,} → {r['new_count']:,})")
        lines.append(f"\n### {name}")
        for title, items, fmt in (
            ("Added", a, lambda t: f"`{t[0]}` {describe(t[1])}"),
            ("Removed", rm, lambda t: f"`{t[0]}` {describe(t[1])}"),
            ("Changed", ch, lambda t: f"`{t[0]}` {change_details(t[1], t[2])}"),
        ):
            if not items:
                continue
            lines.append(f"*{title} ({len(items)})*")
            for t in items[:limit]:
                lines.append(f"• {fmt(t)}")
            if len(items) > limit:
                lines.append(f"• …and {len(items) - limit} more (see reports/changes.csv)")
        for kind, items in (("ADDED", a), ("REMOVED", rm)):
            for sid, recs in items:
                if is_watched(recs, cfg):
                    watched.append(f"{kind} on {name}: `{sid}` {describe(recs)}")
        for sid, o, n in ch:
            if is_watched(o + n, cfg):
                watched.append(f"CHANGED on {name}: `{sid}` {change_details(o, n)}")

    header = f"*sellers.json monitor — {today}*"
    body = [header, ""]
    if watched:
        body += ["🚨 *Changes touching your watch list*"] + [f"• {w}" for w in watched] + [""]
    body += summary_rows
    body += lines
    return "\n".join(body), any_change, any_warning, bool(watched)


def append_csv(results):
    REPORT_DIR.mkdir(exist_ok=True)
    p = REPORT_DIR / "changes.csv"
    new_file = not p.exists()
    date = dt.date.today().isoformat()
    with open(p, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["date", "ssp", "change", "seller_id", "name", "domain",
                        "seller_type", "is_confidential", "details"])
        for r in results:
            if r["status"] != "ok":
                continue
            ssp = r["ssp"]["name"]
            for kind, items in (("added", r["added_log"]), ("removed", r["removed_log"])):
                for sid, recs in items:
                    for rec in recs:
                        w.writerow([date, ssp, kind, sid, *rec, ""])
            for sid, o, n in r["changed_log"]:
                rec = n[0]
                w.writerow([date, ssp, "changed", sid, *rec, change_details(o, n)])


# ---------------------------------------------------------------- dashboard data
DOCS_DATA = ROOT / "docs" / "data"


def _read_json(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                        # noqa: BLE001
        return default


def write_dashboard(results, cfg):
    """Feed docs/index.html: status.json (one row per SSP) + changes.json (history)."""
    DOCS_DATA.mkdir(parents=True, exist_ok=True)
    ts = now_iso()
    date = dt.date.today().isoformat()

    old_status = {s["key"]: s for s in _read_json(DOCS_DATA / "status.json", {}).get("ssps", [])}
    status = []
    for r in results:
        k = r["ssp"]["key"]
        prev = old_status.get(k, {})
        row = {
            "key": k, "name": r["ssp"]["name"], "status": r["status"],
            "url": r.get("url") or prev.get("url"),
            "count": r.get("new_count", prev.get("count")),
            "last_checked": ts,
            "last_ok": ts if r["status"] in ("ok", "baseline") else prev.get("last_ok"),
            "last_change": prev.get("last_change"),
            "added": 0, "removed": 0, "changed": 0,
            "error": "; ".join(r["errors"]) if r["status"] == "error" else
                     (f"seller count dropped {r.get('old_count')} → {r.get('new_count')} (ignored)"
                      if r["status"] == "anomaly" else ""),
        }
        row["ours"] = r["ours"] if "ours" in r else prev.get("ours")
        if r["status"] == "ok":
            row.update(added=len(r["added"]), removed=len(r["removed"]), changed=len(r["changed"]))
            if r["added"] or r["removed"] or r["changed"]:
                row["last_change"] = ts
        status.append(row)
    # keep SSPs that weren't part of this run (e.g. --only)
    ran = {s["key"] for s in status}
    order = [s["key"] for s in cfg["ssps"]]
    status += [s for k, s in old_status.items() if k not in ran and k in order]
    status.sort(key=lambda s: order.index(s["key"]) if s["key"] in order else 999)
    (DOCS_DATA / "status.json").write_text(json.dumps(
        {"generated_at": ts, "ssps": status}, ensure_ascii=False, indent=1), encoding="utf-8")

    hist = _read_json(DOCS_DATA / "changes.json", [])
    for r in results:
        if r["status"] != "ok":
            continue
        ssp = r["ssp"]["name"]
        def row(kind, sid, rec, details, watched):
            return {"date": date, "ssp": ssp, "change": kind, "seller_id": sid,
                    "name": rec[0], "domain": rec[1], "seller_type": rec[2],
                    "confidential": rec[3], "details": details, "watched": watched}
        for kind, items in (("added", r["added_log"]), ("removed", r["removed_log"])):
            for sid, recs in items:
                w = is_watched(recs, cfg)
                hist += [row(kind, sid, rec, "", w) for rec in recs]
        for sid, o, n in r["changed_log"]:
            hist.append(row("changed", sid, n[0], change_details(o, n), is_watched(o + n, cfg)))
    cutoff = (dt.date.today() - dt.timedelta(days=cfg.get("dashboard_history_days", 180))).isoformat()
    hist = [h for h in hist if h["date"] >= cutoff]
    (DOCS_DATA / "changes.json").write_text(
        json.dumps(hist, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


# ---------------------------------------------------------------- notify
def notify_slack(text):
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        return False
    chunks, cur = [], ""
    for line in text.split("\n"):            # Slack caps a message ~40k chars; stay well below
        if len(cur) + len(line) > 3500:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for c in chunks:
        req = urllib.request.Request(url, data=json.dumps({"text": c}).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30).read()
    return True


def notify_email(subject, text):
    host, to = os.environ.get("SMTP_HOST"), os.environ.get("EMAIL_TO")
    if not (host and to):
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("EMAIL_FROM") or os.environ.get("SMTP_USER", "")
    msg["To"] = to
    msg.set_content(text.replace("*", "").replace("`", ""))
    csv_path = REPORT_DIR / "changes.csv"
    if csv_path.exists():
        msg.add_attachment(csv_path.read_bytes(), maintype="text", subtype="csv",
                           filename="changes.csv")
    port = int(os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=60) as s:
        s.starttls()
        if os.environ.get("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
        s.send_message(msg)
    return True


# ---------------------------------------------------------------- main
def run(args):
    cfg = json.loads((ROOT / "config.json").read_text())
    ssps = [s for s in cfg["ssps"] if not args.only or s["key"] in args.only]
    results = []
    for ssp in ssps:
        prev = load_state(ssp["key"])
        print(f"→ {ssp['name']} …", flush=True)
        url, data, errors = fetch_ssp(ssp, prev and prev.get("url"))
        r = {"ssp": ssp, "url": url, "errors": errors}
        if data is None:
            r["status"] = "error"
            results.append(r)
            print("   failed")
            continue
        new = normalize(data)
        r["new_count"] = len(new)
        r["ours"] = find_ours(new, cfg)
        if prev is None:
            r["status"] = "baseline"
        else:
            old = prev["sellers"]
            r["old_count"] = len(old)
            if old and len(new) < len(old) * (1 - cfg.get("max_drop_ratio", 0.5)):
                r["status"] = "anomaly"
                results.append(r)
                print(f"   anomaly: {len(old)} → {len(new)}")
                continue
            r["status"] = "ok"
            r["added"], r["removed"], r["changed"] = diff(old, new)
            r["added_log"] = cap_for_log(r["added"], cfg, lambda t: t[1])
            r["removed_log"] = cap_for_log(r["removed"], cfg, lambda t: t[1])
            r["changed_log"] = cap_for_log(r["changed"], cfg, lambda t: t[1] + t[2])
        if not args.dry_run:
            save_state(ssp["key"], url, new)
        results.append(r)
        print(f"   {r['status']} ({len(new):,} sellers) via {url}")

    report, any_change, any_warning, watched = build_report(results, cfg)
    print("\n" + report)
    if args.dry_run:
        return 0
    append_csv(results)
    write_dashboard(results, cfg)
    if os.environ.get("DASHBOARD_URL"):
        report += f"\n\n<{os.environ['DASHBOARD_URL']}|Open dashboard>"
    REPORT_DIR.mkdir(exist_ok=True)
    (REPORT_DIR / "latest.md").write_text(report, encoding="utf-8")

    first_run = all(r["status"] in ("baseline", "error") for r in results)
    should_notify = any_change or any_warning or first_run or cfg.get("notify_when_no_changes")
    if should_notify:
        subject = ("🚨 " if watched else "") + "sellers.json monitor — " + (
            "changes detected" if any_change else "status")
        sent = []
        try:
            if notify_slack(report):
                sent.append("Slack")
        except Exception as e:                # noqa: BLE001
            print(f"Slack notification failed: {e}", file=sys.stderr)
        try:
            if notify_email(subject, report):
                sent.append("email")
        except Exception as e:                # noqa: BLE001
            print(f"Email notification failed: {e}", file=sys.stderr)
        print(f"\nNotified via: {', '.join(sent) or 'nothing configured'}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="SSP keys to run (see config.json)")
    ap.add_argument("--dry-run", action="store_true")
    sys.exit(run(ap.parse_args()))
