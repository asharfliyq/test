from __future__ import annotations

import os
import sys
import shutil
import subprocess
import re
import requests
import concurrent.futures
from pathlib import Path
from datetime import datetime
import secrets
import random
from collections import Counter, defaultdict
try:
    import tkinter as tk
    from tkinter import filedialog
except ModuleNotFoundError:
    tk = None
    filedialog = None
import json
from urllib.parse import quote, urlparse, parse_qs
try:
    import PyPDF2  # type: ignore
except ImportError:
    PyPDF2 = None
import threading
import atexit
from http.server import HTTPServer, BaseHTTPRequestHandler
from title import generate_title, strip_leading_site_prefix

# ========================= CONFIGURATION =========================
# --- API Keys ---
IMGBB_API_KEY = "c68b06c4f7daabb90d696eafa1f25a5c"   # CHANGE THIS! GET Free API KEY at https://api.imgbb.com/
FREEIMAGE_API_KEY = "6d207e02198a847aa98d0a2a901485a5"

# --- Settings ---
IMAGE_HOST = "imgbb"           # "imgbb", "freeimage"
# Note: legacy "freehost" alias is deprecated; use "freeimage" instead.
# Backward compatibility for deprecated alias.
_DEPRECATED_HOST_ALIASES = {"freehost": "freeimage"}
# imgbb → max file size 32 MB
# freeimage → max file size 64 MB

SCREENSHOT_COUNT = 6               # Number of screenshots to take
# Default now keeps full-frame captures; set to True to restore auto-cropping of letter/pillarbox bars.
CROP_BLACK_BARS = False
LOSSLESS_SCREENSHOT = True         # If True, capture screenshots in lossless / max quality
CROP_LUMINANCE_THRESHOLD = 24      # cropdetect limit: pixels brighter than this are considered content
CROP_ROUNDING = 16                 # cropdetect round: align crop dimensions to this many pixels
CROP_RESET_INTERVAL = 0            # cropdetect reset: 0 = never reset during single-frame analysis
CREATE_TORRENT_FILE = True         # This creates the .torrent file
SKIP_TXT = True                    # If True, the script will NOT save the description as a .txt file
TRACKER_ANNOUNCE = "https://tracker.torrentbd.net/announce"
PRIVATE_TORRENT = True
COPY_TO_CLIPBOARD = True           # Copies description in your clipboard
USE_WP_PROXY = False
USE_GUI_FILE_PICKER = False        # If True, use the Windows file picker instead of the command line to select files

# --- Upload concurrency (mainly for image uploads) ---
MAX_CONCURRENT_UPLOADS = 16        # Hard cap to avoid overwhelming the host/API
MIN_IO_WORKERS = 4                 # Baseline for network-bound uploads
IO_WORKER_MULTIPLIER = 2           # Modest multiplier over CPU count for I/O tasks
UPLOAD_TIMEOUT = 90                # Balanced timeout: allows slow hosts while still limiting stalls

# --- AUTO DELETE SETTINGS ---
# If True, deletes the generated .torrent (and .txt if created) when the script closes.
# latest.json is ALWAYS deleted on exit/start regardless of this setting.
AUTO_DELETE_CREATED_FILES = True

# --- SERVER SETTINGS ---
START_HTTP_SERVER = True               # True = Start local server for Tampermonkey sync
HTTP_PORT = 40452                      # Port for the web app (UI + API endpoints)
# ================================================================

VIDEO_EXTS = {'.mkv', '.mp4', '.avi', '.mov', '.m4v', '.webm', '.flv', '.wmv', '.mpg', '.mpeg', '.ts', '.m2ts'}
AUDIO_EXTS = {'.flac', '.mp3', '.m4a', '.aac', '.ogg', '.opus', '.wav', '.ape', '.wv', '.alac'}
PDF_EXTS = {'.pdf'}
AUDIO_TRACKLIST_SINGLES_SECTION = "Singles"
AUDIO_NO_COVER_PLACEHOLDER = "No cover"
SPECTROGRAM_TITLE_COLOR = "#cddc39"
CATEGORY_BOOKS = "36"
CATEGORY_PRO_WRESTLING = os.getenv("CATEGORY_PRO_WRESTLING", "6")
WRESTLING_TERMS = (
    r"AEW",
    r"WWE",
    r"WWF",
    r"ROH",
    r"NJPW",
    r"TNA",
    r"Impact",
    r"Pro[\s-]?Wrestling",
    r"Wrestlemania",
    r"Royal\s+Rumble",
    r"Smackdown",
    r"RAW",
    r"Dynamite",
    r"Rampage",
    r"Collision",
    r"Full\s+Gear",
    r"All\s+Out",
    r"Forbidden\s+Door",
)
WRESTLING_REGEX = re.compile(r"\b(?:" + "|".join(WRESTLING_TERMS) + r")\b", re.I)

_FANSUB_PACK_RANGE_RE = re.compile(r'\b\d{1,3}\s*~\s*\d{1,3}\b')

LATEST_JSON = None
GENERATED_TORRENT = None
GENERATED_TXT = None
GENERATED_SPECTROGRAM = None
EXTRACTED_COVER = None
COVER_PATH: Path | None = None
GENERATED_PDF_IMAGES: list[Path] = []
INDEX_HTML = None

class c:
    RESET   = '\033[0m'
    BOLD    = '\033[1m'
    DIM     = '\033[2m'
    PURPLE  = '\033[95m'
    CYAN    = '\033[96m'
    GREEN   = '\033[92m'
    YELLOW  = '\033[93m'
    RED     = '\033[91m'
    GRAY    = '\033[90m'
    WHITE   = '\033[97m'

def search_imdb(title: str) -> str | None:
    clean = re.sub(
        r'\b(2160p|1080p|720p|480p|4K|UHD|SDR|HDR10?\+?|HLG|DV|DOVI|'
        r'BluRay|BDRip|BRRip|WEB-DL|WEBRip|HDTC|HDCAM|HDTS|DVDRip|CAM|'
        r'x265|x264|HEVC|AVC|H\.?265|H\.?264|AV1|AAC|DDP?|DD\+|DTS|TrueHD|'
        r'FLAC|Opus|REMUX|REPACK|PROPER|EXTENDED|THEATRICAL|DC|IMAX|LIMITED|MULTI|'
        r'AMZN|NF|DSNP|HMAX|PCOK|SHO|PMTP|ATVP)\b',
        '', title, flags=re.I
    )

    clean = re.sub(r'\[[^\]]*\]', '', clean)

    clean = re.sub(r'[ \t]*-[ \t]*\S+[ \t]*$', '', clean)
    clean = re.sub(r'[._]+', ' ', clean)
    clean = re.sub(r'\s+', ' ', clean).strip(' .-')
    if not clean:
        return None
    _headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        first_char = clean[0].lower()
        resp = requests.get(
            f"https://v3.sg.media-imdb.com/suggestion/{first_char}/{quote(clean)}.json",
            headers=_headers,
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            items = data.get('d') or []
            if items and items[0].get('id'):
                return f"https://www.imdb.com/title/{items[0]['id']}/"
    except Exception as exc:
        error(f"IMDb suggestion API failed for '{clean}': {exc}")

    try:
        resp = requests.get(
            f"https://www.imdb.com/find/?q={quote(clean)}&s=tt",
            headers={**_headers, "Accept": "text/html,application/xhtml+xml"},
            timeout=15,
        )
        if resp.status_code == 200:
            m = re.search(r'href="/title/(tt\d+)/', resp.text)
            if m:
                return f"https://www.imdb.com/title/{m.group(1)}/"
    except Exception as exc:
        error(f"IMDb search fallback failed for '{clean}': {exc}")
    return None

_IMDB_CLEAN_RE = re.compile(
    r'\b(2160p|1080p|720p|480p|4K|UHD|SDR|HDR10?\+?|HLG|DV|DOVI|'
    r'BluRay|BDRip|BRRip|WEB-DL|WEBRip|HDTC|HDCAM|HDTS|DVDRip|CAM|'
    r'x265|x264|HEVC|AVC|H\.?265|H\.?264|AV1|AAC|DDP?|DD\+|DTS|TrueHD|'
    r'FLAC|Opus|REMUX|REPACK|PROPER|EXTENDED|THEATRICAL|DC|IMAX|LIMITED|MULTI|'
    r'AMZN|NF|DSNP|HMAX|PCOK|SHO|PMTP|ATVP)\b',
    re.I,
)


_STEM_SAMPLE_RE = re.compile(r'(?:^|[^a-zA-Z])sample(?:[^a-zA-Z]|$)', re.I)

def _clean_title_for_imdb(title: str) -> str:
    clean = _IMDB_CLEAN_RE.sub('', title)

    clean = re.sub(r'\[[^\]]{0,300}\]', '', clean)

    clean = re.sub(r'(?<=\S)[ \t]+-[ \t]+\S+$', '', clean)
    clean = re.sub(r'[._]+', ' ', clean)
    return re.sub(r'\s+', ' ', clean).strip(' .-')

_IMDB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

def search_imdb_multi(title: str) -> list[dict]:
    clean = _clean_title_for_imdb(title)
    if not clean:
        return []
    try:
        first_char = clean[0].lower()
        resp = requests.get(
            f"https://v3.sg.media-imdb.com/suggestion/{first_char}/{quote(clean)}.json",
            headers=_IMDB_HEADERS,
            timeout=10,
        )
        if resp.status_code == 200:
            items = resp.json().get('d') or []
            results = []
            for item in items:
                if not item.get('id'):
                    continue
                poster = ""
                img = item.get('i')
                if isinstance(img, dict):
                    poster = img.get('imageUrl', '')
                results.append({
                    'id': item['id'],
                    'title': item.get('l', ''),
                    'year': item.get('y') or '',
                    'type': item.get('q', ''),
                    'poster': poster,
                })
            return results
    except Exception as exc:
        error(f"IMDb multi-search failed for '{clean}': {exc}")
    return []

_WEBAPP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>TorrentBD Upload</title>
<style>
:root{--bg:#0e0e10;--surface:#18181c;--border:#2c2c34;--text:#e2e2e8;--muted:#6a6a7a;
  --accent:#7c6ff7;--green:#4ade80;--yellow:#facc15;--r:10px;--sb:148px}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,-apple-system,sans-serif;
  min-height:100vh;padding:16px 16px 16px calc(var(--sb) + 20px);max-width:calc(860px + var(--sb) + 20px);margin:0 auto}
#sidebar{position:fixed;left:0;top:0;bottom:0;width:var(--sb);background:var(--surface);
  border-right:1px solid var(--border);padding:14px 10px;display:flex;flex-direction:column;
  gap:12px;overflow:hidden;z-index:50}
.sb-head{font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:1.2px;
  color:var(--yellow);padding-bottom:6px;border-bottom:1px solid var(--border)}
.sb-item{display:flex;flex-direction:column;gap:4px}
.sb-lbl{font-size:.63rem;color:var(--muted);display:flex;justify-content:space-between;align-items:center}
.sb-pct{font-size:.63rem;color:var(--text);font-weight:600}
.sb-bar{height:5px;background:var(--border);border-radius:3px;overflow:hidden}
.sb-fill{height:100%;border-radius:3px;width:0%;transition:width .5s ease}
.sb-val{font-size:.58rem;color:var(--muted)}
header{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;
  padding-bottom:16px;border-bottom:1px solid var(--border);margin-bottom:20px}
.logo{font-size:clamp(.7rem,1.8vw,1rem);font-weight:700;color:var(--yellow);letter-spacing:.3px;flex:1;text-align:center}
.badges{display:flex;gap:6px;flex-wrap:wrap}
.badge{padding:3px 9px;border-radius:20px;font-size:.7rem;font-weight:600;letter-spacing:.4px}
.cat{background:#1a1a30;color:#9090e0;border:1px solid #2a2a60}
.lang{background:#0f2010;color:#70c070;border:1px solid #1a4020}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:16px;margin-bottom:14px}
.lbl{font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:1px;
  color:var(--muted);margin-bottom:10px}
.title-box{font-size:.9rem;line-height:1.55;word-break:break-word;padding:9px 11px;
  background:var(--bg);border:1px solid var(--border);border-radius:6px;
  margin-bottom:11px;color:var(--text)}
.btns{display:flex;gap:7px;flex-wrap:wrap}
button{padding:6px 13px;border-radius:6px;border:1px solid var(--border);
  background:#22222a;color:var(--text);cursor:pointer;font-size:.8rem;font-weight:500;
  transition:background .12s,color .12s,border-color .12s;white-space:nowrap}
button:hover:not(:disabled){background:#2a2a36}
button:active:not(:disabled){transform:scale(.97)}
button:disabled{opacity:.38;cursor:not-allowed}
button.ok{border-color:var(--green);color:var(--green);background:#0a1f10}
button.dl{border-color:#4a8fc0;color:#80b8e0;background:#0a1828}
.imdb-out{margin-top:9px;font-size:.79rem;color:var(--muted);word-break:break-all}
.imdb-out a{color:#7ab4f0;text-decoration:none}
.imdb-out a:hover{text-decoration:underline}
.desc-box{font-family:'Courier New',monospace;font-size:.75rem;line-height:1.5;
  white-space:pre-wrap;word-break:break-word;max-height:260px;overflow-y:auto;
  padding:9px 11px;background:var(--bg);border:1px solid var(--border);
  border-radius:6px;margin-bottom:11px;color:#b0bac6;
  scrollbar-width:thin;scrollbar-color:var(--border) transparent}
.torrent-name{font-size:.79rem;color:var(--muted);margin-bottom:9px;word-break:break-all}
#loading{display:flex;flex-direction:column;align-items:center;justify-content:center;
  min-height:60vh;gap:14px;color:var(--muted);font-size:.88rem}
.spin{width:26px;height:26px;border:2px solid var(--border);border-top-color:var(--accent);
  border-radius:50%;animation:sp .7s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.toast{position:fixed;bottom:18px;right:18px;background:#22222a;border:1px solid var(--border);
  border-radius:8px;padding:8px 16px;font-size:.82rem;color:var(--green);
  opacity:0;transform:translateY(6px);transition:opacity .18s,transform .18s;
  pointer-events:none;z-index:9999}
.toast.show{opacity:1;transform:translateY(0)}
@media(max-width:600px){
  :root{--sb:0px}
  #sidebar{display:none}
  body{padding-left:16px}
}
@media(max-width:480px){.btns{flex-direction:column}button{width:100%}}
#imdb-overlay{position:fixed;inset:0;background:rgba(0,0,0,.8);z-index:10000;display:none;backdrop-filter:blur(2px)}
#imdb-overlay.show{display:block}
#imdb-modal{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);
  width:min(450px,95vw);background:#1e293b;border:1px solid #334155;border-radius:12px;
  z-index:10001;padding:20px;display:none;flex-direction:column;max-height:85vh;
  box-shadow:0 25px 50px -12px rgba(0,0,0,.8)}
#imdb-modal.show{display:flex}
#imdb-search-inp{width:100%;background:#0f172a;border:1px solid #334155;color:#fff;
  padding:12px;border-radius:8px;font-size:15px;outline:none;margin-bottom:10px;
  box-sizing:border-box}
#imdb-search-inp:focus{border-color:#38bdf8}
#imdb-results-list{overflow-y:auto;flex-grow:1;max-height:60vh}
#imdb-results-list::-webkit-scrollbar{width:6px}
#imdb-results-list::-webkit-scrollbar-thumb{background:#334155;border-radius:3px}
.imdb-item{display:flex;padding:12px;cursor:pointer;border-bottom:1px solid #334155;
  align-items:center;border-radius:6px;gap:0}
.imdb-item:hover{background:#334155}
.imdb-item img{width:40px;height:56px;margin-right:15px;object-fit:cover;
  border-radius:4px;background:#000;flex-shrink:0}
.imdb-item .iinfo b{color:#e2e2e8;display:block}
.imdb-item .iinfo small{color:#64748b}
</style>
</head>
<body>
<div id="sidebar">
  <div class="sb-head">\u26a1 System</div>
  <div class="sb-item">
    <div class="sb-lbl"><span>CPU</span><span class="sb-pct" id="cpu-pct">--%</span></div>
    <div class="sb-bar"><div class="sb-fill" id="cpu-fill" style="background:var(--accent)"></div></div>
  </div>
  <div class="sb-item">
    <div class="sb-lbl"><span>RAM</span><span class="sb-pct" id="ram-pct">--%</span></div>
    <div class="sb-bar"><div class="sb-fill" id="ram-fill" style="background:var(--green)"></div></div>
    <div class="sb-val" id="ram-val"></div>
  </div>
  <div class="sb-item">
    <div class="sb-lbl"><span>Disk</span><span class="sb-pct" id="disk-pct">--%</span></div>
    <div class="sb-bar"><div class="sb-fill" id="disk-fill" style="background:var(--yellow)"></div></div>
    <div class="sb-val" id="disk-val"></div>
  </div>
</div>
<div id="loading"><div class="spin"></div><span>Waiting for data\u2026</span></div>
<div id="app" style="display:none">
  <header>
    <div style="flex:1"></div>
    <span class="logo">\u26a1 TorrentBD Lazy Upload</span>
    <div class="badges" style="flex:1;justify-content:flex-end;display:flex;gap:6px;flex-wrap:wrap">
      <span class="badge cat" id="cat-badge"></span>
      <span class="badge lang" id="lang-badge"></span>
    </div>
  </header>

  <div class="card">
    <div class="lbl">Title</div>
    <div class="title-box" id="title-box"></div>
    <div class="btns">
      <button id="b-ct" onclick="copyTitle()">\U0001F4CB Copy Title</button>
      <button id="b-si" onclick="searchIMDb()">\U0001F50D Search IMDb</button>
      <button id="b-ci" onclick="copyIMDb()" disabled>\U0001F517 Copy IMDb URL</button>
    </div>
    <div class="imdb-out" id="imdb-out"></div>
  </div>

  <div class="card">
    <div class="lbl">Description \u00b7 BBCode</div>
    <pre class="desc-box" id="desc-box"></pre>
    <div class="btns">
      <button id="b-cd" onclick="copyDesc()">\U0001F4CB Copy Description</button>
    </div>
  </div>

  <div class="card">
    <div class="lbl">Torrent File</div>
    <div class="torrent-name" id="torrent-name"></div>
    <div class="btns">
      <button class="dl" onclick="downloadTorrent()">\u2b07 Download Torrent</button>
    </div>
  </div>
</div>
<div id="imdb-overlay" onclick="closeImdbModal()"></div>
<div id="imdb-modal">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:15px">
    <span style="font-weight:700;color:#f5c518;font-size:16px">&#127916; IMDb Search</span>
    <span onclick="closeImdbModal()" style="cursor:pointer;color:#94a3b8;font-size:20px">&#x2715;</span>
  </div>
  <input type="text" id="imdb-search-inp" placeholder="Type title to search\u2026" autocomplete="off">
  <div id="imdb-results-list"></div>
</div>
<div class="toast" id="toast"></div>
<script>
const CAT={
  "0":"Unknown","1":"DVDRip Movie","4":"CAM Movie","5":"SD TV Episode",
  "6":"Pro Wrestling","22":"MP3 Music","24":"BluRay Movie","28":"Anime Episode",
  "36":"Books","41":"SD TV Season","42":"SD BluRay Movie","46":"HDRip Movie",
  "47":"HD BluRay Movie","55":"HD WEB-DL Movie","61":"HD TV Episode",
  "62":"HD TV Season","71":"FLAC Music","76":"HD Remux Movie","80":"UHD BluRay Movie",
  "82":"UHD WEB-DL Movie","83":"WEBRip Movie","84":"UHD TV Episode",
  "85":"UHD TV Season","86":"UHD Remux Movie"
};
const LANG={
  "0":"Unknown","1":"English","2":"French","3":"Hindi","4":"Urdu","5":"Chinese",
  "6":"Spanish","7":"Japanese","8":"Bengali","9":"German","10":"Korean",
  "11":"Telugu","12":"Italian","13":"Russian","14":"Bulgarian","15":"Czech",
  "16":"Filipino","17":"Hungarian","18":"Arabic","19":"Serbian","20":"Swedish",
  "21":"Tamil","22":"Turkish","23":"Vietnamese","24":"Danish","25":"Dutch",
  "26":"Finnish","27":"Greek","28":"Hebrew","30":"Icelandic","31":"Indonesian",
  "32":"Irish","33":"Malayalam","34":"Marathi","35":"Norwegian","36":"Persian",
  "37":"Polish","38":"Portuguese","39":"Romanian","40":"Thai","41":"Kannada",
  "43":"Panjabi"
};
let D=null,imdbUrl=null,toastT=null;

function showToast(m){
  const t=document.getElementById('toast');
  t.textContent=m;t.classList.add('show');
  clearTimeout(toastT);toastT=setTimeout(()=>t.classList.remove('show'),2000);
}
async function cpText(s){
  try{await navigator.clipboard.writeText(s)}
  catch(e){
    const a=document.createElement('textarea');a.value=s;
    a.style.cssText='position:fixed;opacity:0';document.body.appendChild(a);
    a.select();document.execCommand('copy');document.body.removeChild(a);
  }
}
function flash(id,txt){
  const b=document.getElementById(id);if(!b)return;
  const o=b.textContent;b.textContent=txt;b.classList.add('ok');
  setTimeout(()=>{b.textContent=o;b.classList.remove('ok')},1500);
}
async function copyTitle(){if(!D)return;await cpText(D.title);showToast('\u2713 Title copied');flash('b-ct','\u2713 Copied')}
async function copyDesc(){
  if(!D)return;
  await cpText(D.description);
  showToast('\u2713 Description copied');flash('b-cd','\u2713 Copied');
}
async function copyIMDb(){if(!imdbUrl)return;await cpText(imdbUrl);showToast('\u2713 IMDb URL copied');flash('b-ci','\u2713 Copied')}
let imdbSearchTimer=null;
function openImdbModal(){
  const overlay=document.getElementById('imdb-overlay');
  const modal=document.getElementById('imdb-modal');
  overlay.classList.add('show');modal.classList.add('show');
  const inp=document.getElementById('imdb-search-inp');
  inp.focus();
  if(D&&D.title&&!inp.value){
    const q=D.title.replace(/(\\.|--|-|\\s)(2160p|1080p|720p|S\\d+|E\\d+|WEB[- ]DL|WEBRip|BluRay).*/i,'')
      .replace(/[._-]/g,' ').trim();
    inp.value=q;doImdbSearch(q);
  }
}
function closeImdbModal(){
  document.getElementById('imdb-overlay').classList.remove('show');
  document.getElementById('imdb-modal').classList.remove('show');
}
async function doImdbSearch(q){
  if(!q||q.length<2)return;
  const res=document.getElementById('imdb-results-list');
  res.innerHTML='<div style="color:#64748b;padding:12px">\u23f3 Searching\u2026</div>';
  try{
    const r=await fetch('/api/imdb_search?q='+encodeURIComponent(q));
    const j=await r.json();
    res.innerHTML='';
    if(!j.results||!j.results.length){
      res.innerHTML='<div style="color:#64748b;padding:12px">No results found</div>';return;
    }
    j.results.forEach(item=>{
      const div=document.createElement('div');div.className='imdb-item';
      const imgEl=document.createElement('img');
      if(item.poster){imgEl.src=item.poster;imgEl.onerror=function(){this.style.visibility='hidden';};}
      else{imgEl.style.visibility='hidden';}
      const info=document.createElement('div');info.className='iinfo';
      const b=document.createElement('b');b.textContent=item.title||'';
      const sm=document.createElement('small');
      const meta=[item.year,item.type].filter(Boolean).join(' \u00b7 ');
      sm.textContent=meta;
      info.appendChild(b);info.appendChild(sm);
      div.appendChild(imgEl);div.appendChild(info);
      div.onclick=()=>selectImdbResult(item);
      res.appendChild(div);
    });
  }catch(e){res.innerHTML='<div style="color:#ef4444;padding:12px">Search error</div>';}
}
function selectImdbResult(item){
  imdbUrl='https://www.imdb.com/title/'+item.id+'/';
  document.getElementById('imdb-out').innerHTML='IMDb: <a href="'+imdbUrl+'" target="_blank">'+imdbUrl+'</a>';
  document.getElementById('b-ci').disabled=false;
  closeImdbModal();
  showToast('\u2713 IMDb selected');
}
function searchIMDb(){if(!D)return;openImdbModal();}
document.getElementById('imdb-search-inp').addEventListener('input',function(e){
  clearTimeout(imdbSearchTimer);
  imdbSearchTimer=setTimeout(()=>doImdbSearch(e.target.value),400);
});
function downloadTorrent(){window.location.href='/api/torrent'}
function render(d){
  document.getElementById('title-box').textContent=d.title||'';
  document.getElementById('desc-box').textContent=d.description||'';
  document.getElementById('torrent-name').textContent=d.torrentFile||'';
  const c=String(d.category||'0');
  document.getElementById('cat-badge').textContent=c+' \u00b7 '+(CAT[c]||'Category '+c);
  const l=String(d.language||'0');
  document.getElementById('lang-badge').textContent=l+' \u00b7 '+(LANG[l]||'Lang '+l);
  document.getElementById('loading').style.display='none';
  document.getElementById('app').style.display='block';
}
async function poll(){
  try{
    const r=await fetch('/api/data');
    if(r.ok){const j=await r.json();if(j&&j.ready){D=j;render(j);return;}}
    setTimeout(poll,2000);
  }catch(e){setTimeout(poll,2000);}
}
function fmtBytes(b){
  if(b>=1e9)return(b/1e9).toFixed(1)+' GB';
  return(b/1e6).toFixed(0)+' MB';
}
async function pollSys(){
  try{
    const r=await fetch('/api/sysinfo');
    if(r.ok){
      const j=await r.json();
      const cpu=j.cpu||0;
      document.getElementById('cpu-fill').style.width=cpu+'%';
      document.getElementById('cpu-pct').textContent=cpu.toFixed(1)+'%';
      const ri=j.ram_used||0,rt=j.ram_total||1;
      const rp=(ri/rt*100);
      document.getElementById('ram-fill').style.width=rp.toFixed(1)+'%';
      document.getElementById('ram-pct').textContent=rp.toFixed(0)+'%';
      document.getElementById('ram-val').textContent=fmtBytes(ri)+' / '+fmtBytes(rt);
      const di=j.disk_used||0,dt=j.disk_total||1;
      const dp=(di/dt*100);
      document.getElementById('disk-fill').style.width=dp.toFixed(1)+'%';
      document.getElementById('disk-pct').textContent=dp.toFixed(0)+'%';
      document.getElementById('disk-val').textContent=fmtBytes(di)+' / '+fmtBytes(dt);
    }
  }catch(e){}
  setTimeout(pollSys,2000);
}
poll();
pollSys();
</script>
</body>
</html>"""


class WebAppHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        qs = parsed.query

        if route in ('/', '/index.html'):
            self._serve_html()
        elif route == '/api/data':
            self._serve_data()
        elif route == '/api/torrent':
            self._serve_torrent()
        elif route == '/api/cover':
            self._serve_cover()
        elif route == '/api/imdb':
            self._serve_imdb(qs)
        elif route == '/api/imdb_search':
            self._serve_imdb_search(qs)
        elif route == '/api/sysinfo':
            self._serve_sysinfo()
        else:
            self.send_response(404)
            self.end_headers()

    def _send_headers(self, content_type: str, length: int | None = None):
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        self.send_header('Access-Control-Allow-Origin', '*')
        if length is not None:
            self.send_header('Content-Length', str(length))
        self.end_headers()

    def _serve_html(self):
        body = _WEBAPP_HTML.encode('utf-8')
        self._send_headers('text/html; charset=utf-8', len(body))
        self.wfile.write(body)

    def _serve_data(self):
        if LATEST_JSON and LATEST_JSON.exists():
            body = LATEST_JSON.read_bytes()
            self._send_headers('application/json; charset=utf-8', len(body))
            self.wfile.write(body)
        else:
            body = b'{"ready":false}'
            self._send_headers('application/json; charset=utf-8', len(body))
            self.wfile.write(body)

    def _serve_torrent(self):
        if GENERATED_TORRENT and GENERATED_TORRENT.exists():
            body = GENERATED_TORRENT.read_bytes()
            fname = GENERATED_TORRENT.name
            self.send_response(200)
            self.send_header('Content-Type', 'application/x-bittorrent')
            self.send_header('Content-Disposition', f'attachment; filename="{fname}"')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_cover(self):
        cover = COVER_PATH
        if cover and cover.exists():
            ext = cover.suffix.lower()
            content_type = 'image/png' if ext == '.png' else 'image/jpeg'
            body = cover.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_imdb(self, query_string: str):
        params = parse_qs(query_string)
        title = params.get('q', [''])[0]
        imdb_url = search_imdb(title) if title else None
        body = json.dumps({"url": imdb_url}).encode('utf-8')
        self._send_headers('application/json; charset=utf-8', len(body))
        self.wfile.write(body)

    def _serve_imdb_search(self, query_string: str):
        params = parse_qs(query_string)
        title = params.get('q', [''])[0]
        results = search_imdb_multi(title) if title else []
        body = json.dumps({"results": results}).encode('utf-8')
        self._send_headers('application/json; charset=utf-8', len(body))
        self.wfile.write(body)

    def _serve_sysinfo(self):
        with _sysinfo_lock:
            data = dict(_sysinfo_cache)
        body = json.dumps(data).encode('utf-8')
        self._send_headers('application/json; charset=utf-8', len(body))
        self.wfile.write(body)

    def log_message(self, format, *args):
        return

    def handle_error(self, request, client_address):
        exc_type = sys.exc_info()[0]
        if exc_type in (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            return
        super().handle_error(request, client_address)

def _kill_port_if_busy(port: int) -> None:
    import socket as _socket
    import time as _time
    try:
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            if s.connect_ex(('127.0.0.1', port)) != 0:
                return
    except OSError:
        return

    if sys.platform != "linux":
        return


    freed = False
    try:
        subprocess.run(
            ["fuser", "-k", f"{port}/tcp"],
            capture_output=True, check=False, timeout=5,
        )
        freed = True
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    if freed:
        _time.sleep(0.5)


_http_server_started = False
_http_server_lock = threading.Lock()

_sysinfo_cache: dict = {'cpu': 0.0, 'ram_used': 0, 'ram_total': 0, 'disk_used': 0, 'disk_total': 0}
_sysinfo_lock = threading.Lock()
_cpu_prev_stat: tuple | None = None

def _read_proc_stat() -> tuple[int, int]:
    with open('/proc/stat', 'r') as f:
        parts = f.readline().split()
    vals = list(map(int, parts[1:10]))
    total = sum(vals)
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return total, idle

def _update_sysinfo_loop() -> None:
    global _cpu_prev_stat
    import time as _time
    while True:
        _time.sleep(2)
        try:
            total, idle = _read_proc_stat()
            cpu_pct = 0.0
            if _cpu_prev_stat:
                pt, pi = _cpu_prev_stat
                dt = total - pt
                di = idle - pi
                cpu_pct = max(0.0, min(100.0, 100.0 * (1 - di / dt))) if dt > 0 else 0.0
            _cpu_prev_stat = (total, idle)
            with open('/proc/meminfo', 'r') as f:
                mem = f.read()
            m_total = re.search(r'MemTotal:\s+(\d+)', mem)
            m_avail = re.search(r'MemAvailable:\s+(\d+)', mem)
            if not m_total or not m_avail:
                continue
            mem_total_kb = int(m_total.group(1))
            mem_avail_kb = int(m_avail.group(1))
            disk = shutil.disk_usage('/')
            with _sysinfo_lock:
                _sysinfo_cache.update({
                    'cpu': round(cpu_pct, 1),
                    'ram_used': (mem_total_kb - mem_avail_kb) * 1024,
                    'ram_total': mem_total_kb * 1024,
                    'disk_used': disk.used,
                    'disk_total': disk.total,
                })
        except Exception:
            pass


def start_server_thread(port: int):
    global _http_server_started
    with _http_server_lock:
        if _http_server_started:
            return
        _http_server_started = True

    _kill_port_if_busy(port)

    def run():
        global _http_server_started
        import time as _time
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                httpd = HTTPServer(('', port), WebAppHandler)
                _lp.log(f"{c.GREEN}⚡ Web App running on http://localhost:{port}{c.RESET}")
                httpd.serve_forever()
                return
            except OSError:
                if attempt < max_attempts:
                    _time.sleep(0.5 * attempt)
                    _kill_port_if_busy(port)
                else:
                    _lp.log(f"{c.RED}Error: Port {port} is busy.{c.RESET}")
            except Exception as e:
                _lp.log(f"{c.RED}Server error: {e}{c.RESET}")
                return
        with _http_server_lock:
            _http_server_started = False

    if sys.platform == "linux":
        _si_thread = threading.Thread(target=_update_sysinfo_loop, daemon=True, name="sysinfo")
        _si_thread.start()

    t = threading.Thread(target=run, daemon=True)
    t.start()

def cleanup_sync_files():
    try:

        if LATEST_JSON and LATEST_JSON.exists():
            LATEST_JSON.unlink()

        if INDEX_HTML and INDEX_HTML.exists():
            INDEX_HTML.unlink()

        if GENERATED_SPECTROGRAM and GENERATED_SPECTROGRAM.exists():
            GENERATED_SPECTROGRAM.unlink()

        if EXTRACTED_COVER and EXTRACTED_COVER.exists():
            EXTRACTED_COVER.unlink()

        if AUTO_DELETE_CREATED_FILES:
            if GENERATED_TORRENT and GENERATED_TORRENT.exists():
                GENERATED_TORRENT.unlink()
                print(f"{c.YELLOW}Auto-deleted: {GENERATED_TORRENT.name}{c.RESET}")

            if GENERATED_TXT and GENERATED_TXT.exists():
                GENERATED_TXT.unlink()
                print(f"{c.YELLOW}Auto-deleted: {GENERATED_TXT.name}{c.RESET}")
        for img in GENERATED_PDF_IMAGES:
            if img.exists():
                img.unlink()
    except OSError as exc:
        error(f"Cleanup failed while removing generated files: {exc}")

atexit.register(cleanup_sync_files)


_SUPPORTS_LIVE = sys.stdout.isatty()

SLOT_TORRENT = 0
SLOT_CAMERA  = 1
SLOT_UPLOAD  = 2

class _LiveProgress:

    def __init__(self) -> None:
        self._lock   = threading.Lock()
        self._slots: dict[int, str] = {}
        self._order: list[int]      = []
        self._nlines: int           = 0


    def begin(self, slot: int, text: str = "") -> None:
        with self._lock:
            if slot in self._slots:
                return
            self._slots[slot] = text
            self._order.append(slot)
            self._order.sort()
            self._nlines = len(self._order)
            if _SUPPORTS_LIVE:
                sys.stdout.write(f"\r\033[K{text}\n")
                sys.stdout.flush()
            else:
                print(text)

    def update(self, slot: int, text: str) -> None:
        with self._lock:
            if slot not in self._slots:
                return
            self._slots[slot] = text
            if not _SUPPORTS_LIVE:
                return
            n = self._nlines
            if n == 0:
                return
            sys.stdout.write(f"\033[{n}A")
            for s in self._order:
                sys.stdout.write(f"\r\033[K{self._slots[s]}\n")
            sys.stdout.flush()

    def end(self, slot: int) -> None:
        with self._lock:
            if slot not in self._slots:
                return
            old_count = len(self._order)
            del self._slots[slot]
            self._order.remove(slot)
            new_count = len(self._order)
            self._nlines = new_count
            if not _SUPPORTS_LIVE:
                return
            if old_count > 0:
                sys.stdout.write(f"\033[{old_count}A")
                for s in self._order:
                    sys.stdout.write(f"\r\033[K{self._slots[s]}\n")


                extras = old_count - new_count
                for i in range(extras):
                    if i < extras - 1:
                        sys.stdout.write(f"\r\033[K\n")
                    else:
                        sys.stdout.write(f"\r\033[K")
                sys.stdout.flush()

    def log(self, text: str) -> None:
        with self._lock:
            if not _SUPPORTS_LIVE or self._nlines == 0:
                sys.stdout.write(f"{text}\n")
                sys.stdout.flush()
                return
            n = self._nlines
            sys.stdout.write(f"\033[{n}A")
            sys.stdout.write(f"\r\033[K{text}\n")
            for s in self._order:
                sys.stdout.write(f"\r\033[K{self._slots[s]}\n")
            sys.stdout.flush()

_lp = _LiveProgress()


def clear(): os.system('cls' if os.name == 'nt' else 'clear')

def banner():
    clear()
    print(f"""
{c.PURPLE}{c.BOLD}
╔══════════════════════════════════════════════════════════════════╗
║                TorretBD Lazy Upload                              ║
║    By fahimbyte (https://github.com/mazidulmahim)                ║
╚══════════════════════════════════════════════════════════════════╝
{c.RESET}""")

def log(msg: str, icon: str = "•", color: str = c.CYAN):
    t = datetime.now().strftime("%H:%M:%S")
    _lp.log(f"{color}[{t}] {icon} {msg}{c.RESET}")

def success(msg): log(msg, "Success", c.GREEN)
def error(msg):   log(msg, "Error", c.RED)

def hide_window():
    if os.name == 'nt':
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        return si
    return None

def copy_to_clipboard(text: str):
    if not COPY_TO_CLIPBOARD: return
    try:
        if os.name == 'nt':
            subprocess.run('clip', input=text.encode('utf-8'), check=True)
        elif sys.platform == 'darwin':
            subprocess.run('pbcopy', input=text.encode('utf-8'), check=True)
        else:
            subprocess.run(['xclip', '-selection', 'clipboard'], input=text.encode('utf-8'), check=True)
        success("Description copied to clipboard!")
    except:
        pass

def create_torrent(target: Path, include_srt: bool | None = None) -> bool:
    global GENERATED_TORRENT
    if not CREATE_TORRENT_FILE:
        log("Skipping torrent creation (disabled)", "Skip")
        return True
    if not shutil.which("mkbrr"):
        error("mkbrr not found! → https://github.com/autobrr/mkbrr")
        return False


    exclude_patterns: list[str] = []
    if target.is_dir():

        exclude_patterns.extend(["*.nfo", "*.txt","*.srr"])


        _exclude_dir_names = {"screens", "screen", "proof", "screenshots", "screenshot", "Sample", "sample"}
        for item in target.rglob("*"):
            lower_name = item.name.lower()
            stem_lower = item.stem.lower()
            try:
                rel = item.relative_to(target).as_posix()
            except ValueError:
                continue
            if item.is_dir() and lower_name in _exclude_dir_names:
                pattern = f"{rel}/**"
                if pattern not in exclude_patterns:
                    exclude_patterns.append(pattern)
            elif item.is_file() and (_STEM_SAMPLE_RE.search(stem_lower) or stem_lower in _exclude_dir_names):
                if rel not in exclude_patterns:
                    exclude_patterns.append(rel)


        srt_files = [f for f in target.rglob("*.srt")]
        if srt_files:
            if include_srt is None:
                log(f"Found {len(srt_files)} .srt subtitle file(s) in folder.", "SRT", c.YELLOW)
                for sf in srt_files[:3]:
                    print(f"   {c.DIM}{sf.name}{c.RESET}")
                if len(srt_files) > 3:
                    print(f"   {c.DIM}...and {len(srt_files) - 3} more{c.RESET}")
                ans = input(f"\n{c.BOLD}Include .srt files in torrent? [y/N]: {c.RESET}").strip().lower()
                include_srt = (ans == 'y')
            if not include_srt:
                exclude_patterns.append("*.srt")
                log("Excluding .srt files from torrent.", "SRT")

    log("Creating torrent file...", "Torrent")
    out = target.parent / f"{target.name}.torrent"
    GENERATED_TORRENT = out

    cmd = ["mkbrr", "create", "-t", TRACKER_ANNOUNCE,
           f"--private={'true' if PRIVATE_TORRENT else 'false'}", "-o", str(out), str(target)]

    for pattern in exclude_patterns:
        cmd.extend(["--exclude", pattern])

    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        bufsize=1, universal_newlines=True, startupinfo=hide_window()
    )

    _pct_re = re.compile(r'(\d+)\s*%')
    _last_pct = -1

    def _bar_text(pct: int) -> str:
        bar_length = 10
        filled = int(bar_length * pct // 100)
        bar = "█" * filled + "▒" * (bar_length - filled)
        return f"{c.CYAN}Creating torrent... [{bar}] {pct}%{c.RESET}"

    _lp.begin(SLOT_TORRENT, _bar_text(0))
    try:
        while True:
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            if line:
                line = line.strip()
                m = _pct_re.search(line)
                if m:
                    pct = min(int(m.group(1)), 100)
                    if pct != _last_pct:
                        _lp.update(SLOT_TORRENT, _bar_text(pct))
                        _last_pct = pct
                elif "Wrote" in line:
                    _lp.update(SLOT_TORRENT, _bar_text(100))
                    _last_pct = 100
    finally:
        _lp.end(SLOT_TORRENT)

    returncode = process.wait()

    if returncode == 0 and out.exists():
        success(f"Torrent created: {out.name}")
        return True
    else:
        error("Torrent creation failed!")
        return False

def get_mediainfo(path: Path) -> str:
    cmd = ["mediainfo", str(path)]
    if not shutil.which("mediainfo"):
        exe = Path(__file__).parent / "MediaInfo.exe"
        if exe.exists(): cmd = [str(exe), str(path)]
        else: return "MediaInfo not available"
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, startupinfo=hide_window(), timeout=120)
        return result.stdout if result.returncode == 0 else "Failed"
    except: return "Failed"

def create_spectrogram(audio_file: Path) -> Path | None:
    global GENERATED_SPECTROGRAM

    audiowaveform_path = shutil.which("audiowaveform")
    if audiowaveform_path:
        log("Creating spectrogram with audiowaveform...", "Audio")
        spec_output = Path("spectrogram.png")
        GENERATED_SPECTROGRAM = spec_output

        cmd = [audiowaveform_path, "-i", str(audio_file), "-o", str(spec_output), "--width", "1800", "--height", "512"]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=hide_window(), timeout=120)
            if spec_output.exists():
                success(f"Spectrogram created: {spec_output.name}")
                return spec_output
            else:
                error("Spectrogram creation failed!")
                return None
        except Exception as e:
            error(f"Spectrogram creation failed: {e}")
            return None

    if not shutil.which("sox"):
        error("sox not found! Install audiowaveform or sox: sudo apt-get install audiowaveform sox libsox-fmt-all")
        return None

    log("Creating spectrogram with SoX...", "Audio")
    spec_output = Path("spectrogram.png")
    GENERATED_SPECTROGRAM = spec_output

    temp_wav = None
    input_file = audio_file
    if audio_file.suffix.lower() in ['.m4a', '.aac', '.mp4', '.opus', '.wma']:
        if not shutil.which("ffmpeg"):
            error("ffmpeg not found! Install it to process M4A/AAC/MP4/OPUS/WMA files")
            return None

        temp_wav = Path(f"temp_spectrogram_{audio_file.stem}.wav")
        log(f"Converting {audio_file.suffix} to WAV for spectrogram...", "Audio")
        try:
            subprocess.run(
                ["ffmpeg", "-i", str(audio_file), "-y", str(temp_wav)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                startupinfo=hide_window(), timeout=120
            )
            input_file = temp_wav
        except Exception as e:
            error(f"Audio conversion failed: {e}")
            if temp_wav and temp_wav.exists():
                temp_wav.unlink()
            return None

    cmd = ["sox", str(input_file), "-n", "spectrogram", "-o", str(spec_output)]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=hide_window(), timeout=120)
        if spec_output.exists():
            success(f"Spectrogram created: {spec_output.name}")
            return spec_output
        else:
            error("Spectrogram creation failed!")
            return None
    except Exception as e:
        error(f"Spectrogram creation failed: {e}")
        return None
    finally:
        if temp_wav and temp_wav.exists():
            temp_wav.unlink()

def extract_audio_metadata(audio_file: Path) -> dict:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(audio_file)],
            capture_output=True, text=True, startupinfo=hide_window(), timeout=30
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            tags = data.get("format", {}).get("tags", {})
            artist = tags.get("artist") or tags.get("ARTIST") or tags.get("album_artist") or tags.get("ALBUM_ARTIST") or ""
            album = tags.get("album") or tags.get("ALBUM") or ""

            stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
            sample_rate = stream.get("sample_rate", "44100")
            bit_rate = stream.get("bit_rate") or data.get("format", {}).get("bit_rate", "0")
            bit_depth = stream.get("bits_per_sample") or stream.get("bits_per_raw_sample", "16")
            file_extension = audio_file.suffix.lstrip('.')
            codec = stream.get("codec_name", "unknown").upper()
            if codec == "FLAC":
                codec = "FLAC"

            return {
                "artist": artist,
                "album": album,
                "sample_rate": sample_rate,
                "bit_rate": bit_rate,
                "bit_depth": bit_depth,
                "file_extension": file_extension,
                "codec": codec
            }
    except:
        pass
    return {"artist": "", "album": "", "sample_rate": "44100", "bit_rate": "0", "bit_depth": "16", "file_extension": "flac", "codec": "FLAC"}

def find_cover_image(folder: Path) -> Path | None:
    cover_names = ['cover.jpg', 'cover.png', 'cover.jpeg', 'folder.jpg', 'folder.png', 'album.jpg', 'album.png']
    for cover_name in cover_names:
        cover_path = folder / cover_name
        if cover_path.exists() and cover_path.is_file():
            return cover_path

    for file in folder.iterdir():
        if file.is_file() and file.suffix.lower() in {'.jpg', '.jpeg', '.png'} and 'cover' in file.name.lower():
            return file

    return None

_FAKINGTHEFUNK_NAMES = frozenset({"fakingthefunk.jpg", "fakingthefunk.png", "fakingthefunk.jpeg"})

def find_fakingthefunk_image(search_root: Path) -> Path | None:
    if search_root.is_file():
        return None
    for candidate in search_root.rglob("*"):
        if candidate.is_file() and candidate.name.lower() in _FAKINGTHEFUNK_NAMES:
            return candidate
    return None

def extract_cover_from_audio(audio_files: list[Path], dest_dir: Path) -> Path | None:
    if not shutil.which("ffmpeg"):
        return None
    out_path = dest_dir / "extracted_cover.jpg"
    for audio_file in audio_files:
        try:
            result = subprocess.run(
                ["ffmpeg", "-i", str(audio_file), "-an", "-vcodec", "copy", "-y", str(out_path)],
                capture_output=True, timeout=30, startupinfo=hide_window()
            )
            if result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
                return out_path
            elif out_path.exists():
                out_path.unlink()
        except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError):
            if out_path.exists():
                out_path.unlink()
    return None

def select_representative_audio_file(audio_files: list[Path], base_dir: Path | None = None) -> Path:
    if not audio_files:
        raise ValueError("audio_files cannot be empty")

    if not base_dir:
        return audio_files[0]

    bucketed: dict[str, list[Path]] = defaultdict(list)
    for audio_file in audio_files:
        try:
            rel = audio_file.relative_to(base_dir)
        except ValueError:
            rel = audio_file
        bucket = "__root__" if len(rel.parts) == 1 else rel.parts[0].lower()
        bucketed[bucket].append(audio_file)

    if not bucketed:
        return audio_files[0]

    best_bucket, bucket_files = max(
        bucketed.items(),
        key=lambda item: (len(item[1]), item[0] != "__root__", item[0]),
    )

    if not bucket_files:
        return audio_files[0]


    ext_counts = Counter(p.suffix.lower() for p in bucket_files)
    if not ext_counts:
        return sort_paths_by_mtime(bucket_files)[0]
    preferred_ext, _ = max(ext_counts.items(), key=lambda item: (item[1], item[0]))
    ext_files = [p for p in bucket_files if p.suffix.lower() == preferred_ext]
    if not ext_files:
        return sort_paths_by_mtime(bucket_files)[0]
    return sort_paths_by_mtime(ext_files)[0]

def generate_audio_tracklist(audio_files: list[Path], base_dir: Path | None = None) -> str:
    if not audio_files:
        return ""

    if not base_dir:
        return "\n".join(audio_file.name for audio_file in audio_files)

    grouped: dict[str, list[str]] = defaultdict(list)
    for audio_file in audio_files:
        try:
            rel = audio_file.relative_to(base_dir)
        except ValueError:
            grouped[AUDIO_TRACKLIST_SINGLES_SECTION].append(audio_file.name)
            continue

        if len(rel.parts) == 1:
            grouped[AUDIO_TRACKLIST_SINGLES_SECTION].append(rel.name)
        else:
            album = rel.parts[0]
            track = str(Path(*rel.parts[1:]))
            grouped[album].append(track)

    output_lines: list[str] = []
    section_names = sorted(grouped.keys(), key=lambda name: (name != AUDIO_TRACKLIST_SINGLES_SECTION, name.lower()))
    for idx, section in enumerate(section_names):
        output_lines.append(section)
        output_lines.extend(f" - {track}" for track in grouped[section])
        if idx < len(section_names) - 1:
            output_lines.append("")
    return "\n".join(output_lines)

def select_audio_files_for_spectrograms(
    audio_files: list[Path],
    preferred_audio: Path | None = None,
    fallback_count: int = 2,
) -> list[Path]:
    if not audio_files:
        return []

    target_count = 3 if len(audio_files) >= 3 else fallback_count
    selected: list[Path] = []

    if preferred_audio and preferred_audio in audio_files:
        selected.append(preferred_audio)

    for audio_file in audio_files:
        if len(selected) >= target_count:
            break
        if audio_file not in selected:
            selected.append(audio_file)

    base_cycle = selected[:]

    cycle_index = 0
    while len(selected) < target_count:
        selected.append(base_cycle[cycle_index % len(base_cycle)])
        cycle_index += 1

    return selected

def escape_bbcode_text(value: str) -> str:
    return value.replace("[", "&#91;").replace("]", "&#93;")

def generate_audio_description(
    folder_name: str,
    cover_url: str,
    mediainfo_text: str,
    tracklist: str,
    spectrogram_entries: list[tuple[str, str]],
    fakingthefunk_url: str | None = None,
) -> str:
    if not spectrogram_entries:
        raise ValueError("spectrogram_entries must not be empty")

    proof_lines: list[str] = []
    for spectrogram_title, spectrogram_url in spectrogram_entries:
        escaped_title = escape_bbcode_text(spectrogram_title)
        proof_lines.append(f"[center][size=3][color={SPECTROGRAM_TITLE_COLOR}]{escaped_title}[/color][/size][/center]")
        proof_lines.append(f"[center][img]{spectrogram_url}[/img][/center]")

    if fakingthefunk_url:
        proof_lines.append(f"[center][size=3][color={SPECTROGRAM_TITLE_COLOR}]FakingTheFunk[/color][/size][/center]")
        proof_lines.append(f"[center][img]{fakingthefunk_url}[/img][/center]")

    proof_block = "\n".join(proof_lines)

    description = f"""[font=Segoe UI][center][b][color=#FFD700][size=6]{folder_name}[/size][/color][/b][/center][/font]
[center]{cover_url}[/center]
[center][b][size=5][color=#59E817][font=Segoe UI]MediaInfo[/font][/color][/size][/b][/center][font=Courier New]
[mediainfo]
{mediainfo_text}
[/mediainfo]
[/font]
[center][b][font=Segoe UI][color=#59E817][size=5]Tracklist[/size][/color][/font][/b][/center]
[color=#C63968][b][center]{tracklist}[/center][/b][/color]
[center][b][size=5][font=Tahoma][color=#59E817]Proof[/color][/size][/b][/center][/font]
{proof_block}
[hr][i][b][center][font=Segoe UI][size=4][color=#FFD700]If you're downloading my torrent and not getting the desired speed, just comment, and I'll move it to my seedbox.[/color][/size][/font][/center][/b][/i]"""
    return description

def generate_audio_title(folder_name: str, metadata: dict) -> str:
    artist = metadata.get("artist", "")
    sample_rate_hz = int(metadata.get("sample_rate", "44100"))
    sample_rate_khz = sample_rate_hz // 1000
    bit_depth = int(metadata.get("bit_depth", "16"))
    file_extension = metadata.get("file_extension", "flac").upper()

    bit_hz_str = f"[{bit_depth}bit-{sample_rate_khz}kHz]"
    ext_str = f"[{file_extension}]"


    has_e_marker = bool(re.search(r'\[E\]', folder_name))


    folder_name_clean = re.sub(r'\s*\[E\]\s*', ' ', folder_name).strip()


    e_marker = " [E]" if has_e_marker else ""

    if artist:
        title = f"{artist} - {folder_name_clean}{e_marker} {bit_hz_str} {ext_str}-fahimbyte"
    else:
        title = f"{folder_name_clean}{e_marker} {bit_hz_str} {ext_str}-fahimbyte"

    return title


_DOLBY_VISION_PATTERN = r"dolby\s*vision|dvhe\.\d+"
_DOVI_PATTERN = r"dovi(?:\b|[.\-_\s])"

_DV_TOKEN_PATTERN = r"(?:^|[.\-_\s])dv(?:$|[.\-_\s])"
_HDR10_PATTERN = r"hdr(?:10\+?|\s*10\+?)"

_HDR_DV_REGEX = re.compile(
    rf"(?:{_DOLBY_VISION_PATTERN}|{_DOVI_PATTERN}|{_DV_TOKEN_PATTERN}|{_HDR10_PATTERN})",
    re.I,
)

def needs_hdr10_dv_screenshot(mediainfo_text: str = "", file_name: str = "") -> bool:
    return bool(_HDR_DV_REGEX.search(file_name) or _HDR_DV_REGEX.search(mediainfo_text))

def _build_screenshot_cmd(video: Path, timestamp: float, output_file: Path, hdr_dv: bool, crop: str | None = None) -> list[str]:
    cmd = ["ffmpeg", "-ss", f"{timestamp:.3f}", "-i", str(video)]
    if crop:
        cmd += ["-vf", f"crop={crop}"]
    if hdr_dv:

        cmd += ["-frames:v", "1", "-update", "1", "-q:v", "1"]
    else:
        cmd += ["-vframes", "1", "-q:v", "1"]
    cmd += ["-y", str(output_file)]
    return cmd


def _detect_crop(video: Path, timestamp: float) -> str | None:
    try:


        res = subprocess.run(
            [
                "ffmpeg",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(video),
                "-vframes",
                "1",
                "-vf",
                f"cropdetect={CROP_LUMINANCE_THRESHOLD}:{CROP_ROUNDING}:{CROP_RESET_INTERVAL}",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            startupinfo=hide_window(),
            timeout=15,
        )
        stderr = res.stderr or ""
        if res.returncode != 0:
            error(f"ffmpeg cropdetect failed (code {res.returncode}) at {timestamp:.3f}s")
            return None
        matches = re.findall(r"crop=(\d+:\d+:\d+:\d+)", stderr)
        if matches:
            return matches[-1]
    except Exception:
        return None
    return None

def take_screenshots(video: Path, hdr_dv: bool, count: int = SCREENSHOT_COUNT) -> list[Path]:
    log(f"Taking {count} full-size screenshots (20% → 80%)...", "Camera")
    try:
        duration = float(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(video)
        ], startupinfo=hide_window()).decode().strip())
    except: return []

    if duration <= 0: return []

    start_percent, end_percent = 0.20, 0.80
    total_range = end_percent - start_percent
    files = []
    max_size_mb = 32 if IMAGE_HOST.lower() == "imgbb" else 64

    crop = None
    def _timestamp(progress: float) -> float:
        return duration * (start_percent + (total_range * progress))

    if CROP_BLACK_BARS:


        first_progress = 1 / (count + 1)
        first_timestamp = _timestamp(first_progress)
        crop = _detect_crop(video, first_timestamp)

    def _ss_bar_text(done: int) -> str:
        bar_length = 10
        filled = int(bar_length * done // count) if count else bar_length
        bar = "█" * filled + "▒" * (bar_length - filled)
        return f"{c.CYAN}Taking screenshots... [{bar}] {done}/{count}{c.RESET}"

    _lp.begin(SLOT_CAMERA, _ss_bar_text(0))
    try:
        for i in range(1, count + 1):
            progress = i / (count + 1)
            timestamp = _timestamp(progress)
            ext = "png" if LOSSLESS_SCREENSHOT else "jpg"
            output_file = Path(f"ss_{i:02d}.{ext}")

            cmd = _build_screenshot_cmd(video, timestamp, output_file, hdr_dv, crop=crop)
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=hide_window())

            if output_file.exists():
                size_mb = output_file.stat().st_size / (1024 * 1024)
                if LOSSLESS_SCREENSHOT and ext == "png" and size_mb > max_size_mb:
                    output_file.unlink()
                    jpeg_file = Path(f"ss_{i:02d}.jpg")
                    cmd_jpg = _build_screenshot_cmd(video, timestamp, jpeg_file, hdr_dv, crop=crop)
                    subprocess.run(cmd_jpg, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=hide_window())
                    if jpeg_file.exists():
                        files.append(jpeg_file)
                else:
                    files.append(output_file)
            _lp.update(SLOT_CAMERA, _ss_bar_text(i))
    finally:
        _lp.end(SLOT_CAMERA)
    return files

def _parse_upload_error_message(data: dict) -> str:
    err = data.get("error")
    if err is None:
        return "unknown error (no error field)"
    if isinstance(err, dict):
        return str(err.get("message") or err.get("info") or err)
    if err:
        return str(err)
    return "unknown error"

def _upload_via_host(img: Path, host: str, timeout: int = UPLOAD_TIMEOUT) -> tuple[str | None, bool]:
    filename = img.name
    try:
        if host == "imgbb":
            if IMGBB_API_KEY == "YOUR IMGBB API KEY": return None, False
            encoded_name = quote(filename, safe="")
            with img.open("rb") as fh:
                r = requests.post(
                    "https://api.imgbb.com/1/upload",
                    params={"key": IMGBB_API_KEY},
                    data={"name": encoded_name},
                    files={"image": fh},
                    timeout=timeout,
                )
                if r.status_code == 200:
                    data = r.json()
                    image_data = data.get("data") or {}

                    direct_url = image_data.get("url")
                    display_url = image_data.get("display_url")
                    image_url = direct_url if direct_url else display_url
                    if data.get("success") and image_url:
                        return image_url, False
                    error(f"imgbb upload failed for {filename}: {_parse_upload_error_message(data)}")
        elif host == "freeimage":
            encoded_name = quote(filename, safe="")
            with img.open("rb") as fh:
                r = requests.post(
                    "https://freeimage.host/api/1/upload",
                    params={"key": FREEIMAGE_API_KEY},
                    files={"source": fh},
                    data={"format": "json", "name": encoded_name},
                    timeout=timeout,
                )
                if r.status_code == 200:
                    data = r.json()
                    if data.get("status_code") == 200 and data.get("image", {}).get("url"):
                        return data["image"]["url"], False
                    error(f"freeimage upload failed for {filename}: {_parse_upload_error_message(data)}")
    except json.JSONDecodeError as exc:
        error(f"{host} upload failed for {filename}: invalid JSON response ({exc})")
    except requests.RequestException as exc:
        error(f"{host} upload failed for {filename}: network/API error ({exc})")
    except OSError as exc:
        error(f"{host} upload failed for {filename}: file error while reading image ({exc})")
        return None, True
    return None, False

def upload_image(img: Path) -> str | None:
    primary = _DEPRECATED_HOST_ALIASES.get(IMAGE_HOST.lower(), IMAGE_HOST.lower())
    supported_hosts = ("imgbb", "freeimage")
    if primary not in supported_hosts:
        error(f"Unsupported IMAGE_HOST '{primary}', defaulting to supported hosts")
        hosts = list(supported_hosts)
    else:

        hosts = [primary] + [h for h in supported_hosts if h != primary]

    for idx, host in enumerate(hosts):
        url, fatal = _upload_via_host(img, host)
        if url:
            return url
        if fatal:
            break
    return None

def print_progress(done: int, total: int):
    bar_length = 10
    filled = int(bar_length * done // total)
    bar = "█" * filled + "▒" * (bar_length - filled)
    text = f"{c.CYAN}Uploading {total} screenshots... [{bar}] {done}/{total} uploaded{c.RESET}"
    if done == 0:
        _lp.begin(SLOT_UPLOAD, text)
    elif done == total:
        _lp.end(SLOT_UPLOAD)
    else:
        _lp.update(SLOT_UPLOAD, text)

def _bounded_workers(total_items: int) -> int:
    cpu_count = os.cpu_count() or 1
    max_io_workers = max(MIN_IO_WORKERS, cpu_count * IO_WORKER_MULTIPLIER)
    return max(1, min(MAX_CONCURRENT_UPLOADS, total_items, max_io_workers))

def gui_select_target() -> tuple[Path, bool]:
    if tk is None or filedialog is None:
        raise RuntimeError("tkinter is not available")
    root = tk.Tk(); root.withdraw(); root.update()
    while True:
        banner()
        print(f"{c.BOLD}{c.CYAN}Choose an option:{c.RESET}")
        print(f"  {c.WHITE}1{c.RESET} - Select a single video file")
        print(f"  {c.WHITE}2{c.RESET} - Select an entire folder")
        print(f"  {c.GRAY}(q to quit){c.RESET}\n")
        choice = input(f"{c.BOLD}Enter 1 or 2: {c.RESET}").strip().lower()
        if choice == 'q': sys.exit(0)
        if choice == '1':
            f = filedialog.askopenfilename(title="Select a Video File", filetypes=[("Video Files", "*.mkv *.mp4 *.avi")])
            if f: return Path(f), False
        elif choice == '2':
            f = filedialog.askdirectory(title="Select Folder")
            if f: return Path(f), True

def cli_select_target() -> tuple[Path, bool]:
    current = Path.cwd().resolve()
    while True:
        banner()
        print(f"{c.BOLD}{c.CYAN}Current directory: {current}{c.RESET}")
        items = sort_paths_by_mtime([
            p for p in current.iterdir()
            if (p.is_dir() and p.name != "__pycache__") or p.suffix.lower() in VIDEO_EXTS or p.suffix.lower() in AUDIO_EXTS or p.suffix.lower() in PDF_EXTS
        ])
        if not items:
            choice = input(f"{c.BOLD}Enter 0 to go back or q to quit: {c.RESET}").strip().lower()
            if choice == '0' and current != Path.cwd().resolve(): current = current.parent; continue
            elif choice == 'q': sys.exit(0)
            else: continue
        for i, item in enumerate(items, 1):
            typ = f"{c.PURPLE}Dir{c.RESET}" if item.is_dir() else f"{c.CYAN}File{c.RESET}"
            print(f"  {c.WHITE}{i}{c.RESET}. {item.name} ({typ})")
        print(f"  {c.WHITE}0{c.RESET}. Go back" if current != Path.cwd().resolve() else f"  {c.WHITE}0{c.RESET}. Quit")
        choice = input(f"{c.BOLD}Enter number: {c.RESET}").strip().lower()
        if choice == 'q': sys.exit(0)
        if choice == '0':
            if current == Path.cwd().resolve(): sys.exit(0)
            current = current.parent; continue
        try:
            num = int(choice)
            if 1 <= num <= len(items):
                selected = items[num - 1]
                if selected.is_dir():
                    sub = input(f"{c.BOLD}Navigate (n) or select (s)? {c.RESET}").strip().lower()
                    if sub == 'n': current = selected
                    elif sub == 's': return selected, True
                else: return selected, False
        except: pass

def select_target() -> tuple[Path, bool]:
    if USE_GUI_FILE_PICKER and tk is not None:
        return gui_select_target()
    return cli_select_target()

def detect_language(mediainfo_text: str) -> str:
    lang_options = {
        'English': '1', 'Hindi': '3', 'Arabic': '18', 'Bengali': '8',
        'Bulgarian': '14', 'Chinese': '5', 'Czech': '15', 'Danish': '24',
        'Dutch': '25', 'Filipino': '16', 'Finnish': '26', 'French': '2',
        'German': '9', 'Greek': '27', 'Hebrew': '28', 'Hungarian': '17',
        'Icelandic': '30', 'Indonesian': '31', 'Irish': '32', 'Italian': '12',
        'Japanese': '7', 'Kannada': '41', 'Korean': '10', 'Malayalam': '33',
        'Marathi': '34', 'Norwegian': '35', 'Panjabi': '43', 'Persian': '36',
        'Polish': '37', 'Portuguese': '38', 'Romanian': '39', 'Russian': '13',
        'Serbian': '19', 'Spanish': '6', 'Swedish': '20', 'Tamil': '21',
        'Telugu': '11', 'Thai': '40', 'Turkish': '22', 'Urdu': '4',
        'Vietnamese': '23',
    }
    match = re.search(r'Language\s*:\s*([^\r\n]+)', mediainfo_text or "")
    if not match:
        return "1"
    language = match.group(1).strip().split(" / ")[0]
    language = re.sub(r'\s*\([^)]*\)\s*$', '', language).strip().lower()
    lang_options_lower = {name.lower(): language_id for name, language_id in lang_options.items()}
    return lang_options_lower.get(language, "0")

_SAMPLE_DIR_NAMES = {"sample", "samples"}

def _is_sample_file(path: Path, base_dir: Path | None = None) -> bool:
    if _STEM_SAMPLE_RE.search(path.stem.lower()):
        return True
    check = path.parent
    while True:
        if check.name.lower() in _SAMPLE_DIR_NAMES:
            return True
        if base_dir is not None and check == base_dir:
            break
        parent = check.parent
        if parent == check:
            break
        check = parent
    return False

def sort_paths_by_mtime(paths: list[Path]) -> list[Path]:
    mtimes = {p: p.stat().st_mtime for p in paths}
    return sorted(paths, key=lambda p: (mtimes[p], p.name.lower()))

def trim_mediainfo_complete_name(mediainfo_text: str, base_dir: Path) -> str:
    prefix = str(base_dir)
    prefix_posix = prefix.replace("\\", "/").rstrip("/") + "/"
    prefix_windows = prefix.replace("/", "\\").rstrip("\\") + "\\"

    def _trim_complete_name_path(match: re.Match) -> str:
        label = match.group(1)
        full_path = match.group(2).strip()
        if full_path.startswith(prefix_posix):
            return f"{label}{full_path[len(prefix_posix):]}"
        if full_path.startswith(prefix_windows):
            return f"{label}{full_path[len(prefix_windows):]}"
        return match.group(0)

    return re.sub(r"(^\s*Complete name\s*:\s*)([^\r\n]+)", _trim_complete_name_path, mediainfo_text or "", flags=re.MULTILINE)

def detect_category(title: str, mediainfo_text: str = "") -> str | None:
    webrip_regex = re.compile(r'(Webrip|WebRip|WEBRip|WEBRIP|WEBRiP|DS4K|WEB[\s-]?Rip)', re.I)
    webdl_regex = re.compile(r'(WEB-DL|web-dl|WEBDL|webdl|WEB-dl|WEB DL|WEB[\s-]?DL)', re.I)
    lossless_regex = re.compile(r'(Remux|REMUX|remux|ReMux)', re.I)
    bluray_regex = re.compile(r'(BluRay|blu-ray|BLURAY|Blu-Ray|bluray|Blu-ray|BRrip|brrip|BRRIP|BR-Rip|BR[\s-]?RIP|SDRip|SD[\s-]?Rip)', re.I)
    hdrip_regex = re.compile(r'(HDRip|HD[\s-]?RIP|HD Rip|WEBHDRIP|WEB[\s-]?HD[\s-]?RIP)', re.I)
    dvdrip_regex = re.compile(r'(DVD|DVDRIP|DVD[\s-]?RIP)', re.I)
    cam_regex = re.compile(r'(CAM|HDTC|HDCAM|HD[\s-]?CAM|HDTS|HD[\s-]?TS|DVDSCR|PREDVD|PRE DVD|S[\s-]?print|Pre[\s-]?DVD|Pre[\s-]?DVDRip)', re.I)
    games_regex = re.compile(r'(Fitgirl|Dodi|KaOs|ElAmigos|TENOKE|FLT|RUNE|PLAZA|-GOG|SKIDROW|GOG)', re.I)
    games_backup_regex = re.compile(r'(Steam Game Backup|Steam Backup|Epic Backup|Rockstar Backup|Origin\/EA Backup|EA Backup|Ubisoft Backup|Battle\.net Backup)', re.I)
    crack_regex = re.compile(r'(Crack|crack[\s-]?only|crack only|Patch|Patchs|Crackfix)', re.I)
    awards_regex = re.compile(r'(Awards|Award|Ceremony)', re.I)
    audiobook_regex = re.compile(r'(audiobook|Audiobook|AudioBook|Audio Book|Audio[\s-]?book)', re.I)
    tutorial_regex = re.compile(r'(Udemy|Talkpython|Skillshare|Domestika|Fireship|CodeWithMosh|Educative|PacktPub|O\'Reilly Learning|ZeroToMastery|Oreilly|Dometrain|CGBoost|FrontendMasters)', re.I)
    uhd_resolution_regex = re.compile(r'(2160p|2160|4K)', re.I)
    hd_resolution_regex = re.compile(r'(1080p|1080P|1080)', re.I)
    sd_resolution_regex = re.compile(r'(720p|720P|720)', re.I)
    episode_regex = re.compile(r'S\d+E\d+', re.I)
    season_regex = re.compile(r'S\d+', re.I)

    audio_section_regex = re.compile(
        r'^\s*Audio(?:\s*#\d+)?\s*$([\s\S]*?)(?=^\s*(?:General|Video|Audio(?:\s*#\d+)?|Text(?:\s*#\d+)?|Menu)\s*$|\Z)',
        re.I | re.M,
    )
    japanese_audio_regex = re.compile(r'^\s*Language\s*:\s*Japanese(?:\s*\([^)]*\))?\s*$', re.I | re.M)
    has_japanese_audio = any(japanese_audio_regex.search(section.group(1)) for section in audio_section_regex.finditer(mediainfo_text or ""))

    if has_japanese_audio and (episode_regex.search(title) or season_regex.search(title)):
        return '28'

    if WRESTLING_REGEX.search(title):
        return CATEGORY_PRO_WRESTLING

    if webrip_regex.search(title) and not episode_regex.search(title) and not season_regex.search(title) and not awards_regex.search(title):
        return '83'
    if webdl_regex.search(title) and (sd_resolution_regex.search(title) or hd_resolution_regex.search(title)) and not episode_regex.search(title) and not season_regex.search(title) and not awards_regex.search(title):
        return '55'
    if webdl_regex.search(title) and uhd_resolution_regex.search(title) and not episode_regex.search(title) and not season_regex.search(title) and not awards_regex.search(title):
        return '82'
    if lossless_regex.search(title) and (sd_resolution_regex.search(title) or hd_resolution_regex.search(title)) and not episode_regex.search(title) and not season_regex.search(title):
        return '76'
    if lossless_regex.search(title) and uhd_resolution_regex.search(title) and not episode_regex.search(title) and not season_regex.search(title):
        return '86'
    if bluray_regex.search(title) and uhd_resolution_regex.search(title) and not episode_regex.search(title) and not season_regex.search(title):
        return '80'
    if bluray_regex.search(title) and hd_resolution_regex.search(title) and not episode_regex.search(title) and not season_regex.search(title) and not awards_regex.search(title):
        return '47'
    if bluray_regex.search(title) and sd_resolution_regex.search(title) and not episode_regex.search(title) and not season_regex.search(title) and not awards_regex.search(title):
        return '42'
    if bluray_regex.search(title) and not episode_regex.search(title) and not season_regex.search(title):
        return '24'
    if hdrip_regex.search(title) and not episode_regex.search(title) and not season_regex.search(title) and not awards_regex.search(title):
        return '46'
    if cam_regex.search(title) and not tutorial_regex.search(title) and not episode_regex.search(title) and not season_regex.search(title):
        return '4'
    if dvdrip_regex.search(title) and not games_backup_regex.search(title) and not episode_regex.search(title) and not season_regex.search(title):
        return '1'
    if episode_regex.search(title) and not games_backup_regex.search(title) and not sd_resolution_regex.search(title) and not hd_resolution_regex.search(title) and not uhd_resolution_regex.search(title) and not awards_regex.search(title):
        return '5'
    if episode_regex.search(title) and not games_backup_regex.search(title) and (hd_resolution_regex.search(title) or sd_resolution_regex.search(title)) and not awards_regex.search(title):
        return '61'
    if episode_regex.search(title) and not games_backup_regex.search(title) and uhd_resolution_regex.search(title) and not awards_regex.search(title):
        return '84'
    if season_regex.search(title) and not games_backup_regex.search(title) and not sd_resolution_regex.search(title) and not hd_resolution_regex.search(title) and not uhd_resolution_regex.search(title) and not awards_regex.search(title):
        return '41'
    if season_regex.search(title) and not games_regex.search(title) and (hd_resolution_regex.search(title) or sd_resolution_regex.search(title)) and not awards_regex.search(title):
        return '62'
    if season_regex.search(title) and uhd_resolution_regex.search(title) and not awards_regex.search(title):
        return '85'

    complete_name_match = re.search(r'Complete name\s*:\s*([^\r\n]+)', mediainfo_text or "")
    if complete_name_match:
        complete_name = complete_name_match.group(1).strip().lower()
        if complete_name.endswith(('.m4b', '.pdf', '.epub')) or (audiobook_regex.search(title) and complete_name.endswith('.mp3') and not webdl_regex.search(title) and not webrip_regex.search(title) and not episode_regex.search(title) and not bluray_regex.search(title) and not hdrip_regex.search(title) and not games_backup_regex.search(title) and not games_regex.search(title)):
            return CATEGORY_BOOKS
        if complete_name.endswith('.flac'):
            return '71'
        if complete_name.endswith('.mp3'):
            return '22'
    return None


ISBN_PATTERN = re.compile(
    r'\b97[89][0-9Xx\-\s]{10,}\b|\b\d{9}[\dXx]\b'
)

ORDINAL_EXCEPTIONS = (11, 12, 13)

def _build_edition_label(num_str: str) -> str:
    try:
        num = int(num_str)
        suffix = "th"
        if num % 100 not in ORDINAL_EXCEPTIONS:
            suffix = {1: "st", 2: "nd", 3: "rd"}.get(num % 10, "th")
        return f"{num}{suffix} Edition"
    except (TypeError, ValueError):
        return num_str


def normalize_isbn(isbn: str | None) -> str | None:
    digits = re.sub(r'[^0-9Xx]', '', isbn or "")
    if len(digits) in (10, 13):
        return digits.upper()
    return None


def _extract_edition_from_text(text: str) -> tuple[str | None, str]:
    edition = None
    updated = text
    patterns = [
        r'\b(\d{1,2})(?:st|nd|rd|th)?\s*(?:edition|ed)\b',
        r'\b(\d{1,2})\s*e\b',
    ]
    for pat in patterns:
        m = re.search(pat, updated, re.I)
        if m:
            edition = _build_edition_label(m.group(1))
            updated = re.sub(pat, '', updated, flags=re.I).strip(" -._")
            break
    return edition, re.sub(r'\s+', ' ', updated).strip()


def parse_pdf_filename(filename: str) -> dict:
    stem = Path(filename).stem
    cleaned = re.sub(r'[._]+', ' ', stem)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(" -._")

    isbn_match = ISBN_PATTERN.search(cleaned)
    isbn = normalize_isbn(isbn_match.group(0)) if isbn_match else None

    edition, remaining = _extract_edition_from_text(cleaned)

    title_part = remaining
    author_part = ""

    by_split = re.split(r'\bby\b', remaining, flags=re.I)
    if len(by_split) > 1:
        title_part = by_split[0].strip(" -")
        author_part = " ".join(by_split[1:]).strip(" -")
    elif " - " in remaining:
        left, right = remaining.split(" - ", 1)
        if re.search(r'\d', right) and not re.search(r'\d', left):
            author_part = left
            title_part = right
        else:
            title_part = left
            author_part = right

    authors = [a.strip() for a in re.split(r',|&| and ', author_part) if a.strip()]

    return {
        "title": re.sub(r'\s+', ' ', title_part).strip(),
        "authors": authors,
        "edition": edition,
        "isbn": isbn,
    }


def fetch_book_info_by_isbn(isbn: str) -> dict | None:
    if not isbn:
        return None
    try:
        url = f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}"
        r = requests.get(url, timeout=20)
        if r.status_code == 429:
            error("Book API rate limit reached (429). Please retry later.")
            return None
        if r.status_code >= 500:
            error(f"Book API server error ({r.status_code}).")
            return None
        if r.status_code != 200:
            error(f"Book API request failed with status {r.status_code}.")
            return None
        data = r.json()
        items = data.get("items") or []
        if not items:
            return None
        info = items[0].get("volumeInfo", {})
        identifiers = {i.get("type"): i.get("identifier") for i in info.get("industryIdentifiers", []) if i.get("type") and i.get("identifier")}
        edition_from_title, _ = _extract_edition_from_text(info.get("title") or "")
        edition_from_subtitle, _ = _extract_edition_from_text(info.get("subtitle") or "")
        return {
            "title": info.get("title"),
            "authors": info.get("authors") or [],
            "publisher": info.get("publisher"),
            "year": (info.get("publishedDate") or "").split("-")[0],
            "description": info.get("description"),
            "pageCount": info.get("pageCount"),
            "isbn10": identifiers.get("ISBN_10"),
            "isbn13": identifiers.get("ISBN_13"),
            "edition": edition_from_title or edition_from_subtitle,
        }
    except (requests.RequestException, json.JSONDecodeError) as exc:
        error(f"Book API lookup failed: {exc}")
        return None


def prompt_for_isbn() -> str | None:
    while True:
        user_input = input(f"{c.BOLD}Enter ISBN (leave blank to skip): {c.RESET}").strip()
        if not user_input:
            return None
        normalized = normalize_isbn(user_input)
        if normalized:
            return normalized
        print(f"{c.RED}Invalid ISBN. Please try again.{c.RESET}")


def build_book_info(pdf_path: Path, page_count: int | None) -> dict:
    parsed = parse_pdf_filename(pdf_path.name)
    isbn = parsed.get("isbn")
    book_info = {
        "title": parsed.get("title"),
        "authors": parsed.get("authors") or [],
        "edition": parsed.get("edition"),
        "isbn": isbn,
        "publisher": None,
        "year": None,
        "description": None,
        "pageCount": page_count,
        "isbn10": None,
        "isbn13": None,
    }

    api_data = fetch_book_info_by_isbn(isbn) if isbn else None


    has_isbn = bool(isbn)
    has_title = bool(book_info["title"])
    has_authors = bool(book_info["authors"])
    needs_isbn_prompt = not api_data and (not has_isbn or not has_title or not has_authors)

    if needs_isbn_prompt:
        isbn_from_user = prompt_for_isbn()
        if isbn_from_user:
            api_data = fetch_book_info_by_isbn(isbn_from_user)
            book_info["isbn"] = isbn_from_user

    if api_data:
        for key in ["title", "authors", "publisher", "year", "description", "pageCount", "isbn10", "isbn13"]:
            if api_data.get(key):
                book_info[key] = api_data[key]
        if not book_info["edition"] and api_data.get("edition"):
            book_info["edition"] = api_data["edition"]
        if not book_info["isbn"]:
            book_info["isbn"] = api_data.get("isbn13") or api_data.get("isbn10")

    if not book_info["title"]:
        isbn_value = book_info.get("isbn")
        if isbn_value:
            book_info["title"] = f"ISBN: {isbn_value}"
        else:
            book_info["title"] = pdf_path.stem

    if not book_info["pageCount"]:
        book_info["pageCount"] = page_count

    return book_info


def get_pdf_page_count(pdf_path: Path) -> int | None:
    if shutil.which("pdfinfo"):
        try:
            res = subprocess.run(
                ["pdfinfo", str(pdf_path)],
                capture_output=True,
                text=True,
                startupinfo=hide_window(),
                timeout=10,
            )
            if res.returncode == 0:
                match = re.search(r'^Pages:\s+(\d+)', res.stdout, re.M)
                if match:
                    return int(match.group(1))
        except (subprocess.SubprocessError, subprocess.TimeoutExpired, OSError) as exc:
            error(f"pdfinfo failed: {exc}")
    try:
        if PyPDF2 is None:
            return None
        with open(pdf_path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            return len(reader.pages)
    except (OSError, AttributeError) as exc:
        error(f"PyPDF2 fallback failed: {exc}")
        return None


def pick_pdf_pages(page_count: int | None, sample_count: int = 5) -> list[int]:
    pages = [1]
    if page_count and page_count > 1:
        take = min(sample_count, page_count - 1)
        step = (page_count - 1) / (take + 1)
        for idx in range(take):
            page = 1 + round((idx + 1) * step)
            page = min(page_count, max(2, page))
            pages.append(page)
        pages[1:] = sorted(dict.fromkeys(pages[1:]))
    return pages


def render_pdf_pages(pdf_path: Path, pages: list[int]) -> list[Path]:
    if not shutil.which("pdftoppm"):
        error("pdftoppm not found! Install poppler-utils to extract PDF pages.")
        return []
    outputs = []
    def _render_page(page: int) -> Path | None:
        prefix = f"pdf_page_{page:03d}"
        cmd = ["pdftoppm", "-f", str(page), "-l", str(page), "-png", str(pdf_path), prefix]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, startupinfo=hide_window(), timeout=60)
            candidates = sorted(
                p for p in Path(".").glob(f"{prefix}-*.png")
                if re.fullmatch(rf"{re.escape(prefix)}-\d+\.png", p.name)
            )
            if not candidates:
                candidates = [
                    Path(f"{prefix}-001.png"),
                    Path(f"{prefix}-01.png"),
                    Path(f"{prefix}-1.png"),
                ]
            generated = next((p for p in candidates if p.exists()), None)
            if generated:
                final_name = Path(f"{prefix}.png")
                generated.rename(final_name)
                log(f"Extracted PDF page {page}: {generated.name} → {final_name.name}", "PDF")
                GENERATED_PDF_IMAGES.append(final_name)
                return final_name
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            extra = ""
            if isinstance(e, subprocess.CalledProcessError) and e.stderr:
                extra = f": {e.stderr.decode(errors='ignore').strip()}"
            error(f"Failed to extract page {page}{extra or f': {e}'}")
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(4, len(pages)))) as executor:
        for result in executor.map(_render_page, pages):
            if result:
                outputs.append(result)
    return outputs


def build_pdf_title(book_info: dict) -> str:
    authors = book_info.get("authors") or []
    author_str = authors[0] if authors else "Unknown"
    edition = book_info.get("edition")
    edition_block = f" [{edition}]" if edition else ""
    return f"{author_str} - {book_info.get('title', '').strip()}{edition_block} [PDF]".strip()


def _resolve_isbn_values(book_info: dict) -> tuple[str, str]:
    isbn10 = book_info.get("isbn10")
    isbn13 = book_info.get("isbn13")
    isbn_generic = book_info.get("isbn")
    if not isbn10 and isbn_generic and len(isbn_generic) == 10:
        isbn10 = isbn_generic
    if not isbn13 and isbn_generic and len(isbn_generic) == 13:
        isbn13 = isbn_generic
    return isbn10 or "Unknown", isbn13 or "Unknown"


def generate_pdf_description(book_info: dict, cover_url: str, screenshot_urls: list[str], mediainfo_text: str) -> str:
    edition = book_info.get("edition") or "N/A"
    authors = ", ".join(book_info.get("authors") or []) or "Unknown"
    publisher = book_info.get("publisher") or "Unknown"
    year = book_info.get("year") or "Unknown"
    isbn10, isbn13 = _resolve_isbn_values(book_info)
    page_count = book_info.get("pageCount") or "Unknown"
    description_text = book_info.get("description") or "N/A"
    ss_bbcode = "\n".join([f"[img]{u}[/img]" for u in screenshot_urls]) if screenshot_urls else "Screenshots not available."
    cover_bbcode = cover_url if cover_url.startswith("[img]") else f"[img]{cover_url}[/img]" if cover_url else "Cover not available."

    return f"""[color=#FF8040][center][font=Segoe UI][size=5]Title:  {book_info.get('title', 'Unknown')}[/size][/font]
    [/color]

{cover_bbcode}
[color=#7EC544]
[font=Segoe UI]
[size=5]Edition: {edition}
By: {authors}
Publisher: {publisher}, {year}
ISBN-10: {isbn10}
ISBN-13: {isbn13}
Page Count:  {page_count}[/size][/font][/color][/center]
[font=Segoe UI][size=4][color=#d500f9]Description: {description_text}[/color]
[/size][/font]
[center]
[b][size=5][color=#59E817][font=Segoe UI]MediaInfo[/font][/color][/size][/b][/center][font=Courier New][mediainfo]
{mediainfo_text}
[/mediainfo]
[/font]
[center][b][size=5][font=Tahoma][color=#59E817]Screenshots[/color][/size][/b]
[/center][center][/font]
{ss_bbcode}
[/center]
[hr][i][b][center][font=Segoe UI][size=4][color=#FFD700]If you're downloading my torrent and not getting the desired speed, just comment, and I'll move it to my seedbox.[/color][/size][/font][/center][/b][/i]"""

def format_title_for_metadata(target_path: Path, is_folder: bool, video_path: Path | None = None, torrent_name: str | None = None) -> str:
    if is_folder and not video_path:
        return target_path.name

    source_path = video_path if (is_folder and video_path) else target_path
    stem = source_path.stem


    if is_folder and torrent_name:
        folder_has_episode = bool(re.search(r'\bS\d{2}E\d{2}\b', target_path.name, re.I))
        file_has_episode = bool(re.search(r'\bS\d{2}E\d{2}\b', stem, re.I))
        torrent_has_episode = bool(re.search(r'\bS\d{2}E\d{2}\b', torrent_name, re.I))

        if file_has_episode and not folder_has_episode and not torrent_has_episode:
            try:
                return generate_title(str(source_path), is_season_pack=True)
            except Exception:
                return target_path.name.replace('.torrent', '')


        pack_names = [torrent_name, target_path.name]
        is_fansub_pack = any(_FANSUB_PACK_RANGE_RE.search(n) for n in pack_names if n)
        if is_fansub_pack:
            try:
                return generate_title(str(source_path), is_pack=True)
            except Exception as exc:
                error(f"Title generation failed for {source_path.name}: {exc}")
                return target_path.name

    preformatted_tech_block = (
        r'\([^)]*\b(?:WEB-DL|WEBRip|BluRay|2160p|1080p|720p|'
        r'x264|x265|H\.?264|H\.?265|DDP?\d|DD\+\d|AAC)\b[^)]*\)'
    )
    if re.search(r'\bS\d{2}E\d{2}\b', stem) and " - " in stem:
        return stem
    if (
        re.search(r'\bS\d{2}E\d{2}\b', stem)
        and re.search(preformatted_tech_block, stem, re.I)
        and re.search(r'\[[^\]]+\]\s*$', stem)
    ):
        anime_format_match = re.match(
            r'^(?P<show>.+?)\s+\(\d{4}\)\s+(?P<ep>S\d{2}E\d{2})\s+\((?P<tech>[^)]*?)\)\s+\[(?P<group>[^\]]+)\]\s*$',
            stem,
            re.I,
        )
        if anime_format_match:
            show = anime_format_match.group("show").strip()
            episode = anime_format_match.group("ep").upper()
            tech = anime_format_match.group("tech").strip()
            group = anime_format_match.group("group").strip()

            video_match = re.search(r'\b(?:H\.?264|H\.?265|x264|x265|AV1|HEVC|AVC)\b', tech, re.I)
            audio_match = re.search(r'\b(?:AAC|DDP|DD\+|DD|TrueHD|DTS(?:-HD)?|FLAC|Opus)\b', tech, re.I)

            video = ""
            audio = ""
            audio_channels = ""
            remaining = tech

            if video_match:
                video = video_match.group(0).upper().replace(".", "")
                remaining = re.sub(re.escape(video_match.group(0)), " ", remaining, count=1, flags=re.I)

            if audio_match:
                audio = audio_match.group(0).upper()
                remaining = re.sub(re.escape(audio_match.group(0)), " ", remaining, count=1, flags=re.I)
                ch_match = re.search(r'\b([2-9](?:[.\s][0-1])?)\b', remaining)
                if ch_match:
                    audio_channels = ch_match.group(1).replace(".", " ")
                    remaining = remaining[:ch_match.start()] + " " + remaining[ch_match.end():]

            remaining = re.sub(r'\s+', ' ', remaining).strip()
            bits = [show, episode]
            if remaining:
                bits.append(remaining)
            if audio:
                bits.append(f"{audio} {audio_channels}".strip())
            if video:
                bits.append(video)
            return f"{' '.join(bits)}-{group}"
        return stem

    try:
        return generate_title(str(source_path))
    except Exception as exc:
        error(f"Title generation failed for {source_path.name}: {exc}")
        return target_path.name

def main():

    global LATEST_JSON, GENERATED_TXT, EXTRACTED_COVER, INDEX_HTML, COVER_PATH

    target_path, is_folder = select_target()
    if not target_path or not target_path.exists(): return


    _stripped = strip_leading_site_prefix(target_path.name)
    if not is_folder:
        _stem, _ext = os.path.splitext(_stripped)
        _norm_stem = re.sub(r'[._ ]+', '.', _stem).strip('.')
        _final_name = (_norm_stem + _ext) if _norm_stem else _stripped
    else:
        _norm = re.sub(r'[._ ]+', '.', _stripped).strip('.')
        _final_name = _norm if _norm else _stripped
    if _final_name and _final_name != target_path.name:
        renamed_path = target_path.parent / _final_name
        if not renamed_path.exists():
            try:
                target_path.rename(renamed_path)
                log(f"Renamed: '{target_path.name}' → '{_final_name}'", "Rename", c.YELLOW)
                target_path = renamed_path
            except OSError as exc:
                error(f"Could not rename '{target_path.name}': {exc}")
        else:
            log(f"Skipping rename – '{_final_name}' already exists in the same directory.", "Rename", c.YELLOW)

    sync_dir = target_path.parent
    LATEST_JSON = sync_dir / "latest.json"
    INDEX_HTML = sync_dir / "index.html"


    _stale_cleanup = [
        LATEST_JSON,
        INDEX_HTML,
        target_path.parent / f"{target_path.name}.torrent",
    ]
    for _stale in _stale_cleanup:
        if _stale.exists():
            try:
                _stale.unlink()
                log(f"Removed stale file: {_stale.name}", "Cleanup", c.YELLOW)
            except OSError:
                pass

    try:
        INDEX_HTML.write_text(_WEBAPP_HTML, encoding='utf-8')
    except OSError as exc:
        error(f"Could not write temporary index.html: {exc}")

    clear(); banner()
    print(f"{c.BOLD}{c.PURPLE}Selected → {target_path.name}{c.RESET} {'(Folder Mode)' if is_folder else ''}\n")


    _srt_include: bool | None = None
    if is_folder:
        _srt_check = list(target_path.rglob("*.srt"))
        if _srt_check:
            log(f"Found {len(_srt_check)} .srt subtitle file(s) in folder.", "SRT", c.YELLOW)
            for _sf in _srt_check[:3]:
                print(f"   {c.DIM}{_sf.name}{c.RESET}")
            if len(_srt_check) > 3:
                print(f"   {c.DIM}...and {len(_srt_check) - 3} more{c.RESET}")
            _srt_include = (input(f"\n{c.BOLD}Include .srt files in torrent? [y/N]: {c.RESET}").strip().lower() == 'y')


    _torrent_result: list[bool] = [False]
    def _torrent_worker():
        _torrent_result[0] = create_torrent(target_path, _srt_include)
    _torrent_thread = threading.Thread(target=_torrent_worker, daemon=True, name="torrent-creator")
    _torrent_thread.start()

    is_audio_folder = False
    is_pdf = False
    audio_files = []
    video_files = []
    pdf_files = []

    if is_folder:
        audio_files = sort_paths_by_mtime([f for f in target_path.rglob('*') if f.is_file() and f.suffix.lower() in AUDIO_EXTS])
        video_files = sort_paths_by_mtime([f for f in target_path.rglob('*') if f.is_file() and f.suffix.lower() in VIDEO_EXTS])
        pdf_files = sort_paths_by_mtime([f for f in target_path.rglob('*') if f.is_file() and f.suffix.lower() in PDF_EXTS])

        if audio_files and not video_files and not pdf_files:
            is_audio_folder = True
        elif pdf_files and not audio_files and not video_files:
            is_pdf = True
        elif not audio_files and not video_files and not pdf_files:
            error("No audio, video, or PDF files found!")
            return
    elif target_path.suffix.lower() in AUDIO_EXTS:
        is_audio_folder = True
        audio_files = [target_path]
    elif target_path.suffix.lower() in PDF_EXTS:
        is_pdf = True
        pdf_files = [target_path]
    else:
        video_files = [target_path]

    if is_audio_folder:
        if not audio_files:
            error("No audio files found!")
            return


        is_discography_selection = False
        if is_folder:
            relative_parts_list: list[tuple[str, ...]] = []
            for f in audio_files:
                try:
                    relative_parts_list.append(f.relative_to(target_path).parts)
                except ValueError:
                    continue
            top_level_album_dirs = {
                parts[0]
                for parts in relative_parts_list
                if len(parts) > 1
            }
            is_discography_selection = len(top_level_album_dirs) > 1


        first_audio = select_representative_audio_file(audio_files, target_path if is_folder else None)
        metadata = extract_audio_metadata(first_audio)

        mediainfo_text = get_mediainfo(first_audio)
        mediainfo_text = trim_mediainfo_complete_name(mediainfo_text, sync_dir)


        preferred_spectrogram_audio = random.choice(audio_files) if is_discography_selection else first_audio
        spectrogram_audios = select_audio_files_for_spectrograms(audio_files, preferred_spectrogram_audio, fallback_count=2)
        spectrogram_entries: list[tuple[str, str]] = []
        for index, spectrogram_audio in enumerate(spectrogram_audios, start=1):
            log(f"Creating spectrogram {index}/{len(spectrogram_audios)} for {spectrogram_audio.name}...", "Audio")
            spectrogram = create_spectrogram(spectrogram_audio)
            if not spectrogram:
                error("Spectrogram creation failed!")
                return

            log(f"Uploading spectrogram {index}/{len(spectrogram_audios)}...", "Upload")
            spectrogram_url = upload_image(spectrogram)
            if not spectrogram_url:
                error("Spectrogram upload failed!")
                return
            spectrogram_entries.append((spectrogram_audio.name, spectrogram_url))
        success("Spectrogram(s) uploaded!")


        cover_url = AUDIO_NO_COVER_PLACEHOLDER
        local_cover_path = None
        cover_search_dir = target_path if is_folder else target_path.parent
        cover_image = find_cover_image(cover_search_dir)
        if cover_image:
            log(f"Found cover image: {cover_image.name}", "Cover")
            local_cover_path = cover_image
        else:
            if is_discography_selection:
                log(f"No root cover found for discography selection; using {AUDIO_NO_COVER_PLACEHOLDER}", "Cover")
            else:
                log("No cover image found in folder, trying embedded cover...", "Cover")
                extracted = extract_cover_from_audio(audio_files, sync_dir)
                if extracted:
                    EXTRACTED_COVER = extracted
                    local_cover_path = extracted
                    log("Extracted embedded cover from audio track", "Cover")
                else:
                    log("No cover found (folder or embedded)", "Cover")

        if local_cover_path:
            COVER_PATH = local_cover_path
            log("Uploading cover image...", "Upload")
            uploaded_cover_url = upload_image(local_cover_path)
            if uploaded_cover_url:
                cover_url = f"[img]{uploaded_cover_url}[/img]"
                success("Cover image uploaded!")
            else:
                error(f"Cover image upload failed, using {AUDIO_NO_COVER_PLACEHOLDER}")


        fakingthefunk_url: str | None = None
        ftf_search_root = target_path if is_folder else target_path.parent
        fakingthefunk_image = find_fakingthefunk_image(ftf_search_root)
        if fakingthefunk_image:
            log(f"Found FakingTheFunk proof: {fakingthefunk_image.name}", "Proof")
            log("Uploading FakingTheFunk proof image...", "Upload")
            fakingthefunk_url = upload_image(fakingthefunk_image)
            if fakingthefunk_url:
                success("FakingTheFunk proof uploaded!")
            else:
                error("FakingTheFunk proof upload failed; it will be omitted from description")
                fakingthefunk_url = None

        tracklist = generate_audio_tracklist(audio_files, target_path if is_folder else None)
        description = generate_audio_description(target_path.name, cover_url, mediainfo_text, tracklist, spectrogram_entries, fakingthefunk_url)

        title = generate_audio_title(target_path.name, metadata)

        if not SKIP_TXT:
            save_name = f"{target_path.name}_description.txt" if is_folder else f"{target_path.stem}_TBD_Description.txt"
            txt_path = target_path.parent / save_name
            txt_path.write_text(description, encoding="utf-8")
            GENERATED_TXT = txt_path
            success(f"Saved → {save_name}")

        copy_to_clipboard(description)

        if START_HTTP_SERVER:
            try:
                torrent_filename = f"{target_path.name}.torrent"
                category = "71" if metadata.get("codec") == "FLAC" else "22"
                language = detect_language(mediainfo_text)
                payload = {
                    "ready": True,
                    "title": title,
                    "category": category,
                    "language": language,
                    "description": description,
                    "torrentFile": torrent_filename
                }

                if local_cover_path:
                    payload["coverFile"] = str(local_cover_path.relative_to(sync_dir)).replace('\\', '/')

                with open(LATEST_JSON, "w", encoding="utf-8") as f:
                    json.dump(payload, f)

                success(f"Sync files ready for Localhost!")

                start_server_thread(HTTP_PORT)

            except Exception as e:
                error(f"HTTP Sync Failed: {e}")

    elif is_pdf:
        pdf_path = pdf_files[0]
        mediainfo_text = get_mediainfo(pdf_path)
        mediainfo_text = trim_mediainfo_complete_name(mediainfo_text, sync_dir)

        page_count = get_pdf_page_count(pdf_path)
        if page_count is None:
            log("PDF page count unavailable; only the first page will be extracted.", "Warn", c.YELLOW)
        book_info = build_book_info(pdf_path, page_count)

        pages_to_extract = pick_pdf_pages(page_count, sample_count=5)
        extracted_images = render_pdf_pages(pdf_path, pages_to_extract)
        cover_url = ""
        screenshot_urls: list[str] = []
        cover_local_path: Path | None = None

        if extracted_images:
            cover_local_path = extracted_images[0]
            if cover_local_path.parent != sync_dir:
                hex_suffix = secrets.token_hex(8)
                staged_cover_path = sync_dir / f"{pdf_path.stem}_cover_{hex_suffix}{cover_local_path.suffix}"
                try:
                    shutil.copy(cover_local_path, staged_cover_path)
                    GENERATED_PDF_IMAGES.append(staged_cover_path)
                    cover_local_path = staged_cover_path
                    extracted_images[0] = staged_cover_path
                except OSError as exc:
                    error(f"Failed to stage PDF cover image from {cover_local_path} to {staged_cover_path}: {exc}")

            COVER_PATH = cover_local_path

            log("Uploading extracted pages...", "Upload")
            total_pages = len(extracted_images)
            print_progress(0, total_pages)
            uploaded_map: dict[int, str] = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=_bounded_workers(total_pages)) as executor:
                future_to_idx = {executor.submit(upload_image, img): idx for idx, img in enumerate(extracted_images)}
                done_count = 0
                for future in concurrent.futures.as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    try:
                        res = future.result()
                    except Exception as exc:
                        error(f"Upload failed for page {idx + 1}: {exc}")
                        res = None
                    if res:
                        uploaded_map[idx] = res
                    done_count += 1
                    print_progress(done_count, total_pages)

            cover_entry = uploaded_map.get(0)
            if cover_entry:
                cover_url = f"[img]{cover_entry}[/img]"
            screenshot_urls = []
            for idx in range(1, total_pages):
                url = uploaded_map.get(idx)
                if url:
                    screenshot_urls.append(url)
        else:
            error("No PDF pages extracted; screenshots unavailable.")

        description = generate_pdf_description(book_info, cover_url, screenshot_urls, mediainfo_text)
        title = build_pdf_title(book_info)

        if not SKIP_TXT:
            save_name = f"{pdf_path.stem}_TBD_Description.txt"
            txt_path = pdf_path.parent / save_name
            txt_path.write_text(description, encoding="utf-8")
            GENERATED_TXT = txt_path
            success(f"Saved → {save_name}")

        copy_to_clipboard(description)

        if START_HTTP_SERVER:
            try:
                torrent_filename = f"{target_path.name}.torrent"
                payload = {
                    "ready": True,
                    "title": title,
                    "category": CATEGORY_BOOKS,
                    "language": "0",
                    "description": description,
                    "torrentFile": torrent_filename
                }
                if cover_local_path and cover_local_path.exists():
                    try:


                        payload["coverFile"] = cover_local_path.relative_to(sync_dir).as_posix()
                    except ValueError:
                        error(
                            f"Cover image '{cover_local_path.resolve()}' is outside sync directory '{sync_dir.resolve()}' while setting coverFile; "
                            "move it into the target folder (avoid resolving through symlinks) or verify the sync directory before retrying."
                        )
                with open(LATEST_JSON, "w", encoding="utf-8") as f:
                    json.dump(payload, f)
                success("Sync files ready for Localhost!")
                start_server_thread(HTTP_PORT)
            except Exception as e:
                error(f"HTTP Sync Failed: {e}")

    else:
        _base = target_path if is_folder else None
        _non_sample = [f for f in video_files if not _is_sample_file(f, _base)]
        video_for_ss = (_non_sample[0] if _non_sample else video_files[0]) if video_files else None

        if not video_for_ss: error("No video found!"); return

        mediainfo_text = get_mediainfo(video_for_ss)
        mediainfo_text = trim_mediainfo_complete_name(mediainfo_text, sync_dir)
        hdr_dv = needs_hdr10_dv_screenshot(mediainfo_text, video_for_ss.name)
        screenshots = take_screenshots(video_for_ss, hdr_dv=hdr_dv, count=SCREENSHOT_COUNT)

        uploaded_direct_urls = []
        if screenshots:
            print_progress(0, len(screenshots))
            with concurrent.futures.ThreadPoolExecutor(max_workers=_bounded_workers(len(screenshots))) as executor:
                futures = {executor.submit(upload_image, img): img for img in screenshots}
                done_count = 0
                for future in concurrent.futures.as_completed(futures):
                    if res := future.result(): uploaded_direct_urls.append(res)
                    done_count += 1
                    print_progress(done_count, len(screenshots))
            if uploaded_direct_urls:
                success(f"Uploaded {len(uploaded_direct_urls)}/{len(screenshots)} screenshots")
            else:
                error("Screenshot upload failed; no URLs returned")

        for f in Path(".").glob("ss_*.*"):
            try: f.unlink()
            except: pass

        ss_bbcode = "\n".join([f"[img]{u}[/img]" for u in uploaded_direct_urls])
        description = f"[center][b][size=5][color=#59E817][font=Oswald]MediaInfo[/color][/size][/b][/center][b][/font][mediainfo]\n{mediainfo_text}[/mediainfo]\n[center][/b][b][size=5][color=#59E817][font=Oswald]Screenshots[/color][/size][/b][/center]\n[center]\n{ss_bbcode}[/center][/font]"

        if not SKIP_TXT:
            save_name = f"{target_path.name}_description.txt" if is_folder else f"{target_path.stem}_TBD_Description.txt"
            txt_path = target_path.parent / save_name
            txt_path.write_text(description, encoding="utf-8")
            GENERATED_TXT = txt_path
            success(f"Saved → {save_name}")

        copy_to_clipboard(description)

        if START_HTTP_SERVER:
            try:

                torrent_filename = f"{target_path.name}.torrent"
                title = format_title_for_metadata(target_path, is_folder, video_for_ss, torrent_filename)
                category = detect_category(title, mediainfo_text) or "0"
                language = detect_language(mediainfo_text)
                payload = {
                    "ready": True,
                    "title": title,
                    "category": category,
                    "language": language,
                    "description": description,
                    "torrentFile": torrent_filename,
                    "screenshots": uploaded_direct_urls,
                }

                with open(LATEST_JSON, "w", encoding="utf-8") as f:
                    json.dump(payload, f)

                success(f"Sync files ready for Localhost!")

                start_server_thread(HTTP_PORT)

            except Exception as e:
                error(f"HTTP Sync Failed: {e}")


    _torrent_thread.join()
    _torrent_ok = _torrent_result[0]
    if not _torrent_ok and CREATE_TORRENT_FILE:
        error("Torrent creation failed!")

    if _torrent_ok or not CREATE_TORRENT_FILE:
        print(f"\n{c.BOLD}{c.GREEN}ALL DONE!{c.RESET}")
    else:
        print(f"\n{c.BOLD}{c.YELLOW}DONE (torrent creation failed — description still copied to clipboard).{c.RESET}")
    print(f"{c.DIM}When you exit, sync files & generated torrents will be deleted.{c.RESET}")
    input(f"\nPress Enter to exit...")

if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: print(f"\n\n{c.YELLOW}Cancelled.{c.RESET}")
