"""Local web app server.

Listens on 127.0.0.1 only. Every API call must carry the random per-launch token from
the page URL and a matching Host header, so other websites can't drive it. The server
opens the browser on start, and quits after 15 idle minutes (never during a merge).
"""
import json
import logging
import logging.handlers
import os
import secrets
import shutil
import socket
import threading
import time
import traceback
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import APP_NAME, REPO, __version__, engine, system

IDLE_LIMIT = 15 * 60
TOKEN = secrets.token_urlsafe(18)
log = logging.getLogger("kizuna")

state = {"last_seen": time.time(), "job": None, "previews": {}, "update": None}
lock = threading.Lock()


def _setup_logging():
    handler = logging.handlers.RotatingFileHandler(
        os.path.join(system.data_dir(), "log.txt"), maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


# ------------------------------------------------------------------ jobs
def _norm(p):
    return os.path.normcase(os.path.abspath(os.path.expanduser(p.strip())))


def worker_count(n_items):
    """Episodes processed at the same time: 2 on machines with 6+ CPU cores, else 1."""
    return 2 if (os.cpu_count() or 1) >= 6 and n_items > 1 else 1


def start_job(body):
    with lock:
        if state["job"] and state["job"]["running"]:
            raise ValueError("A merge is already running.")
    missing = system.missing_tools()
    if missing:
        raise ValueError("Missing required tools: " + ", ".join(missing))
    base_dir = body["base"]
    donor_dir = body.get("donor") or ""          # empty: one-folder clean-up mode
    out_dir = os.path.abspath(os.path.expanduser(body["out"].strip()))
    if not os.path.isdir(base_dir) or (donor_dir and not os.path.isdir(donor_dir)):
        raise ValueError("One of the source folders no longer exists.")
    if donor_dir and _norm(base_dir) == _norm(donor_dir):
        raise ValueError("Folder A and Folder B are the same folder.")
    if _norm(out_dir) in [_norm(d) for d in (base_dir, donor_dir) if d]:
        raise ValueError("Pick an output folder that is different from the source folders.")
    episodes = body.get("episodes") or []
    if not episodes:
        raise ValueError("No episodes selected.")
    names = [os.path.splitext(e["base"])[0] + ".mkv" for e in episodes]
    existing = [n for n in names if os.path.exists(os.path.join(out_dir, n))]
    if existing and not body.get("overwrite"):
        return {"confirm_overwrite": len(existing)}
    os.makedirs(out_dir, exist_ok=True)
    items = [{"ep": e["ep"], "base": e["base"], "donor": e.get("donor") if donor_dir else None, "out": n,
              "state": "waiting", "step": "", "detail": "", "notes": [], "preview": False,
              "started_at": None, "finished_at": None}
             for e, n in zip(episodes, names)]
    job = {"running": True, "cancel": False, "items": items, "out": out_dir,
           "subs_dir": os.path.join(out_dir, SUBS_FOLDER) if body.get("export_subs") else None,
           "workers": worker_count(len(items)), "started": time.time(), "finished": None}
    with lock:
        state["job"] = job
        state["previews"] = {}
    threading.Thread(target=run_job, args=(job, base_dir, donor_dir, body["sel"], body.get("defaults")),
                     daemon=True).start()
    return {"ok": True}


def run_job(job, base_dir, donor_dir, sel, defaults=None):
    log_path = os.path.join(job["out"], "Kizuna log.txt")
    log.info("job start: base=%s donor=%s out=%s episodes=%d workers=%d",
             base_dir, donor_dir, job["out"], len(job["items"]), job["workers"])
    try:
        fh = open(log_path, "a", encoding="utf-8")
    except OSError:
        fh = None
    if fh:
        fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M')}  {APP_NAME} {__version__}\n"
                 f"    video from:  {base_dir}\n    tracks from: {donor_dir or '(none: keeping chosen tracks only)'}\n")
    uploads = {}
    write_lock = threading.Lock()

    def one(it):
        if job["cancel"]:
            it["state"] = "cancelled"
            return
        it["state"], it["started_at"] = "working", time.time()
        base = os.path.join(base_dir, it["base"])
        donor = os.path.join(donor_dir, it["donor"]) if it["donor"] else None
        out = os.path.join(job["out"], it["out"])
        work = os.path.join(system.data_dir(), "work", uuid.uuid4().hex)
        try:
            r = engine.merge_episode(base, donor, sel, out, work,
                                     step=lambda s, it=it: it.update(step=s),
                                     should_cancel=lambda: job["cancel"], subs_dir=job["subs_dir"],
                                     defaults=defaults)
            it["notes"] = r["notes"]
            it["state"], it["detail"] = describe(r)
            subs = r.get("subtitles")
            if subs:
                n = len(subs["files"])
                it["detail"] += f" · {n} subtitle file{'' if n == 1 else 's'} saved"
                uploads[it["out"]] = subs
            if r.get("preview"):
                state["previews"][str(it["ep"])] = (out, r["preview"])
                it["preview"] = True
        except engine.Cancelled:
            it["state"], it["detail"] = "cancelled", "Stopped"
        except engine.MergeError as e:
            it["state"], it["detail"] = "failed", str(e).strip()[:500]
        except Exception as e:  # noqa: BLE001 - unexpected: log the traceback, show a summary
            it["state"], it["detail"] = "failed", f"Unexpected error: {e}"[:500]
            log.error("episode %s crashed:\n%s", it["ep"], traceback.format_exc())
        it["step"], it["finished_at"] = "", time.time()
        log.info("episode %s: %s %s", it["ep"], it["state"], it["detail"])
        if fh:
            with write_lock:
                fh.write(f"Episode {it['ep']}: {it['state'].upper()}  {it['detail']}"
                         + "".join(f"\n    {n}" for n in it["notes"]) + "\n")
                fh.flush()

    try:
        with ThreadPoolExecutor(job["workers"]) as pool:
            list(pool.map(one, job["items"]))
    finally:
        if fh:
            fh.close()
        if uploads:
            write_upload_info(job["subs_dir"], [(it["out"], uploads[it["out"]])
                                                for it in job["items"] if it["out"] in uploads])
        job["running"], job["finished"] = False, time.time()
        state["last_seen"] = time.time()


# Not "Subtitles" or "Subs": VLC, Plex and Jellyfin load subtitle files from folders with those
# names next to a video, and would then show these files instead of the tracks in the video.
SUBS_FOLDER = "Subtitles for upload"


def write_upload_info(subs_dir, uploads):
    """Details OpenSubtitles asks for when uploading, one block per video."""
    path = os.path.join(subs_dir, "Upload info.txt")
    try:
        new = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as fh:
            if new:
                fh.write("Subtitles saved by Kizuna.\n\n"
                         "Each subtitle file here is synced to the merged video with the same name.\n"
                         "When uploading to OpenSubtitles, choose that video's release; the \"movie hash\"\n"
                         "below lets OpenSubtitles match the subtitle to that exact video file.\n")
            fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M')}\n")
            for video, s in uploads:
                fh.write(f"\nVideo:      {video}\n")
                fh.write(f"Movie hash: {s['hash'] or 'n/a'}\n")
                fh.write(f"Size:       {s['size']} bytes\n")
                if s.get("fps"):
                    fh.write(f"Frame rate: {s['fps']} fps\n")
                for f in s["files"]:
                    fh.write(f"  - {f}\n")
    except OSError:
        log.error("couldn't write %s:\n%s", path, traceback.format_exc())


def describe(r):
    segs, sync = r["segments"], r["sync"]
    if segs is None:
        return "done", "Done (nothing to sync)"
    off = segs[0][2]
    if len(segs) == 1:
        how = "same cut" + (f", shifted {abs(off):.2f} s" if abs(off) >= 0.005 else "")
    else:
        how = f"{len(segs)} sections re-timed"
    how += f" · matched on {r['reference']} audio" if r.get("reference") else ""
    if sync and sync["ok"]:
        return "done", f"In sync (within {sync['typical_ms']} ms) · {how}"
    if sync and sync["points"]:
        return "warning", f"Please spot-check: {sync['bad']} of {sync['points']} check points looked off · {how}"
    return "warning", f"Couldn't double-check the sync, please spot-check · {how}"


# ------------------------------------------------------------------ http
class Handler(BaseHTTPRequestHandler):
    server_version = "Kizuna"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                             "style-src 'self' 'unsafe-inline'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        return self.headers.get("Host", "") == f"127.0.0.1:{self.server.server_port}"

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        if self.path.split("?")[0] == "/":
            with open(os.path.join(system.web_dir(), "index.html"), "rb") as fh:
                html = fh.read()
            theme = load_settings().get("theme")
            if theme in THEMES:   # applied before first paint, so the page never flashes
                html = html.replace(b'data-saved-theme=""', f'data-saved-theme="{theme}"'.encode(), 1)
            return self._send(200, html, "text/html; charset=utf-8")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok() or not secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN):
            return self._send(403, {"error": "forbidden"})
        state["last_seen"] = time.time()
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n < 2_000_000 else {}
            self._send(200, self.route(self.path.split("?")[0], body))
        except (ValueError, KeyError, TypeError, engine.MergeError) as e:
            self._send(200, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            log.error("api error:\n%s", traceback.format_exc())
            self._send(500, {"error": f"Unexpected error: {e}"})

    def route(self, route, body):
        if route == "/api/ping":
            return {"ok": True}
        if route == "/api/info":
            settings = load_settings()
            return {"version": __version__, "missing": system.missing_tools(),
                    "platform": "windows" if system.IS_WIN else "mac" if system.IS_MAC else "linux",
                    "update": state["update"], "check_updates": settings.get("check_updates", True), "repo": REPO,
                    "last": settings.get("last")}
        if route == "/api/settings":
            changes = {}
            if "theme" in body:
                if body["theme"] not in THEMES:
                    raise ValueError("unknown theme")
                changes["theme"] = body["theme"]
            if "check_updates" in body:
                changes["check_updates"] = bool(body["check_updates"])
                if changes["check_updates"]:
                    threading.Thread(target=check_for_update, daemon=True).start()
                else:
                    state["update"] = None
            if "last" in body:       # the last setup, restored next time (folders, ticks, options)
                if not isinstance(body["last"], dict) or len(json.dumps(body["last"])) > 50_000:
                    raise ValueError("invalid settings")
                changes["last"] = body["last"]
            if changes:
                save_settings(changes)
            return {"ok": True, "settings": load_settings()}
        if route == "/api/preview":
            return {"ok": True, "path": open_preview(str(body.get("ep")))}
        if route == "/api/pick":
            return {"path": system.pick_folder(str(body.get("prompt") or "Choose a folder"), body.get("start"))}
        if route == "/api/scan":
            path = os.path.abspath(os.path.expanduser(str(body["path"]).strip()))
            if not os.path.isdir(path):
                return {"error": "That folder doesn't exist."}
            return engine.scan(path)
        if route == "/api/pair":
            rows, by_number = engine.pair(body["a"], body["b"])
            return {"rows": rows, "by_number": by_number}
        if route == "/api/space":
            return {"free": free_space(body.get("path"))}
        if route == "/api/start":
            return start_job(body)
        if route == "/api/status":
            return {"job": state["job"]}
        if route == "/api/cancel":
            if state["job"]:
                state["job"]["cancel"] = True
            return {"ok": True}
        if route == "/api/reveal":
            path = str(body.get("path") or "")
            if os.path.isdir(path):
                system.reveal(path)
            return {"ok": True}
        if route == "/api/quit":
            threading.Thread(target=_shutdown, args=(self.server,), daemon=True).start()
            return {"ok": True}
        raise KeyError("unknown endpoint")


THEMES = ("light", "dark", "kon", "urushi")


# ------------------------------------------------------------------ previews and updates
def open_preview(ep):
    """Make a 20-second clip of a merged episode (added tracks switched on) and open it in
    the user's video player."""
    entry = state["previews"].get(ep)
    if not entry:
        raise ValueError("No preview available for that episode.")
    out, info = entry
    if not os.path.exists(out):
        raise ValueError("The merged file isn't there any more.")
    folder = os.path.join(system.data_dir(), "previews")
    shutil.rmtree(folder, ignore_errors=True)            # only ever keep the latest clip
    os.makedirs(folder, exist_ok=True)
    stem = os.path.splitext(os.path.basename(out))[0]
    clip = engine.make_preview(out, info, os.path.join(folder, f"Preview - {stem}.mkv"))
    system.reveal(clip)
    return clip


def version_tuple(v):
    parts = []
    for p in str(v).lstrip("vV").split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple((parts + [0, 0, 0])[:3])


def check_for_update():
    """Ask GitHub for the latest published release. Only the version number is read;
    nothing about the user or their files is sent. Can be turned off on the page."""
    if not load_settings().get("check_updates", True):
        return
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                     headers={"User-Agent": f"Kizuna/{__version__}",
                                              "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode("utf-8"))
        latest = data.get("tag_name", "")
        if version_tuple(latest) > version_tuple(__version__):
            state["update"] = {"version": latest.lstrip("vV"), "url": data.get("html_url")
                               or f"https://github.com/{REPO}/releases/latest"}
            log.info("update available: %s", latest)
    except Exception as e:  # noqa: BLE001 - offline etc.: silently skip
        log.info("update check skipped: %s", e)


def _settings_path():
    return os.path.join(system.data_dir(), "settings.json")


def load_settings():
    try:
        with open(_settings_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(changes):
    data = load_settings()
    data.update(changes)
    tmp = _settings_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, _settings_path())


def free_space(path):
    p = os.path.abspath(os.path.expanduser(path or os.path.expanduser("~")))
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


# ------------------------------------------------------------------ lifecycle
def _url_file():
    return os.path.join(system.data_dir(), "running.url")


def _shutdown(server):
    time.sleep(0.3)
    try:
        os.remove(_url_file())
    except OSError:
        pass
    server.shutdown()


def _watchdog(server):
    while True:
        time.sleep(30)
        job = state["job"]
        if not (job and job["running"]) and time.time() - state["last_seen"] > IDLE_LIMIT:
            log.info("idle for %d min, quitting", IDLE_LIMIT // 60)
            _shutdown(server)
            return


def already_running():
    """URL of a Kizuna server that is already running for this user, or None."""
    try:
        with open(_url_file(), encoding="utf-8") as fh:
            url = fh.read().strip()
        base, token = url.split("/?t=")
        req = urllib.request.Request(base + "/api/ping", data=b"{}", method="POST",
                                     headers={"X-Token": token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=1.5) as r:
            return url if r.status == 200 else None
    except Exception:  # noqa: BLE001 - any failure means "not running"
        return None


def make_server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server, f"http://127.0.0.1:{port}/?t={TOKEN}"


def main(open_browser=True):
    _setup_logging()
    url = already_running()
    if url:
        if open_browser:
            system.open_url(url)
        return 0
    server, url = make_server()
    with open(_url_file(), "w", encoding="utf-8") as fh:
        fh.write(url)
    log.info("%s %s started at %s; tools: %s", APP_NAME, __version__, url.split("/?")[0],
             {n: system.find_tool(n) for n in system.TOOL_NAMES})
    threading.Thread(target=_watchdog, args=(server,), daemon=True).start()
    threading.Thread(target=check_for_update, daemon=True).start()
    if open_browser:
        system.open_url(url)
    else:
        try:
            print(url, flush=True)
        except Exception:  # noqa: BLE001
            pass
    try:
        server.serve_forever()
    finally:
        try:
            os.remove(_url_file())
        except OSError:
            pass
    return 0
