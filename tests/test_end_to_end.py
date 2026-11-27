"""End-to-end merges on synthetic videos (needs ffmpeg, ffprobe and mkvmerge)."""
import json
import os

import pytest

from kizuna import engine, selftest, system

needs_tools = pytest.mark.skipif(bool(system.missing_tools()),
                                 reason="ffmpeg / ffprobe / mkvmerge not installed")


@needs_tools
@pytest.mark.parametrize("kind", ["recut", "shift"])
def test_merge_scenario(kind, tmp_path):
    problems, result = selftest.run_case(kind, str(tmp_path))
    assert problems == [], problems
    assert not os.path.exists(str(tmp_path / "out.mkv.partial.mkv"))
    assert not os.path.exists(str(tmp_path / "work"))      # temporary files cleaned up


@needs_tools
def test_unrelated_videos_are_refused(tmp_path):
    base, _, _ = selftest.build_case(str(tmp_path), "shift")
    other = tmp_path / "other"
    other.mkdir()
    selftest._wav(str(other / "x.wav"), selftest._signal(99, 90))
    unrelated = str(other / "unrelated.mkv")
    selftest._make_video(unrelated, 90, [(str(other / "x.wav"), "jpn", "aac")])
    sel = {"base": {"audio": ["jpn"], "subtitles": []}, "donor": {"audio": ["jpn"], "subtitles": []}}
    out = str(tmp_path / "out.mkv")
    with pytest.raises(engine.MergeError):
        engine.merge_episode(base, unrelated, sel, out, str(tmp_path / "work"))
    assert not os.path.exists(out) and not os.path.exists(out + ".partial.mkv")


@needs_tools
def test_cancel_leaves_no_files(tmp_path):
    base, donor, _ = selftest.build_case(str(tmp_path), "recut")
    sel = {"base": {"audio": ["jpn"], "subtitles": []}, "donor": {"audio": ["eng"], "subtitles": ["eng"]}}
    out = str(tmp_path / "out.mkv")
    calls = {"n": 0}

    def cancel_after_two():
        calls["n"] += 1
        return calls["n"] > 2

    with pytest.raises(engine.Cancelled):
        engine.merge_episode(base, donor, sel, out, str(tmp_path / "work"), should_cancel=cancel_after_two)
    assert not os.path.exists(out) and not os.path.exists(out + ".partial.mkv")


@needs_tools
def test_base_only_selection_drops_unticked_tracks(tmp_path):
    base, donor, _ = selftest.build_case(str(tmp_path), "shift")
    sel = {"base": {"audio": [], "subtitles": []}, "donor": {"audio": ["eng"], "subtitles": []}}
    out = str(tmp_path / "out.mkv")
    engine.merge_episode(base, donor, sel, out, str(tmp_path / "work"))
    kinds = [(t["type"], t["lang"]) for t in engine.info(out)["tracks"]]
    assert kinds[1:] == [("audio", "eng")]


@needs_tools
def test_scan_folder(tmp_path):
    selftest.build_case(str(tmp_path), "shift")
    (tmp_path / "notes.txt").write_text("not a video")
    (tmp_path / "broken.mkv").write_bytes(b"not really a video")
    s = engine.scan(str(tmp_path))
    names = sorted(f["name"] for f in s["files"])
    assert names == ["base.mkv", "donor.mkv"]
    assert [e["name"] for e in s["errors"]] == ["broken.mkv"]
    assert {g["key"] for g in s["audio"]} == {"jpn", "eng"}
    assert json.dumps(s)  # serialisable for the web page


@needs_tools
def test_merge_with_subtitle_export(tmp_path):
    base, donor, expected = selftest.build_case(str(tmp_path), "recut")
    sel = {"base": {"audio": ["jpn"], "subtitles": []}, "donor": {"audio": ["eng"], "subtitles": ["eng"]}}
    out = str(tmp_path / "Show - S01E01.mkv")
    subs = tmp_path / "Subtitles"
    r = engine.merge_episode(base, donor, sel, out, str(tmp_path / "work"), subs_dir=str(subs))
    assert r["subtitles"]["files"] == ["Show - S01E01.en.srt"]
    assert r["subtitles"]["hash"] == engine.opensubtitles_hash(out) and r["subtitles"]["fps"] == 10.0
    text = (subs / "Show - S01E01.en.srt").read_text(encoding="utf-8")
    starts = [engine._srt_t(l.split("-->")[0]) for l in text.splitlines() if "-->" in l]
    assert [round(s, 2) for s in starts] == expected["cues"]     # synced to the merged video
    assert not list(subs.glob("*.part"))


@needs_tools
def test_export_ass_srt_and_forced(tmp_path):
    srt = tmp_path / "in.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nhello\n", encoding="utf-8")
    video = str(tmp_path / "Clip.mkv")
    selftest._ffmpeg(["-f", "lavfi", "-i", "testsrc2=s=160x90:r=10:d=3", "-i", str(srt), "-i", str(srt),
                      "-map", "0:v", "-map", "1:s", "-map", "2:s", "-c:v", "mpeg4",
                      "-c:s:0", "ass", "-c:s:1", "srt",
                      "-metadata:s:s:0", "language=eng", "-metadata:s:s:0", "title=Dialogue",
                      "-metadata:s:s:1", "language=spa", "-disposition:s:1", "forced", video])
    r = engine.export_subtitles(video, str(tmp_path / "out"))
    assert r["files"] == ["Clip.en.ass", "Clip.es.forced.srt"]
    assert (tmp_path / "out" / "Clip.en.ass").read_text(encoding="utf-8").lstrip("﻿").startswith("[Script Info]")
    assert "hello" in (tmp_path / "out" / "Clip.es.forced.srt").read_text(encoding="utf-8")
    assert r["hash"] is None      # file smaller than 128 KiB


def _flags(path):
    import subprocess
    d = json.loads(subprocess.run([system.find_tool("mkvmerge"), "-J", path], capture_output=True, text=True).stdout)
    return [(t["type"], t["properties"].get("language"), t["properties"].get("default_track"),
             t["properties"].get("forced_track", False)) for t in d["tracks"]]


@needs_tools
@pytest.mark.parametrize("kind", ["recut", "shift"])
def test_play_by_default_flags(kind, tmp_path):
    base, donor, _ = selftest.build_case(str(tmp_path), kind)
    sel = {"base": {"audio": ["jpn"], "subtitles": []}, "donor": {"audio": ["eng"], "subtitles": ["eng"]}}
    out = str(tmp_path / "out.mkv")
    defaults = {"audio": {"from": "donor", "lang": "eng"}, "subtitles": {"from": "donor", "lang": "eng", "kind": "full"}}
    engine.merge_episode(base, donor, sel, out, str(tmp_path / "work"), defaults=defaults)
    f = _flags(out)
    assert f[1][:3] == ("audio", "jpn", False)       # original audio no longer the default
    assert f[2][:3] == ("audio", "eng", True)        # the dub plays first
    assert f[3][:3] == ("subtitles", "eng", True)
    # without a choice, added tracks stay off
    out2 = str(tmp_path / "out2.mkv")
    engine.merge_episode(base, donor, sel, out2, str(tmp_path / "work"))
    assert [x[2] for x in _flags(out2)[2:]] == [False, False]


@needs_tools
@pytest.mark.parametrize("kind", ["recut", "shift"])
def test_choice_overrides_forced_flags_from_the_source(kind, tmp_path):
    """Blu-ray dubs often carry a "forced" flag, and VLC plays a forced track over the default
    one, so a choice must clear forced flags left over from the source files."""
    import subprocess
    base, donor, _ = selftest.build_case(str(tmp_path), kind)
    mkv = system.find_tool("mkvmerge")
    forced_base = str(tmp_path / "forced_base.mkv")
    a_id = [t["id"] for t in engine.info(base)["tracks"] if t["type"] == "audio"][0]
    subprocess.run([mkv, "-q", "-o", forced_base, "--forced-display-flag", f"{a_id}:yes", base], check=True)
    assert _flags(forced_base)[1][3] is True
    sel = {"base": {"audio": ["jpn"], "subtitles": []}, "donor": {"audio": ["eng"], "subtitles": ["eng"]}}

    out = str(tmp_path / "out.mkv")
    defaults = {"audio": {"from": "donor", "lang": "eng"}, "subtitles": {"from": "donor", "lang": "eng", "kind": "full"}}
    engine.merge_episode(forced_base, donor, sel, out, str(tmp_path / "work"), defaults=defaults)
    assert _flags(out)[1:] == [("audio", "jpn", False, False), ("audio", "eng", True, False),
                               ("subtitles", "eng", True, False)]

    # choosing "no subtitles" clears every subtitle flag; audio left as in the files keeps its own
    out2 = str(tmp_path / "out2.mkv")
    engine.merge_episode(forced_base, donor, sel, out2, str(tmp_path / "work"),
                         defaults={"audio": None, "subtitles": "none"})
    f = _flags(out2)
    assert f[1][3] is True and f[3][2:] == (False, False)


@needs_tools
def test_one_folder_clean_up(tmp_path):
    _, donor, _ = selftest.build_case(str(tmp_path), "recut")     # has eng + jpn audio, eng subs
    sel = {"base": {"audio": ["jpn"], "subtitles": []}, "donor": {"audio": [], "subtitles": []}}
    out = str(tmp_path / "clean.mkv")
    r = engine.merge_episode(donor, None, sel, out, str(tmp_path / "work"))
    assert r["segments"] is None and r["preview"] is None
    assert [(t["type"], t["lang"]) for t in engine.info(out)["tracks"]][1:] == [("audio", "jpn")]


@needs_tools
def test_preview_clip(tmp_path):
    base, donor, expected = selftest.build_case(str(tmp_path), "recut")
    sel = {"base": {"audio": ["jpn"], "subtitles": []}, "donor": {"audio": ["eng"], "subtitles": ["eng"]}}
    out = str(tmp_path / "out.mkv")
    r = engine.merge_episode(base, donor, sel, out, str(tmp_path / "work"))
    assert abs(r["preview"]["at"] - expected["boundary"]) < 1.0      # at the point where timing changed
    clip = engine.make_preview(out, r["preview"], str(tmp_path / "clip.mkv"))
    streams, _ = engine.ff_streams(clip)
    audio = [s for s in streams if s["codec_type"] == "audio"]
    assert [s["disposition"]["default"] for s in audio] == [0, 1]      # added English audio switched on
    dur = float(json.loads(system.run([system.find_tool("ffprobe"), "-v", "error", "-show_entries",
                                       "format=duration", "-of", "json", clip]).stdout)["format"]["duration"])
    assert 15 < dur < 25
