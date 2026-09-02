#!/usr/bin/env python3
"""
Auto-updater for pu_dashboard.html.
Reads new install notifications from #hiperglobal-installation-notification (Slack),
adds new SITES entries, updates MONTHLY counts, stats, and date.
GitHub Actions commits & pushes the result.

Required env var: SLACK_TOKEN  (Slack bot token with channels:history scope)
"""

import os, re, json, datetime, requests

SLACK_TOKEN  = os.environ['SLACK_TOKEN']
CHANNEL_ID   = 'C0A137H7BU7'           # #hiperglobal-installation-notification
JENKINS_BASE = 'https://ci.cloud.uveye.xyz/job/versions_management/job/dealership/job/install_dealership_site'
HTML_PATH    = 'pu_dashboard.html'
STATE_PATH   = 'state.json'

# ── Slack helpers ─────────────────────────────────────────────────────────────

def slack_history(oldest=None, cursor=None):
    params = {'channel': CHANNEL_ID, 'limit': 200}
    if oldest: params['oldest'] = oldest
    if cursor:  params['cursor']  = cursor
    r = requests.get(
        'https://slack.com/api/conversations.history',
        headers={'Authorization': f'Bearer {SLACK_TOKEN}'},
        params=params, timeout=30
    )
    r.raise_for_status()
    data = r.json()
    if not data.get('ok'):
        raise RuntimeError(f"Slack API error: {data.get('error')}")
    return data

def get_all_new_messages(oldest_ts):
    """Fetch all messages newer than oldest_ts (paginated)."""
    messages, cursor = [], None
    while True:
        data = slack_history(oldest=oldest_ts, cursor=cursor)
        messages.extend(data.get('messages', []))
        cursor = data.get('response_metadata', {}).get('next_cursor')
        if not cursor:
            break
    return messages

# ── Message parsing ───────────────────────────────────────────────────────────

SUCCESS_RE = re.compile(
    r'SUCCESS.*?install_dealership_site.*?/(\d+)/.*?#Site[:`\s]*(atlas-lite|artelios)-(\d+)',
    re.DOTALL | re.IGNORECASE
)

def parse_messages(messages):
    """Return list of (site, pu, build_num, date_str, ts) for SUCCESS messages."""
    results = []
    for msg in messages:
        ts = msg.get('ts', '0')
        for att in msg.get('attachments', []):
            text = att.get('text', '') + ' ' + att.get('fallback', '')
            m = SUCCESS_RE.search(text)
            if m:
                build_num = int(m.group(1))
                pu        = m.group(2)
                site      = m.group(3)
                dt        = datetime.datetime.utcfromtimestamp(float(ts))
                date_str  = dt.strftime('%Y-%m-%d')
                results.append((site, pu, build_num, date_str, ts))
    return results

# ── HTML update ───────────────────────────────────────────────────────────────

def update_html(new_installs):
    """
    Append new entries to SITES, update MONTHLY + stats.
    new_installs: list of (site, pu, build, date, ts) — deduplicated, newest first.
    Returns count of entries added.
    """
    with open(HTML_PATH, encoding='utf-8') as f:
        c = f.read()

    # Existing site numbers in SITES array
    sites_m = re.search(r'const SITES=\[(.*?)\];', c, re.DOTALL)
    if not sites_m:
        raise RuntimeError("SITES array not found in HTML")
    existing = set(re.findall(r"site:'(\d+)'", sites_m.group(1)))

    added_entries = []
    added_by_month = {}  # 'YYYY-MM' -> count

    for site, pu, build, date, ts in new_installs:
        if site in existing:
            continue
        existing.add(site)
        url   = f"{JENKINS_BASE}/{build}/"
        runs  = f"SUCCESS({date})"
        entry = (f"  {{site:'{site}',pu:'{pu}',att:1,attLabel:'1st',"
                 f"date:'{date}',src:'Jenkins',runs:'{runs}',url:'{url}'}}")
        added_entries.append(entry)
        month_key = date[:7]
        added_by_month[month_key] = added_by_month.get(month_key, 0) + 1

    if not added_entries:
        print("No new entries to add.")
        return 0

    # ── Append to SITES ──
    sites_end = re.search(r'(const SITES=\[.*?)(\n\];)', c, re.DOTALL)
    c = (c[:sites_end.end(1)]
         + ',\n' + ',\n'.join(added_entries)
         + sites_end.group(2)
         + c[sites_end.end():])

    # ── Update MONTHLY per month ──
    for month_key, count in added_by_month.items():
        dt = datetime.datetime.strptime(month_key, '%Y-%m')
        label = dt.strftime('%b %Y')          # e.g. "Sep 2026"
        m = re.search(
            r"(\{month:'" + re.escape(label) + r"',total:)(\d+)"
            r"(,s1:)(\d+)([^}]*?,succ:)(\d+)([^}]*?\})", c
        )
        if m:
            c = (c[:m.start()]
                 + m.group(1) + str(int(m.group(2)) + count)
                 + m.group(3) + str(int(m.group(4)) + count)
                 + m.group(5) + str(int(m.group(6)) + count)
                 + m.group(7)
                 + c[m.end():])
        else:
            # New month entry
            new_m = (f",\n  {{month:'{label}',total:{count},s1:{count},"
                     f"s2:0,s3:0,succ:{count},failEvt:0,permFail:0,"
                     f"partial:false,src:'Jenkins'}}")
            monthly_end = re.search(r'(const MONTHLY=\[.*?)(\n\];)', c, re.DOTALL)
            if monthly_end:
                c = (c[:monthly_end.end(1)]
                     + new_m
                     + monthly_end.group(2)
                     + c[monthly_end.end():])

    # ── Update hero month to current ──
    now = datetime.datetime.utcnow()
    cur_month = now.strftime('%b %Y')
    c = re.sub(
        r'(<div class="hero-month">)[^<]+(</div>)',
        f'\\g<1>{cur_month}\\g<2>', c
    )

    # ── Update subtitle, stats, last-updated ──
    sites_body = re.search(r'const SITES=\[(.*?)\];', c, re.DOTALL).group(1)
    total = len(re.findall(r"site:'", sites_body))

    c = re.sub(
        r'<div class="sub">[^<]+</div>',
        f'<div class="sub">Jenkins &amp; Slack &nbsp;·&nbsp; {cur_month} &nbsp;·&nbsp; {total} installs on record</div>',
        c
    )
    c = re.sub(r'(<div class="stat-num" id="total-sites">)\d+', f'\\g<1>{total}', c)
    c = re.sub(r'(<div class="stat-num" id="success-rate">)[^<]+', r'\g<1>100.0%', c)
    c = re.sub(r'(<div class="stat-num" id="ok-sites">)\d+', f'\\g<1>{total}', c)

    today_str = now.strftime('%b %d, %Y')
    c = re.sub(r'Last updated: [^<]+', f'Last updated: {today_str}', c)

    with open(HTML_PATH, 'w', encoding='utf-8') as f:
        f.write(c)

    print(f"Added {len(added_entries)} new SITES entries across {list(added_by_month.items())}.")
    return len(added_entries)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Load state (tracks last processed Slack message TS)
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            state = json.load(f)
    else:
        state = {}

    last_ts = state.get('last_ts', '0')
    print(f"Fetching Slack messages since TS={last_ts} …")

    messages = get_all_new_messages(last_ts)
    print(f"  {len(messages)} new messages found.")

    if not messages:
        print("Nothing new. Done.")
        return

    # Advance the cursor to the newest message seen
    new_last_ts = max(m['ts'] for m in messages)

    # Parse and deduplicate SUCCESS installs (keep most recent per site)
    installs = parse_messages(messages)
    print(f"  {len(installs)} SUCCESS events parsed.")

    seen = {}
    for item in sorted(installs, key=lambda x: float(x[4]), reverse=True):
        key = (item[1], item[0])   # (pu, site)
        if key not in seen:
            seen[key] = item
    unique_installs = list(seen.values())
    print(f"  {len(unique_installs)} unique new installs after dedup.")

    added = update_html(unique_installs)

    # Save state
    state.update({
        'last_ts':   new_last_ts,
        'last_run':  datetime.datetime.utcnow().isoformat(),
        'last_added': added,
    })
    with open(STATE_PATH, 'w') as f:
        json.dump(state, f, indent=2)

    print(f"Done. State saved (last_ts={new_last_ts}).")

if __name__ == '__main__':
    main()
