"""End-to-end self-test with synthetic videos whose correct answer is known.

Used by the test suite and by packaged builds (`--self-test`), so every installer is
checked with the exact ffmpeg / mkvmerge it ships with.
"""
import json
import os
import shutil
import tempfile
import time
import traceback
import wave

import numpy as np

from . import engine, system

RATE = 48000
BASE_LEN = 90.0


def _signal(seed, seconds):
    """Speech-like test audio: noise with a randomly varying loudness every 0.25 s."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    x = rng.standard_normal(n)
    blocks = int(np.ceil(seconds * 4))
    env = np.repeat(rng.uniform(0.05, 1.0, blocks), RATE // 4)[:n]
    return (x * env * 6000).clip(-32000, 32000).astype(np.int16)


def _cut(x, a, b):
    return x[int(a * RATE): int(b * RATE)]


def _wav(path, x):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(x.tobytes())


def _srt(path, events):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for i, (t, text) in enumerate(events, 1):
            fh.write(f"{i}\n{engine._srt_f(t)} --> {engine._srt_f(t + 2)}\n{text}\n\n")


def _ffmpeg(args):
    p = system.run([engine.tool("ffmpeg"), "-v", "error", "-y"] + args)
    if p.returncode:
        raise RuntimeError("ffmpeg failed: " + (p.stderr or "")[-800:])


def _make_video(path, seconds, audio_tracks, sub=None):
    """audio_tracks: list of (wav_path, language, codec)."""
    args = ["-f", "lavfi", "-i", f"testsrc2=s=160x90:r=10:d={seconds:.3f}"]
    for wav_path, _, _ in audio_tracks:
        args += ["-i", wav_path]
    if sub:
        args += ["-i", sub]
    args += ["-map", "0:v"]
    for i in range(len(audio_tracks)):
        args += ["-map", f"{i + 1}:a"]
    if sub:
        args += ["-map", f"{len(audio_tracks) + 1}:s"]
    args += ["-c:v", "mpeg4", "-q:v", "12"]
    for i, (_, lang, codec) in enumerate(audio_tracks):
        args += [f"-c:a:{i}", codec, f"-metadata:s:a:{i}", f"language={lang}"]
        if codec == "aac":
            args += [f"-b:a:{i}", "128k"]
    if sub:
        args += ["-c:s", "srt", "-metadata:s:s:0", "language=eng"]
    _ffmpeg(args + [path])


def build_case(folder, kind):
    """Create base.mkv and donor.mkv for a scenario. Returns the expected answer.

    kind = "recut": donor has a 5 s intro, is missing 0.5 s at 45 s, and a 5 s outro
           "shift": donor is the same cut, 3 s later
    """
    jpn = _signal(1, BASE_LEN)            # shared original-language audio
    eng = _signal(2, BASE_LEN)            # the dub, aligned to the base timeline
    _wav(os.path.join(folder, "jpn.wav"), jpn)
    _wav(os.path.join(folder, "eng_ref.wav"), eng)
    base = os.path.join(folder, "base.mkv")
    _make_video(base, BASE_LEN, [(os.path.join(folder, "jpn.wav"), "jpn", "flac")])

    if kind == "recut":
        intro, gap_at, gap, outro = 5.0, 45.0, 0.5, 5.0
        def edit(x, seed):
            return np.concatenate([_signal(seed, intro), _cut(x, 0, gap_at),
                                   _cut(x, gap_at + gap, BASE_LEN), _signal(seed + 1, outro)])
        d_jpn, d_eng = edit(jpn, 10), edit(eng, 20)
        # subtitle cues placed at donor times that correspond to base 20 s and 60 s
        subs = [(20 + intro, "cue at 20"), (60 + intro - gap, "cue at 60")]
        expected = {"offsets": [intro, intro - gap], "boundary": gap_at, "cues": [20.0, 60.0],
                    "skip": (gap_at - 1, gap_at + gap + 1)}
    else:
        lead = 3.0
        d_jpn = np.concatenate([_signal(30, lead), jpn])
        d_eng = np.concatenate([_signal(31, lead), eng])
        subs = [(20 + lead, "cue at 20"), (60 + lead, "cue at 60")]
        expected = {"offsets": [lead], "boundary": None, "cues": [20.0, 60.0], "skip": None}

    _wav(os.path.join(folder, "d_jpn.wav"), d_jpn)
    _wav(os.path.join(folder, "d_eng.wav"), d_eng)
    _srt(os.path.join(folder, "d.srt"), subs)
    donor = os.path.join(folder, "donor.mkv")
    # English first, like many streaming releases
    _make_video(donor, len(d_jpn) / RATE, [(os.path.join(folder, "d_eng.wav"), "eng", "aac"),
                                          (os.path.join(folder, "d_jpn.wav"), "jpn", "aac")],
                sub=os.path.join(folder, "d.srt"))
    return base, donor, expected


def verify(folder, out, result, expected):
    """Check the merged file against the known answer. Returns a list of problems."""
    problems = []
    segs = result["segments"] or []
    offs = [s[2] for s in segs]
    if len(offs) != len(expected["offsets"]) or any(abs(a - b) > 0.015 for a, b in zip(offs, expected["offsets"])):
        problems.append(f"offsets {offs} != expected {expected['offsets']}")
    if expected["boundary"] is not None and len(segs) == 2 and abs(segs[0][1] - expected["boundary"]) > 1.0:
        problems.append(f"section boundary at {segs[0][1]:.2f}s, expected about {expected['boundary']}s")
    if not (result["sync"] and result["sync"]["ok"]):
        problems.append(f"sync check did not pass: {result['sync']}")

    inf = engine.info(out)
    kinds = [(t["type"], t["lang"]) for t in inf["tracks"]]
    want = [("video", "und"), ("audio", "jpn"), ("audio", "eng"), ("subtitles", "eng")]
    if [k[0] for k in kinds] != [w[0] for w in want] or [k[1] for k in kinds][1:] != [w[1] for w in want][1:]:
        problems.append(f"unexpected tracks in output: {kinds}")
        return problems

    # the added English audio must line up with the reference English signal
    streams, _ = engine.ff_streams(out)
    idx = engine.ff_index_map(inf["tracks"], streams)[inf["tracks"][2]["id"]]["index"]
    got = engine.load_pcm(out, idx)
    ref = engine.load_pcm(os.path.join(folder, "eng_ref.wav"), 0)
    errs = []
    for t in np.arange(2.0, BASE_LEN - 8, 4.0):
        if expected["skip"] and expected["skip"][0] - 4 < t < expected["skip"][1]:
            continue
        lag, sc = engine.best_lag(ref, got, float(t), 3.0, -0.5, 0.5)
        if lag is None or sc < 0.8:
            problems.append(f"English audio doesn't match the reference at {t:.0f}s (score {sc:.2f})")
            break
        errs.append(abs(lag) * 1000)
    if errs and max(errs) > 25:
        problems.append(f"English audio off by up to {max(errs):.0f} ms")

    # subtitle cues must land on the base timeline
    srt = os.path.join(folder, "out.srt")
    sub_idx = engine.ff_index_map(inf["tracks"], streams)[inf["tracks"][3]["id"]]["index"]
    _ffmpeg(["-copyts", "-i", out, "-map", f"0:{sub_idx}", "-c:s", "srt", srt])
    with open(srt, encoding="utf-8") as fh:
        starts = [engine._srt_t(line.split("-->")[0]) for line in fh if "-->" in line]
    if len(starts) != len(expected["cues"]) or any(abs(a - b) > 0.015 for a, b in zip(starts, expected["cues"])):
        problems.append(f"subtitle cues at {starts}, expected {expected['cues']}")
    return problems


def run_case(kind, folder):
    base, donor, expected = build_case(folder, kind)
    out = os.path.join(folder, "out.mkv")
    sel = {"base": {"audio": ["jpn"], "subtitles": []},
           "donor": {"audio": ["eng"], "subtitles": ["eng"]}}
    result = engine.merge_episode(base, donor, sel, out, os.path.join(folder, "work"))
    return verify(folder, out, result, expected), result


def run(report_path=None):
    """Run all scenarios. Returns True if everything passed."""
    lines = [f"Kizuna self-test  {time.strftime('%Y-%m-%d %H:%M:%S')}"]
    for name in engine.system.TOOL_NAMES:
        lines.append(f"  {name}: {system.find_tool(name) or 'NOT FOUND'}")
    ok = not system.missing_tools()
    if ok:
        for kind in ("recut", "shift"):
            folder = tempfile.mkdtemp(prefix=f"kizuna-selftest-{kind}-")
            try:
                t0 = time.time()
                problems, result = run_case(kind, folder)
                status = "PASS" if not problems else "FAIL"
                lines.append(f"{status} {kind}: segments={json.dumps(result['segments'])} "
                             f"sync={json.dumps(result['sync'])} ({time.time() - t0:.1f}s)")
                lines += [f"    - {p}" for p in problems]
                ok = ok and not problems
            except Exception:  # noqa: BLE001 - report everything
                ok = False
                lines.append(f"FAIL {kind}: exception\n" + traceback.format_exc())
            finally:
                shutil.rmtree(folder, ignore_errors=True)
    lines.append("RESULT: " + ("PASS" if ok else "FAIL"))
    text = "\n".join(lines) + "\n"
    if report_path:
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        try:
            print(text)
        except Exception:  # noqa: BLE001 - no console in windowed builds
            pass
    return ok
