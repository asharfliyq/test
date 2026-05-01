from __future__ import annotations

import os
import re
import json
import subprocess
import functools
from collections import OrderedDict
import requests


VIDEO_EXT = {".mkv", ".mp4", ".ts", ".m4v", ".avi"}

SOURCE_MAP = {
    "amazon": "AMZN",
    "prime": "AMZN",
    "netflix": "NF",
    "aha": "AHA",
    "viki": "VIKI",
    "viki rakuten": "VIKI",
    "KOCOWA+": "KCW",
    "disney": "DSNP",
    "disney+": "DSNP",
    "hbo": "HMAX",
    "hbomax": "HMAX",
    "apple": "ATVP",
    "itunes": "iT",
    "hulu": "HULU",
    "SunNXT": "SNXT",
    "peacock": "PCOK",
    "paramount": "PMTP",
    "paramount+": "PMTP",
    "stan": "STAN",
    "crave": "CRAV",
    "mubi": "MUBI",
    "criterion": "CC",
    "crunchyroll": "CR",
    "funimation": "FUNI",
    "hotstar": "HTSR",
    "hotstar": "HS",
    "bbc iplayer": "iP",
    "iplayer": "iP",
    "all4": "ALL4",
    "channel4": "ALL4",
    "warhammertv": "WTV",
    "JioHotstar" : "JHS",
    "AppleTV" : "ATV",
    "now": "NOW",
}

FILENAME_SERVICES = [
    "AMZN", "NF", "VIKI", "AHA", "SNXT", "KCW", "DSNP", "ATV" , "JHS" , "WTV", "HULU", "ATVP", "HMAX", "PCOK", "PMTP",
    "STAN", "CRAV", "MUBI", "CC", "CR", "FUNI", "HTSR", "HS", "iP", "ALL4", "iT", "BBC", "NOW",
]

_SOURCE_PATTERNS = [
    (re.compile(rf'(?<![A-Za-z]){re.escape(k)}(?![A-Za-z])', re.I), vtag)
    for k, vtag in SOURCE_MAP.items()
]

UNKNOWN_YEAR_FALLBACK = 9999

_TAG_PAT = (
    r"(?:2160p|1080p|720p|480p|WEB-DL|WEBRip|WEB|"
    + "|".join(re.escape(s) for s in FILENAME_SERVICES)
    + r"|x264|x265|H\.264|H\.265|HEVC|AVC|AV1|VP9"

    r"|DD\+\d|DDP\d|DD\d|DD\+|DDP|DD(?![A-Za-z])|TrueHD|DTS|AAC|FLAC|Opus"
    r"|BluRay|REMUX|HDTV|HDR10\+|HDR10|HDR|HLG|DV|REPACK|PROPER|INTERNAL|READNFO|UHD)"
)


_CHANNEL_LAYOUT_PAT = r"\d(?:[.\s]?\d)?"
_OPTIONAL_CHANNEL_LAYOUT_PAT = rf"(?:[.\s]?{_CHANNEL_LAYOUT_PAT})?"
_EDITION_LEADING_CHARS = " .-_()"
_EDITION_TRAILING_CHARS = " .-_"

_HDR_BLOCK_PATTERN = r"(?:DV(?:\.HDR10\+?|\.HDR10|\.HLG)?|HDR10\+?|HDR10|HDR|HLG)"
_BASE_AUDIO_PATTERN = (
    r"(?:DDP?\+?\d(?:\.\d)?"
    r"|TrueHD\d(?:\.\d)?|DTS(?:-HD(?:\.MA|\.HRA)|-X)?\d(?:\.\d)?"
    r"|AAC\d\.\d|FLAC|Opus)"
)
_AUDIO_BLOCK_PATTERN = rf"(?:{_BASE_AUDIO_PATTERN}(?:\.Atmos)?|Atmos)"
_AUDIO_FULL_RE = re.compile(rf"(?i)\b{_AUDIO_BLOCK_PATTERN}\b")
_HDR_FULL_RE = re.compile(rf"(?i)\b{_HDR_BLOCK_PATTERN}\b")
_VIDEO_FULL_RE = re.compile(r"(?i)\b(?:x264|x265|H\.264|H\.265|AVC|HEVC|AV1|VP9)\b")

_WEBDL_MIN_BITRATE = {
    "2160p": 8_000_000,
    "1080p": 3_000_000,
    "720p":  1_500_000,
}

_REENCODE_RE = re.compile(
    r'x264|x265|libx264|libx265|HandBrake|obs[\s-]?studio|'
    r'FFmpeg|rav1e|SVT-AV1|aomenc|kvazaar|xvid',
    re.I,
)


_FANSUB_RE = re.compile(
    r'\[([^\]]+)\]\s*'
    r'(.+?)\s+-\s+'
    r'(E?\d+(?:\.\d+)?)\s*'
    r'(?:v\d+\s*)?'
    r'(?:[\(\[](\d+p)(?:[^\)\]]*)?[\)\]])?\s*'
    r'(?:\[(?![A-Fa-f0-9]{8,}\])[^\]]+\]\s*)*'
    r'(?:\[[A-Fa-f0-9]+\])?\s*$'
)

ORDINAL_SEASON_RE = re.compile(
    r"\b(\d+)(?:st|nd|rd|th)\s+Season\b", re.I
)


ORDINAL_SEASON_D_RE = re.compile(r"\b([1-9])d\s+Season\b", re.I)
MULTIPLE_SPACES_RE = re.compile(r"\s{2,}")


TRAILING_SEASON_MIN = 2
TRAILING_SEASON_MAX = 40
TRAILING_SEASON_DELIMITERS = " .-_"
TRAILING_SEASON_RE = re.compile(r"(\d+)\s*$")
TRAILING_SEASON_DELIMS_RE = re.compile(r"[{}]+$".format(re.escape(TRAILING_SEASON_DELIMITERS)))
_ALPHA_RE = re.compile(r"[A-Za-z]")

SEASON_WORD_RE = re.compile(r"\s+Season\s+(\d+)\s*$", re.I)


_GROUP_TLD_SUFFIXES = ("com", "org", "net", "in", "co", "io", "cc", "me", "tv")
_GROUP_TLD_SUFFIX_PATTERN = "|".join(_GROUP_TLD_SUFFIXES)
_GROUP_TLD_SUFFIX_RE = re.compile(
    r"-\s*([A-Za-z0-9]+)\s+(?:" + _GROUP_TLD_SUFFIX_PATTERN + r")\s*(?:[\]\)\}]+)?$",
    re.I,
)
_PAHE_GROUP_SUFFIX_RE = re.compile(
    r"(?i)-\s*Pahe(?:[.\s]+(?:" + _GROUP_TLD_SUFFIX_PATTERN + r"))?\s*(?:[\]\)\}]+)?$"
)
_PSA_GROUP_SUFFIX_RE = re.compile(r"(?i)-\s*PSA\s*(?:[\]\)\}]+)?$")

def _strip_ordinal_season_phrase(text, pattern):
    return MULTIPLE_SPACES_RE.sub(" ", pattern.sub("", text)).strip()

ANILIST_API_URL = "https://graphql.anilist.co"

_AUDIO_LANGUAGE_CACHE: OrderedDict[str, list[str]] = OrderedDict()
_AUDIO_LANGUAGE_CACHE_LIMIT = 256

def get_mediainfo(path):
    try:
        r = subprocess.run(
            ["mediainfo", "--Output=JSON", path],
            capture_output=True, text=True, timeout=15,
        )
        if not r.stdout:
            return None, None, None
        data = json.loads(r.stdout)
        tracks = data.get("media", {}).get("track", [])
        g = v = a = None
        audio_languages: list[str] = []
        for t in tracks:
            tt = t.get("@type")
            if tt == "General":
                g = t
            elif tt == "Video" and v is None:
                v = t
            elif tt == "Audio":
                lang = (t.get("Language") or "").strip()
                if lang:
                    audio_languages.append(lang.lower())
                if a is None:
                    a = t

        if audio_languages:
            path_str = str(path)
            deduped_langs = _dedupe_preserve_order(audio_languages)
            _AUDIO_LANGUAGE_CACHE[path_str] = deduped_langs
            _AUDIO_LANGUAGE_CACHE.move_to_end(path_str)
            if len(_AUDIO_LANGUAGE_CACHE) > _AUDIO_LANGUAGE_CACHE_LIMIT:
                _AUDIO_LANGUAGE_CACHE.popitem(last=False)

        return g, v, a
    except Exception:
        return None, None, None

def _safe_int(val, default=0):
    try:
        s = str(val).split("/")[0].strip()

        m = re.search(r"-?\d+", s)
        if m:
            return int(m.group(0))
        return default
    except (ValueError, TypeError, AttributeError):
        return default

def _safe_float(val, default=0.0):
    try:
        return float(str(val).split("/")[0].strip())
    except (ValueError, TypeError, AttributeError):
        return default

def _normalize_channel_layout(val: str) -> str:
    digits = re.findall(r"\d", str(val))
    if len(digits) >= 2:
        return f"{digits[0]}.{digits[1]}"
    if len(digits) == 1:
        return digits[0]
    return ""

def clean_name(n):
    n = re.sub(r'[._ ]+', '.', n)
    n = re.sub(r'\.+', '.', n)
    return n.strip('.')

def strip_leading_site_prefix(name: str) -> str:
    return re.sub(
        r"^\s*(?:https?://)?www\.(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\s*[-–—:|]\s*",
        "",
        name,
        count=1,
        flags=re.I,
    )

def _strip_video_extension(name: str) -> str:
    base, ext = os.path.splitext(name)
    if ext.lower() in VIDEO_EXT and base:
        return base
    return name

def _normalize_pahe_suffix(name: str) -> str:
    return _PAHE_GROUP_SUFFIX_RE.sub("-Pahe", name)

def _title_from_psa_filename(name: str) -> str | None:
    cleaned = strip_leading_site_prefix(_strip_video_extension(name)).strip()
    if not _PSA_GROUP_SUFFIX_RE.search(cleaned):
        return None
    return cleaned

def _title_from_pahe_filename(name: str) -> str | None:
    cleaned = strip_leading_site_prefix(_strip_video_extension(name)).strip()
    if not _PAHE_GROUP_SUFFIX_RE.search(cleaned):
        return None
    return _normalize_pahe_suffix(cleaned).strip()

def _extract_edition_text(after_year: str) -> str | None:
    if not after_year:
        return None
    stripped = after_year.lstrip(_EDITION_LEADING_CHARS)
    qual_match = re.search(rf'(?:^|[-.()\s]){_TAG_PAT}', stripped, re.I)
    if qual_match:
        candidate = stripped[:qual_match.start()]
    else:
        candidate = stripped
    candidate = candidate.strip(_EDITION_TRAILING_CHARS)
    if candidate and re.search(r"\w", candidate):
        return clean_name(candidate)
    return None

def _strip_parenthesized_year(text):
    return re.sub(r"\((?:19|20)\d{2}\)", "", text)

def _is_interlaced(scan_type):
    return bool(scan_type) and str(scan_type).upper() != "PROGRESSIVE"

def resolution(width, scan_type=None):
    w = _safe_int(width)
    if w == 0:
        return None
    suffix = "i" if _is_interlaced(scan_type) else "p"
    if w >= 3800:
        return f"2160{suffix}"
    if w >= 1900:
        return f"1080{suffix}"
    if w >= 1200:
        return f"720{suffix}"
    if w >= 640:
        return f"480{suffix}"
    return None

def video_codec(v):
    if not v:
        return None
    fmt = v.get("Format", "")
    if "AVC" in fmt:
        return "H.264"
    if "HEVC" in fmt:
        return "H.265"
    if "AV1" in fmt:
        return "AV1"
    if "VP9" in fmt:
        return "VP9"
    return fmt if fmt else None

def detect_hdr(v):
    if not v:
        return None

    bit_depth = _safe_int(v.get("BitDepth"))
    colour = str(v.get("colour_primaries", "") or "")
    transfer = str(v.get("transfer_characteristics", "") or "")

    hdr_fmt = str(v.get("HDR_Format", "") or "")
    hdr_compat = str(v.get("HDR_Format_Compatibility", "") or "")

    tags = []

    if "dolby vision" in hdr_fmt.lower():
        tags.append("DV")

    if "hdr10+" in hdr_fmt.lower() or "smpte st 2094" in hdr_fmt.lower():
        tags.append("HDR10+")
    elif "hdr10" in hdr_compat.lower() or "hdr10" in hdr_fmt.lower() or "smpte st 2086" in hdr_fmt.lower() or ("2020" in colour and "PQ" in transfer.upper() and bit_depth >= 10):
        if "HDR10+" not in tags:
            tags.append("HDR10")
    elif "HLG" in transfer.upper() and "2020" in colour:
        tags.append("HLG")

    return ".".join(tags) if tags else None

def audio_info(a):
    if not a:
        return None
    codec = a.get("Format")
    if not codec:
        return None

    mapping = {
        "E-AC-3": "DDP",
        "AC-3": "DD",
        "MLP FBA": "TrueHD",
        "DTS": "DTS",
        "MPEG Audio": "MPEG",
        "FLAC": "FLAC",
        "Opus": "Opus",
        "Vorbis": "Vorbis",
        "PCM": "LPCM",
    }

    normalized_codec = str(codec).strip()
    codec_upper = normalized_codec.upper()

    if codec_upper.startswith("DTS"):
        profile = str(a.get("Format_Profile", "") or "")
        commercial = str(a.get("Format_Commercial_IfAny", "") or "")
        profile_upper = profile.upper()
        commercial_upper = commercial.upper()
        if "X" in profile_upper or "DTS:X" in commercial_upper:
            codec_tag = "DTS-X"
        elif "MA" in profile_upper or ("DTS-HD" in commercial_upper and "MASTER AUDIO" in commercial_upper):
            codec_tag = "DTS-HD.MA"
        elif "HRA" in profile_upper or ("DTS-HD" in commercial_upper and "HIGH RES" in commercial_upper):
            codec_tag = "DTS-HD.HRA"
        else:
            codec_tag = "DTS"
    else:
        codec_tag = None

        for key, tag in mapping.items():
            if codec_upper.startswith(key.upper()):
                codec_tag = tag
                break
        codec_tag = codec_tag or normalized_codec

    ch_raw = str(a.get("Channels") or a.get("Channel(s)", "") or "").split("/")[0].strip()
    ch = _safe_int(ch_raw)
    if ch >= 8:
        ch_str = "7.1"
    elif ch >= 6:
        ch_str = "5.1"
    elif ch >= 2:
        ch_str = "2.0"
    elif ch == 1:
        ch_str = "1.0"
    else:
        ch_str = ""

    base_audio = f"{codec_tag}{ch_str}" if ch_str else codec_tag

    atmos_fields = [
        str(a.get("Format_Commercial_IfAny", "")),
        str(a.get("Format_Profile", "")),
        str(a.get("Format_AdditionalFeatures", "")),
    ]
    if any("atmos" in f.lower() for f in atmos_fields):
        return f"{base_audio}.Atmos"
    return base_audio

def detect_group(name):

    m = _GROUP_TLD_SUFFIX_RE.search(name)
    if m:
        return m.group(1)

    m = re.search(r"-\s*([A-Za-z0-9]+)\s*(?:[\]\)\}]+)?$", name)
    if m:
        return m.group(1)


    if not re.search(_TAG_PAT, name, re.I):
        return None
    tail = re.search(r"[.\s]\s*([A-Za-z0-9]+)\s*(?:[\]\)\}]+)?$", name)
    if not tail:
        return None
    candidate = tail.group(1)

    if re.fullmatch(_TAG_PAT, candidate, re.I):
        return None
    return candidate

def detect_source_tags_filename(name):
    webtypes = ["WEB-DL", "WEBRip", "BluRay", "REMUX", "HDTV", "WEB"]
    service = None
    webtype = None


    if re.search(r"(?<![A-Za-z0-9])MA(?=[.\s_-]+WEB(?:-DL|Rip)?)(?![A-Za-z0-9])", name, re.I):
        service = "MA"
    for s in FILENAME_SERVICES:
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(s)}(?![A-Za-z0-9])", name, re.I):
            service = s
            break
    for w in webtypes:
        if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", name, re.I):
            webtype = w
            break
    return service, webtype

def detect_source_mediainfo(g, v):
    text = str(g) + str(v)
    for pat, vtag in _SOURCE_PATTERNS:
        if pat.search(text):
            return vtag
    return None

def _collect_encoding_text(g, v):
    fields = []
    if v:
        for key in ("Encoded_Library", "Encoded_Library_Name",
                     "Encoded_Library_Settings", "Writing_Library"):
            fields.append(str(v.get(key, "") or ""))
    if g:
        for key in ("Encoded_Application", "Encoded_Library",
                     "Writing_Application", "Writing_Library"):
            fields.append(str(g.get(key, "") or ""))
    return " ".join(fields)

def _has_reencode_signature(encoding_text):
    return bool(_REENCODE_RE.search(encoding_text))

def _video_bitrate_low_for_resolution(v, res):
    if not v or not res:
        return False

    br = _safe_float(v.get("BitRate") or v.get("BitRate_Nominal"))
    if br <= 0:
        return False
    threshold = _WEBDL_MIN_BITRATE.get(res)
    if threshold and br < threshold:
        return True
    return False

def detect_web_type_mediainfo(g, v, a=None):
    service = detect_source_mediainfo(g, v)
    encoding_text = _collect_encoding_text(g, v)

    if service:

        if _has_reencode_signature(encoding_text):
            return service, "WEBRip"
        return service, "WEB-DL"


    if _has_reencode_signature(encoding_text):
        res = resolution(v.get("Width") if v else None)
        if res in ("2160p", "1080p", "720p"):
            br = _safe_float(v.get("BitRate") or v.get("BitRate_Nominal")) if v else 0
            threshold = _WEBDL_MIN_BITRATE.get(res, 0)


            if threshold > 0 and br >= threshold * 1.8:
                return None, "WEB-DL"
        return None, "WEBRip"

    res = resolution(v.get("Width") if v else None)

    if _video_bitrate_low_for_resolution(v, res):
        return None, "WEBRip"

    if v and res == "2160p":
        fmt = v.get("Format", "")
        bit_depth = _safe_int(v.get("BitDepth"))
        if "HEVC" in fmt and 0 < bit_depth < 10:
            return None, "WEBRip"


    if a and res in ("2160p", "1080p"):
        audio_fmt = a.get("Format", "")
        if audio_fmt == "AAC":
            br = _safe_float(v.get("BitRate") or v.get("BitRate_Nominal")) if v else 0
            threshold = _WEBDL_MIN_BITRATE.get(res, 0)

            if threshold > 0 and br > 0 and br < threshold * 0.8:
                return None, "WEBRip"

    if v:
        enc_lib = str(v.get("Encoded_Library", "") or "").strip()
        if not enc_lib:
            return None, "WEB-DL"

    return None, None

def detect_episode(name):

    m = re.search(r"S(\d{2})E(\d{2})[-.]?E(\d{2})", name, re.I)
    if m:
        return (m.group(1), f"{m.group(2)}-E{m.group(3)}")

    m = re.search(r"S(\d{2})E(\d{2})", name, re.I)
    if m:
        return (m.group(1), m.group(2))

    m = re.search(r"(?<!\d)(\d{1,2})x(\d{2,3})(?!\d)", name, re.I)
    if m:
        return (m.group(1).zfill(2), m.group(2))
    return (None, None)

def episode_title(show, season, episode):
    query = show.replace(".", " ").replace("_", " ").strip()
    fallback_query = re.sub(r"\b(?:19|20)\d{2}\b$", "", query).strip()
    queries = [query]
    if fallback_query and fallback_query != query:
        queries.append(fallback_query)

    try:
        s_int = int(season)
        e_int = int(episode)
    except Exception:
        return None

    def _is_placeholder(name: str, target_episode: int = e_int) -> bool:
        if not name:
            return False
        normalized = name.strip().lower()
        if normalized in ("not found", "notfound", "404 not found", "episode not found"):
            return True
        pattern = rf"(?:ep(?:isode)?|e)[\s._-]*0*{target_episode}\s*$"
        return re.fullmatch(pattern, name.strip(), re.I) is not None

    for q in queries:
        try:
            r = requests.get(
                f"https://api.tvmaze.com/singlesearch/shows?q={q}", timeout=5,
            )
            sid = r.json()["id"]
            r = requests.get(
                f"https://api.tvmaze.com/shows/{sid}/episodebynumber"
                f"?season={s_int}&number={e_int}",
                timeout=5,
            )
            raw_name = r.json()["name"]
            if _is_placeholder(str(raw_name)) or _is_placeholder(clean_name(str(raw_name))):
                continue
            return clean_name(str(raw_name))
        except Exception:
            continue
    return None


@functools.lru_cache(maxsize=64)
def _anilist_search(title):
    query = """
    query ($search: String) {
        Page(page: 1, perPage: 10) {
            media(search: $search, type: ANIME, format: TV, sort: START_DATE) {
                id
                title { romaji english native }
                episodes
                startDate { year }
            }
        }
    }
    """
    try:
        r = requests.post(
            ANILIST_API_URL,
            json={"query": query, "variables": {"search": title}},
            timeout=10,
        )
        results = r.json().get("data", {}).get("Page", {}).get("media", []) or []
        return tuple(results)
    except Exception:
        return tuple()


def _matching_anilist_results(title, results):
    title_lower = title.lower().strip()
    matching = []
    for item in results:
        romaji = (item.get("title", {}).get("romaji") or "").lower()
        english = (item.get("title", {}).get("english") or "").lower()
        if title_lower in romaji or romaji in title_lower or\
           title_lower in english or english in title_lower:
            matching.append(item)

    if not matching:
        matching = list(results[:1])

    matching.sort(
        key=lambda x: x.get("startDate", {}).get("year") or UNKNOWN_YEAR_FALLBACK,
    )
    return matching


def anime_english_title(title):
    try:
        results = _anilist_search(title)
        if not results:
            return None
        matching = _matching_anilist_results(title, results)
        if not matching:
            return None
        primary = matching[0]
        titles = primary.get("title", {}) or {}
        english = (titles.get("english") or "").strip()
        romaji = (titles.get("romaji") or "").strip()
        native = (titles.get("native") or "").strip()
        if not english:
            return None
        lowered = english.lower()
        if lowered == romaji.lower() or lowered == native.lower():
            return None
        return english
    except Exception:
        return None

def _normalize_video_codec(video, web_tag):
    if not video or not web_tag:
        return video
    upper = video.upper().replace(".", "")
    is_avc = upper in ("H264", "X264", "AVC")
    is_hevc = upper in ("H265", "X265", "HEVC")
    if web_tag == "WEBRip":
        if is_avc:
            return "x264"
        if is_hevc:
            return "x265"
    elif web_tag == "WEB-DL":
        if is_avc:
            return "H.264"
        if is_hevc:
            return "H.265"
    return video

def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result

def _collect_audio_languages(path: str | None, *sources) -> list[str]:
    langs: list[str] = []
    if path:
        cached = _AUDIO_LANGUAGE_CACHE.get(path)
        if cached:
            _AUDIO_LANGUAGE_CACHE.move_to_end(path, last=True)
            langs.extend(cached)
    for src in sources:
        if not isinstance(src, dict):
            continue
        raw = src.get("_audio_languages")
        candidates: list[str] = []
        if isinstance(raw, str):
            candidates = [raw]
        elif isinstance(raw, (list, tuple)):
            candidates = list(raw)


        elif isinstance(src.get("Audio_Language_List"), str):
            candidates = str(src["Audio_Language_List"]).split(" / ")

        for lang in candidates:
            norm = (lang or "").strip().lower()
            if norm:
                langs.append(norm)
    return _dedupe_preserve_order(langs)

def _has_multiple_audio_languages(audio_languages: list[str]) -> bool:
    return len(audio_languages) > 1

def build_name(path, is_season_pack=False):
    name = os.path.basename(path)
    base, ext = os.path.splitext(name)

    ext_is_video = ext.lower() in VIDEO_EXT
    if not ext_is_video:


        base, ext = name, ""
    base = strip_leading_site_prefix(base)
    g, v, a = get_mediainfo(path)

    group = detect_group(base)
    service_tag, web_tag = detect_source_tags_filename(base)

    video_match = re.search(r"[xX]264|[xX]265|H\.?264|H\.?265|HEVC|AVC|AV1|VP9", base, re.I)
    video = video_match.group(0) if video_match else None
    video_from_filename = bool(video_match)

    audio_match = re.search(
        rf"(?<![A-Za-z0-9])(?:"
        rf"(?:DD\+|DDP|DD|TrueHD|DTS(?:-(?:HD\.MA|HD\.HRA|X))?)"
        rf"(?:{_OPTIONAL_CHANNEL_LAYOUT_PAT}(?:\.Atmos)?|(?:\.Atmos){_OPTIONAL_CHANNEL_LAYOUT_PAT})?"
        r"|AAC\d\.\d|FLAC|Opus|Atmos)(?![A-Za-z0-9])",
        base,
        re.I,
    )
    if audio_match:
        audio = audio_match.group(0)
        tail = base[audio_match.end():]
        if re.search(
            r"(?i)(?:DD\+|DDP|DD|TrueHD|DTS(?:-(?:HD\.MA|HD\.HRA|X))?)\.Atmos$",
            audio,
        ):
            tail_layout = re.match(rf"^[.\s]*({_CHANNEL_LAYOUT_PAT})\b", tail)
            if tail_layout:
                layout = _normalize_channel_layout(tail_layout.group(1))
                if layout:
                    audio = f"{audio}.{layout}"

        if re.match(r"(?i)^DDP\.", audio):
            audio = re.sub(r"(?i)^DDP\.\s*", "DDP", audio)
        elif re.match(r"(?i)^DD\+", audio):
            audio = re.sub(r"(?i)^DD\+\s*", "DDP", audio)
        audio = re.sub(r"(?i)(DDP)[\s.]+(?=\d)", r"\1", audio)


        atmos_layout = re.search(rf"(?i)\.Atmos[.\s]*({_CHANNEL_LAYOUT_PAT})$", audio)
        if atmos_layout:
            layout = _normalize_channel_layout(atmos_layout.group(1))
            audio = re.sub(rf"(?i)\.Atmos[.\s]*{_CHANNEL_LAYOUT_PAT}$", "", audio)
            if layout:
                audio = f"{audio}{layout}.Atmos"
    else:
        audio = None

    res_match = re.search(r"2160[pi]?|1080[pi]?|720[pi]?|480[pi]?", base, re.I)
    res = res_match.group(0) if res_match else None


    if v:
        _scan_type = str(v.get("ScanType", "") or "").strip()
        if _is_interlaced(_scan_type):

            _mi_res = resolution(v.get("Width"), _scan_type)
            if _mi_res:
                res = _mi_res
            elif res:

                res = re.sub(r"[pi]$", "i", res, flags=re.I)

    hdr_match = re.search(
        r"(?:DV\.HDR10\+|DV\.HDR10|DV\.HLG|DV|HDR10\+|HDR10|HDR|HLG)", base, re.I,
    )
    hdr = hdr_match.group(0) if hdr_match else None


    bit_depth_match = re.search(r'(?<![A-Za-z\d])(8|10|12)bit(?!\w)', base, re.I)
    bit_depth_tag = f"{bit_depth_match.group(1)}bit" if bit_depth_match else None


    hevc_format_match = re.search(r'(?<![A-Za-z0-9])HEVC(?![A-Za-z0-9])', base, re.I)


    meta_tags = []
    for tag in ["REPACK", "PROPER", "INTERNAL", "READNFO"]:
        if re.search(rf"(?<![A-Za-z]){tag}(?![A-Za-z])", base, re.I):
            meta_tags.append(tag)
    has_remux = bool(re.search(r"(?<![A-Za-z])REMUX(?![A-Za-z])", base, re.I))

    season, episode_num = detect_episode(base)

    _ep_lookup = episode_num.split("-")[0] if episode_num and "-" in episode_num else episode_num
    parts = []

    if season:

        show = re.split(r"S\d{2}E\d{2}", base, flags=re.I)[0]
        show = _strip_parenthesized_year(show)

        show = re.sub(r"(?<![A-Za-z0-9])(19|20)\d{2}(?![A-Za-z0-9])", "", show)
        show = clean_name(show)

        ep_match = re.search(rf"S{season}E{_ep_lookup}", base, re.I)
        existing_title = None
        if ep_match:
            after_ep = re.sub(r'^[.\s-]+', '', base[ep_match.end():])

            after_ep = re.sub(r'^-E\d{2}', '', after_ep, flags=re.I)
            after_ep = re.sub(r'^[.\s-]+', '', after_ep)
            qual_match = re.search(rf'(?:^|[.\-]){_TAG_PAT}', after_ep, re.I)
            if qual_match:
                raw = after_ep[:qual_match.start()].strip('.-')
            else:
                raw = re.sub(r'-[A-Za-z0-9]+$', '', after_ep).strip('.-')
            if raw:
                if group and raw == group:
                    pass
                else:
                    existing_title = clean_name(raw)

        parts.append(show)
        if is_season_pack:
            parts.append(f"S{season}")
        else:
            parts.append(f"S{season}E{episode_num}")
            title = episode_title(show, season, _ep_lookup)
            if title:
                parts.append(title)
            elif existing_title:
                parts.append(existing_title)
    else:

        year_match = re.search(r"(19|20)\d{2}", base)
        extra_title = None
        if year_match:
            mov = base[:year_match.start()].rstrip(" ._-(")
            after_year = base[year_match.end():]
            extra_title = _extract_edition_text(after_year)
        else:
            qual_match = re.search(rf'(?:^|[.\-]){_TAG_PAT}', base, re.I)
            if qual_match:
                mov = base[:qual_match.start()].strip('.')
            else:
                mov = re.sub(r'-[A-Za-z0-9]+$', '', base).strip('.-')

            extra_title = None
        parts.append(clean_name(mov))
        if year_match:
            parts.append(year_match.group())
        if extra_title:
            parts.append(extra_title)

    if not res:
        _scan_type = str((v or {}).get("ScanType", "") or "").strip()
        res = resolution(v.get("Width") if v else None, _scan_type or None)
    if res:
        parts.append(res)
    if bit_depth_tag:
        parts.append(bit_depth_tag)


    for meta_tag in meta_tags:
        parts.append(meta_tag)

    effective_web = None


    explicit_non_web_source = web_tag in ("BluRay", "REMUX", "HDTV")
    if service_tag or web_tag:
        if explicit_non_web_source:
            if service_tag:
                parts.append(service_tag)
            parts.append(web_tag)
            effective_web = web_tag
        else:
            detected_service, detected_webtype = detect_web_type_mediainfo(g, v, a)
            effective_service = service_tag or detected_service

            if web_tag == "WEB":
                effective_web = "WEB-DL"
            elif web_tag in ("WEB-DL", "WEBRip"):
                effective_web = web_tag
            elif detected_webtype:
                effective_web = detected_webtype
            else:

                effective_web = web_tag
            if effective_service:
                parts.append(effective_service)
            if effective_web:
                parts.append(effective_web)
    else:
        detected_service, detected_webtype = detect_web_type_mediainfo(g, v, a)
        if detected_service:
            parts.append(detected_service)
        if detected_webtype:
            parts.append(detected_webtype)
        effective_web = detected_webtype


    x265_in_filename = bool(re.search(r'(?<![A-Za-z0-9])x265(?![A-Za-z0-9])', base, re.I))
    hevc_and_x265_in_filename = bool(hevc_format_match) and x265_in_filename
    if hevc_and_x265_in_filename and effective_web != "WEBRip" and not explicit_non_web_source:
        if "WEB-DL" in parts:
            parts[parts.index("WEB-DL")] = "WEBRip"
        elif effective_web is None:
            parts.append("WEBRip")
        effective_web = "WEBRip"


    if has_remux and "REMUX" not in parts:
        parts.append("REMUX")

    encoding_text = _collect_encoding_text(g, v)


    if not video:
        video = video_codec(v)

    if video_from_filename and video:
        upper_video = video.upper().replace(".", "")
        if upper_video == "H264":
            video = "H.264"
        elif upper_video == "H265":
            video = "H.265"

        elif upper_video in ("HEVC",) and effective_web == "WEBRip":
            video = "x265"
    else:
        lower_encoding = encoding_text.lower() if encoding_text else ""
        if re.search(r"\bx265\b", lower_encoding):
            video = "x265"
        elif re.search(r"\bx264\b", lower_encoding):
            video = "x264"
        else:
            video = _normalize_video_codec(video, effective_web)

    if not audio:
        audio = audio_info(a)
    if not audio:

        ch_bare_match = re.search(r'(?<![A-Za-z\d])(\d+(?:\.\d)?CH)(?!\w)', base, re.I)
        if ch_bare_match:
            audio = ch_bare_match.group(0).upper()

    audio_languages = _collect_audio_languages(str(path), g, a)
    if _has_multiple_audio_languages(audio_languages):
        parts.append("DUAL")

    if audio:
        parts.append(audio)
    hdr_from_mediainfo = detect_hdr(v)
    if hdr_from_mediainfo:
        hdr = hdr_from_mediainfo
    if hdr:
        parts.append(hdr)
    if video:
        parts.append(video)


    if hevc_format_match and video and video.upper() in ("X265", "X264"):
        is_webrip_standard_res = effective_web == "WEBRip" and res in ("1080p", "720p")
        if not is_webrip_standard_res:
            parts.append("HEVC")

    final = ".".join(p for p in parts if p)
    final = clean_name(final)
    if group:
        final += "-" + group

    return final + ext


def _reorder_audio_hdr_tokens(core: str) -> str:

    core = re.sub(
        rf"(?i)(^|\.)(?P<hdr>{_HDR_BLOCK_PATTERN})\.(?P<audio>{_AUDIO_BLOCK_PATTERN})(?=\.|$)",
        lambda m: f"{m.group(1)}{m.group('audio')}.{m.group('hdr')}",
        core,
    )

    core = re.sub(
        r'(?i)\b(x264|x265|H\.264|H\.265|AVC|HEVC)\.(DDP?\+?\d(?:\.\d)?)\b',
        r'\2.\1',
        core,
    )

    raw_parts = core.split(".")
    rebuilt_tokens = []
    i = 0
    while i < len(raw_parts):
        part = raw_parts[i]
        if i + 1 < len(raw_parts):
            two_token_candidate = f"{part}.{raw_parts[i+1]}"
            three_token_candidate = None
            if i + 2 < len(raw_parts):
                three_token_candidate = f"{two_token_candidate}.{raw_parts[i+2]}"
            if _AUDIO_FULL_RE.fullmatch(two_token_candidate):
                rebuilt_tokens.append(two_token_candidate)
                i += 2
                continue
            if three_token_candidate and _AUDIO_FULL_RE.fullmatch(three_token_candidate):
                rebuilt_tokens.append(three_token_candidate)
                i += 3
                continue
            if _VIDEO_FULL_RE.fullmatch(two_token_candidate):
                rebuilt_tokens.append(two_token_candidate)
                i += 2
                continue
        rebuilt_tokens.append(part)
        i += 1

    audio_tokens: list[str] = []
    hdr_tokens: list[str] = []
    video_tokens: list[str] = []
    prefix_tokens: list[str] = []

    for tok in rebuilt_tokens:
        if _AUDIO_FULL_RE.fullmatch(tok):
            audio_tokens.append(tok)
        elif _HDR_FULL_RE.fullmatch(tok):
            hdr_tokens.append(tok)
        elif _VIDEO_FULL_RE.fullmatch(tok):
            video_tokens.append(tok)
        else:
            prefix_tokens.append(tok)

    result_tokens = rebuilt_tokens
    if audio_tokens and hdr_tokens:
        result_tokens = prefix_tokens + audio_tokens + hdr_tokens + video_tokens
    return ".".join(result_tokens)


def build_title(new_name):
    _, ext = os.path.splitext(new_name)
    base = new_name[:-len(ext)] if ext.lower() in VIDEO_EXT else new_name

    head, sep, group = base.rpartition("-")
    core = head if sep else base
    group_suffix = f"-{group}" if sep else ""
    core = _reorder_audio_hdr_tokens(core)
    base = core + group_suffix

    if re.search(r"S\d{2}E\d{2}", base, re.I):
        base = _strip_parenthesized_year(base)

        base = re.sub(r"(?<![A-Za-z0-9])(19|20)\d{2}(?=[.\s_-]*S\d{2}E\d{2})", "", base, flags=re.I)
        base = re.sub(r"\.{2,}", ".", base).strip(".")
    elif re.search(r"S\d{2}(?!E\d{2})", base, re.I):
        base = _strip_parenthesized_year(base)

        base = re.sub(r"(?<![A-Za-z0-9])(19|20)\d{2}(?=[.\s_-]*S\d{2}(?!E\d{2}))", "", base, flags=re.I)
        base = re.sub(r"\.{2,}", ".", base).strip(".")

    base = base.replace('.', ' ')
    return base

def detect_fansub(name):
    base = os.path.splitext(name)[0]
    m = _FANSUB_RE.match(base)
    if not m:
        return None
    raw_ep = m.group(3).strip()
    is_e_prefixed = raw_ep.upper().startswith("E")
    ep_num = int(float(raw_ep.lstrip("Ee")))
    return {
        "group": m.group(1).strip(),
        "title": m.group(2).strip(),
        "episode": ep_num,
        "resolution": m.group(4),


        "is_e_prefixed": is_e_prefixed,
        "ep_str": raw_ep if is_e_prefixed else None,
    }

def anime_season_episode(title, absolute_episode):
    try:
        results = _anilist_search(title)
        if not results:
            return 1, absolute_episode

        matching = _matching_anilist_results(title, results)

        ep = absolute_episode
        for i, season in enumerate(matching, 1):
            season_eps = season.get("episodes") or 0
            if season_eps <= 0:
                continue
            if ep <= season_eps:
                return i, ep
            ep -= season_eps

        return len(matching), ep
    except Exception:
        return 1, absolute_episode

def _build_fansub_title(path, fansub_info, is_pack=False):
    group = fansub_info["group"]
    title = fansub_info["title"]
    absolute_ep = fansub_info["episode"]
    fn_res = fansub_info["resolution"]
    is_e_prefixed = fansub_info.get("is_e_prefixed", False)
    ep_str = fansub_info.get("ep_str") or ""

    title_for_api = title
    season_suffix = None
    season_match = re.search(r"\s+S(\d+)\s*$", title, re.I)
    if season_match:
        season_suffix = int(season_match.group(1))
        title_for_api = title[:season_match.start()].strip()

    if season_suffix is None:
        matched_pattern = None
        for pattern in (ORDINAL_SEASON_RE, ORDINAL_SEASON_D_RE):
            match = pattern.search(title_for_api)
            if match:
                season_suffix = int(match.group(1))
                matched_pattern = pattern
                break

        if matched_pattern:
            title_for_api = _strip_ordinal_season_phrase(title_for_api, matched_pattern)

    if season_suffix is None:
        match = SEASON_WORD_RE.search(title_for_api)
        if match:
            season_suffix = int(match.group(1))
            title_for_api = title_for_api[:match.start()].strip()

    if season_suffix is None:
        match = TRAILING_SEASON_RE.search(title_for_api)
        if match:
            raw_prefix = title_for_api[:match.start()]
            prefix = TRAILING_SEASON_DELIMS_RE.sub("", raw_prefix)
            prefix_contains_alpha = bool(prefix and _ALPHA_RE.search(prefix))
            if prefix_contains_alpha:
                value = int(match.group(1))

                if TRAILING_SEASON_MIN <= value <= TRAILING_SEASON_MAX:
                    season_suffix = value
                    title_for_api = prefix


    use_e_prefix_format = is_e_prefixed and season_suffix is None

    if not use_e_prefix_format:
        season, episode = anime_season_episode(title_for_api, absolute_ep)
        if season_suffix is not None:
            season = season_suffix
        season = season or 1
        episode = episode or absolute_ep
    else:
        season = 1
        episode = absolute_ep

    english_title = anime_english_title(title_for_api)
    if english_title and english_title.lower() != title_for_api.lower():
        title_display = f"{title_for_api} aka {english_title}"
    else:
        title_display = title_for_api

    g, v, a = get_mediainfo(path)

    res = fn_res or resolution(
        v.get("Width") if v else None,
        str((v or {}).get("ScanType", "") or "").strip() or None,
    )


    filename = os.path.basename(path)
    service, webtype_fn = detect_source_tags_filename(filename)

    _, web_type = detect_web_type_mediainfo(g, v, a)

    if webtype_fn and webtype_fn != "WEB":
        web_tag = webtype_fn
    else:
        web_tag = web_type or "WEB-DL"

    hdr = detect_hdr(v)

    ai = audio_info(a)
    audio_languages = _collect_audio_languages(str(path), g, a)

    vc = video_codec(v)


    encoding_text = _collect_encoding_text(g, v)
    has_reencode = _has_reencode_signature(encoding_text)
    if has_reencode and vc:

        upper = vc.upper().replace(".", "")
        if upper in ("H264", "X264", "AVC"):
            vc = "x264"
        elif upper in ("H265", "X265", "HEVC"):
            vc = "x265"
    else:
        vc = _normalize_video_codec(vc, web_tag)

    if use_e_prefix_format:

        ep_label = ep_str.upper()
        ep_title = episode_title(title_for_api, "1", f"{absolute_ep:02d}") if not is_pack else None
        if is_pack:
            parts = [title_display]
        else:
            parts = [title_display, ep_label]
    else:
        ep_title = episode_title(title_for_api, f"{season:02d}", f"{episode:02d}") if not is_pack else None
        if is_pack:
            parts = [title_display, f"S{season:02d}"]
        else:
            parts = [title_display, f"S{season:02d}E{episode:02d}"]

    if ep_title:
        parts.append(ep_title)
    if res:
        parts.append(res)

    if service:
        parts.append(service)
    parts.append(web_tag)
    if hdr:
        parts.append(hdr)
    if _has_multiple_audio_languages(audio_languages):
        parts.append("DUAL")
    if ai:

        audio_fmt = re.sub(r'^(\D+?)(\d)', r'\1 \2', ai)
        parts.append(audio_fmt)
    if vc:
        parts.append(vc)

    result = " ".join(p for p in parts if p)
    result += f" - {group}"
    return result

def generate_title(path, is_pack=False, is_season_pack=False):
    name = os.path.basename(path)

    psa_title = _title_from_psa_filename(name)
    if psa_title:
        return psa_title

    pahe_title = _title_from_pahe_filename(name)
    if pahe_title:
        return pahe_title

    fansub = detect_fansub(name)
    if fansub:
        return _build_fansub_title(path, fansub, is_pack=is_pack)

    new_name = build_name(path, is_season_pack=is_season_pack)
    return build_title(new_name)
