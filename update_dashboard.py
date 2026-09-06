#!/usr/bin/env python3
"""
Auto-updater for pu_dashboard.html.
Reads new install notifications from #hiperglobal-installation-notification (Slack),
adds new SITES entries (SUCCESS and FAILURE), updates MONTHLY counts, ERRORS array,
stats, and date.
GitHub Actions commits & pushes the result.

Required env var: SLACK_TOKEN  (Slack user token with groups:history scope)
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

# ── Error categorization ──────────────────────────────────────────────────────

ERROR_RULES = [
    ('prometheus', 'Prometheus Monitoring Fail', 'post-install',
     re.compile(r'prometheus.deployment|prometheus.*fail', re.I)),
    ('bitbucket',  'Bitbucket Push Fail',        'post-install',
     re.compile(r'customers.configuration\.git|failed to push', re.I)),
    ('oom',        'Jenkins OOM',                'real-fail',
     re.compile(r'OutOfMemoryError|out of memory', re.I)),
    ('ansible',    'Ansible Script Failure',     'real-fail',
     re.compile(r'FAILED!.*changed|ansible.*FAILED|operation.tools', re.I)),
    ('rancher',    'Rancher 503',                'real-fail',
     re.compile(r'rancher.*503|503.*rancher', re.I)),
    ('k3s',        'k3s Permission',             'real-fail',
     re.compile(r'k3s.*permission|permission denied.*k3s', re.I)),
    ('rsync',      'rsync Failure',              'real-fail',
     re.compile(r'rsync.*fail|failed.*rsync', re.I)),
]

def categorize_error(text):
    """Return (cat, catLabel, type, err_snippet) from failure message text."""
    for cat, label, typ, pattern in ERROR_RULES:
        if pattern.search(text):
            # Try to grab a short snippet around the match
            m = pattern.search(text)
            start = max(0, m.start() - 20)
            snippet = text[start:start+120].replace('\n', ' ').strip()
            return cat, label, typ, snippet
    # Fallback
    snippet = text[:120].replace('\n', ' ').strip() if text.strip() else 'See Jenkins build'
    return 'other', 'Unknown Error', 'real-fail', snippet

# ── Message parsing ───────────────────────────────────────────────────────────

INSTALL_RE = re.compile(
    r'(SUCCESS|FAILURE).*?install_dealership_site.*?/(\d+)/.*?#Site[:`\s]*(atlas-lite|artelios)-(\d+)',
    re.DOTALL | re.IGNORECASE
)

def parse_messages(messages):
    """
    Return list of dicts with keys:
      site, pu, build_num, date_str, ts, status,
      cat, catLabel, err_type, err_snippet   (for FAILURE only)
    """
    results = []
    for msg in messages:
        ts = msg.get('ts', '0')
        full_text = ''
        for att in msg.get('attachments', []):
            full_text += att.get('text', '') + ' ' + att.get('fallback', '') + ' '
        m = INSTALL_RE.search(full_text)
        if m:
            status    = m.group(1).upper()
            build_num = int(m.group(2))
            pu        = m.group(3)
            site      = m.group(4)
            dt        = datetime.datetime.utcfromtimestamp(float(ts))
            date_str  = dt.strftime('%Y-%m-%d')
            item = dict(site=site, pu=pu, build_num=build_num,
                        date_str=date_str, ts=ts, status=status)
            if status == 'FAILURE':
                cat, label, typ, snippet = categorize_error(full_text)
                item.update(cat=cat, catLabel=label, err_type=typ, err_snippet=snippet)
            results.append(item)
    return results

# ── HTML update ───────────────────────────────────────────────────────────────

def _escape_js(s):
    return s.replace('\\', '\\\\').replace("'", "\\'").replace('\n', ' ')

def update_html(new_installs):
    """
    Append new SITES entries and ERRORS entries, update MONTHLY + stats.
    new_installs: list of dicts — deduplicated, newest-per-(site,pu).
    Returns count of entries added.
    """
    with open(HTML_PATH, encoding='utf-8') as f:
        c = f.read()

    # ── Existing (site, pu) pairs in SITES ──
    sites_m = re.search(r'const SITES=\[(.*?)\];', c, re.DOTALL)
    if not sites_m:
        raise RuntimeError("SITES array not found in HTML")
    existing_sites = set(re.findall(r"site:'(\d+)',pu:'([^']+)'", sites_m.group(1)))

    # ── Existing build numbers in ERRORS ──
    errors_m = re.search(r'const ERRORS=\[(.*?)\];', c, re.DOTALL)
    existing_errors = set(int(n) for n in re.findall(r'\bnum:(\d+)', errors_m.group(1) if errors_m else ''))

    new_sites_entries   = []
    new_errors_entries  = []
    added_success  = {}
    added_failures = {}
    added_fail_evts = {}

    for item in new_installs:
        site      = item['site']
        pu        = item['pu']
        build     = item['build_num']
        date      = item['date_str']
        status    = item['status']

        # ── SITES entry ──
        if (site, pu) not in existing_sites:
            existing_sites.add((site, pu))
            url   = f"{JENKINS_BASE}/{build}/"
            runs  = f"{status}({date})"
            new_sites_entries.append(
                f"  {{site:'{site}',pu:'{pu}',att:1,attLabel:'1st',"
                f"date:'{date}',src:'Jenkins',runs:'{runs}',url:'{url}'}}"
            )
            month_key = date[:7]
            if status == 'SUCCESS':
                added_success[month_key]    = added_success.get(month_key, 0) + 1
            else:
                added_failures[month_key]   = added_failures.get(month_key, 0) + 1
                added_fail_evts[month_key]  = added_fail_evts.get(month_key, 0) + 1

        # ── ERRORS entry (FAILURE only, deduplicated by build number) ──
        if status == 'FAILURE' and build not in existing_errors:
            existing_errors.add(build)
            cat     = item.get('cat', 'other')
            label   = item.get('catLabel', 'Unknown Error')
            typ     = item.get('err_type', 'real-fail')
            snippet = _escape_js(item.get('err_snippet', 'See Jenkins build'))
            new_errors_entries.append(
                f"  {{num:{build},site:'{site}',date:'{date}',"
                f"cat:'{cat}',catLabel:'{label}',type:'{typ}',err:'{snippet}'}}"
            )

    # ── Append to SITES ──
    if new_sites_entries:
        sites_end = re.search(r'(const SITES=\[.*?)(\n\];)', c, re.DOTALL)
        c = (c[:sites_end.end(1)]
             + ',\n' + ',\n'.join(new_sites_entries)
             + sites_end.group(2)
             + c[sites_end.end():])

    # ── Append to ERRORS ──
    if new_errors_entries:
        errors_end = re.search(r'(const ERRORS=\[.*?)(\n\];)', c, re.DOTALL)
        if errors_end:
            c = (c[:errors_end.end(1)]
                 + ',\n' + ',\n'.join(new_errors_entries)
                 + errors_end.group(2)
                 + c[errors_end.end():])

    if not new_sites_entries and not new_errors_entries:
        print("No new entries to add.")
        return 0

    # ── Update MONTHLY ──
    all_months = set(list(added_success.keys()) + list(added_failures.keys()))
    for month_key in all_months:
        dt = datetime.datetime.strptime(month_key, '%Y-%m')
        label = dt.strftime('%b %Y')
        succ_count      = added_success.get(month_key, 0)
        fail_count      = added_failures.get(month_key, 0)
        fail_evt_count  = added_fail_evts.get(month_key, 0)
        total_count     = succ_count + fail_count

        m = re.search(
            r"(\{month:'" + re.escape(label) + r"',total:)(\d+)"
            r"(,s1:)(\d+)(,s2:\d+,s3:\d+,succ:)(\d+)"
            r"(,failEvt:)(\d+)(,permFail:)(\d+)([^}]*?\})", c
        )
        if m:
            c = (c[:m.start()]
                 + m.group(1)  + str(int(m.group(2))  + total_count)
                 + m.group(3)  + str(int(m.group(4))  + succ_count)
                 + m.group(5)  + str(int(m.group(6))  + succ_count)
                 + m.group(7)  + str(int(m.group(8))  + fail_evt_count)
                 + m.group(9)  + str(int(m.group(10)) + fail_count)
                 + m.group(11)
                 + c[m.end():])
        else:
            new_m = (f",\n  {{month:'{label}',total:{total_count},s1:{succ_count},"
                     f"s2:0,s3:0,succ:{succ_count},failEvt:{fail_evt_count},"
                     f"permFail:{fail_count},partial:false,src:'Jenkins'}}")
            monthly_end = re.search(r'(const MONTHLY=\[.*?)(\n\];)', c, re.DOTALL)
            if monthly_end:
                c = (c[:monthly_end.end(1)]
                     + new_m
                     + monthly_end.group(2)
                     + c[monthly_end.end():])

    # ── Update hero month ──
    now = datetime.datetime.utcnow()
    cur_month = now.strftime('%b %Y')
    c = re.sub(r'(<div class="hero-month">)[^<]+(</div>)',
               f'\\g<1>{cur_month}\\g<2>', c)

    # ── Subtitle, stats, last-updated ──
    sites_body = re.search(r'const SITES=\[(.*?)\];', c, re.DOTALL).group(1)
    total = len(re.findall(r"site:'", sites_body))
    c = re.sub(r'<div class="sub">[^<]+</div>',
               f'<div class="sub">Jenkins &amp; Slack &nbsp;·&nbsp; {cur_month} &nbsp;·&nbsp; {total} installs on record</div>', c)
    c = re.sub(r'(<div class="stat-num" id="total-sites">)\d+', f'\\g<1>{total}', c)
    today_str = now.strftime('%b %d, %Y')
    c = re.sub(r'Last updated: [^<]+', f'Last updated: {today_str}', c)

    with open(HTML_PATH, 'w', encoding='utf-8') as f:
        f.write(c)

    print(f"Added {len(new_sites_entries)} SITES entries, "
          f"{len(new_errors_entries)} ERRORS entries.")
    return len(new_sites_entries)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
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

    new_last_ts = max(m['ts'] for m in messages)
    installs = parse_messages(messages)
    print(f"  {len(installs)} install events parsed.")

    # Deduplicate: keep newest per (pu, site) — if FAILURE then SUCCESS, SUCCESS wins
    seen = {}
    for item in sorted(installs, key=lambda x: float(x['ts']), reverse=True):
        key = (item['pu'], item['site'])
        if key not in seen:
            seen[key] = item
    unique_installs = list(seen.values())
    print(f"  {len(unique_installs)} unique (site,pu) pairs after dedup.")

    added = update_html(unique_installs)

    state.update({
        'last_ts':    new_last_ts,
        'last_run':   datetime.datetime.utcnow().isoformat(),
        'last_added': added,
    })
    with open(STATE_PATH, 'w') as f:
        json.dump(state, f, indent=2)

    print(f"Done. State saved (last_ts={new_last_ts}).")

if __name__ == '__main__':
    main()
