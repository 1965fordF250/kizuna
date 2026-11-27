"""Fast unit tests (no ffmpeg needed)."""
import numpy as np
import pytest

from kizuna import engine


@pytest.mark.parametrize("name,ep", [
    ("Some Show - S05E04 - CUSTOM REPACK MULTi 1080p 10bits BluRay x265-Group.mkv", 4),
    ("[Group] Some Show Part 6 - Subtitle Here - 38.mkv", 38),
    ("[SubsPlease] Frieren - 12 (1080p) [ABCD1234].mkv", 12),
    ("Show.Name.2019.S01E07.1080p.WEB-DL.DDP5.1.H.264.mkv", 7),
    ("Show Name Episode 3.mp4", 3),
    ("Show Name - Ep.11 [720p].mkv", 11),
    ("[Group] Show - 05v2 [BD 1080p HEVC FLAC].mkv", 5),
    ("Show 1x09 720p.mkv", 9),
    ("show.s02e110.mkv", 110),
    ("Movie 1920x1080.mkv", None),
    ("no numbers here.mkv", None),
])
def test_episode_number(name, ep):
    assert engine.episode_number(name) == ep


def test_pair_by_number_and_missing():
    a = [{"name": "A - 01.mkv", "ep": 1}, {"name": "A - 02.mkv", "ep": 2}, {"name": "A - 03.mkv", "ep": 3}]
    b = [{"name": "B S01E02.mkv", "ep": 2}, {"name": "B S01E01.mkv", "ep": 1}]
    rows, by_number = engine.pair(a, b)
    assert by_number
    assert rows == [{"ep": 1, "a": "A - 01.mkv", "b": "B S01E01.mkv"},
                    {"ep": 2, "a": "A - 02.mkv", "b": "B S01E02.mkv"},
                    {"ep": 3, "a": "A - 03.mkv", "b": None}]


def test_pair_falls_back_to_order_when_numbers_ambiguous():
    a = [{"name": "x.mkv", "ep": None}, {"name": "y.mkv", "ep": None}]
    b = [{"name": "p.mkv", "ep": 5}, {"name": "q.mkv", "ep": 5}]
    rows, by_number = engine.pair(a, b)
    assert not by_number
    assert [(r["a"], r["b"]) for r in rows] == [("x.mkv", "p.mkv"), ("y.mkv", "q.mkv")]


# donor has a 13 s intro; base loses 0.5 s at 600 s (Blu-ray vs streaming style)
SEGS = [[0.0, 600.0, 13.0], [600.0, 1400.0, 12.5]]


def test_ranges_and_mapping():
    rng = engine.donor_ranges(SEGS)
    assert rng[0][3:] == (13.0, 612.5)          # overlap handed to the later segment
    assert rng[1][3:] == (612.5, 1412.5)
    assert engine.to_base(5.0, rng) is None     # inside the streaming intro
    assert engine.to_base(13.0, rng) == 0.0
    assert engine.to_base(100.0, rng) == pytest.approx(87.0)
    assert engine.to_base(700.0, rng) == pytest.approx(687.5)
    assert engine.to_base(1450.0, rng) is None  # streaming credits


def test_mapping_with_skipped_donor_content():
    segs = [[0.0, 100.0, 5.0], [100.0, 200.0, 8.0]]   # donor has 3 s extra at the break
    rng = engine.donor_ranges(segs)
    assert engine.to_base(106.0, rng) == 100.0       # the extra donor bit snaps to the boundary
    assert engine.to_base(110.0, rng) == pytest.approx(102.0)


def test_build_graph_shape():
    g = engine.build_graph(engine.donor_ranges(SEGS), 3)
    assert g.count("atrim") == 2 and g.endswith("concat=n=2:v=0:a=1[aout]")
    assert "[0:3]" in g and "apad=whole_dur=" in g


def test_retime_srt(tmp_path):
    src = tmp_path / "in.srt"
    src.write_text("1\n00:00:05,000 --> 00:00:06,000\nintro line\n\n"
                   "2\n00:00:20,000 --> 00:00:22,500\nhello\nsecond line\n\n"
                   "3\n00:11:00,000 --> 00:11:01,000\nafter break\n", encoding="utf-8")
    dst = tmp_path / "out.srt"
    engine.retime_srt(str(src), str(dst), engine.donor_ranges(SEGS))
    out = dst.read_text(encoding="utf-8")
    assert "intro line" not in out
    assert "1\n00:00:07,000 --> 00:00:09,500\nhello\nsecond line" in out
    assert "2\n00:10:47,500 --> 00:10:48,500\nafter break" in out


def test_retime_ass_keeps_header_and_styles(tmp_path):
    src = tmp_path / "in.ass"
    src.write_text("﻿[Script Info]\nPlayResX: 1920\n\n[V4+ Styles]\nStyle: Default,Arial,70\n\n[Events]\n"
                   "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                   "Dialogue: 0,0:00:20.00,0:00:22.00,Default,,0,0,0,,Hello, world\n"
                   "Comment: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,intro note\n", encoding="utf-8")
    dst = tmp_path / "out.ass"
    engine.retime_ass(str(src), str(dst), engine.donor_ranges(SEGS))
    out = dst.read_text(encoding="utf-8")
    assert out.startswith("[Script Info]") and "Style: Default,Arial,70" in out
    assert "Dialogue: 0,0:00:07.00,0:00:09.00,Default,,0,0,0,,Hello, world" in out
    assert "intro note" not in out


def test_best_lag_finds_known_offset():
    rng = np.random.default_rng(0)
    ref = rng.standard_normal(engine.SR * 60).astype(np.float32) * 1000
    oth = np.concatenate([rng.standard_normal(int(engine.SR * 7.25)).astype(np.float32) * 1000, ref])
    lag, score = engine.best_lag(ref, oth, 20.0, 8.0, 0, 15)
    assert lag == pytest.approx(7.25, abs=1 / engine.SR) and score > 0.99


def test_compute_map_piecewise():
    rng = np.random.default_rng(1)
    sr = engine.SR
    ref = (rng.standard_normal(sr * 300) * 1000).astype(np.float32)
    intro = (rng.standard_normal(sr * 10) * 1000).astype(np.float32)
    # donor: 10 s intro, then ref with 0.5 s missing at 150 s
    oth = np.concatenate([intro, ref[: 150 * sr], ref[int(150.5 * sr):]])
    m = engine.compute_map(ref, oth)
    offs = [round(s[2], 3) for s in m["segments"]]
    assert offs == [10.0, 9.5]
    assert abs(m["segments"][0][1] - 150.0) < 1.0


def test_compute_map_rejects_unrelated_audio():
    rng = np.random.default_rng(2)
    a = (rng.standard_normal(engine.SR * 120) * 1000).astype(np.float32)
    b = (rng.standard_normal(engine.SR * 120) * 1000).astype(np.float32)
    m = engine.compute_map(a, b)
    assert m is None or m["points"] < engine.MIN_MATCH_POINTS


def test_audio_codec_args():
    assert engine.audio_codec_args({"codec_name": "flac"}) == ["-c:a", "flac"]
    assert engine.audio_codec_args({"codec_name": "pcm_s24le"}) == ["-c:a", "flac"]
    assert engine.audio_codec_args({"codec_name": "aac", "channels": 2, "bit_rate": "128000"}) == ["-c:a", "aac", "-b:a", "192k"]
    assert engine.audio_codec_args({"codec_name": "eac3", "channels": 6, "tags": {"BPS": "640000"}}) == ["-c:a", "aac", "-b:a", "640k"]


# ---------------------------------------------------------------- subtitle export naming / hash
def _t(lang, name="", forced=False, hi=False):
    return {"lang": lang, "name": name, "forced": forced, "hi": hi}


def test_subtitle_filenames():
    used = set()
    names = [engine.subtitle_filename("Show - S01E01", t, ext, used) for t, ext in [
        (_t("eng", "English Dub"), "srt"),          # order must not decide who gets the plain name
        (_t("eng", "English"), "srt"),
        (_t("eng", "English (Forced)"), "srt"),
        (_t("eng", "English SDH"), "srt"),
        (_t("eng", "English Dub"), "srt"),
        (_t("eng", "English [Netflix]"), "srt"),
        (_t("eng", "Signs & Songs"), "ass"),
        (_t("eng", "Dialogue"), "ass"),
        (_t("fre", "", forced=True), "ass"),
        (_t("und"), "srt"),
    ]]
    assert names == [
        "Show - S01E01.en.dub.srt",
        "Show - S01E01.en.srt",
        "Show - S01E01.en.forced.srt",
        "Show - S01E01.en.sdh.srt",
        "Show - S01E01.en.dub.2.srt",
        "Show - S01E01.en.netflix.srt",
        "Show - S01E01.en.forced.ass",
        "Show - S01E01.en.ass",
        "Show - S01E01.fr.forced.ass",
        "Show - S01E01.und.srt",
    ]


@pytest.mark.parametrize("lang,name,expected", [
    ("fre", "FR Complets", "x.fr.ass"),
    ("fre", "FR Forcés", "x.fr.forced.ass"),
    ("spa", "Español (Forzados)", "x.es.forced.ass"),
    ("ger", "Deutsch Untertitel", "x.de.ass"),
    ("eng", "English Hearing Impaired", "x.en.sdh.ass"),
    ("eng", "Commentary by the director", "x.en.commentary-by-director.ass"),
])
def test_subtitle_names_in_other_languages(lang, name, expected):
    assert engine.subtitle_filename("x", _t(lang, name), "ass", set()) == expected


def test_full_subtitles_with_signs_are_not_forced():
    assert engine._sub_flags(_t("eng", "Full Subtitles (Dialogue + Signs)")) == (False, False)
    assert engine._sub_flags(_t("eng", "Signs")) == (True, False)
    assert engine._sub_flags(_t("eng", "English CC")) == (False, True)


def _reference_hash(path):
    """Straightforward pure-Python version of the published OpenSubtitles algorithm."""
    import os
    import struct
    size = os.path.getsize(path)
    h = size
    with open(path, "rb") as f:
        for off in (0, size - 65536):
            f.seek(off)
            for (v,) in struct.iter_unpack("<Q", f.read(65536)):
                h = (h + v) & 0xFFFFFFFFFFFFFFFF
    return "%016x" % h


def test_opensubtitles_hash(tmp_path):
    rng = np.random.default_rng(5)
    for size in (131072, 500_003, 2_000_000):
        p = tmp_path / f"f{size}.bin"
        p.write_bytes(rng.integers(0, 256, size, dtype=np.uint8).tobytes())
        assert engine.opensubtitles_hash(str(p)) == _reference_hash(str(p))
    small = tmp_path / "small.bin"
    small.write_bytes(b"x" * 1000)
    assert engine.opensubtitles_hash(str(small)) is None


# ---------------------------------------------------------------- play-by-default choice
def _tr(i, kind, lang, name="", forced=False):
    return {"id": i, "type": kind, "lang": lang, "name": name, "forced": forced, "hi": False}


def test_pick_defaults():
    b_audio, b_subs = [_tr(1, "audio", "jpn")], [_tr(3, "subtitles", "fre")]
    d_audio = [_tr(1, "audio", "eng")]
    d_subs = [_tr(4, "subtitles", "eng", "English SDH"), _tr(5, "subtitles", "eng", "English"),
              _tr(6, "subtitles", "eng", "Signs & Songs")]
    pick = lambda d: engine.pick_defaults(b_audio, b_subs, d_audio, d_subs, d)
    assert pick(None) == {"audio": None, "subtitles": None, "forced": False}
    r = pick({"audio": {"from": "donor", "lang": "eng"}, "subtitles": {"from": "donor", "lang": "eng", "kind": "full"}})
    assert r == {"audio": ("donor", 1), "subtitles": ("donor", 5), "forced": False}   # non-SDH preferred
    r = pick({"audio": None, "subtitles": {"from": "donor", "lang": "eng", "kind": "forced"}})
    assert r["subtitles"] == ("donor", 6) and r["forced"]
    assert pick({"subtitles": "none"})["subtitles"] == "none"
    # a language this episode doesn't have: keep the files' own flags
    assert pick({"audio": {"from": "donor", "lang": "spa"}})["audio"] is None


def test_version_compare_and_workers(monkeypatch):
    from kizuna import server
    vt = server.version_tuple
    assert vt("v2.0.0") == (2, 0, 0) and vt("2.1") == (2, 1, 0) and vt("v10.0.1-beta") == (10, 0, 1)
    assert vt("v2.0.10") > vt("v2.0.9") > vt("1.9.9")
    monkeypatch.setattr(server.os, "cpu_count", lambda: 8)
    assert server.worker_count(10) == 2 and server.worker_count(1) == 1
    monkeypatch.setattr(server.os, "cpu_count", lambda: 4)
    assert server.worker_count(10) == 1


def test_settings_carried_over_from_old_name(tmp_path):
    from kizuna import system
    old, new = tmp_path / "Track Merger", tmp_path / "Kizuna"
    old.mkdir()
    (old / "settings.json").write_text('{"theme": "dark"}', encoding="utf-8")
    (old / "log.txt").write_text("old log", encoding="utf-8")
    system._carry_over_settings(str(old), str(new))
    assert (new / "settings.json").read_text(encoding="utf-8") == '{"theme": "dark"}'
    assert not (new / "log.txt").exists()
    system._carry_over_settings(str(tmp_path / "missing"), str(tmp_path / "fresh"))
    assert not (tmp_path / "fresh").exists()
