# -*- coding: utf-8 -*-
import os, re, json, subprocess, functools, requests
from collections import OrderedDict
from typing import Optional, Union, List, Tuple, Dict, Any

VIDEO_EXT = {".mkv", ".mp4", ".ts", ".m4v", ".avi"}
SOURCE_MAP = {"amazon": "AMZN", "prime": "AMZN", "netflix": "NF", "aha": "AHA", "viki": "VIKI", "KOCOWA+": "KCW", "disney": "DSNP", "hbo": "HMAX", "apple": "ATVP", "hulu": "HULU", "SunNXT": "SNXT", "peacock": "PCOK", "paramount": "PMTP", "stan": "STAN", "crunchyroll": "CR", "hotstar": "HS", "iplayer": "iP", "all4": "ALL4", "JioHotstar": "JHS", "AppleTV": "ATV"}
FILENAME_SERVICES = ["AMZN","NF","VIKI","AHA","SNXT","KCW","DSNP","ATV","JHS","WTV","HULU","ATVP","HMAX","PCOK","PMTP","STAN","CRAV","MUBI","CC","CR","FUNI","HTSR","HS","iP","ALL4","iT","BBC"]
_SOURCE_PATTERNS = [(re.compile(rf'(?<![A-Za-z]){re.escape(k)}(?![A-Za-z])', re.I), v) for k, v in SOURCE_MAP.items()]
UNKNOWN_YEAR_FALLBACK = 9999
_TAG_PAT = r"(?:2160p|1080p|720p|480p|WEB-DL|WEBRip|WEB|"+ "|".join(re.escape(s) for s in FILENAME_SERVICES)+ r"|x264|x265|H\.264|H\.265|HEVC|AAC|FLAC|DV|HDR|REPACK|PROPER|INTERNAL)"
_CHANNEL_LAYOUT_PAT = r"\d(?:[.\s]?\d)?"
_OPTIONAL_CHANNEL_LAYOUT_PAT = rf"(?:[.\s]?{_CHANNEL_LAYOUT_PAT})?"
_HDR_BLOCK_PATTERN = r"(?:DV(?:\.HDR10\+?|\.HDR10|\.HLG)?|HDR10\+?|HDR10|HDR|HLG)"
_BASE_AUDIO_PATTERN = r"(?:DDP?\+?\d(?:\.\d)?|TrueHD\d(?:\.\d)?|DTS(?:-HD(?:\.MA|\.HRA)|-X)?\d(?:\.\d)?|AAC\d\.\d|FLAC|Opus)"
_AUDIO_BLOCK_PATTERN = rf"(?:{_BASE_AUDIO_PATTERN}(?:\.Atmos)?|Atmos)"
_AUDIO_FULL_RE, _HDR_FULL_RE, _VIDEO_FULL_RE = re.compile(rf"(?i)\b{_AUDIO_BLOCK_PATTERN}\b"), re.compile(rf"(?i)\b{_HDR_BLOCK_PATTERN}\b"), re.compile(r"(?i)\b(?:x264|x265|H\.264|H\.265|AVC|HEVC|AV1|VP9)\b")
_WEBDL_MIN_BITRATE = {"2160p": 8000000, "1080p": 3000000, "720p": 1500000}
_REENCODE_RE = re.compile(r'x264|x265|libx264|HandBrake|FFmpeg', re.I)
_FANSUB_RE = re.compile(r'\[([^\]]+)\]\s*(.+?)\s+-\s+(\d+(?:\.\d+)?)\s*(?:v\d+\s*)?(?:[\(\[](\d+p))?')
ORDINAL_SEASON_RE, ORDINAL_SEASON_D_RE = re.compile(r"\b(\d+)(?:st|nd|rd|th)\s+Season\b", re.I), re.compile(r"\b([1-9])d\s+Season\b", re.I)
_GROUP_TLD_SUFFIX_RE = re.compile(r"-\s*([A-Za-z0-9]+)\s+(?:com|org|net|in|co|io|cc|me|tv)\s*$", re.I)
_PAHE_GROUP_SUFFIX_RE = re.compile(r"(?i)-\s*Pahe(?:[.\s]+(?:com|org|net|in|co|io|cc|me|tv))?\s*")
_PSA_GROUP_SUFFIX_RE = re.compile(r"(?i)-\s*PSA\s*")
ANILIST_API_URL = "https://graphql.anilist.co"
_AUDIO_LANGUAGE_CACHE = OrderedDict()

def get_mediainfo(path):
    try:
        r = subprocess.run(["mediainfo", "--Output=JSON", path], capture_output=True, text=True, timeout=15)
        tracks = json.loads(r.stdout).get("media", {}).get("track", [])
        g=v=a=None; langs=[]
        for t in tracks:
            tt = t.get("@type")
            if tt=="General": g=t
            elif tt=="Video" and v is None: v=t
            elif tt=="Audio":
                l = (t.get("Language") or "").strip().lower()
                if l: langs.append(l)
                if a is None: a=t
        if langs: _AUDIO_LANGUAGE_CACHE[str(path)] = _dedupe_preserve_order(langs)
        return g, v, a
    except: return None, None, None

def _safe_int(val, default=0):
    try: return int(re.search(r"-?\d+", str(val).split("/")[0]).group(0))
    except: return default

def _safe_float(val, default=0.0):
    try: return float(str(val).split("/")[0].strip())
    except: return default

def _normalize_channel_layout(val):
    d = re.findall(r"\d", str(val))
    return f"{d[0]}.{d[1]}" if len(d)>=2 else d[0] if len(d)==1 else ""

def clean_name(n): return re.sub(r'\.+', '.', re.sub(r'[._ ]+', '.', n)).strip('.')

def _strip_leading_site_prefix(name): return re.sub(r"^\s*(?:https?://)?www\.(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\s*[-–—:|]\s*", "", name, count=1, flags=re.I)

def _title_from_psa_filename(name: str) -> Optional[str]:
    c = _strip_leading_site_prefix(re.sub(rf"(?i)\.({'|'.join(VIDEO_EXT)})$", "", name)).strip()
    return c if _PSA_GROUP_SUFFIX_RE.search(c) else None

def _title_from_pahe_filename(name: str) -> Optional[str]:
    c = _strip_leading_site_prefix(re.sub(rf"(?i)\.({'|'.join(VIDEO_EXT)})$", "", name)).strip()
    return _PAHE_GROUP_SUFFIX_RE.sub("-Pahe", c).strip() if _PAHE_GROUP_SUFFIX_RE.search(c) else None

def resolution(w, s=None):
    w = _safe_int(w); suffix = "i" if s and str(s).upper()!="PROGRESSIVE" else "p"
    if w>=3800: return f"2160{suffix}"
    if w>=1900: return f"1080{suffix}"
    if w>=1200: return f"720{suffix}"
    return f"480{suffix}" if w>=640 else None

def video_codec(v):
    f = v.get("Format", "") if v else ""
    for k,t in [("AVC","H.264"),("HEVC","H.265"),("AV1","AV1"),("VP9","VP9")]:
        if k in f: return t
    return f if f else None

def detect_hdr(v):
    if not v: return None
    b, c, t, f, cp = _safe_int(v.get("BitDepth")), str(v.get("colour_primaries","")), str(v.get("transfer_characteristics","")), str(v.get("HDR_Format","")), str(v.get("HDR_Format_Compatibility",""))
    tags = []
    if "dolby vision" in f.lower(): tags.append("DV")
    if "hdr10+" in f.lower(): tags.append("HDR10+")
    elif "hdr10" in cp.lower() or "hdr10" in f.lower() or ("2020" in c and "PQ" in t.upper() and b>=10): tags.append("HDR10")
    elif "HLG" in t.upper(): tags.append("HLG")
    return ".".join(tags) if tags else None

def audio_info(a):
    if not a: return None
    f = str(a.get("Format","")).upper()
    m = {"E-AC-3":"DDP","AC-3":"DD","MLP FBA":"TrueHD","DTS":"DTS","FLAC":"FLAC","OPUS":"Opus"}
    tag = next((v for k,v in m.items() if f.startswith(k)), f)
    if tag == "DTS":
        p, cm = str(a.get("Format_Profile","")).upper(), str(a.get("Format_Commercial_IfAny","")).upper()
        tag = "DTS-X" if "X" in p else "DTS-HD.MA" if "MA" in p else "DTS-HD.HRA" if "HRA" in p else "DTS"
    ch = _safe_int(str(a.get("Channels")).split("/")[0])
    chs = {8:"7.1", 6:"5.1", 2:"2.0", 1:"1.0"}.get(ch, "")
    base = f"{tag}{chs}" if chs else tag
    return f"{base}.Atmos" if any("atmos" in str(a.get(x,"")).lower() for x in ["Format_Commercial_IfAny","Format_Profile"]) else base

def detect_source_tags_filename(n):
    s = next((s for s in FILENAME_SERVICES if re.search(rf"(?<!\w){s}(?!\w)", n, re.I)), None)
    w = next((w for w in ["WEB-DL","WEBRip","BluRay","REMUX","HDTV"] if re.search(rf"(?<!\w){w}(?!\w)", n, re.I)), None)
    return s, w

def detect_web_type_mediainfo(g, v, a=None):
    s = next((vtag for pat,vtag in _SOURCE_PATTERNS if pat.search(str(g)+str(v))), None)
    enc = (str(v.get("Encoded_Library","")) + str(g.get("Writing_Library",""))).lower() if v or g else ""
    if s: return (s, "WEBRip") if _REENCODE_RE.search(enc) else (s, "WEB-DL")
    return (None, "WEBRip") if _REENCODE_RE.search(enc) else (None, "WEB-DL")

def detect_episode(n):
    m = re.search(r"S(\d{2})E(\d{2})(?:[-.]?E(\d{2}))?", n, re.I)
    if m: return m.group(1), (f"{m.group(2)}-E{m.group(3)}" if m.group(3) else m.group(2))
    m = re.search(r"(?<!\d)(\d{1,2})x(\d{2,3})", n, re.I)
    return (m.group(1).zfill(2), m.group(2)) if m else (None, None)

def _dedupe_preserve_order(i):
    s = set(); return [x for x in i if not (x in s or s.add(x))]

def _collect_audio_languages(p, *src) -> List[str]:
    l = list(_AUDIO_LANGUAGE_CACHE.get(p, []))
    for s in filter(lambda x: isinstance(x,dict), src):
        raw = s.get("_audio_languages") or s.get("Audio_Language_List")
        c = raw.split(" / ") if isinstance(raw,str) else list(raw or [])
        l.extend(x.strip().lower() for x in c if x)
    return _dedupe_preserve_order(l)

def build_name(path):
    n = os.path.basename(path); b, e = os.path.splitext(n)
    if e.lower() not in VIDEO_EXT: b, e = n, ""
    b = _strip_leading_site_prefix(b); g, v, mi_a = get_mediainfo(path)
    grp = _GROUP_TLD_SUFFIX_RE.search(b) or re.search(r"-\s*([A-Za-z0-9]+)\s*$", b)
    s_tag, w_tag = detect_source_tags_filename(b)
    res = resolution(v.get("Width"), v.get("ScanType")) if v else None
    hdr = detect_hdr(v); sn, en = detect_episode(b)
    parts = []
    if sn:
        sh = clean_name(re.sub(r"(?<!\w)(19|20)\d{2}(?!\w)", "", re.split(r"S\d{2}E\d{2}", b, flags=re.I)[0]))
        parts.extend([sh, f"S{sn}E{en}"])
    else:
        y = re.search(r"(19|20)\d{2}", b)
        if y: parts.extend([clean_name(b[:y.start()]), y.group()])
        else: parts.append(clean_name(b))
    if res: parts.append(res)
    _, d_w = detect_web_type_mediainfo(g, v, mi_a)
    ew = w_tag if w_tag and w_tag!="WEB" else d_w or "WEB-DL"
    if s_tag: parts.append(s_tag)
    parts.append(ew)
    if _safe_int(v.get("BitDepth")) == 10: parts.append("10bit")
    if mi_a: parts.append(audio_info(mi_a))
    if hdr: parts.append(hdr)
    vc = video_codec(v)
    if vc: parts.append("x265" if "265" in vc else "x264" if "264" in vc else vc)
    res_n = clean_name(".".join(p for p in parts if p))
    return (res_n + "-" + grp.group(1) + e) if grp else (res_n + e)

def generate_title(path: str, is_pack: bool = False) -> str:
    n = os.path.basename(path)
    p = _title_from_psa_filename(n); ph = _title_from_pahe_filename(n)
    if p: return p
    if ph: return ph
    f = detect_fansub(n)
    if f:
        mi_g, mi_v, mi_a = get_mediainfo(path)
        parts = [f["title"], f"S01E{f['episode']:02d}", f["resolution"], audio_info(mi_a)]
        return " ".join(p for p in parts if p) + f" - {f['group']}"
    return build_name(path).replace('.', ' ')

def detect_fansub(n):
    m = _FANSUB_RE.match(os.path.splitext(n)[0])
    return {"group":m.group(1),"title":m.group(2),"episode":int(float(m.group(3))),"resolution":m.group(4)} if m else None
