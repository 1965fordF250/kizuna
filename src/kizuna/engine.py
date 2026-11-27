"""Kizuna engine.

Takes the video (plus any chosen tracks) from a BASE file and adds chosen audio /
subtitle tracks from a DONOR file of the same episode. The donor tracks are re-timed
so they line up even when the two releases are cut differently (streaming intros,
credits, short gaps at scene breaks). The timing is found by matching an audio track
the two files share, for example the original-language audio.

Terms used below:
  base time  - a timestamp in the base file (the one whose video is kept)
  segment    - [base_start, base_end, offset]: donor time = base time + offset
"""
import json
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from . import system

SR = 4000                      # sample rate used for matching audio (Hz)
VIDEO_EXT = (".mkv", ".mp4", ".m4v", ".webm")
MIN_MATCH_POINTS = 10          # fewer matching windows than this = can't trust the sync

LANG_NAMES = {
    "jpn": "Japanese", "eng": "English", "fre": "French", "fra": "French", "ger": "German",
    "deu": "German", "spa": "Spanish", "ita": "Italian", "por": "Portuguese", "rus": "Russian",
    "chi": "Chinese", "zho": "Chinese", "kor": "Korean", "ara": "Arabic", "cze": "Czech",
    "ces": "Czech", "dan": "Danish", "dut": "Dutch", "nld": "Dutch", "fin": "Finnish",
    "gre": "Greek", "ell": "Greek", "heb": "Hebrew", "hin": "Hindi", "hrv": "Croatian",
    "hun": "Hungarian", "ind": "Indonesian", "may": "Malay", "msa": "Malay", "nob": "Norwegian",
    "nor": "Norwegian", "pol": "Polish", "rum": "Romanian", "ron": "Romanian", "swe": "Swedish",
    "tha": "Thai", "tur": "Turkish", "ukr": "Ukrainian", "vie": "Vietnamese", "fil": "Filipino",
    "tgl": "Tagalog", "cat": "Catalan", "bul": "Bulgarian", "srp": "Serbian", "slo": "Slovak",
    "slk": "Slovak", "slv": "Slovenian", "est": "Estonian", "lav": "Latvian", "lit": "Lithuanian",
    "per": "Persian", "fas": "Persian", "urd": "Urdu", "ben": "Bengali", "tam": "Tamil",
    "tel": "Telugu", "isl": "Icelandic", "ice": "Icelandic", "glg": "Galician", "baq": "Basque",
    "eus": "Basque", "und": "Unknown", "zxx": "No language", "mul": "Multiple",
}


def lang_name(code):
    return LANG_NAMES.get(code or "und", code or "Unknown")


class MergeError(Exception):
    """A problem with one episode, explained in plain language."""


class Cancelled(Exception):
    pass


def tool(name):
    path = system.find_tool(name)
    if not path:
        raise MergeError(f"{name} was not found. Install ffmpeg and MKVToolNix, "
                         f"or use the packaged Kizuna download, which includes them.")
    return path


def _run(cmd):
    p = system.run(cmd)
    if p.returncode != 0:
        msg = ((p.stderr or "") + (p.stdout or "")).strip()
        raise MergeError(msg[-1500:] or f"{os.path.basename(cmd[0])} failed")
    return p.stdout


# ---------------------------------------------------------------- file info
def info(path):
    """Tracks of a video file as mkvmerge sees them."""
    d = json.loads(_run([tool("mkvmerge"), "-J", path]))
    if not d.get("container", {}).get("recognized"):
        raise MergeError("not a recognised video file")
    tracks = []
    for t in d.get("tracks", []):
        p = t.get("properties", {})
        tracks.append({
            "id": t["id"], "type": t["type"], "codec": t.get("codec", ""),
            "lang": p.get("language") or "und", "name": p.get("track_name") or "",
            "dims": p.get("pixel_dimensions", ""),
            "forced": bool(p.get("forced_track")),
            "hi": bool(p.get("flag_hearing_impaired")),
        })
    dur = (d.get("container", {}).get("properties", {}).get("duration") or 0) / 1e9
    return {"tracks": tracks, "attachments": len(d.get("attachments", [])), "duration": dur}


def ff_streams(path):
    d = json.loads(_run([tool("ffprobe"), "-v", "error", "-show_streams", "-show_chapters",
                         "-of", "json", path]))
    return d.get("streams", []), [float(c["start_time"]) for c in d.get("chapters", [])]


def ff_index_map(tracks, streams):
    """mkvmerge track id -> ffprobe stream (same order; attachments excluded)."""
    kind = {"video": "video", "audio": "audio", "subtitles": "subtitle"}
    for keep_pics in (False, True):
        ff = [s for s in streams if s.get("codec_type") in ("video", "audio", "subtitle")
              and (keep_pics or not s.get("disposition", {}).get("attached_pic"))]
        if len(ff) == len(tracks) and all(kind.get(t["type"]) == s["codec_type"]
                                          for t, s in zip(tracks, ff)):
            return {t["id"]: s for t, s in zip(tracks, ff)}
    raise MergeError("couldn't read the track layout of this file")


# ---------------------------------------------------------------- folders
NOISE = re.compile(
    r"(?i)\b(\d{3,4}p|[xh]\.?26[45]|\d{1,2}.?bits?|hevc|avc|aac|flac|opus|ddp?\d?|dts|e?ac3|"
    r"truehd|atmos|web(-?dl|rip)?|blu-?ray|bdrip|remux|hdr|sdr|nf|amzn|dsnp|(19|20)\d\d|\d\.\d)\b")


def episode_number(name):
    """Best guess at the episode number in a file name, or None."""
    s = os.path.splitext(name)[0]
    m = re.search(r"(?i)S\d{1,2}\s*[._-]?\s*E(\d{1,4})", s)
    if m:
        return int(m.group(1))
    m = re.search(r"(?i)(?<![\dA-Za-z])\d{1,2}x(\d{2,3})(?!\d)", s)
    if m:
        return int(m.group(1))
    m = re.search(r"(?i)\b(?:episode|ep)\s*[._-]?\s*(\d{1,4})\b", s)
    if m:
        return int(m.group(1))
    s = re.sub(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}", " ", s)
    s = NOISE.sub(" ", s)
    nums = re.findall(r"(?<![\dA-Za-z])(\d{1,4})(?:v\d)?(?![\dA-Za-z])", s)
    return int(nums[-1]) if nums else None


def list_videos(folder):
    out = []
    for n in sorted(os.listdir(folder), key=str.lower):
        p = os.path.join(folder, n)
        if n.startswith(".") or ".partial." in n or not n.lower().endswith(VIDEO_EXT):
            continue
        if os.path.isfile(p):
            out.append(p)
    return out


def _safe_info(p):
    try:
        return info(p)
    except Exception as e:  # noqa: BLE001 - shown to the user per file
        return str(e)[:200] or "couldn't be read"


def scan(folder):
    """Summary of the videos in a folder and the tracks they contain."""
    paths = list_videos(folder)
    with ThreadPoolExecutor(6) as ex:
        infos = list(ex.map(_safe_info, paths))
    files, errors = [], []
    groups = {"video": {}, "audio": {}, "subtitles": {}}
    for p, inf in zip(paths, infos):
        name = os.path.basename(p)
        if isinstance(inf, str):
            errors.append({"name": name, "error": inf})
            continue
        files.append({"name": name, "ep": episode_number(name), "size": os.path.getsize(p),
                      "duration": inf["duration"]})
        seen = set()
        for t in inf["tracks"]:
            if t["type"] == "video":
                key = f"{t['codec']} {t['dims']}".strip()
            elif t["type"] in ("audio", "subtitles"):
                key = t["lang"]
            else:
                continue
            g = groups[t["type"]].setdefault(key, {"key": key, "names": [], "codecs": [], "count": 0})
            if (t["type"], key) not in seen:
                g["count"] += 1
                seen.add((t["type"], key))
            if t["name"] and t["name"] not in g["names"] and len(g["names"]) < 6:
                g["names"].append(t["name"])
            if t["codec"] not in g["codecs"]:
                g["codecs"].append(t["codec"])
            if t["type"] == "subtitles":       # full vs forced/signs-only, for "play by default"
                kind = "forced" if _sub_flags(t)[0] else "full"
                g.setdefault("kinds", [])
                if kind not in g["kinds"]:
                    g["kinds"].append(kind)
    for cat in ("audio", "subtitles"):
        for g in groups[cat].values():
            g["label"] = lang_name(g["key"])
    return {"folder": folder, "files": files, "errors": errors,
            "size": sum(f["size"] for f in files),
            "video": list(groups["video"].values()),
            "audio": list(groups["audio"].values()),
            "subtitles": list(groups["subtitles"].values())}


def pair(a_files, b_files):
    """Match episodes between two folders by episode number (or by order as a fallback)."""
    def keyed(files):
        keys = [f["ep"] for f in files]
        if None in keys or len(set(keys)) != len(keys):
            return {i + 1: f for i, f in enumerate(files)}, False
        return {f["ep"]: f for f in files}, True
    ka, oka = keyed(a_files)
    kb, okb = keyed(b_files)
    rows = [{"ep": e, "a": ka.get(e, {}).get("name"), "b": kb.get(e, {}).get("name")}
            for e in sorted(set(ka) | set(kb))]
    return rows, oka and okb


# ---------------------------------------------------------------- audio matching
def load_pcm(path, stream_index):
    """Mono PCM of one stream at SR Hz; sample 0 = file time 0 (late starts are padded)."""
    p = system.run([tool("ffmpeg"), "-v", "error", "-copyts", "-i", path,
                    "-map", f"0:{stream_index}", "-af", "aresample=async=1:first_pts=0",
                    "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"], binary=True)
    if p.returncode:
        raise MergeError("couldn't decode audio: " + p.stderr.decode("utf-8", "replace")[-400:])
    return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32)


def best_lag(ref, oth, t, win, lo, hi):
    """Lag L in [lo, hi] seconds such that ref[t:t+win] best matches oth[t+L:...].
    Returns (lag, normalised correlation) or (None, 0) for silence / out of range."""
    a = ref[int(t * SR): int((t + win) * SR)]
    if len(a) < int(win * SR * 0.9):
        return None, 0.0
    a = a - a.mean()
    ea = float((a * a).sum())
    if ea < 1e3:
        return None, 0.0
    s0 = max(0, int(round((t + lo) * SR)))
    e0 = min(len(oth), int(round((t + win + hi) * SR)))
    b = oth[s0:e0]
    if len(b) < len(a):
        return None, 0.0
    b = b - b.mean()
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    c = np.fft.irfft(np.fft.rfft(b, n) * np.conj(np.fft.rfft(a, n)), n)[: len(b) - len(a) + 1]
    cs = np.concatenate([[0.0], np.cumsum(b.astype(np.float64) ** 2)])
    eb = cs[len(a):] - cs[: len(b) - len(a) + 1]
    score = c / (np.sqrt(ea * np.maximum(eb, 1e-9)) + 1e-9)
    k = int(np.argmax(score))
    return (s0 + k) / SR - t, float(score[k])


def corr_at(ref, oth, t, win, lag, slack=0.015):
    """Correlation of ref[t:t+win] with oth at a given lag (+- slack seconds)."""
    a = ref[int(t * SR): int((t + win) * SR)]
    if len(a) == 0:
        return 0.0
    a = a - a.mean()
    best = -1.0
    for d in range(-int(slack * SR), int(slack * SR) + 1):
        s = int(round((t + lag) * SR)) + d
        if s < 0:
            continue
        b = oth[s: s + len(a)]
        if len(b) < len(a):
            continue
        b = b - b.mean()
        best = max(best, float((a * b).sum() / (np.sqrt((a * a).sum() * (b * b).sum()) + 1e-9)))
    return best


def compute_map(ref, oth, chapters=()):
    """Find the piecewise time mapping between two versions from their shared audio.
    Returns {"segments": [[base_start, base_end, offset], ...], "coverage", "points"} or None."""
    dur = len(ref) / SR
    if dur < 10:
        return None
    # 1) rough offset from a few long windows, searching +-120 s
    rough = []
    for f in np.linspace(0.08, 0.92, 9):
        lag, sc = best_lag(ref, oth, f * dur, min(20.0, dur * 0.15), -120, 120)
        if lag is not None and sc > 0.3:
            rough.append(lag)
    if not rough:
        return None
    centre = max(rough, key=lambda x: sum(abs(x - y) < 2 for y in rough))
    L0 = float(np.median([y for y in rough if abs(centre - y) < 2]))

    # 2) dense pass: short windows every 8 s, searching +-20 s around the rough offset
    STEP = WIN = 8.0
    samples, t = [], 0.0
    while t + WIN <= dur:
        lag, sc = best_lag(ref, oth, t, WIN, L0 - 20, L0 + 20)
        if lag is not None and sc > 0.3:
            samples.append((t, lag, sc))
        t += STEP

    # 3) group runs with the same offset; drop isolated outliers
    groups = []
    for s in samples:
        if groups and abs(groups[-1]["lag"] - s[1]) < 0.03:
            groups[-1]["pts"].append(s)
        else:
            groups.append({"lag": s[1], "pts": [s]})
    groups = [g for g in groups if len(g["pts"]) >= 2]
    merged = []
    for g in groups:
        if merged and abs(merged[-1]["lag"] - g["lag"]) < 0.03:
            merged[-1]["pts"] += g["pts"]
        else:
            merged.append(g)
    if not merged:
        return None
    for g in merged:
        g["lag"] = float(np.median([p[1] for p in g["pts"]]))

    # 4) place each boundary precisely (1 s windows), snapping to a chapter mark nearby
    segs, start = [], 0.0
    for g, h in zip(merged, merged[1:]):
        lo, hi = g["pts"][-1][0], h["pts"][0][0] + WIN
        ts = np.arange(lo, hi, 0.25)
        sa = [corr_at(ref, oth, x, 1.0, g["lag"]) for x in ts]
        sb = [corr_at(ref, oth, x, 1.0, h["lag"]) for x in ts]
        best_b, best_v = lo, -1e9
        for i in range(len(ts) + 1):
            v = sum(sa[:i]) + sum(sb[i:])
            if v > best_v:
                best_v, best_b = v, (float(ts[i]) if i < len(ts) else hi)
        near = [c for c in chapters if abs(c - best_b) <= 1.5]
        if near:
            best_b = min(near, key=lambda c: abs(c - best_b))
        segs.append([start, best_b, g["lag"]])
        start = best_b
    segs.append([start, dur, merged[-1]["lag"]])
    n_windows = max(1, int((dur - WIN) / STEP) + 1)
    return {"segments": segs, "coverage": len(samples) / n_windows, "points": len(samples)}


def donor_ranges(segs):
    """Donor-time range each segment uses; an overlap at a boundary goes to the later one."""
    out = []
    for i, (s, e, o) in enumerate(segs):
        lo, hi = s + o, e + o
        if i + 1 < len(segs):
            hi = min(hi, segs[i + 1][0] + segs[i + 1][2])
        out.append((s, e, o, lo, hi))
    return out


def to_base(t, rng):
    """Donor timestamp -> base timestamp, or None if outside the matched content."""
    if t < rng[0][3] - 0.05 or t > rng[-1][4] + 0.05:
        return None
    for s, e, o, lo, hi in rng:
        if t < hi:
            return max(s, t - o) if t >= lo else s
    return rng[-1][1]


def build_graph(rng, stream_index):
    """ffmpeg filter graph: cut donor stream (input 0) per segment onto the base timeline."""
    parts, labels = [], []
    for k, (s, e, o, lo, hi) in enumerate(rng):
        need = e - s
        lo_c = max(0.0, lo)
        # place donor time `lo` at 0, then pad any gap before the first sample with silence
        f = (f"[0:{stream_index}]atrim=start={lo_c:.4f}:end={max(lo_c, hi):.4f},"
             f"asetpts=PTS-({lo:.4f})/TB,aresample=async=1:first_pts=0")
        if hi - lo < need - 0.001:
            f += f",apad=whole_dur={need:.4f}"
        parts.append(f + f"[p{k}]")
        labels.append(f"[p{k}]")
    return ";".join(parts) + ";" + "".join(labels) + f"concat=n={len(labels)}:v=0:a=1[aout]"


def check_sync(ref, donor_path, donor_ref_index, rng):
    """Push the donor's reference audio through the same cut as the added tracks and
    measure how far it lands from the base reference audio."""
    p = system.run([tool("ffmpeg"), "-v", "error", "-copyts", "-i", donor_path,
                    "-filter_complex", build_graph(rng, donor_ref_index), "-map", "[aout]",
                    "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"], binary=True)
    if p.returncode:
        return None
    al = np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32)
    dur = len(ref) / SR
    step = max(2.0, min(20.0, (dur - 20) / 12))      # ~12+ checkpoints even for short videos
    errs = []
    for t in np.arange(10.0, dur - 10, step):
        lag, sc = best_lag(ref, al, float(t), 6, -1, 1)
        if lag is not None and sc > 0.4:
            errs.append(abs(lag) * 1000)
    if not errs:
        return {"points": 0, "typical_ms": None, "bad": 0, "ok": False}
    bad = sum(e >= 40 for e in errs)
    good = sorted(e for e in errs if e < 40) or [0.0]
    return {"points": len(errs), "typical_ms": int(round(good[min(len(good) - 1, int(len(good) * 0.9))])),
            "bad": bad, "ok": len(errs) >= 5 and bad <= max(1, len(errs) * 0.03)}


# ---------------------------------------------------------------- subtitles
def _ass_t(s):
    h, m, r = s.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(r)


def _ass_f(t):
    cs = int(round(max(0.0, t) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def _srt_t(s):
    h, m, r = s.strip().split(":")
    sec, ms = r.replace(".", ",").split(",")
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms.ljust(3, "0")[:3]) / 1000


def _srt_f(t):
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def _shift(a, b, rng):
    na = to_base(a, rng)
    return None if na is None else (na, max(na + 0.01, na + (b - a)))


def retime_ass(src, dst, rng):
    out = []
    with open(src, encoding="utf-8-sig", errors="replace") as fh:
        for line in fh:
            m = re.match(r"^(Dialogue|Comment):\s*([^,]*),([^,]*),([^,]*),(.*)$", line.rstrip("\r\n"))
            if m:
                try:
                    r = _shift(_ass_t(m[3]), _ass_t(m[4]), rng)
                except ValueError:
                    r = None
                if r is None:
                    continue
                line = f"{m[1]}: {m[2]},{_ass_f(r[0])},{_ass_f(r[1])},{m[5]}\n"
            out.append(line)
    with open(dst, "w", encoding="utf-8", newline="\n") as fh:
        fh.writelines(out)


def retime_srt(src, dst, rng):
    with open(src, encoding="utf-8-sig", errors="replace") as fh:
        text = fh.read().replace("\r\n", "\n").replace("\r", "\n")
    out, n = [], 0
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        i = next((k for k, l in enumerate(lines) if "-->" in l), None)
        if i is None:
            continue
        try:
            a, z = [x.strip().split()[0] for x in lines[i].split("-->")[:2]]
            r = _shift(_srt_t(a), _srt_t(z), rng)
        except (ValueError, IndexError):
            continue
        if r is None:
            continue
        n += 1
        out.append(f"{n}\n{_srt_f(r[0])} --> {_srt_f(r[1])}\n" + "\n".join(lines[i + 1:]))
    with open(dst, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n\n".join(out) + "\n")


def audio_codec_args(s):
    """Re-encode settings that keep quality: lossless stays lossless, lossy gets headroom."""
    c = s.get("codec_name", "")
    if c in ("flac", "alac", "truehd", "mlp") or c.startswith("pcm_"):
        return ["-c:a", "flac"]
    ch = int(s.get("channels") or 2)
    tags = s.get("tags", {})
    try:
        br = int(s.get("bit_rate") or tags.get("BPS") or tags.get("BPS-eng") or 0)
    except ValueError:
        br = 0
    floor = 192000 if ch <= 2 else 448000
    return ["-c:a", "aac", "-b:a", f"{int(min(max(br * 1.3, floor), 640000) / 1000)}k"]


# ---------------------------------------------------------------- subtitle export
ISO639_1 = {
    "eng": "en", "jpn": "ja", "fre": "fr", "fra": "fr", "ger": "de", "deu": "de", "spa": "es",
    "ita": "it", "por": "pt", "rus": "ru", "chi": "zh", "zho": "zh", "kor": "ko", "ara": "ar",
    "cze": "cs", "ces": "cs", "dan": "da", "dut": "nl", "nld": "nl", "fin": "fi", "gre": "el",
    "ell": "el", "heb": "he", "hin": "hi", "hrv": "hr", "hun": "hu", "ind": "id", "may": "ms",
    "msa": "ms", "nob": "nb", "nor": "no", "pol": "pl", "rum": "ro", "ron": "ro", "swe": "sv",
    "tha": "th", "tur": "tr", "ukr": "uk", "vie": "vi", "cat": "ca", "bul": "bg", "srp": "sr",
    "slo": "sk", "slk": "sk", "slv": "sl", "est": "et", "lav": "lv", "lit": "lt", "per": "fa",
    "fas": "fa", "urd": "ur", "ben": "bn", "tam": "ta", "tel": "te", "isl": "is", "ice": "is",
    "glg": "gl", "baq": "eu", "eus": "eu", "tgl": "tl", "fil": "tl",
}
# subtitle codec -> (file extension, ffmpeg muxer, copy without converting?)
SUB_FORMATS = {
    "subrip": ("srt", "srt", True), "ass": ("ass", "ass", True), "ssa": ("ass", "ass", True),
    "webvtt": ("vtt", "webvtt", True), "mov_text": ("srt", "srt", False), "text": ("srt", "srt", False),
}


FORCED_WORDS = r"forced|forc[eé]s?|forc[eé]es?|forzad[oa]s?|for[cç]ad[oa]s?|erzwungen|forzati|forzate"
HI_WORDS = r"sdh|cc|hearing[ -]impaired|hoh|malentendants?|h[oö]rgesch[aä]digte"
# words that describe a track's language or kind rather than set it apart from its siblings
GENERIC_WORDS = (r"full|complete|complets?|completos?|completas?|vollst[aä]ndig|dialog(ue)?s?|subtitles?|"
                 r"subs?|sous[ -]titres?|untertitel|subt[ií]tulos?|legendas?|default|normal|regular|"
                 r"signs?|songs?|and|hearing|impaired|track|the")


# how languages often name themselves in track titles (accents removed)
NATIVE_NAMES = {
    "ger": {"deutsch"}, "deu": {"deutsch"}, "spa": {"espanol", "castellano"},
    "fre": {"francais"}, "fra": {"francais"}, "por": {"portugues"}, "ita": {"italiano"},
    "jpn": {"nihongo"}, "dut": {"nederlands"}, "nld": {"nederlands"}, "pol": {"polski"},
    "rus": {"russkiy", "russkij"}, "swe": {"svenska"}, "dan": {"dansk"}, "nor": {"norsk"},
    "nob": {"norsk", "bokmal"}, "fin": {"suomi"}, "tur": {"turkce"}, "cze": {"cestina"},
    "ces": {"cestina"}, "hun": {"magyar"}, "rum": {"romana"}, "ron": {"romana"},
}


def _plain(text):
    """Lower case without accents: 'Forcés' -> 'forces'."""
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", (text or "").lower())
                   if not unicodedata.combining(c))


def _sub_flags(t):
    """(forced, hearing impaired) from the track flags, or from its name as a fallback."""
    name = (t.get("name") or "").lower()
    forced = t.get("forced") or bool(re.search(rf"\b({FORCED_WORDS})\b", name)) or (
        bool(re.search(r"\bsigns?\b", name)) and not re.search(r"dialog|full", name))
    hi = t.get("hi") or bool(re.search(rf"\b({HI_WORDS})\b", name))
    return forced, hi


def _variant(track):
    """What sets a track apart from others in the same language ('English Dub' -> 'dub'),
    or '' for a plain track. Language names/codes, forced/SDH words and generic words drop out."""
    words = re.findall(r"[a-z0-9]+", _plain(track.get("name")))
    code = track.get("lang") or ""
    lang_words = {code, ISO639_1.get(code, ""), _plain(lang_name(code))} | NATIVE_NAMES.get(code, set())
    drop = re.compile(rf"^({_plain(FORCED_WORDS)}|{_plain(HI_WORDS)}|{_plain(GENERIC_WORDS)})$")
    keep = [w for w in words if w not in lang_words and not drop.match(w)]
    return "-".join(keep)[:30].strip("-")


def subtitle_filename(stem, track, ext, used):
    """'<video name>.<lang>[.forced|.sdh][.<variant>].<ext>' - the pattern players and
    OpenSubtitles recognise. `used` (lower-case names) keeps names unique per video."""
    code = track.get("lang") or "und"
    lang = ISO639_1.get(code, code)
    forced, hi = _sub_flags(track)
    parts = [stem, lang] + (["forced"] if forced else ["sdh"] if hi else [])
    variant = _variant(track)
    first = parts + [variant] if variant else parts
    for c in [first] + [first + [str(n)] for n in range(2, 100)]:
        name = ".".join(c) + "." + ext
        if name.lower() not in used:
            used.add(name.lower())
            return name
    raise MergeError("too many subtitle tracks with the same language")


def opensubtitles_hash(path):
    """The 64-bit hash OpenSubtitles uses to match subtitles to an exact video file:
    file size + sum of the first and last 64 KiB read as little-endian 64-bit integers."""
    size = os.path.getsize(path)
    if size < 2 * 65536:
        return None
    h = size
    with open(path, "rb") as fh:
        for offset in (0, size - 65536):
            fh.seek(offset)
            h = (h + int(np.frombuffer(fh.read(65536), dtype="<u8").sum(dtype=np.uint64))) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def export_subtitles(video_path, dest_dir):
    """Save every text subtitle track of a (merged) video as its own file in dest_dir.
    Returns {"files": [...], "notes": [...], "hash", "size", "fps"}."""
    os.makedirs(dest_dir, exist_ok=True)
    inf = info(video_path)
    streams, _ = ff_streams(video_path)
    smap = ff_index_map(inf["tracks"], streams)
    stem = os.path.splitext(os.path.basename(video_path))[0]
    used, files, notes = set(), [], []
    for t in inf["tracks"]:
        if t["type"] != "subtitles":
            continue
        s = smap[t["id"]]
        fmt = SUB_FORMATS.get(s.get("codec_name"))
        label = f"{lang_name(t['lang'])} subtitles" + (f" “{t['name']}”" if t["name"] else "")
        if not fmt:
            notes.append(f"Not saved as a file: {label} (picture-based subtitles can't be saved as text).")
            continue
        ext, muxer, copy = fmt
        name = subtitle_filename(stem, t, ext, used)
        path = os.path.join(dest_dir, name)
        tmp = path + ".part"
        try:
            # -copyts: keep the file's own timestamps (otherwise some ffmpeg versions shift
            # subtitles by the audio's start offset, e.g. AAC priming)
            _run([tool("ffmpeg"), "-v", "error", "-copyts", "-y", "-i", video_path, "-map", f"0:{s['index']}",
                  "-c:s", "copy" if copy else "srt", "-f", muxer, tmp])
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        files.append(name)
    fps = None
    for s in streams:
        if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic"):
            try:
                num, den = (s.get("r_frame_rate") or "0/1").split("/")
                fps = round(int(num) / int(den), 3) if int(den) else None
            except ValueError:
                pass
            break
    return {"files": files, "notes": notes, "hash": opensubtitles_hash(video_path),
            "size": os.path.getsize(video_path), "fps": fps}


# ---------------------------------------------------------------- spot-check preview
def make_preview(video_path, preview, dest, seconds=20):
    """Write a short clip of a merged video around preview["at"], with the added audio and
    subtitles switched on, so the sync can be checked in a few seconds. Nothing is re-encoded."""
    start = max(0.0, float(preview["at"]) - seconds / 2)
    streams, _ = ff_streams(video_path)
    n_audio = sum(1 for s in streams if s.get("codec_type") == "audio")
    n_subs = sum(1 for s in streams if s.get("codec_type") == "subtitle")
    args = [tool("ffmpeg"), "-v", "error", "-y", "-ss", f"{start:.3f}", "-i", video_path,
            "-t", str(seconds), "-map", "0", "-c", "copy"]
    for key, count, rel in (("a", n_audio, preview.get("audio_rel")), ("s", n_subs, preview.get("sub_rel"))):
        if rel is not None and rel < count:
            for k in range(count):
                args += [f"-disposition:{key}:{k}", "default" if k == rel else "0"]
    tmp = dest + ".part.mkv"
    try:
        _run(args + ["-f", "matroska", tmp])
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return dest


# ---------------------------------------------------------------- merge one episode
def pick_defaults(b_audio, b_subs, d_audio, d_subs, defaults):
    """Which tracks should play by default.

    defaults = {"audio": None | {"from": "base"|"donor", "lang": code},
                "subtitles": None | "none" | {"from": "base"|"donor", "lang": code, "kind": "full"|"forced"}}
    None means "keep what the files say". Returns {"audio": (src, id) | None,
    "subtitles": (src, id) | "none" | None, "forced": bool}; a choice that matches no track in
    this episode falls back to None (keep the files' own flags).
    """
    out = {"audio": None, "subtitles": None, "forced": False}
    if not defaults:
        return out
    pools = {"base": (b_audio, b_subs), "donor": (d_audio, d_subs)}
    a = defaults.get("audio")
    if isinstance(a, dict) and a.get("from") in pools:
        match = [t for t in pools[a["from"]][0] if t["lang"] == a.get("lang")]
        if match:
            out["audio"] = (a["from"], match[0]["id"])
    s = defaults.get("subtitles")
    if s == "none":
        out["subtitles"] = "none"
    elif isinstance(s, dict) and s.get("from") in pools:
        want_forced = s.get("kind") == "forced"
        match = [t for t in pools[s["from"]][1] if t["lang"] == s.get("lang") and _sub_flags(t)[0] == want_forced]
        match.sort(key=lambda t: _sub_flags(t)[1])          # prefer a non-SDH track
        if match:
            out["subtitles"] = (s["from"], match[0]["id"])
            out["forced"] = want_forced
    return out


def merge_episode(base_path, donor_path, sel, out_path, work, step=lambda s: None,
                  should_cancel=lambda: False, subs_dir=None, defaults=None):
    """Build out_path from base_path (+ tracks from donor_path, which may be None).

    sel = {"base":  {"audio": [lang, ...], "subtitles": [lang, ...]},
           "donor": {"audio": [lang, ...], "subtitles": [lang, ...]}}
    subs_dir: if given, the merged file's text subtitles are also saved there as files.
    defaults: which tracks play by default, see pick_defaults().
    Returns {"segments", "sync", "notes", "reference", "subtitles", "preview"}.
    Raises MergeError / Cancelled.
    """
    def checkpoint(msg):
        if should_cancel():
            raise Cancelled()
        step(msg)

    os.makedirs(work, exist_ok=True)
    tmp = out_path + ".partial.mkv"
    result = {"notes": [], "segments": None, "sync": None, "reference": None, "subtitles": None,
              "preview": None}
    try:
        checkpoint("Reading tracks")
        bi = info(base_path)
        bs, bch = ff_streams(base_path)
        bmap = ff_index_map(bi["tracks"], bs)

        def pick(inf, cat, langs):
            return [t for t in inf["tracks"] if t["type"] == cat and t["lang"] in langs]

        b_video = [t for t in bi["tracks"] if t["type"] == "video"]
        if not b_video:
            raise MergeError("the file chosen for video has no video track")
        b_audio = pick(bi, "audio", sel["base"]["audio"])
        b_subs = pick(bi, "subtitles", sel["base"]["subtitles"])

        d_audio, d_subs, di, dmap = [], [], None, None
        if donor_path:
            di = info(donor_path)
            ds, _ = ff_streams(donor_path)
            dmap = ff_index_map(di["tracks"], ds)
            d_audio = pick(di, "audio", sel["donor"]["audio"])
            d_subs = pick(di, "subtitles", sel["donor"]["subtitles"])

        chosen = pick_defaults(b_audio, b_subs, d_audio, d_subs, defaults)

        def flag_opts(src, t, tid, keep):
            """mkvmerge flags for one audio/subtitle track. With no choice for its kind, the
            default flag is `keep` (None: as in the source file). With a choice, every track of
            that kind gets both flags set explicitly: players such as VLC rank a "forced" track
            above the "default" one, so a forced flag left over from the source (common on
            Blu-ray dubs) would otherwise win over the user's choice."""
            pick_ = chosen["audio"] if t["type"] == "audio" else chosen["subtitles"]
            if pick_ is None:
                return [] if keep is None else ["--default-track-flag", f"{tid}:{keep}"]
            on = pick_ == (src, t["id"])
            forced = on and t["type"] == "subtitles" and chosen["forced"]
            return ["--default-track-flag", f"{tid}:{'yes' if on else 'no'}",
                    "--forced-display-flag", f"{tid}:{'yes' if forced else 'no'}"]

        cmd = [tool("mkvmerge"), "-q", "-o", tmp]
        cmd += ["--audio-tracks", ",".join(str(t["id"]) for t in b_audio)] if b_audio else ["-A"]
        cmd += ["--subtitle-tracks", ",".join(str(t["id"]) for t in b_subs)] if b_subs else ["-S"]
        for t in b_audio + b_subs:
            cmd += flag_opts("base", t, t["id"], None)      # None: leave the base file's flags alone
        cmd.append(base_path)
        d_audio_ids, d_sub_ids, nfile = [], [], 1

        if d_audio or d_subs:
            checkpoint("Lining up the two versions")
            ref_b, ref_d, ref, oth = _pick_reference(base_path, bi, bmap, donor_path, di, dmap)
            m = compute_map(ref, oth, bch)
            if m is None or m["points"] < MIN_MATCH_POINTS:
                raise MergeError("couldn't line the two versions up: their audio doesn't match "
                                 "closely enough (are these really the same episode?)")
            segs = m["segments"]
            rng = donor_ranges(segs)
            result["segments"] = [[round(s, 3), round(e, 3), round(o, 4)] for s, e, o in segs]
            result["reference"] = lang_name(ref_b["lang"])

            if len(segs) == 1:
                # Same cut, only shifted: mkvmerge moves the tracks without re-encoding.
                o_ms = int(round(segs[0][2] * 1000))
                dc = ["-D", "--no-chapters", "--no-global-tags", "--no-buttons"]
                dc += ["--audio-tracks", ",".join(str(t["id"]) for t in d_audio)] if d_audio else ["-A"]
                dc += ["--subtitle-tracks", ",".join(str(t["id"]) for t in d_subs)] if d_subs else ["-S"]
                if not d_subs:
                    dc.append("--no-attachments")
                for t in d_audio + d_subs:
                    dc += flag_opts("donor", t, t["id"], "no")   # added tracks are off unless chosen
                if o_ms:
                    dc += ["--sync", f"-1:{-o_ms}"]
                cmd += dc + [donor_path]
                d_audio_ids = [f"1:{t['id']}" for t in d_audio]
                d_sub_ids = [f"1:{t['id']}" for t in d_subs]
                nfile = 2
            else:
                # Cut differently: rebuild each track section by section on the base timeline.
                for t in d_audio:
                    checkpoint(f"Re-timing {lang_name(t['lang'])} audio")
                    s = dmap[t["id"]]
                    f = os.path.join(work, f"audio{t['id']}.mka")
                    _run([tool("ffmpeg"), "-v", "error", "-copyts", "-y", "-i", donor_path,
                          "-filter_complex", build_graph(rng, s["index"]), "-map", "[aout]"]
                         + audio_codec_args(s) + [f])
                    cmd += _track_opts(t) + flag_opts("donor", t, 0, "no") + ["--no-chapters", "--no-global-tags", f]
                    d_audio_ids.append(f"{nfile}:0")
                    nfile += 1
                for t in d_subs:
                    s = dmap[t["id"]]
                    kind = {"ass": "ass", "ssa": "ass", "subrip": "srt", "webvtt": "srt",
                            "mov_text": "srt", "text": "srt"}.get(s.get("codec_name"))
                    label = f"{lang_name(t['lang'])} subtitles" + (f" “{t['name']}”" if t["name"] else "")
                    if not kind:
                        result["notes"].append(f"Skipped {label}: picture-based subtitles can't be "
                                               f"re-timed when the two versions are cut differently.")
                        continue
                    checkpoint("Re-timing subtitles")
                    raw = os.path.join(work, f"sub{t['id']}.raw.{kind}")
                    fixed = os.path.join(work, f"sub{t['id']}.{kind}")
                    conv = ["-c:s", "copy"] if s["codec_name"] in ("ass", "ssa", "subrip") else ["-c:s", "srt"]
                    _run([tool("ffmpeg"), "-v", "error", "-copyts", "-y", "-i", donor_path,
                          "-map", f"0:{s['index']}"] + conv + [raw])
                    (retime_ass if kind == "ass" else retime_srt)(raw, fixed, rng)
                    cmd += ["--sub-charset", "0:UTF-8"] + _track_opts(t) + flag_opts("donor", t, 0, "no") + [fixed]
                    d_sub_ids.append(f"{nfile}:0")
                    nfile += 1
                if d_sub_ids and di["attachments"]:
                    # fonts used by styled subtitles
                    cmd += ["-A", "-D", "-S", "--no-chapters", "--no-global-tags", "--no-track-tags",
                            "--no-buttons", donor_path]
                    nfile += 1

            checkpoint("Checking sync")
            result["sync"] = check_sync(ref, donor_path, dmap[ref_d["id"]]["index"], rng)
            # spot-check clip: around the first point where the timing changes, else 1/4 in
            dur = segs[-1][1]
            at = segs[1][0] if len(segs) > 1 else min(60.0, dur * 0.25)
            result["preview"] = {"at": round(at, 3),
                                 "audio_rel": len(b_audio) if d_audio_ids else None,
                                 "sub_rel": len(b_subs) if d_sub_ids else None}

        order = [f"0:{t['id']}" for t in b_video] + [f"0:{t['id']}" for t in b_audio] + d_audio_ids
        order += [f"0:{t['id']}" for t in b_subs] + d_sub_ids
        cmd += ["--track-order", ",".join(order)]
        checkpoint("Writing file")
        p = system.run(cmd)
        if p.returncode >= 2:  # 1 = finished with warnings
            raise MergeError(((p.stdout or "") + (p.stderr or "")).strip()[-1500:] or "mkvmerge failed")
        if should_cancel():
            raise Cancelled()
        try:
            os.replace(tmp, out_path)
        except OSError as e:
            raise MergeError(f"couldn't save {os.path.basename(out_path)}: {e.strerror or e}. "
                             f"If that file is open in a video player, close it and try again.")
        if subs_dir:
            step("Saving subtitle files")
            try:
                result["subtitles"] = export_subtitles(out_path, subs_dir)
                result["notes"] += result["subtitles"]["notes"]
            except (MergeError, OSError) as e:      # the video itself is fine; report and go on
                result["notes"].append(f"Couldn't save the subtitle files: {e}")
        return result
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        shutil.rmtree(work, ignore_errors=True)


def _track_opts(t):
    return ["--language", f"0:{t['lang']}", "--track-name", f"0:{t['name'] or lang_name(t['lang'])}"]


def _pick_reference(base_path, bi, bmap, donor_path, di, dmap):
    """Choose the audio pair (same language where possible) that matches best."""
    ba = [t for t in bi["tracks"] if t["type"] == "audio"]
    da = [t for t in di["tracks"] if t["type"] == "audio"]
    if not ba or not da:
        raise MergeError("both versions need at least one audio track so they can be lined up")
    pairs = [(b, d) for b in ba for d in da if b["lang"] == d["lang"] and b["lang"] not in ("und", "zxx")]
    pairs = pairs or [(b, d) for b in ba[:2] for d in da[:3]]
    cache, best = {}, None

    def pcm(path, idx):
        if (path, idx) not in cache:
            cache[(path, idx)] = load_pcm(path, idx)
        return cache[(path, idx)]

    for b, d in pairs[:6]:
        r = pcm(base_path, bmap[b["id"]]["index"])
        o = pcm(donor_path, dmap[d["id"]]["index"])
        dur = len(r) / SR
        win = min(20.0, dur * 0.15)
        sc = sum(best_lag(r, o, f * dur, win, -120, 120)[1] for f in (0.3, 0.6))
        if best is None or sc > best[0]:
            best = (sc, b, d, r, o)
    return best[1], best[2], best[3], best[4]
