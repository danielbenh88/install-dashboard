#!/usr/bin/env python3
"""
Auto-updater for pu_dashboard.html (encrypted data edition).
Flow:
  1. Decrypt ENCRYPTED_BLOB in HTML  →  Python data dict
  2. Fetch new Slack messages
  3. Add new SITES + ERRORS entries, update MONTHLY
  4. Re-encrypt  →  write new blob back to HTML
  5. Update public stats (subtitle, last-updated) in HTML text

Required env vars:
  SLACK_TOKEN        — Slack user token (groups:history scope)
  DASHBOARD_PASSWORD — plain-text dashboard password (for encrypt/decrypt)
  JENKINS_USER       — Jenkins username  (optional, for error fetching)
  JENKINS_TOKEN      — Jenkins API token (optional, for error fetching)
"""

import os, re, json, base64, datetime, secrets, requests

from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SLACK_TOKEN        = os.environ['SLACK_TOKEN']
DASHBOARD_PASSWORD = os.environ['DASHBOARD_PASSWORD']
JENKINS_USER       = os.environ.get('JENKINS_USER', '')
JENKINS_TOKEN      = os.environ.get('JENKINS_TOKEN', '')
JENKINS_AUTH       = (JENKINS_USER, JENKINS_TOKEN) if JENKINS_USER and JENKINS_TOKEN else None

CHANNEL_ID   = 'C0A137H7BU7'
JENKINS_BASE = 'https://ci.cloud.uveye.xyz/job/versions_management/job/dealership/job/install_dealership_site'
HTML_PATH    = 'pu_dashboard.html'
STATE_PATH   = 'state.json'

# ── Crypto ────────────────────────────────────────────────────────────────────

def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=100000)
    return kdf.derive(password.encode())

def decrypt_blob(blob: dict) -> dict:
    salt = bytes.fromhex(blob['salt'])
    iv   = bytes.fromhex(blob['iv'])
    key  = _derive_key(DASHBOARD_PASSWORD, salt)
    ct   = base64.b64decode(blob['data'])
    plain = AESGCM(key).decrypt(iv, ct, None)
    return json.loads(plain.decode())

def encrypt_data(data: dict) -> dict:
    salt = secrets.token_bytes(16)
    iv   = secrets.token_bytes(12)
    key  = _derive_key(DASHBOARD_PASSWORD, salt)
    ct   = AESGCM(key).encrypt(iv, json.dumps(data, ensure_ascii=False).encode(), None)
    return {'salt': salt.hex(), 'iv': iv.hex(), 'data': base64.b64encode(ct).decode()}

def read_blob_from_html() -> dict:
    with open(HTML_PATH, encoding='utf-8') as f:
        html = f.read()
    m = re.search(r'const ENCRYPTED_BLOB = (\{.*?\});', html)
    if not m:
        raise RuntimeError("ENCRYPTED_BLOB not found in HTML — run setup_encryption.py first")
    return json.loads(m.group(1))

def write_blob_to_html(blob: dict, data: dict) -> None:
    with open(HTML_PATH, encoding='utf-8') as f:
        html = f.read()

    # Update blob
    blob_js = json.dumps(blob)
    html = re.sub(r'const ENCRYPTED_BLOB = \{.*?\};',
                  f'const ENCRYPTED_BLOB = {blob_js};', html)

    # Update public stats (not sensitive)
    now = datetime.datetime.utcnow()
    cur_month = now.strftime('%b %Y')
    total = len(data['SITES'])

    html = re.sub(r'<div class="sub">[^<]+</div>',
                  f'<div class="sub">Jenkins &amp; Slack &nbsp;·&nbsp; {cur_month} &nbsp;·&nbsp; {total} installs on record</div>',
                  html)
    html = re.sub(r'(<div class="stat-num" id="total-sites">)\d+', f'\\g<1>{total}', html)
    html = re.sub(r'(<div class="hero-month">)[^<]+(</div>)',
                  f'\\g<1>{cur_month}\\g<2>', html)
    today_str = now.strftime('%b %d, %Y')
    html = re.sub(r'Last updated: [^<]+', f'Last updated: {today_str}', html)

    with open(HTML_PATH, 'w', encoding='utf-8') as f:
        f.write(html)

# ── Slack helpers ─────────────────────────────────────────────────────────────

def slack_history(oldest=None, cursor=None):
    params = {'channel': CHANNEL_ID, 'limit': 200}
    if oldest: params['oldest'] = oldest
    if cursor: params['cursor'] = cursor
    r = requests.get('https://slack.com/api/conversations.history',
                     headers={'Authorization': f'Bearer {SLACK_TOKEN}'},
                     params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get('ok'):
        raise RuntimeError(f"Slack API error: {data.get('error')}")
    return data

def get_all_new_messages(oldest_ts):
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
    for cat, label, typ, pattern in ERROR_RULES:
        if pattern.search(text):
            m = pattern.search(text)
            snippet = text[max(0, m.start()-20):m.start()+120].replace('\n', ' ').strip()
            return cat, label, typ, snippet
    snippet = text[:120].replace('\n', ' ').strip() if text.strip() else 'See Jenkins build'
    return 'other', 'Unknown Error', 'real-fail', snippet

def fetch_jenkins_error(build_num):
    if not JENKINS_AUTH:
        return None
    url = f"{JENKINS_BASE}/{build_num}/consoleText"
    try:
        r = requests.get(url, auth=JENKINS_AUTH, timeout=20)
        if r.status_code != 200:
            return None
        lines = r.text.splitlines()
        tail = lines[-60:]
        error_lines = [l.strip() for l in tail if any(k in l for k in
            ['ERROR', 'FAILED', 'fatal:', 'Exception', 'Error:', '503', 'FAILURE'])]
        if error_lines:
            return max(error_lines, key=len)[:160]
        for l in reversed(tail):
            if l.strip():
                return l.strip()[:160]
    except Exception as e:
        print(f"  Jenkins fetch failed for #{build_num}: {e}")
    return None

# ── Message parsing ───────────────────────────────────────────────────────────

INSTALL_RE = re.compile(
    r'(SUCCESS|FAILURE).*?install_dealership_site.*?/(\d+)/.*?#Site[:`\s]*(atlas-lite|artelios)-(\d+)',
    re.DOTALL | re.IGNORECASE
)

def parse_messages(messages):
    results = []
    for msg in messages:
        ts = msg.get('ts', '0')
        full_text = ''.join(
            att.get('text', '') + ' ' + att.get('fallback', '') + ' '
            for att in msg.get('attachments', [])
        )
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
                jenkins_err = fetch_jenkins_error(build_num)
                combined    = (jenkins_err or '') + ' ' + full_text
                cat, label, typ, snippet = categorize_error(combined)
                if jenkins_err:
                    snippet = jenkins_err[:160]
                item.update(cat=cat, catLabel=label, err_type=typ, err_snippet=snippet)
            results.append(item)
    return results

# ── Update data dict ──────────────────────────────────────────────────────────

def update_data(data: dict, new_installs: list) -> int:
    """Mutates data dict. Returns count of new SITES entries added."""
    existing_sites = {(s['site'], s['pu']) for s in data['SITES']}
    existing_errs  = {e['num'] for e in data['ERRORS']}
    added = 0

    for item in new_installs:
        site   = item['site']
        pu     = item['pu']
        build  = item['build_num']
        date   = item['date_str']
        status = item['status']

        # ── SITES ──
        if (site, pu) not in existing_sites:
            existing_sites.add((site, pu))
            data['SITES'].append({
                'site': site, 'pu': pu, 'att': 1, 'attLabel': '1st',
                'date': date, 'src': 'Jenkins',
                'runs': f"{status}({date})",
                'url':  f"{JENKINS_BASE}/{build}/"
            })
            added += 1

            # ── MONTHLY ──
            month_label = datetime.datetime.strptime(date[:7], '%Y-%m').strftime('%b %Y')
            month = next((m for m in data['MONTHLY'] if m['month'] == month_label), None)
            if month:
                month['total'] += 1
                if status == 'SUCCESS':
                    month['s1']   += 1
                    month['succ'] += 1
                else:
                    month['failEvt']  += 1
                    month['permFail'] += 1
            else:
                data['MONTHLY'].append({
                    'month': month_label, 'total': 1,
                    's1': 1 if status == 'SUCCESS' else 0,
                    's2': 0, 's3': 0,
                    'succ':     1 if status == 'SUCCESS' else 0,
                    'failEvt':  0 if status == 'SUCCESS' else 1,
                    'permFail': 0 if status == 'SUCCESS' else 1,
                    'partial': True, 'src': 'Jenkins'
                })

        # ── ERRORS ──
        if status == 'FAILURE' and build not in existing_errs:
            existing_errs.add(build)
            data['ERRORS'].append({
                'num': build, 'site': site, 'date': date,
                'cat':      item.get('cat', 'other'),
                'catLabel': item.get('catLabel', 'Unknown Error'),
                'type':     item.get('err_type', 'real-fail'),
                'err':      item.get('err_snippet', 'See Jenkins build')
            })

    return added

# ── Always update date ────────────────────────────────────────────────────────

def update_date_only():
    with open(HTML_PATH, encoding='utf-8') as f:
        c = f.read()
    now = datetime.datetime.utcnow()
    c2 = re.sub(r'Last updated: [^<]+', f'Last updated: {now.strftime("%b %d, %Y")}', c)
    if c2 != c:
        with open(HTML_PATH, 'w', encoding='utf-8') as f:
            f.write(c2)
        print(f"Date updated to {now.strftime('%b %d, %Y')}.")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Load state
    state = {}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            state = json.load(f)
    last_ts = state.get('last_ts', '0')

    print(f"Fetching Slack messages since TS={last_ts} …")
    messages = get_all_new_messages(last_ts)
    print(f"  {len(messages)} new messages found.")

    if not messages:
        print("Nothing new — updating date only.")
        update_date_only()
        return

    new_last_ts = max(m['ts'] for m in messages)
    installs = parse_messages(messages)
    print(f"  {len(installs)} install events parsed.")

    # Deduplicate: keep newest per (pu, site)
    seen = {}
    for item in sorted(installs, key=lambda x: float(x['ts']), reverse=True):
        key = (item['pu'], item['site'])
        if key not in seen:
            seen[key] = item
    unique = list(seen.values())
    print(f"  {len(unique)} unique (site,pu) pairs after dedup.")

    # Decrypt current data
    print("Decrypting current dashboard data...")
    blob = read_blob_from_html()
    data = decrypt_blob(blob)
    print(f"  Loaded {len(data['SITES'])} SITES, {len(data['ERRORS'])} ERRORS.")

    # Update data
    added = update_data(data, unique)
    print(f"  Added {added} new SITES entries.")

    # Re-encrypt and write
    print("Re-encrypting and writing HTML...")
    new_blob = encrypt_data(data)
    write_blob_to_html(new_blob, data)

    # Save state
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
