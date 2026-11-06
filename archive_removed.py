#!/usr/bin/env python3
"""One-off: intermediaries removed over ~30 days, using Internet Archive copies of each sellers.json."""
import csv, datetime as dt, json, re, sys, time, urllib.parse, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sellers_monitor as sm

ROOT = Path(__file__).resolve().parent
TARGET = (dt.date.today() - dt.timedelta(days=30)).strftime("%Y%m%d")
FROM = (dt.date.today() - dt.timedelta(days=75)).strftime("%Y%m%d")
UA = {"User-Agent": "sellers-json-monitor archive check"}

def get(url, timeout=120, want_json=True):
    err = None
    for a in range(6):
        time.sleep(6)                                  # be gentle with the archive
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                body = r.read()
            if not want_json or body.lstrip()[:1] in (b"[", b"{") or body.strip() == b"":
                return body
            err = RuntimeError(f"non-JSON reply: {body[:120]!r}")
        except Exception as e:
            err = e
        print("  retry", a + 1, url[:90], err)
        time.sleep(30 * (a + 1))
    raise err

def snapshots(url):
    q = urllib.parse.urlencode({"url": re.sub(r"^https?://", "", url), "from": FROM,
                                "output": "json", "fl": "timestamp,statuscode", "filter": "statuscode:200"})
    body = get("https://web.archive.org/cdx/search/cdx?" + q)
    print("  cdx", url, len(body))
    rows = json.loads(body or b"[]")
    return [r[0] for r in rows[1:]]

cfg = json.loads((ROOT / "config.json").read_text())
status = {s["key"]: s for s in json.loads((ROOT / "docs/data/status.json").read_text())["ssps"]}
out, summary = [], []
for ssp in cfg["ssps"]:
    urls = ([status.get(ssp["key"], {}).get("url")] if status.get(ssp["key"], {}).get("url") else []) + ssp["urls"]
    urls = list(dict.fromkeys(u for u in urls if u))
    best = None
    for u in urls:
        try:
            ts = snapshots(u)
        except Exception as e:
            print(ssp["name"], u, "cdx error", e); continue
        for t in ts:
            cand = (abs(int(t[:8]) - int(TARGET)), t, u)
            if best is None or cand < best: best = cand
        time.sleep(1)
    if not best:
        summary.append({"ssp": ssp["name"], "archive_date": None, "note": "no archive copy"}); print(ssp["name"], "no copy"); continue
    _, ts, u = best
    try:
        old = sm.normalize(json.loads(get(f"https://web.archive.org/web/{ts}id_/{u}", 300).decode("utf-8-sig", "replace")))
        _, new_raw, errs = sm.fetch_ssp(ssp)
        if new_raw is None: raise RuntimeError("; ".join(errs))
        new = sm.normalize(new_raw)
    except Exception as e:
        summary.append({"ssp": ssp["name"], "archive_date": ts[:8], "note": f"error: {e}"[:200]}); print(ssp["name"], "error", e); continue
    n = 0
    for sid in old.keys() - new.keys():
        for rec in old[sid]:
            if rec[2] in ("INTERMEDIARY", "BOTH") and rec[1] != "google.com":
                out.append([ssp["name"], ts[:8], sid, rec[0], rec[1], rec[2]]); n += 1
    summary.append({"ssp": ssp["name"], "archive_date": ts[:8], "old_count": len(old), "new_count": len(new), "removed_intermediaries": n})
    print(ssp["name"], ts[:8], len(old), "->", len(new), "removed int:", n)

(ROOT / "reports").mkdir(exist_ok=True)
with open(ROOT / "reports/archive_removed_intermediaries.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f); w.writerow(["ssp", "archive_date", "seller_id", "name", "domain", "seller_type"]); w.writerows(out)
(ROOT / "reports/archive_summary.json").write_text(json.dumps({"generated": sm.now_iso(), "target": TARGET, "ssps": summary}, indent=1))
