"""Local server: security checks and a full merge through the API."""
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from kizuna import selftest, server, system


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "data_dir", lambda: str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    srv, url = server.make_server()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = url.split("/?t=")[0]
    yield base, server.TOKEN
    srv.shutdown()


def call(base, path, body=None, token=None, host=None):
    req = urllib.request.Request(base + path, data=json.dumps(body or {}).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Token", token)
    if host:
        req.add_header("Host", host)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def test_rejects_missing_or_wrong_token(app):
    base, _ = app
    for tok in (None, "wrong"):
        with pytest.raises(urllib.error.HTTPError) as e:
            call(base, "/api/info", token=tok)
        assert e.value.code == 403


def test_rejects_foreign_host_header(app):
    base, token = app
    with pytest.raises(urllib.error.HTTPError) as e:
        call(base, "/api/info", token=token, host="evil.example:80")
    assert e.value.code == 403


def test_serves_page(app):
    base, _ = app
    with urllib.request.urlopen(base + "/", timeout=5) as r:
        html = r.read().decode()
        assert "Kizuna" in html and r.headers["Content-Security-Policy"]


def test_info(app):
    base, token = app
    r = call(base, "/api/info", token=token)
    assert r["version"] and isinstance(r["missing"], list)


def test_start_validation(app, tmp_path):
    base, token = app
    d = str(tmp_path)
    r = call(base, "/api/start", {"base": d, "donor": d, "out": d + "/x", "sel": {}, "episodes": [{"ep": 1, "base": "a"}]},
             token=token)
    assert "error" in r


@pytest.mark.skipif(bool(system.missing_tools()), reason="tools not installed")
def test_full_merge_through_api(app, tmp_path):
    base_url, token = app
    a, b, out = tmp_path / "A", tmp_path / "B", tmp_path / "out"
    a.mkdir()
    b.mkdir()
    work = tmp_path / "gen"
    work.mkdir()
    base_file, donor_file, _ = selftest.build_case(str(work), "recut")
    (work / "base.mkv").rename(a / "Show - S01E01.mkv")
    (work / "donor.mkv").rename(b / "[Grp] Show - 01.mkv")

    sa = call(base_url, "/api/scan", {"path": str(a)}, token=token)
    sb = call(base_url, "/api/scan", {"path": str(b)}, token=token)
    pr = call(base_url, "/api/pair", {"a": sa["files"], "b": sb["files"]}, token=token)
    assert pr["rows"] == [{"ep": 1, "a": "Show - S01E01.mkv", "b": "[Grp] Show - 01.mkv"}]

    body = {"base": str(a), "donor": str(b), "out": str(out),
            "sel": {"base": {"audio": ["jpn"], "subtitles": []}, "donor": {"audio": ["eng"], "subtitles": ["eng"]}},
            "episodes": [{"ep": 1, "base": "Show - S01E01.mkv", "donor": "[Grp] Show - 01.mkv"}],
            "export_subs": True}
    assert call(base_url, "/api/start", body, token=token) == {"ok": True}
    for _ in range(240):
        job = call(base_url, "/api/status", token=token)["job"]
        if not job["running"]:
            break
        time.sleep(0.5)
    item = job["items"][0]
    assert item["state"] == "done", item
    assert (out / "Show - S01E01.mkv").exists()
    assert "EPISODE 1: DONE" in (out / "Kizuna log.txt").read_text(encoding="utf-8").upper()
    assert "1 subtitle file saved" in item["detail"]
    assert (out / "Subtitles for upload" / "Show - S01E01.en.srt").exists()
    info = (out / "Subtitles for upload" / "Upload info.txt").read_text(encoding="utf-8")
    assert "Show - S01E01.mkv" in info and "Movie hash: " in info and "Show - S01E01.en.srt" in info

    # running again asks before overwriting
    assert call(base_url, "/api/start", body, token=token) == {"confirm_overwrite": 1}


def test_theme_is_saved_and_served(app):
    base, token = app
    with urllib.request.urlopen(base + "/", timeout=5) as r:
        assert b'data-saved-theme=""' in r.read()            # nothing chosen yet: follow the system
    assert call(base, "/api/settings", {"theme": "kon"}, token=token)["settings"]["theme"] == "kon"
    with urllib.request.urlopen(base + "/", timeout=5) as r:
        assert b'data-saved-theme="kon"' in r.read()
    assert call(base, "/api/settings", {"theme": "urushi"}, token=token)["settings"]["theme"] == "urushi"
    assert "error" in call(base, "/api/settings", {"theme": "neon"}, token=token)



def test_last_setup_and_update_setting(app):
    base, token = app
    last = {"A": {"path": "/x", "video": True, "audio": ["jpn"], "subtitles": []}, "out": "/y", "export": True}
    call(base, "/api/settings", {"last": last}, token=token)
    info = call(base, "/api/info", token=token)
    assert info["last"] == last and info["check_updates"] is True and info["repo"]
    call(base, "/api/settings", {"check_updates": False}, token=token)
    assert call(base, "/api/info", token=token)["check_updates"] is False
    assert "error" in call(base, "/api/settings", {"last": "not a dict"}, token=token)


def test_update_check(monkeypatch, tmp_path):
    import io
    monkeypatch.setattr(system, "data_dir", lambda: str(tmp_path))
    replies = {"tag": "v99.0.0"}

    class Reply(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        assert req.full_url.endswith(f"/repos/{server.REPO}/releases/latest")
        return Reply(json.dumps({"tag_name": replies["tag"], "html_url": "https://example/rel"}).encode())

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)
    server.state["update"] = None
    server.check_for_update()
    assert server.state["update"] == {"version": "99.0.0", "url": "https://example/rel"}
    server.state["update"] = None
    replies["tag"] = "v" + server.__version__
    server.check_for_update()
    assert server.state["update"] is None                  # same version: nothing to show
    server.save_settings({"check_updates": False})
    replies["tag"] = "v99.0.0"
    server.check_for_update()
    assert server.state["update"] is None                  # turned off: no check


def _wait(base_url, token):
    for _ in range(400):
        job = call(base_url, "/api/status", token=token)["job"]
        if not job["running"]:
            return job
        time.sleep(0.5)
    raise AssertionError("job did not finish")


@pytest.mark.skipif(bool(system.missing_tools()), reason="tools not installed")
def test_parallel_job_defaults_and_preview(app, tmp_path, monkeypatch):
    base_url, token = app
    monkeypatch.setattr(server, "worker_count", lambda n: 2)
    opened = []
    monkeypatch.setattr(system, "reveal", lambda p: opened.append(p))
    a, b, out = tmp_path / "A", tmp_path / "B", tmp_path / "out"
    a.mkdir()
    b.mkdir()
    for ep in (1, 2, 3):
        g = tmp_path / f"gen{ep}"
        g.mkdir()
        selftest.build_case(str(g), "recut")
        (g / "base.mkv").rename(a / f"Show - S01E0{ep}.mkv")
        (g / "donor.mkv").rename(b / f"[Grp] Show - 0{ep}.mkv")
    eps = [{"ep": n, "base": f"Show - S01E0{n}.mkv", "donor": f"[Grp] Show - 0{n}.mkv"} for n in (1, 2, 3)]
    body = {"base": str(a), "donor": str(b), "out": str(out), "episodes": eps,
            "sel": {"base": {"audio": ["jpn"], "subtitles": []}, "donor": {"audio": ["eng"], "subtitles": ["eng"]}},
            "defaults": {"audio": {"from": "donor", "lang": "eng"}, "subtitles": None}}
    assert call(base_url, "/api/start", body, token=token) == {"ok": True}
    job = _wait(base_url, token)
    assert job["workers"] == 2
    assert [i["state"] for i in job["items"]] == ["done"] * 3, job["items"]
    assert all(i["started_at"] and i["finished_at"] >= i["started_at"] and i["preview"] for i in job["items"])
    r = call(base_url, "/api/preview", {"ep": 2}, token=token)
    assert r["ok"] and opened == [r["path"]] and r["path"].endswith("Preview - Show - S01E02.mkv")
    log_text = (out / "Kizuna log.txt").read_text(encoding="utf-8")
    assert all(f"Episode {n}: DONE" in log_text for n in (1, 2, 3))


@pytest.mark.skipif(bool(system.missing_tools()), reason="tools not installed")
def test_one_folder_job(app, tmp_path):
    base_url, token = app
    a, out = tmp_path / "A", tmp_path / "out"
    a.mkdir()
    g = tmp_path / "gen"
    g.mkdir()
    selftest.build_case(str(g), "shift")
    (g / "donor.mkv").rename(a / "Show - S01E01.mkv")          # eng + jpn audio, eng subs
    body = {"base": str(a), "donor": "", "out": str(out),
            "episodes": [{"ep": 1, "base": "Show - S01E01.mkv", "donor": None}],
            "sel": {"base": {"audio": ["eng"], "subtitles": []}, "donor": {"audio": [], "subtitles": []}}}
    assert call(base_url, "/api/start", body, token=token) == {"ok": True}
    job = _wait(base_url, token)
    assert job["items"][0]["state"] == "done", job["items"][0]
    from kizuna import engine
    assert [(t["type"], t["lang"]) for t in engine.info(str(out / "Show - S01E01.mkv"))["tracks"]][1:] == [("audio", "eng")]
