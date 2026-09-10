#!/usr/bin/env python3
"""
One-time setup: encrypt dashboard data and rewrite HTML to use encrypted blob.
Run: DASHBOARD_PASSWORD='UvEye2026!' python setup_encryption.py

After this, update_dashboard.py handles all ongoing updates (decrypt → update → re-encrypt).
"""
import os, re, json, base64, subprocess, sys, secrets

try:
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    print("Run: pip install cryptography"); sys.exit(1)

HTML_PATH = 'pu_dashboard.html'
PASSWORD  = os.environ.get('DASHBOARD_PASSWORD', '')
if not PASSWORD:
    print("Set DASHBOARD_PASSWORD env var"); sys.exit(1)

# ── Encrypt / decrypt ─────────────────────────────────────────────────────────

def encrypt(data: dict) -> dict:
    salt = secrets.token_bytes(16)
    kdf  = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=100000)
    key  = kdf.derive(PASSWORD.encode())
    iv   = secrets.token_bytes(12)
    ct   = AESGCM(key).encrypt(iv, json.dumps(data, ensure_ascii=False).encode(), None)
    return {'salt': salt.hex(), 'iv': iv.hex(), 'data': base64.b64encode(ct).decode()}

# ── Extract JS arrays from HTML via Node.js ───────────────────────────────────

NODE_SCRIPT = r"""
const fs = require('fs');
const html = fs.readFileSync(process.env.HTML_FILE, 'utf8');
let MONTHLY=[], ERROR_CATS=[], WEEKLY=[], ERRORS=[], SITES=[];
for(const name of ['MONTHLY','ERROR_CATS','WEEKLY','ERRORS','SITES']){
  const re = new RegExp(`(?:const|var)\\s+${name}\\s*=\\s*\\[([\\s\\S]*?)\\];`);
  const m = html.match(re);
  if(m){ try{ eval(`${name}=[${m[1]}]`); }catch(e){ console.error(name,e.message); } }
}
process.stdout.write(JSON.stringify({MONTHLY, ERROR_CATS, WEEKLY, ERRORS, SITES}));
"""

def extract_data() -> dict:
    env = {**os.environ, 'HTML_FILE': os.path.abspath(HTML_PATH)}
    r = subprocess.run(['node', '-e', NODE_SCRIPT],
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        print('Node error:', r.stderr); sys.exit(1)
    if r.stderr:
        print('Node warnings:', r.stderr)
    return json.loads(r.stdout)

# ── New login gate HTML ───────────────────────────────────────────────────────

NEW_GATE_TEMPLATE = '''\
<script>
// ── Auth + decrypt gate ───────────────────────────────────────────────────
const PASS_HASH = '__PASS_HASH__';
const ENCRYPTED_BLOB = __ENCRYPTED_BLOB__;
var MONTHLY=[],ERROR_CATS=[],WEEKLY=[],ERRORS=[],SITES=[];

function _h2b(h){return new Uint8Array(h.match(/.{2}/g).map(b=>parseInt(b,16)));}
function _b2b(s){const b=atob(s);return new Uint8Array([...b].map(c=>c.charCodeAt(0)));}
async function _sha256(s){
  const b=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(s));
  return Array.from(new Uint8Array(b)).map(x=>x.toString(16).padStart(2,'0')).join('');
}
async function _key(pw){
  const km=await crypto.subtle.importKey('raw',new TextEncoder().encode(pw),'PBKDF2',false,['deriveKey']);
  return crypto.subtle.deriveKey(
    {name:'PBKDF2',salt:_h2b(ENCRYPTED_BLOB.salt),iterations:100000,hash:'SHA-256'},
    km,{name:'AES-GCM',length:256},false,['decrypt']
  );
}
async function _decrypt(pw){
  try{
    const k=await _key(pw);
    const p=await crypto.subtle.decrypt({name:'AES-GCM',iv:_h2b(ENCRYPTED_BLOB.iv)},k,_b2b(ENCRYPTED_BLOB.data));
    return JSON.parse(new TextDecoder().decode(p));
  }catch(e){return null;}
}
function _inject(d){
  if(d.MONTHLY)   MONTHLY.splice(0,0,...d.MONTHLY);
  if(d.ERROR_CATS)ERROR_CATS.splice(0,0,...d.ERROR_CATS);
  if(d.WEEKLY)    WEEKLY.splice(0,0,...d.WEEKLY);
  if(d.ERRORS)    ERRORS.splice(0,0,...d.ERRORS);
  if(d.SITES)     SITES.splice(0,0,...d.SITES);
}
(function(){
  document.documentElement.style.visibility='hidden';
  window.addEventListener('DOMContentLoaded',async function(){
    document.documentElement.style.visibility='';
    if(sessionStorage.getItem('auth')==='1'){
      const c=sessionStorage.getItem('_dd');
      if(c){_inject(JSON.parse(c));_initDashboard();return;}
    }
    const overlay=document.createElement('div');
    overlay.id='login-overlay';
    overlay.innerHTML=`
      <div style="background:#0a1628;min-height:100vh;display:flex;align-items:center;justify-content:center;">
        <div style="background:#fff;border-radius:16px;padding:40px 48px;box-shadow:0 8px 40px rgba(0,0,0,.35);text-align:center;min-width:320px;">
          <div style="font-size:30px;font-family:'Exo 2','Arial Black',Arial,sans-serif;font-weight:900;margin-bottom:4px;">
            <span style="color:#F4722B;">UV</span><span style="color:#0a1628;">EYE</span>
          </div>
          <div style="font-size:11px;color:#aaa;letter-spacing:.5px;text-transform:uppercase;margin-bottom:24px;">Install Dashboard</div>
          <input id="gate-pw" type="password" placeholder="Password"
            style="width:100%;box-sizing:border-box;padding:10px 14px;border:1px solid #ddd;border-radius:8px;font-size:14px;margin-bottom:12px;outline:none;"/>
          <button id="gate-btn"
            style="width:100%;padding:10px;background:#F4722B;color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:700;cursor:pointer;letter-spacing:.3px;">
            Enter Dashboard
          </button>
          <div id="gate-err" style="color:#e53935;font-size:12px;margin-top:10px;display:none;">
            Incorrect password. Try again.
          </div>
        </div>
      </div>`;
    document.body.prepend(overlay);
    document.body.style.overflow='hidden';
    const input=document.getElementById('gate-pw');
    const btn  =document.getElementById('gate-btn');
    const err  =document.getElementById('gate-err');
    async function tryLogin(){
      err.style.display='none';
      const h=await _sha256(input.value);
      if(h===PASS_HASH){
        const data=await _decrypt(input.value);
        if(!data){err.textContent='Decryption error.';err.style.display='block';return;}
        sessionStorage.setItem('auth','1');
        sessionStorage.setItem('_dd',JSON.stringify(data));
        _inject(data);
        overlay.remove();
        document.body.style.overflow='';
        _initDashboard();
      }else{err.style.display='block';}
    }
    btn.addEventListener('click',tryLogin);
    input.addEventListener('keydown',e=>{if(e.key==='Enter')tryLogin();});
  });
})();
</script>'''

# ── Transform HTML ────────────────────────────────────────────────────────────

def transform_html(blob: dict) -> str:
    with open(HTML_PATH, encoding='utf-8') as f:
        html = f.read()

    blob_js = json.dumps(blob)
    gate = NEW_GATE_TEMPLATE.replace('__ENCRYPTED_BLOB__', blob_js)

    # 1. Replace old login gate
    html = re.sub(
        r'<script>\s*// ── Login gate ─[\s\S]*?</script>',
        gate, html
    )

    # 2. Remove the data arrays section (replace with a comment)
    html = re.sub(
        r'// (Weekly breakdown|Error categories)[\s\S]*?const SITES=\[[\s\S]*?\];',
        '// Data arrays populated after decrypt — see login gate above',
        html, flags=re.DOTALL
    )

    # 3. Wrap all init render calls in _initDashboard()
    html = re.sub(
        r'(populateFilters\(\);[\s\S]*?renderSites\(\);)',
        r'function _initDashboard(){\n\1\n}',
        html, flags=re.DOTALL
    )

    return html

# ── Main ──────────────────────────────────────────────────────────────────────

print("Extracting data from HTML via Node.js...")
data = extract_data()
print(f"  SITES: {len(data['SITES'])}, MONTHLY: {len(data['MONTHLY'])}, "
      f"ERRORS: {len(data['ERRORS'])}, WEEKLY: {len(data['WEEKLY'])}, "
      f"ERROR_CATS: {len(data['ERROR_CATS'])}")

print("Encrypting data...")
blob = encrypt(data)
print(f"  Encrypted blob: {len(blob['data'])} chars")

print("Transforming HTML...")
new_html = transform_html(blob)

with open(HTML_PATH, 'w', encoding='utf-8') as f:
    f.write(new_html)

print(f"\nDone. {HTML_PATH} is now encrypted.")
print("Add DASHBOARD_PASSWORD to GitHub secrets, then commit and push.")
