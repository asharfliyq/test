from __future__ import annotations

import os
import re
import json
import subprocess
import functools
from collections import OrderedDict
import requests

# Lowercase, dot-prefixed video extensions expected from os.path.splitext().
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
    "AppleTV" : "ATV" ,
}

FILENAME_SERVICES = [
    "AMZN", "NF", "VIKI", "AHA", "SNXT", "KCW", "DSNP", "ATV" , "JHS" , "WTV", "HULU", "ATVP", "HMAX", "PCOK", "PMTP",
    "STAN", "CRAV", "MUBI", "CC", "CR", "FUNI", "HTSR", "HS", "iP", "ALL4", "iT", "BBC",
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

# Optional channel layout suffix used in filename audio tokens (e.g., 5.1, .5.1, or 5 1)
# Allows an optional separator before the first digit so bare codec tags still match.
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

# Allow optional extra bracketed tags (e.g., [MultiSub]) after resolution but leave CRC-like hashes (8+ hex) for the trailing optional group
_FANSUB_RE = re.compile(
    r'\[([^\]]+)\]\s*'
    r'(.+?)\s+-\s+'
    r'(\d+(?:\.\d+)?)\s*'
    r'(?:v\d+\s*)?'
    r'(?:[\(\[](\d+p)(?:[^\)\]]*)?[\)\]])?\s*'
    r'(?:\[(?![A-Fa-f0-9]{8,}\])[^\]]+\]\s*)*'
    r'(?:\[[A-Fa-f0-9]+\])?\s*$'
)

ORDINAL_SEASON_RE = re.compile(
    r"\b(\d+)(?:st|nd|rd|th)\s+Season\b", re.I
)
# Common typo variant seen as "3d Season" (single-digit only, e.g., 2d/3d/4d)
# Intentional single-digit limit to avoid false positives like "23d Season"
ORDINAL_SEASON_D_RE = re.compile(r"\b([1-9])d\s+Season\b", re.I)
MULTIPLE_SPACES_RE = re.compile(r"\s{2,}")

# Trailing numeric season suffix bounds for fansub titles (e.g., "Yami Shibai 16 - 12" where 16 is the season).
# Season 1 is the implicit default when no suffix is present; the upper bound helps avoid
# misclassifying truncated years or other numeric tokens as season indicators.
TRAILING_SEASON_MIN = 2
TRAILING_SEASON_MAX = 40
TRAILING_SEASON_DELIMITERS = " .-_"
TRAILING_SEASON_RE = re.compile(r"(\d+)\s*$")
TRAILING_SEASON_DELIMS_RE = re.compile(r"[{}]+$".format(re.escape(TRAILING_SEASON_DELIMITERS)))
_ALPHA_RE = re.compile(r"[A-Za-z]")
# Matches explicit "Season N" suffix (e.g., "Dorohedoro Season 2")
SEASON_WORD_RE = re.compile(r"\s+Season\s+(\d+)\s*$", re.I)
# Common top-level-domain tokens seen as trailing artifacts in scene groups
# after site watermarks are stripped (e.g., "-Pahe in" from "pahe.in").
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
    """Remove matched ordinal season phrase and collapse extra spaces."""
    return MULTIPLE_SPACES_RE.sub(" ", pattern.sub("", text)).strip()

ANILIST_API_URL = "https://graphql.anilist.co"
# Small LRU cache for per-path audio language detection to avoid mutating MediaInfo dicts.
_AUDIO_LANGUAGE_CACHE: OrderedDict[str, list[str]] = OrderedDict()
_AUDIO_LANGUAGE_CACHE_LIMIT = 256

def get_mediainfo(path):
    """Run ``mediainfo --Output=JSON`` and return (general, video, audio) dicts."""
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
    """Convert *val* to int, returning *default* on failure."""
    try:
        s = str(val).split("/")[0].strip()
        # MediaInfo often reports values like "6 channels" or "5.1ch"; grab the first integer part.
        m = re.search(r"-?\d+", s)
        if m:
            return int(m.group(0))
        return default
    except (ValueError, TypeError, AttributeError):
        return default

def _safe_float(val, default=0.0):
    """Convert *val* to float, returning *default* on failure."""
    try:
        return float(str(val).split("/")[0].strip())
    except (ValueError, TypeError, AttributeError):
        return default

def _normalize_channel_layout(val: str) -> str:
    """Normalize channel layout fragments like ``7 1``/``7.1`` to ``7.1``."""
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

def _strip_leading_site_prefix(name: str) -> str:
    """Remove leading site watermark prefixes like ``www.example.org - ``.

    Separator support is intentionally conservative and targets common upload
    watermark formats (`-`, `:`, `|`, and Unicode dash variants).
    """
    return re.sub(
        r"^\s*(?:https?://)?www\.(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\s*[-–—:|]\s*",
        "",
        name,
        count=1,
        flags=re.I,
    )

def _strip_video_extension(name: str) -> str:
    """Remove known video extension from *name*."""
    base, ext = os.path.splitext(name)
    if ext.lower() in VIDEO_EXT and base:
        return base
    return name

def _normalize_pahe_suffix(name: str) -> str:
    """Normalize Pahe group suffix variants like ``-Pahe.in``/``-Pahe in`` to ``-Pahe``."""
    return _PAHE_GROUP_SUFFIX_RE.sub("-Pahe", name)

def _title_from_psa_filename(name: str) -> str | None:
    """Return direct title for PSA filenames after basic cleanup, or ``None``."""
    cleaned = _strip_leading_site_prefix(_strip_video_extension(name)).strip()
    if not _PSA_GROUP_SUFFIX_RE.search(cleaned):
        return None
    return cleaned

def _title_from_pahe_filename(name: str) -> str | None:
    """Return direct title for Pahe filenames after basic cleanup, or ``None``."""
    cleaned = _strip_leading_site_prefix(_strip_video_extension(name)).strip()
    if not _PAHE_GROUP_SUFFIX_RE.search(cleaned):
        return None
    return _normalize_pahe_suffix(cleaned).strip()

def _extract_edition_text(after_year: str) -> str | None:
    """Extract edition/variant text that appears after the year but before tags.

    Args:
        after_year: The substring following the detected year in the filename.
                    May include delimiters and technical tags.
    Returns:
        A cleaned edition string with dot separators (e.g., ``Open.Matte``),
        as produced by ``clean_name()``, or ``None`` if no edition-like text
        is present.
    """
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
    """Return True when *scan_type* indicates interlaced content (MBAFF, Interlaced, etc.)."""
    return bool(scan_type) and str(scan_type).upper() != "PROGRESSIVE"

def resolution(width, scan_type=None):
    """Return a resolution tag like ``1080p`` or ``1080i``.

    *scan_type* accepts the ``ScanType`` value from a MediaInfo video track
    (e.g. ``"Progressive"``, ``"Interlaced"``, ``"MBAFF"``).  Any non-progressive
    value (MBAFF or Interlaced) causes the suffix to be ``i`` instead of ``p``.
    """
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
    """Return an HDR tag string or None.

    Uses bit depth, colour primaries, transfer characteristics and
    Dolby Vision metadata to determine the dynamic-range format.
    """
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
    """Build scene-style audio tag from mediainfo audio track dict.

    Uses codec format, channels, Atmos metadata, bit depth and bit rate
    to produce tags like ``DDP5.1.Atmos``, ``AAC2.0``, ``TrueHD.Atmos``.
    """
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
        # Prefix match to catch variants like "E-AC-3 JOC" while preserving mapping precedence order.
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
    # Common site watermark suffix leak: "...-Pahe in" (from pahe.in style prefixes).
    m = _GROUP_TLD_SUFFIX_RE.search(name)
    if m:
        return m.group(1)

    m = re.search(r"-\s*([A-Za-z0-9]+)\s*(?:[\]\)\}]+)?$", name)
    if m:
        return m.group(1)

    # Fallback: some releases omit the hyphen and use a trailing token
    # separated by space or dot (e.g., "... WEB-DL x265 BONE"). Require a
    # preceding quality/codec tag to avoid treating ordinary words as groups.
    if not re.search(_TAG_PAT, name, re.I):
        return None
    tail = re.search(r"[.\s]\s*([A-Za-z0-9]+)\s*(?:[\]\)\}]+)?$", name)
    if not tail:
        return None
    candidate = tail.group(1)
    # Avoid mistaking quality/codec tags for a group.
    if re.fullmatch(_TAG_PAT, candidate, re.I):
        return None
    return candidate

def detect_source_tags_filename(name):
    """Returns (service_tag, web_tag) found in the filename.

    A standalone ``WEB`` token is kept as ``WEB`` so the final WEB type
    (``WEB-DL`` vs ``WEBRip``) can be resolved from MediaInfo metadata.
    """
    webtypes = ["WEB-DL", "WEBRip", "BluRay", "REMUX", "HDTV", "WEB"]
    service = None
    webtype = None
    # MA is a special service token and should only be treated as service when
    # it appears as a standalone token immediately before a WEB source marker,
    # avoiding false positives from audio tags like DTS-HD.MA.
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
    """Search General+Video metadata text for streaming service identifiers."""
    text = str(g) + str(v)
    for pat, vtag in _SOURCE_PATTERNS:
        if pat.search(text):
            return vtag
    return None

def _collect_encoding_text(g, v):
    """Gather all encoding-related metadata fields into a single string."""
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
    """Return True if encoding text contains known re-encode tool names."""
    return bool(_REENCODE_RE.search(encoding_text))

def _video_bitrate_low_for_resolution(v, res):
    """Return True if the video bit rate is suspiciously low for its resolution,
    which is a strong indicator of a WEBRip (re-encode)."""
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
    """Detect web source type (WEBRip or WEB-DL) from mediainfo metadata.

    Uses multiple signals:
    1. Streaming-service identifier in metadata → WEB-DL
    2. Re-encode tool signatures (x264, x265, HandBrake, FFmpeg …) → WEBRip
    3. Video bit rate compared to resolution-based thresholds → WEBRip hint
    4. Video bit depth: 8-bit HEVC at high resolution is unusual for WEB-DL
    5. Audio codec: AAC audio on content that should be DDP hints at WEBRip
       (services deliver DDP 5.1; re-encoders often transcode to AAC)

    Returns a ``(service_tag, web_tag)`` tuple where either may be ``None``.
    """
    service = detect_source_mediainfo(g, v)
    encoding_text = _collect_encoding_text(g, v)

    if service:

        if _has_reencode_signature(encoding_text):
            return service, "WEBRip"
        return service, "WEB-DL"

    # Check for re-encode signatures, but allow high-bitrate x264/x265 content
    # to be classified as WEB-DL (e.g., anime from CR/Funi via SubsPlease)
    if _has_reencode_signature(encoding_text):
        res = resolution(v.get("Width") if v else None)
        if res in ("2160p", "1080p", "720p"):
            br = _safe_float(v.get("BitRate") or v.get("BitRate_Nominal")) if v else 0
            threshold = _WEBDL_MIN_BITRATE.get(res, 0)
            # If bitrate significantly exceeds WEB-DL threshold (1.8x), classify as WEB-DL
            # despite x264/x265 signature (streaming services use these encoders).
            # Requiring 1.8x threshold prevents low-bitrate re-encodes from being classified as WEB-DL.
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

    # AAC audio can indicate WEBRip (services typically use DDP), but only if
    # the video bitrate is also suspiciously low. High-bitrate AAC content
    # (e.g., anime from CR/Funi/SubsPlease) is often legitimate WEB-DL.
    if a and res in ("2160p", "1080p"):
        audio_fmt = a.get("Format", "")
        if audio_fmt == "AAC":
            br = _safe_float(v.get("BitRate") or v.get("BitRate_Nominal")) if v else 0
            threshold = _WEBDL_MIN_BITRATE.get(res, 0)
            # Only flag as WEBRip if bitrate is significantly below WEB-DL threshold
            if threshold > 0 and br > 0 and br < threshold * 0.8:
                return None, "WEBRip"

    if v:
        enc_lib = str(v.get("Encoded_Library", "") or "").strip()
        if not enc_lib:
            return None, "WEB-DL"

    return None, None

def detect_episode(name):
    # Multi-episode: S01E01-E02 or S01E01.E02 or S01E01E02
    m = re.search(r"S(\d{2})E(\d{2})[-.]?E(\d{2})", name, re.I)
    if m:
        return (m.group(1), f"{m.group(2)}-E{m.group(3)}")
    # Standard single episode: S01E01
    m = re.search(r"S(\d{2})E(\d{2})", name, re.I)
    if m:
        return (m.group(1), m.group(2))
    # Alternate NxNN format (e.g., 1x01) – season limited to 1-2 digits to avoid matching resolutions
    m = re.search(r"(?<!\d)(\d{1,2})x(\d{2,3})(?!\d)", name, re.I)
    if m:
        return (m.group(1).zfill(2), m.group(2))
    return (None, None)

def episode_title(show, season, episode):
    """Fetch episode title from TVMaze API.

    Dots / underscores in *show* are replaced with spaces so the search
    query is cleaner (e.g. ``The.Rookie`` → ``The Rookie``).  Season and
    episode numbers are cast to int so leading zeros don't confuse the API.
    """
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
    """Return English title from AniList when available and different."""
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
    """Adjust video codec notation based on web type.

    WEBRip (re-encode) uses encoder names: x264 / x265
    WEB-DL (direct download) uses format names: H.264 / H.265
    """
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
    """Return items with duplicates removed while preserving order."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result

def _collect_audio_languages(path: str | None, *sources) -> list[str]:
    """Collect audio languages (lowercased) from mediainfo tracks before deduplicating and returning.

    Args:
        path: Optional file path string used to look up cached language lists.
        *sources: MediaInfo track dictionaries that may contain language metadata.

    Returns:
        A deduplicated list of lowercased language codes, preserving the order of appearance.
    """
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
        # Audio_Language_List is present in some MediaInfo general tracks; keep for
        # backward compatibility when the cache is unavailable (e.g., mocked data).
        elif isinstance(src.get("Audio_Language_List"), str):
            candidates = str(src["Audio_Language_List"]).split(" / ")

        for lang in candidates:
            norm = (lang or "").strip().lower()
            if norm:
                langs.append(norm)
    return _dedupe_preserve_order(langs)

def _has_multiple_audio_languages(audio_languages: list[str]) -> bool:
    """Return True when multiple distinct audio languages are present.

    Args:
        audio_languages: Lowercased, deduplicated language codes.

    Returns:
        True when more than one distinct language is present; otherwise False.
    """
    return len(audio_languages) > 1

def build_name(path):
    name = os.path.basename(path)
    base, ext = os.path.splitext(name)
    # Use case-insensitive comparison to handle uppercase extensions (e.g., ".MKV").
    ext_is_video = ext.lower() in VIDEO_EXT
    if not ext_is_video:
        # VIDEO_EXT holds real video extensions with leading dots (e.g., ".mkv"), matching splitext() output.
        # Treat other suffixes (e.g., codec/group like .H.264-GROUP)
        # as part of the base name so folder/pack titles don't lose the video codec token when splitext()
        # would otherwise treat them as an extension.
        base, ext = name, ""
    base = _strip_leading_site_prefix(base)
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
        # Normalize to DDP convention: DDP.5.1 → DDP5.1, DD+5.1 → DDP5.1
        if re.match(r"(?i)^DDP\.", audio):
            audio = re.sub(r"(?i)^DDP\.\s*", "DDP", audio)
        elif re.match(r"(?i)^DD\+", audio):
            audio = re.sub(r"(?i)^DD\+\s*", "DDP", audio)
        audio = re.sub(r"(?i)(DDP)[\s.]+(?=\d)", r"\1", audio)
        # Normalize codec.Atmos.7.1 → codec7.1.Atmos so channel layout is preserved
        # and kept in scene-style order.
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

    # Override progressive tag with interlaced when MediaInfo reports MBAFF/Interlaced.
    # This corrects filenames that say "1080p" but the content is actually interlaced.
    if v:
        _scan_type = str(v.get("ScanType", "") or "").strip()
        if _is_interlaced(_scan_type):
            # Content is interlaced – use scan-type-aware resolution.
            _mi_res = resolution(v.get("Width"), _scan_type)
            if _mi_res:
                res = _mi_res
            elif res:
                # Fall back: swap the suffix on whatever the filename reported.
                res = re.sub(r"[pi]$", "i", res, flags=re.I)

    hdr_match = re.search(
        r"(?:DV\.HDR10\+|DV\.HDR10|DV\.HLG|DV|HDR10\+|HDR10|HDR|HLG)", base, re.I,
    )
    hdr = hdr_match.group(0) if hdr_match else None

    # Detect bit depth tag from filename (e.g., 10bit, 8bit, 12bit).
    bit_depth_match = re.search(r'(?<![A-Za-z\d])(8|10|12)bit(?!\w)', base, re.I)
    bit_depth_tag = f"{bit_depth_match.group(1)}bit" if bit_depth_match else None

    # Track whether the filename explicitly contains a HEVC codec format marker alongside
    # an encoder tag (x265/x264); in that case, both should appear in the output.
    hevc_format_match = re.search(r'(?<![A-Za-z0-9])HEVC(?![A-Za-z0-9])', base, re.I)

    # Detect meta tags like REPACK, PROPER, INTERNAL, etc.
    meta_tags = []
    for tag in ["REPACK", "PROPER", "INTERNAL", "READNFO"]:
        if re.search(rf"(?<![A-Za-z]){tag}(?![A-Za-z])", base, re.I):
            meta_tags.append(tag)
    has_remux = bool(re.search(r"(?<![A-Za-z])REMUX(?![A-Za-z])", base, re.I))

    season, episode_num = detect_episode(base)
    # For multi-episode files (e.g., S01E01-E02), use only the first episode for lookups.
    _ep_lookup = episode_num.split("-")[0] if episode_num and "-" in episode_num else episode_num
    parts = []

    if season:
        # Split show name at the first SxxExx occurrence (covers both single and multi-episode).
        show = re.split(r"S\d{2}E\d{2}", base, flags=re.I)[0]
        show = _strip_parenthesized_year(show)
        # `show` is already split at SxxExx, so remove any year from this show-only fragment.
        show = re.sub(r"(?<![A-Za-z0-9])(19|20)\d{2}(?![A-Za-z0-9])", "", show)
        show = clean_name(show)

        ep_match = re.search(rf"S{season}E{_ep_lookup}", base, re.I)
        existing_title = None
        if ep_match:
            after_ep = re.sub(r'^[.\s-]+', '', base[ep_match.end():])
            # For multi-episode, skip the trailing -Exx continuation before looking for a title.
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
        parts.append(f"S{season}E{episode_num}")
        if existing_title:
            parts.append(existing_title)
        else:
            title = episode_title(show, season, _ep_lookup)
            if title:
                parts.append(title)
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
            # No distinct edition segment when year is absent
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

    # Add meta tags (REPACK, PROPER, etc.) after resolution
    for meta_tag in meta_tags:
        parts.append(meta_tag)

    effective_web = None

    # If filename already declares a non-WEB source type, preserve it as-is.
    # WEB inference from MediaInfo should only affect WEB/WEB-DL/WEBRip paths.
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
                # Fallback for uncommon or missing web tags when there is no MediaInfo override
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

    # Ensure REMUX tag is retained (after source tags for correct ordering)
    if has_remux and "REMUX" not in parts:
        parts.append("REMUX")

    encoding_text = _collect_encoding_text(g, v)

    # Always prefer MediaInfo HDR detection over filename-based detection
    # as it's more accurate (e.g., can distinguish HDR10 vs generic HDR)
    if not video:
        video = video_codec(v)

    if video_from_filename and video:
        upper_video = video.upper().replace(".", "")
        if upper_video == "H264":
            video = "H.264"
        elif upper_video == "H265":
            video = "H.265"
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
        # Last-resort fallback: bare channel-count token from filename (e.g., 2CH, 6CH, 5.1CH).
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
    # Preserve an explicit HEVC codec format tag when it appears alongside an encoder tag
    # (x265 or x264); many groups annotate both (e.g., "x265.HEVC-GROUP").
    if hevc_format_match and video and video.upper() in ("X265", "X264"):
        parts.append("HEVC")

    final = ".".join(p for p in parts if p)
    final = clean_name(final)
    if group:
        final += "-" + group

    return final + ext


def _reorder_audio_hdr_tokens(core: str) -> str:
    """Ensure audio tokens precede HDR tokens, keeping other tags stable."""
    # First, swap HDR before audio when they appear as consecutive tokens (DV.HDR10.DD+5.1 → DD+5.1.DV.HDR10).
    core = re.sub(
        rf"(?i)(^|\.)(?P<hdr>{_HDR_BLOCK_PATTERN})\.(?P<audio>{_AUDIO_BLOCK_PATTERN})(?=\.|$)",
        lambda m: f"{m.group(1)}{m.group('audio')}.{m.group('hdr')}",
        core,
    )
    # Next, swap video/audio ordering when audio is embedded in the video token (H.265.DD+5.1 → DD+5.1.H.265).
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
    """Convert a scene-formatted filename into a human-readable title.

    Strips the file extension and replaces **all** dots with spaces so the
    title contains no dots at all (e.g. ``H 264``, ``DDP5 1``).
    """
    _, ext = os.path.splitext(new_name)
    base = new_name[:-len(ext)] if ext.lower() in VIDEO_EXT else new_name

    head, sep, group = base.rpartition("-")
    core = head if sep else base
    group_suffix = f"-{group}" if sep else ""
    core = _reorder_audio_hdr_tokens(core)
    base = core + group_suffix

    if re.search(r"S\d{2}E\d{2}", base, re.I):
        base = _strip_parenthesized_year(base)
        # base is still the full string here, so anchor the year to the pre-episode segment.
        base = re.sub(r"(?<![A-Za-z0-9])(19|20)\d{2}(?=[.\s_-]*S\d{2}E\d{2})", "", base, flags=re.I)
        base = re.sub(r"\.{2,}", ".", base).strip(".")

    base = base.replace('.', ' ')
    return base

def detect_fansub(name):
    """Detect fansub-style filename and return parsed info dict, or ``None``.

    Handles formats like ``[SubsPlease] Title - 22 (1080p) [817B9AE4].mkv``.
    """
    base = os.path.splitext(name)[0]
    m = _FANSUB_RE.match(base)
    if not m:
        return None
    return {
        "group": m.group(1).strip(),
        "title": m.group(2).strip(),

        "episode": int(float(m.group(3).strip())),
        "resolution": m.group(4),
    }

def anime_season_episode(title, absolute_episode):
    """Look up anime season/episode from an absolute episode number.

    Uses the AniList GraphQL API to find the anime and its sequel chain,
    then maps the absolute episode to the correct season and relative
    episode number.

    Returns ``(season, episode)`` tuple.  Falls back to ``(1, absolute_episode)``
    when the API is unavailable or the anime is not found.
    """
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
    """Build a human-readable title for a fansub-format file.

    Combines anime API season/episode lookup with mediainfo-derived codec
    details and TVMaze episode titles to produce a title like:
    ``Hime-sama Goumon no Jikan desu S02E08 Episode Title 1080p WEB-DL AAC 2.0 H.264 - SubsPlease``
    """
    group = fansub_info["group"]
    title = fansub_info["title"]
    absolute_ep = fansub_info["episode"]
    fn_res = fansub_info["resolution"]

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
                # Apply season suffix bounds (see TRAILING_SEASON_MIN/MAX).
                if TRAILING_SEASON_MIN <= value <= TRAILING_SEASON_MAX:
                    season_suffix = value
                    title_for_api = prefix

    season, episode = anime_season_episode(title_for_api, absolute_ep)
    if season_suffix is not None:
        season = season_suffix
    season = season or 1
    episode = episode or absolute_ep

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

    # Extract source tags from filename
    filename = os.path.basename(path)
    service, webtype_fn = detect_source_tags_filename(filename)

    _, web_type = detect_web_type_mediainfo(g, v, a)
    # Use filename webtype if specific (WEB-DL/WEBRip), otherwise use mediainfo detection
    if webtype_fn and webtype_fn != "WEB":
        web_tag = webtype_fn
    else:
        web_tag = web_type or "WEB-DL"

    hdr = detect_hdr(v)

    ai = audio_info(a)
    audio_languages = _collect_audio_languages(str(path), g, a)

    vc = video_codec(v)
    # For fansub releases, prefer encoder names (x264/x265) when re-encode signatures exist
    # even if classified as WEB-DL, to reflect the actual encoding method
    encoding_text = _collect_encoding_text(g, v)
    has_reencode = _has_reencode_signature(encoding_text)
    if has_reencode and vc:
        # Use encoder names instead of format names for fansub releases
        upper = vc.upper().replace(".", "")
        if upper in ("H264", "X264", "AVC"):
            vc = "x264"
        elif upper in ("H265", "X265", "HEVC"):
            vc = "x265"
    else:
        vc = _normalize_video_codec(vc, web_tag)

    ep_title = episode_title(title_for_api, f"{season:02d}", f"{episode:02d}") if not is_pack else None

    if is_pack:
        parts = [title_display, f"S{season:02d}"]
    else:
        parts = [title_display, f"S{season:02d}E{episode:02d}"]
    if ep_title:
        parts.append(ep_title)
    if res:
        parts.append(res)
    # Add service tag before web type if present
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

def generate_title(path, is_pack=False):
    """Generate a human-readable title for any video file.

    Dispatch order:

    1. **PSA** – filenames ending in ``-PSA`` (or with a ``www.UIndex.org``
       prefix): strips the site prefix and video extension, returns the
       cleaned filename as-is (preserving token casing and dots/spaces).
    2. **Pahe** – filenames ending in ``-Pahe.in`` / ``-Pahe in`` (or with
       a ``www.UIndex.org`` prefix): same prefix/extension stripping, plus
       normalises the trailing TLD suffix to plain ``-Pahe``.
    3. **Fansub** files (``[Group] Title - Ep …``) → anime API lookup +
       mediainfo.
    4. **Scene-format** files → ``build_name()`` → ``build_title()``.

    The original file is **never** renamed; only the title string is produced.

    When *is_pack* is ``True`` (the file is part of a multi-episode pack),
    the per-episode subtitle is omitted from the result.
    """
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

    new_name = build_name(path)
    return build_title(new_name)
