"""Everything that differs between Windows, macOS and Linux lives here."""
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import webbrowser

from . import APP_NAME

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
FROZEN = bool(getattr(sys, "frozen", False))
TOOL_NAMES = ("ffmpeg", "ffprobe", "mkvmerge")


# ------------------------------------------------------------------ locations
def data_dir():
    """Per-user folder for logs, temporary work files and the running-instance file."""
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    elif IS_MAC:
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    path = os.path.join(base, APP_NAME)
    if not os.path.isdir(path):
        _carry_over_settings(os.path.join(base, OLD_APP_NAME), path)
    os.makedirs(path, exist_ok=True)
    return path


OLD_APP_NAME = "Track Merger"           # this app's name before 2.1.0


def _carry_over_settings(old, new):
    """First launch after the rename: keep the theme and last setup saved under the old name."""
    src = os.path.join(old, "settings.json")
    if os.path.isfile(src):
        try:
            os.makedirs(new, exist_ok=True)
            shutil.copy2(src, os.path.join(new, "settings.json"))
        except OSError:
            pass                        # starting with default settings is fine


def resource_dir():
    """Folder holding bundled files (web page, tools) in a packaged build."""
    if FROZEN:
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def web_dir():
    # source: <package>/web ; packaged: <bundle>/web (see packaging/kizuna.spec)
    return os.path.join(resource_dir(), "web")


# ------------------------------------------------------------------ external tools
def _exe(name):
    return name + ".exe" if IS_WIN else name


def _candidate_dirs():
    dirs = []
    env = os.environ.get("KIZUNA_TOOLS_DIR")
    if env:
        dirs.append(env)
    if FROZEN:
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        dirs += [os.path.join(exe_dir, "tools"),                                 # Windows build
                 os.path.join(os.path.dirname(exe_dir), "Helpers", "tools"),     # macOS .app
                 os.path.join(resource_dir(), "tools")]
    dirs += (os.environ.get("PATH") or "").split(os.pathsep)
    if IS_MAC:
        dirs += ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"]
        dirs += sorted(glob.glob("/Applications/MKVToolNix*.app/Contents/MacOS"), reverse=True)
    elif IS_WIN:
        for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            root = os.environ.get(var)
            if root:
                dirs.append(os.path.join(root, "MKVToolNix"))
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(os.path.join(local, "Microsoft", "WinGet", "Links"))
    else:
        dirs += ["/usr/bin", "/usr/local/bin"]
    seen, out = set(), []
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


_tool_cache = {}


def find_tool(name):
    """Full path of ffmpeg / ffprobe / mkvmerge, or None if it can't be found."""
    if name in _tool_cache:
        return _tool_cache[name]
    found = None
    for d in _candidate_dirs():
        p = os.path.join(d, _exe(name))
        if os.path.isfile(p) and os.access(p, os.X_OK):
            found = p
            break
    _tool_cache[name] = found
    return found


def missing_tools():
    return [n for n in TOOL_NAMES if not find_tool(n)]


def reset_tool_cache():
    _tool_cache.clear()


# ------------------------------------------------------------------ processes
def _no_window():
    """Keep Windows from flashing a console window for every ffmpeg/mkvmerge call."""
    if IS_WIN:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": si}
    return {}


def run(cmd, binary=False, timeout=None):
    """Run a command, capturing output. Text output is decoded as UTF-8."""
    kw = dict(capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout, **_no_window())
    if not binary:
        kw.update(text=True, encoding="utf-8", errors="replace")
    return subprocess.run(cmd, **kw)


def spawn_detached(cmd):
    """Start a process that keeps running after this one exits."""
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if IS_WIN:
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kw["start_new_session"] = True
    subprocess.Popen(cmd, **kw)


# ------------------------------------------------------------------ desktop integration
def open_url(url):
    webbrowser.open(url)


def reveal(path):
    """Show a folder in Finder / Explorer / the file manager."""
    if IS_WIN:
        os.startfile(path)  # noqa: S606 - opening a local folder the user chose
    elif IS_MAC:
        subprocess.run(["open", path], check=False)
    else:
        subprocess.run(["xdg-open", path], check=False)


def pick_folder(prompt, start=None):
    """Native 'choose folder' dialog. Returns the chosen path or None."""
    start = start if start and os.path.isdir(start) else None
    if IS_MAC:
        safe = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
        loc = f' default location (POSIX file "{safe(start)}")' if start else ""
        p = subprocess.run(["osascript", "-e", "activate", "-e",
                            f'POSIX path of (choose folder with prompt "{safe(prompt)}"{loc})'],
                           capture_output=True, text=True)
        path = p.stdout.strip()
        return path.rstrip("/") or "/" if p.returncode == 0 and path else None
    # Windows / Linux: show a Tk dialog in a separate process (Tk wants the main thread).
    fd, out = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        if FROZEN:
            cmd = [sys.executable]
        else:
            cmd = [sys.executable, "-m", "kizuna"]
        cmd += ["--pick-folder", prompt, start or "", out]
        env = dict(os.environ)
        if not FROZEN:
            src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
        subprocess.run(cmd, env=env, **_no_window())
        with open(out, encoding="utf-8") as fh:
            path = fh.read().strip()
        return os.path.normpath(path) if path else None
    except OSError:
        return None
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


def folder_dialog_main(prompt, start, out_file):
    """Entry point for the helper process started by pick_folder()."""
    path = ""
    try:
        if IS_WIN:
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:  # noqa: BLE001 - cosmetic only
                pass
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(parent=root, title=prompt, initialdir=start or None,
                                       mustexist=True) or ""
        root.destroy()
    except Exception:  # noqa: BLE001 - no GUI available: fall back to zenity on Linux
        if not IS_WIN and shutil.which("zenity"):
            p = subprocess.run(["zenity", "--file-selection", "--directory", f"--title={prompt}"],
                               capture_output=True, text=True)
            path = p.stdout.strip() if p.returncode == 0 else ""
    with open(out_file, "w", encoding="utf-8") as fh:
        fh.write(path)
