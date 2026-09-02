#!/usr/local/bin/python3.13
#!/usr/bin/env python3
"""
HiperGlobal PU Installation Reporter
======================================
Fetches Jenkins builds for IPXE-100, computes monthly stats,
regenerates the HTML dashboard, and posts a summary to Slack.

Usage:
  python3 hiperglobal_reporter.py           # run full report
  python3 hiperglobal_reporter.py --dry-run # skip Slack post
  python3 hiperglobal_reporter.py --html-only # just update dashboard
"""

import os, re, sys, time, json, argparse
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict, Counter
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the same directory as this script
load_dotenv(Path(__file__).parent / ".env", override=True)

# ─── SELF-MANAGED LOG (bypasses launchd stdout redirect) ─────────────────────
_LOG_PATH = Path(__file__).parent / "reporter.log"
_log_file = open(_LOG_PATH, "a", buffering=1)  # line-buffered

class _Tee:
    """Write to both original stdout and log file."""
    def __init__(self, original):
        self._orig = original
    def write(self, msg):
        self._orig.write(msg)
        _log_file.write(msg)
    def flush(self):
        self._orig.flush()
        _log_file.flush()

sys.stdout = _Tee(sys.__stdout__)
sys.stderr = _Tee(sys.__stderr__)

# ─── CONFIG ──────────────────────────────────────────────────────────────────
JENKINS_BASE    = os.getenv("JENKINS_URL", "https://ci.cloud.uveye.xyz")
JENKINS_JOB     = "job/versions_management/job/dealership/job/install_dealership_site"
JENKINS_USER    = os.getenv("JENKINS_USER", "")
JENKINS_TOKEN   = os.getenv("JENKINS_TOKEN_REPORT", os.getenv("JENKINS_TOKEN", ""))
SLACK_TOKEN     = os.getenv("SLACK_TOKEN", "").strip()
SLACK_CHANNEL   = os.getenv("SLACK_CHANNEL", "#factory-installation-report")
CHANNEL_FILTER  = "IPXE-100"
DASHBOARD_PATH  = Path(os.getenv(
    "DASHBOARD_PATH",
    str(Path.home() / "Documents/hiperglobal/pu_dashboard.html")
))
PAGE_SIZE       = 100
MAX_PAGES       = 5   # 500 builds max stored in Jenkins

# ─── HISTORICAL DATA (Slack-sourced, Dec 2025 – Jun 2026) ────────────────────
# These months predate Jenkins history; kept as a fixed baseline.
# Source: #hiperglobal-installation-notification (C0A137H7BU7) — only channel used.
SLACK_HISTORY = [
    {"month":"Dec 2025","total":2,  "s1":1,  "s2":0,  "s3":0,"succ":1,  "failEvt":2,  "permFail":1,"partial":False,"src":"Slack","url":""},
    {"month":"Jan 2026","total":2,  "s1":1,  "s2":1,  "s3":0,"succ":2,  "failEvt":1,  "permFail":0,"partial":False,"src":"Slack","url":""},
    {"month":"Feb 2026","total":23, "s1":19, "s2":3,  "s3":0,"succ":22, "failEvt":4,  "permFail":1,"partial":False,"src":"Slack","url":""},
    {"month":"Mar 2026","total":136,"s1":112,"s2":22, "s3":2,"succ":136,"failEvt":25, "permFail":0,"partial":False,"src":"Slack","url":""},
    {"month":"Apr 2026","total":69, "s1":52, "s2":13, "s3":4,"succ":69, "failEvt":16, "permFail":0,"partial":False,"src":"Slack","url":""},
    # May 2026 — script not running, no Slack report posted, Jenkins history gone
    # Jun 2026 — only 2 sites; sourced from summary in #factory-installation-report, not build notifications
    {"month":"Jun 2026","total":2,  "s1":2,  "s2":0,  "s3":0,"succ":2,  "failEvt":0,  "permFail":0,"partial":False,"src":"Slack","url":"https://uveye.slack.com/archives/C0BCQPBSD4P/p1782898513610469"},
    # Jul 2026 — NO DATA: #hiperglobal-installation-notification (C0A137H7BU7) has no messages
    #             before Aug 18 2026; plexus channel data excluded per data-source rule.
]

# ─── ERROR PATTERNS ──────────────────────────────────────────────────────────
ERROR_PATTERNS = [
    ("prometheus","Prometheus Monitoring Fail","post-install",[
        r"prometheus-deployment.*status FAILURE",
        r"prometheus-deployment.*FAILURE",
    ]),
    ("bitbucket","Bitbucket Config Push Fail","post-install",[
        r"failed to push some refs",
    ]),
    ("ansible","Ansible Script Failure","real-fail",[
        r"fatal:.*FAILED!",
    ]),
    ("rancher","Rancher 503 Unavailable","real-fail",[
        r"503 Service Unavailable",
        r"rancher\.uveye\.cloud.*503",
    ]),
    ("k3s","k3s Permission Denied","real-fail",[
        r"k3s\.yaml: permission denied",
    ]),
    ("rsync","rsync Failure","real-fail",[
        r"rsync.*exit code",
    ]),
    ("oom","Jenkins OOM","real-fail",[
        r"OutOfMemoryError",
        r"unable to create native thread",
    ]),
    ("network","Network / Connection Error","real-fail",[
        r"ConnectionResetError",
        r"Connection reset by peer",
        r"RemoteDisconnected",
        r"connection was forcibly closed",
        r"Network is unreachable",
        r"Temporary failure in name resolution",
        r"Failed to connect",
        r"Connection timed out",
        r"No route to host",
    ]),
]

SNIPPET_PATTERNS = {
    "prometheus": r"(prometheus-deployment[^\n]{0,80})",
    "bitbucket":  r"(error: failed to push[^\n]{0,80})",
    "ansible":    r"(fatal:[^\n]{0,100})",
    "rancher":    r"(HTTPError:[^\n]{0,80}|503[^\n]{0,60}rancher[^\n]{0,60})",
    "k3s":        r"(open /etc/rancher[^\n]{0,80})",
    "rsync":      r"(rsync[^\n]{0,80}exit code[^\n]{0,40})",
    "oom":        r"(OutOfMemoryError[^\n]{0,80})",
    "network":    r"(ConnectionResetError[^\n]{0,80}|Connection reset[^\n]{0,80}|Failed to connect[^\n]{0,80})",
}


# ─── WEEKLY HELPERS ───────────────────────────────────────────────────────────

def iso_week_key(b):
    ts = datetime.fromtimestamp(b["timestamp"] / 1000, tz=timezone.utc)
    return ts.strftime("%G-W%V")  # e.g. "2026-W33"

def week_label(wk):
    year, w = wk.split("-W")
    monday = datetime.strptime(f"{year}-W{w.zfill(2)}-1", "%G-W%V-%u")
    sunday = monday + timedelta(days=6)
    if monday.month == sunday.month:
        return f"{monday.strftime('%b %-d')}–{sunday.strftime('%-d')}"
    return f"{monday.strftime('%b %-d')}–{sunday.strftime('%b %-d')}"


# ═══════════════════ JENKINS CLIENT ══════════════════════════════════════════

class JenkinsClient:
    def __init__(self):
        self.base = f"{JENKINS_BASE}/{JENKINS_JOB}"
        self.session = requests.Session()
        if JENKINS_USER and JENKINS_TOKEN:
            self.session.auth = (JENKINS_USER, JENKINS_TOKEN)
        self.session.headers.update({"Accept": "application/json"})

    def _get(self, url, params=None, timeout=30):
        r = self.session.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def fetch_builds(self):
        """Fetch all builds (paginated), return list filtered to IPXE-100."""
        tree = "number,result,timestamp,actions[parameters[name,value]]"
        all_builds = []

        for page in range(MAX_PAGES):
            start = page * PAGE_SIZE
            end   = start + PAGE_SIZE
            params = {"tree": f"allBuilds[{tree}]{{{start},{end}}}"}
            try:
                data = self._get(f"{self.base}/api/json", params=params)
            except requests.HTTPError as e:
                print(f"  Jenkins API error at offset {start}: {e}")
                break

            page_builds = data.get("allBuilds", [])
            if not page_builds:
                break

            all_builds.extend(page_builds)
            print(f"  Fetched {len(all_builds)} builds so far…")

            if len(page_builds) < PAGE_SIZE:
                break
            time.sleep(0.3)

        # Extract parameters and filter
        filtered = []
        for b in all_builds:
            params = {}
            for action in (b.get("actions") or []):
                for p in (action.get("parameters") or []):
                    params[p["name"]] = p.get("value", "")
            if params.get("factory_slack_channel") == CHANNEL_FILTER:
                b["_p"] = params
                filtered.append(b)

        print(f"  → {len(filtered)} IPXE-100 builds (of {len(all_builds)} total)")
        return filtered

    def fetch_console(self, build_num):
        url = f"{self.base}/{build_num}/consoleText"
        try:
            r = self.session.get(url, timeout=60)
            r.raise_for_status()
            return r.text
        except Exception as e:
            print(f"    Warning: console #{build_num} failed: {e}")
            return ""


# ═══════════════════ ERROR CATEGORIZATION ════════════════════════════════════

def categorize(console):
    for key, label, err_type, patterns in ERROR_PATTERNS:
        for pat in patterns:
            if re.search(pat, console, re.IGNORECASE):
                # Ansible false-positive guard: failed=0 means no real failure
                if key == "ansible":
                    recap = re.search(r"PLAY RECAP.*", console)
                    if recap and re.search(r"failed=0", recap.group()):
                        continue
                return key, label, err_type
    return "other", "Unknown Error", "real-fail"

def snippet(console, key):
    pat = SNIPPET_PATTERNS.get(key)
    if pat:
        m = re.search(pat, console, re.IGNORECASE)
        if m:
            return m.group(1).strip()[:120]
    return ""


# ═══════════════════ DATA PROCESSING ═════════════════════════════════════════

def process(client, raw_builds):
    """
    Group builds by site, compute monthly stats, collect error records.
    Returns (monthly_list, errors_list, sites_list).
    """
    # Group by site
    by_site = defaultdict(list)
    for b in raw_builds:
        site = b["_p"].get("SYSTEMNUMBER", "unknown")
        by_site[site].append(b)
    for site in by_site:
        by_site[site].sort(key=lambda x: x["timestamp"])

    # Which months do we have?
    def month_key(b):
        ts = datetime.fromtimestamp(b["timestamp"] / 1000, tz=timezone.utc)
        return ts.strftime("%Y-%m")

    # Determine which builds need console logs (FAILURE builds in recent 2 months)
    all_months = sorted({month_key(b) for b in raw_builds})
    recent = set(all_months[-2:]) if len(all_months) >= 2 else set(all_months)

    failure_nums = [
        b["number"]
        for b in raw_builds
        if b.get("result") == "FAILURE" and month_key(b) in recent
    ]
    print(f"  Fetching {len(failure_nums)} console logs…")
    consoles = {}
    for num in failure_nums:
        consoles[num] = client.fetch_console(num)
        time.sleep(0.2)

    # Group sites by the month of their FINAL build
    month_sites = defaultdict(list)
    for site, builds in by_site.items():
        mk = month_key(builds[-1])
        month_sites[mk].append((site, builds))

    errors_out = []
    sites_out  = []
    monthly_jenkins = []

    now = datetime.now(tz=timezone.utc)

    for mk in sorted(month_sites.keys()):
        dt = datetime.strptime(mk + "-01", "%Y-%m-%d")
        month_label = dt.strftime("%b %Y")
        is_partial = (dt.year == now.year and dt.month == now.month)

        s1 = s2 = s3 = succ = fail_evt = perm_fail = 0

        for site, builds in month_sites[mk]:
            pu = builds[0]["_p"].get("PU", builds[0]["_p"].get("TAG", "unknown"))
            results = [b.get("result") for b in builds]
            final   = results[-1]

            # Count meaningful attempts (SUCCESS or FAILURE, not ABORTED)
            n_att = sum(1 for r in results if r in ("SUCCESS", "FAILURE"))
            # Failed runs before success (FAILURE or ABORTED)
            n_fail = sum(1 for r in results[:-1] if r in ("FAILURE", "ABORTED"))
            if final == "FAILURE":
                n_fail += 1
            fail_evt += n_fail

            if final == "SUCCESS":
                succ += 1
                if n_att <= 1:  s1 += 1
                elif n_att == 2: s2 += 1
                else:            s3 += 1
            else:
                perm_fail += 1

            # Run chain string
            chain = " → ".join(
                f"{b.get('result','?')}({datetime.fromtimestamp(b['timestamp']/1000,tz=timezone.utc).strftime('%Y-%m-%d')})"
                for b in builds
            )
            att = len(builds)
            att_label = {1:"1st",2:"2nd",3:"3rd"}.get(att, f"{att}th")
            final_date = datetime.fromtimestamp(builds[-1]["timestamp"]/1000, tz=timezone.utc)

            sites_out.append({
                "site":     site,
                "pu":       pu,
                "att":      att,
                "attLabel": att_label,
                "date":     final_date.strftime("%Y-%m-%d"),
                "runs":     chain,
            })

            # Errors
            for b in builds:
                if b.get("result") == "FAILURE" and b["number"] in consoles:
                    key, label, err_type = categorize(consoles[b["number"]])
                    snip = snippet(consoles[b["number"]], key)
                    b_dt = datetime.fromtimestamp(b["timestamp"]/1000, tz=timezone.utc)
                    errors_out.append({
                        "num":      b["number"],
                        "site":     site,
                        "date":     b_dt.strftime("%Y-%m-%d"),
                        "cat":      key,
                        "catLabel": label,
                        "type":     err_type,
                        "err":      snip or label,
                    })

        monthly_jenkins.append({
            "month":    month_label,
            "total":    len(month_sites[mk]),
            "s1":       s1,
            "s2":       s2,
            "s3":       s3,
            "succ":     succ,
            "failEvt":  fail_evt,
            "permFail": perm_fail,
            "partial":  is_partial,
            "src":      "Jenkins",
        })

    # Merge: Slack baseline + Jenkins (Jenkins wins on overlap)
    jenkins_labels = {m["month"] for m in monthly_jenkins}
    slack_base = [m for m in SLACK_HISTORY if m["month"] not in jenkins_labels]
    monthly_all = slack_base + monthly_jenkins

    # ── WEEKLY grouping (Jenkins data only) ───────────────────────────────
    week_sites = defaultdict(list)
    for site, builds in by_site.items():
        wk = iso_week_key(builds[-1])
        week_sites[wk].append((site, builds))

    now_utc = datetime.now(tz=timezone.utc)
    weekly_out = []
    for wk in sorted(week_sites.keys()):
        yr, wnum = wk.split("-W")
        monday = datetime.strptime(f"{yr}-W{wnum.zfill(2)}-1", "%G-W%V-%u")
        is_partial = (monday.isocalendar()[:2] == now_utc.isocalendar()[:2])

        ws1 = ws2 = ws3 = wsucc = wfail_evt = wperm_fail = 0
        for site, builds in week_sites[wk]:
            results = [b.get("result") for b in builds]
            final   = results[-1]
            n_att   = sum(1 for r in results if r in ("SUCCESS", "FAILURE"))
            n_fail  = sum(1 for r in results[:-1] if r in ("FAILURE", "ABORTED"))
            if final == "FAILURE":
                n_fail += 1
            wfail_evt += n_fail
            if final == "SUCCESS":
                wsucc += 1
                if n_att <= 1:   ws1 += 1
                elif n_att == 2: ws2 += 1
                else:             ws3 += 1
            else:
                wperm_fail += 1

        weekly_out.append({
            "week":     wk,
            "label":    week_label(wk),
            "total":    len(week_sites[wk]),
            "s1":       ws1,
            "s2":       ws2,
            "s3":       ws3,
            "succ":     wsucc,
            "failEvt":  wfail_evt,
            "permFail": wperm_fail,
            "partial":  is_partial,
        })

    return monthly_all, weekly_out, errors_out, sites_out


# ═══════════════════ HTML UPDATER ════════════════════════════════════════════

def _js_val(v):
    if isinstance(v, bool):   return "true" if v else "false"
    if isinstance(v, str):    return "'" + v.replace("\\","\\\\").replace("'","\\'") + "'"
    return str(v)

def _js_obj(d):
    return "{" + ",".join(f"{k}:{_js_val(v)}" for k,v in d.items()) + "}"

def update_dashboard(monthly, weekly, errors, sites):
    if not DASHBOARD_PATH.exists():
        print(f"  Dashboard not found at {DASHBOARD_PATH} — skipping")
        return False

    html = DASHBOARD_PATH.read_text(encoding="utf-8")

    def replace_array(src, name, rows):
        inner = ",\n  ".join(_js_obj(r) for r in rows)
        new_block = f"const {name}=[\n  {inner}\n];"
        # Find the opening marker (handles optional spaces around =)
        start = -1
        found_marker = None
        for marker in (f"const {name}=[", f"const {name} = [", f"const {name}= [", f"const {name} =["):
            idx = src.find(marker)
            if idx != -1:
                start = idx
                found_marker = marker
                break
        if start == -1:
            # Debug: show what we do find near the name
            nearby = src.find(name)
            ctx = repr(src[nearby:nearby+40]) if nearby != -1 else "not found at all"
            print(f"  Warning: {name} array not found — nearby: {ctx}")
            return src
        # Find the closing ]; after the opening bracket
        end = src.find("];", start)
        if end == -1:
            print(f"  Warning: {name} closing ]; not found")
            return src
        return src[:start] + new_block + src[end + 2:]

    html = replace_array(html, "MONTHLY", monthly)
    html = replace_array(html, "WEEKLY",  weekly)
    html = replace_array(html, "ERRORS",  errors)
    html = replace_array(html, "SITES",   sites)

    # Update current month hero title
    cur = monthly[-1]
    partial_badge = ' <span class="hero-badge">Current month (partial)</span>' if cur.get("partial") else ""
    html = re.sub(
        r'<div class="hero-month">.*?</div>',
        f'<div class="hero-month">{cur["month"]}{partial_badge}</div>',
        html, flags=re.DOTALL
    )

    # Update hero subtitle
    total_sites = sum(m["total"] for m in monthly)
    html = re.sub(
        r'<div style="font-size:11px;opacity:.7;margin-top:4px">Source:.*?</div>',
        f'<div style="font-size:11px;opacity:.7;margin-top:4px">Source: {cur["src"]} · {cur["total"]} sites this month</div>',
        html, flags=re.DOTALL
    )

    # Update header subtitle
    slack_months   = [m for m in monthly if m["src"] == "Slack"]
    jenkins_months = [m for m in monthly if m["src"] == "Jenkins"]
    parts = []
    if slack_months:
        parts.append(f"Slack {slack_months[0]['month']}–{slack_months[-1]['month']}")
    if jenkins_months:
        j_start = jenkins_months[0]["month"]
        j_end   = jenkins_months[-1]["month"]
        parts.append(f"Jenkins {j_start}–{j_end}")
    parts.append(f"{total_sites} total sites")
    if parts:
        html = re.sub(
            r'<div class="sub">.*?</div>',
            f'<div class="sub">{" &nbsp;·&nbsp; ".join(parts)}</div>',
            html, flags=re.DOTALL
        )

    now_str = datetime.now().strftime("%b %d, %Y %H:%M")
    html = re.sub(r"Last updated:[^\n<]+", f"Last updated: {now_str}", html, count=1)

    DASHBOARD_PATH.write_text(html, encoding="utf-8")
    print(f"  Dashboard saved → {DASHBOARD_PATH}")
    return True


# ═══════════════════ SLACK REPORTER ══════════════════════════════════════════

def build_message(monthly, weekly, errors):
    if not monthly:
        return "No data to report."

    # Use weekly data for current-period stats (WoW comparison)
    # Fall back to monthly if no weekly data
    if weekly:
        cur  = weekly[-1]
        prev = weekly[-2] if len(weekly) >= 2 else None
        period_label = cur["label"]
        period_key   = cur["week"]   # e.g. "2026-W36"
        wow_label    = prev["label"] if prev else None
    else:
        cur  = monthly[-1]
        prev = monthly[-2] if len(monthly) >= 2 else None
        period_label = cur["month"]
        period_key   = None
        wow_label    = prev["month"] if prev else None

    def pct(n, d):
        return f"{round(n/d*100,1)}%" if d else "—"

    cur_1r  = pct(cur["s1"], cur["total"])
    cur_1rn = cur["s1"] / cur["total"] * 100 if cur["total"] else 0

    # WoW trend
    wow = ""
    if prev and prev["total"]:
        prev_1rn = prev["s1"] / prev["total"] * 100
        diff = cur_1rn - prev_1rn
        if abs(diff) >= 0.5:
            arrow = "↑" if diff > 0 else "↓"
            wow = f"  {arrow} {abs(diff):.1f}pp vs {wow_label}"

    # Current week errors (match by date range if weekly, else by month)
    if period_key:
        yr, wnum = period_key.split("-W")
        week_start = datetime.strptime(f"{yr}-W{wnum.zfill(2)}-1", "%G-W%V-%u")
        week_end   = week_start + timedelta(days=6)
        cur_errs = [e for e in errors
                    if week_start.strftime("%Y-%m-%d") <= e["date"] <= week_end.strftime("%Y-%m-%d")]
    else:
        try:
            cur_ym = datetime.strptime(period_label, "%b %Y").strftime("%Y-%m")
        except ValueError:
            cur_ym = ""
        cur_errs = [e for e in errors if e["date"].startswith(cur_ym)]

    post_only  = sum(1 for e in cur_errs if e["type"] == "post-install")
    real_fails = sum(1 for e in cur_errs if e["type"] == "real-fail")
    err_counts = Counter(e["cat"] for e in cur_errs)
    err_meta   = {e["cat"]: e for e in cur_errs}

    partial = " _(partial week)_" if cur.get("partial") else ""

    lines = [
        f"📊 *HiperGlobal PU Installation Report — {period_label}*{partial}",
        "",
        "*Overview*",
        f"• Sites installed: *{cur['total']}*",
        f"• ✅ 1st attempt: *{cur['s1']}* ({cur_1r}){wow}",
    ]
    if cur["s2"]:
        lines.append(f"• ⚠️ 2nd attempt: {cur['s2']}")
    if cur["s3"]:
        lines.append(f"• 🔁 3rd+ attempt: {cur['s3']}")
    lines.append(f"• ❌ Permanent failures: {'*' + str(cur['permFail']) + '*' if cur['permFail'] else '0'}")

    if cur["failEvt"]:
        lines += ["", "*Failed Build Runs*", f"• Total: {cur['failEvt']}"]
        if post_only:
            lines.append(f"  ↳ Post-install only (PU was live): {post_only}  ✅ no impact on site")
        if real_fails:
            lines.append(f"  ↳ Real install failures: {real_fails}  ❌")
        if err_counts:
            lines += ["", "*Error Breakdown*"]
            for cat, count in err_counts.most_common():
                rec   = err_meta.get(cat, {})
                label = rec.get("catLabel", cat)
                icon  = "✅" if rec.get("type") == "post-install" else "❌"
                lines.append(f"• {label}: {count}  {icon}")
    else:
        lines += ["", "✨ *Zero failures this month!*"]

    return "\n".join(lines)


def upload_html_to_slack():
    """Upload the HTML dashboard file to Slack using the v2 upload API."""
    if not SLACK_TOKEN:
        return
    if not DASHBOARD_PATH.exists():
        print(f"  Dashboard file not found at {DASHBOARD_PATH} — skipping upload")
        return

    token   = SLACK_TOKEN.strip()
    headers = {"Authorization": f"Bearer {token}"}
    content = DASHBOARD_PATH.read_bytes()

    # Step 1 — get upload URL
    r1 = requests.get(
        "https://slack.com/api/files.getUploadURLExternal",
        headers=headers,
        params={"filename": "pu_dashboard.html", "length": len(content)},
        timeout=60,
    )
    d1 = r1.json()
    if not d1.get("ok"):
        print(f"  ⚠️  Could not get upload URL: {d1.get('error')}")
        return

    upload_url = d1["upload_url"]
    file_id    = d1["file_id"]

    # Step 2 — upload the file content
    r2 = requests.post(upload_url, data=content,
                       headers={"Content-Type": "text/html"}, timeout=60)
    if r2.status_code not in (200, 201):
        print(f"  ⚠️  File upload failed: HTTP {r2.status_code}")
        return

    # Resolve channel name → ID if needed
    channel_id = SLACK_CHANNEL
    if SLACK_CHANNEL.startswith("#"):
        rc = requests.get(
            "https://slack.com/api/conversations.list",
            headers=headers,
            params={"limit": 200, "exclude_archived": "true"},
            timeout=30,
        )
        for ch in rc.json().get("channels", []):
            if ch["name"] == SLACK_CHANNEL.lstrip("#"):
                channel_id = ch["id"]
                break

    # Step 3 — complete and share to channel
    r3 = requests.post(
        "https://slack.com/api/files.completeUploadExternal",
        headers={**headers, "Content-Type": "application/json"},
        json={
            "files": [{"id": file_id, "title": "PU Installation Dashboard"}],
            "channel_id": channel_id,
            "initial_comment": "📊 Interactive dashboard — download and open in any browser",
        },
        timeout=60,
    )
    d3 = r3.json()
    if d3.get("ok"):
        print(f"  ✅ HTML file uploaded to {SLACK_CHANNEL}")
    else:
        print(f"  ⚠️  File share failed: {d3.get('error')}")


def post_slack(message, dry_run=False):
    if dry_run:
        print("\n── Slack message (dry-run) ─────────────────────────────")
        print(message)
        print("────────────────────────────────────────────────────────\n")
        return True

    if not SLACK_TOKEN:
        print("  SLACK_TOKEN not set — printing message:")
        print(message)
        return False

    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}",
                 "Content-Type": "application/json"},
        json={"channel": SLACK_CHANNEL, "text": message, "mrkdwn": True},
        timeout=30,
    )
    data = r.json()
    if data.get("ok"):
        print(f"  ✅ Posted to {SLACK_CHANNEL}")
        return True
    else:
        print(f"  ❌ Slack error: {data.get('error')}")
        return False


# ═══════════════════ MAIN ════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="HiperGlobal PU Reporter")
    parser.add_argument("--dry-run",   action="store_true", help="Print Slack message, don't post")
    parser.add_argument("--html-only", action="store_true", help="Update HTML only, skip Slack")
    args = parser.parse_args()

    print(f"\n{'═'*58}")
    print(f"  HiperGlobal PU Reporter · {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'═'*58}\n")

    if not JENKINS_USER or not JENKINS_TOKEN:
        print("ERROR: JENKINS_USER and JENKINS_TOKEN must be set in .env")
        sys.exit(1)

    client = JenkinsClient()

    print("1/4  Fetching Jenkins builds…")
    builds = client.fetch_builds()
    if not builds:
        print("  No builds found — exiting.")
        sys.exit(0)

    print("2/4  Processing data…")
    monthly, weekly, errors, sites = process(client, builds)
    print(f"      {len(monthly)} months · {len(weekly)} weeks · {len(errors)} errors · {len(sites)} sites")

    print("3/4  Updating HTML dashboard…")
    update_dashboard(monthly, weekly, errors, sites)

    if not args.html_only:
        print("4/4  Posting Slack message…")
        message = build_message(monthly, weekly, errors)
        post_slack(message, dry_run=args.dry_run)
        if not args.dry_run:
            print("     Uploading HTML dashboard…")
            for attempt in range(2):
                try:
                    upload_html_to_slack()
                    break
                except Exception as e:
                    if attempt == 0:
                        print(f"  ⚠️  Upload failed ({e.__class__.__name__}), retrying in 15s…")
                        time.sleep(15)
                    else:
                        print(f"  ⚠️  Upload failed after retry: {e.__class__.__name__} — skipping")
    else:
        print("4/4  Skipping Slack (--html-only)")

    print(f"\n{'═'*58}")
    print("  Done ✅")
    print(f"{'═'*58}\n")


if __name__ == "__main__":
    main()