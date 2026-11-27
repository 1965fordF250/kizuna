"""Download, verify and stage the third-party tools bundled into the installers.

    python packaging/fetch_tools.py --target windows-x64   --dest build/tools
    python packaging/fetch_tools.py --target macos-arm64   --dest build/tools
    python packaging/fetch_tools.py --target macos-x86_64  --dest build/tools
    python packaging/fetch_tools.py --target auto          --dest build/tools   (this machine)
    python packaging/fetch_tools.py --target sources       --dest build/sources

Every download is checked against the sha256 in packaging/tools.json; a mismatch stops
the build. The staged folder contains only what Kizuna needs (ffmpeg, ffprobe,
mkvmerge and the libraries mkvmerge loads), the license texts, and VERSIONS.txt.
After staging, each tool is run once with --version to prove it works on this machine.
"""
import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE = os.path.join(ROOT, "build", "download-cache")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(entry):
    """Download (or reuse a cached copy) and verify the checksum. Returns the local path."""
    os.makedirs(CACHE, exist_ok=True)
    name = entry["sha256"][:12] + "-" + entry["url"].rstrip("/").split("/")[-1]
    path = os.path.join(CACHE, name)
    if os.path.exists(path) and sha256(path) == entry["sha256"]:
        print(f"  cached  {entry['url']}")
        return path
    print(f"  fetch   {entry['url']}")
    tmp = path + ".part"
    req = urllib.request.Request(entry["url"], headers={"User-Agent": "Kizuna-build"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as out:
                shutil.copyfileobj(r, out, 1 << 20)
            break
        except OSError as e:
            if attempt == 2:
                raise SystemExit(f"download failed: {entry['url']}: {e}")
            print(f"  retry   ({e})")
    got = sha256(tmp)
    if got != entry["sha256"]:
        os.remove(tmp)
        raise SystemExit(f"CHECKSUM MISMATCH for {entry['url']}\n  expected {entry['sha256']}\n  got      {got}")
    os.replace(tmp, path)
    return path


def make_executable(path):
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def extract_7z_member(archive, members, dest):
    """Extract selected files from a .7z (7-Zip if present, else bsdtar/Windows tar)."""
    tmp = tempfile.mkdtemp()
    try:
        seven = shutil.which("7z") or (r"C:\Program Files\7-Zip\7z.exe" if os.path.exists(r"C:\Program Files\7-Zip\7z.exe") else None)
        if seven:
            subprocess.run([seven, "x", "-y", f"-o{tmp}", archive] + members, check=True, stdout=subprocess.DEVNULL)
        else:
            subprocess.run(["tar", "-xf", archive, "-C", tmp] + members, check=True)
        for m in members:
            src = os.path.join(tmp, *m.split("/"))
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(dest, os.path.basename(src)), dirs_exist_ok=True)
            else:
                shutil.copy2(src, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def otool_deps(binary):
    out = subprocess.run(["otool", "-L", binary], capture_output=True, text=True, check=True).stdout
    deps = []
    for line in out.splitlines()[1:]:
        dep = line.strip().split(" (")[0]
        if dep.startswith(("@executable_path/", "@loader_path/", "@rpath/")):
            deps.append(dep)
    return deps


def copy_mkvmerge_from_app(macos_dir, dest):
    """Copy mkvmerge plus every library it (transitively) loads, keeping the libs/ layout."""
    todo, done = ["mkvmerge"], set()
    while todo:
        rel = todo.pop()
        if rel in done:
            continue
        done.add(rel)
        src = os.path.join(macos_dir, rel)
        dst = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.realpath(src), dst)
        for dep in otool_deps(src):
            name = dep.split("/", 1)[1]
            if dep.startswith("@executable_path/"):
                todo.append(name)
            else:  # @rpath / @loader_path: mkvtoolnix keeps these in libs/
                base = os.path.basename(name)
                todo.append(f"libs/{base}" if os.path.exists(os.path.join(macos_dir, "libs", base)) else name)
    return sorted(done)


def stage_windows(cfg, dest):
    lic = os.path.join(dest, "licenses")
    os.makedirs(lic, exist_ok=True)
    zpath = download(cfg["ffmpeg"])
    with zipfile.ZipFile(zpath) as z:
        for n in z.namelist():
            base = n.split("/", 1)[-1]
            if base in ("bin/ffmpeg.exe", "bin/ffprobe.exe"):
                with z.open(n) as src, open(os.path.join(dest, os.path.basename(base)), "wb") as out:
                    shutil.copyfileobj(src, out)
            elif base == "LICENSE":
                with z.open(n) as src, open(os.path.join(lic, "FFmpeg-LICENSE.txt"), "wb") as out:
                    shutil.copyfileobj(src, out)
    mk = download(cfg["mkvtoolnix"])
    stage = os.path.join(dest, "_mkvtoolnix")     # own folder: its "licenses" dir must not clash with ours
    os.makedirs(stage)
    extract_7z_member(mk, ["mkvtoolnix/mkvmerge.exe", "mkvtoolnix/doc/COPYING.txt", "mkvtoolnix/doc/licenses"], stage)
    shutil.move(os.path.join(stage, "mkvmerge.exe"), os.path.join(dest, "mkvmerge.exe"))
    shutil.move(os.path.join(stage, "COPYING.txt"), os.path.join(lic, "MKVToolNix-COPYING.txt"))
    shutil.move(os.path.join(stage, "licenses"), os.path.join(lic, "MKVToolNix-components"))
    shutil.rmtree(stage)


def stage_macos(cfg, dest):
    lic = os.path.join(dest, "licenses")
    os.makedirs(lic, exist_ok=True)
    for name in ("ffmpeg", "ffprobe"):
        with zipfile.ZipFile(download(cfg[name])) as z:
            with z.open(name) as src, open(os.path.join(dest, name), "wb") as out:
                shutil.copyfileobj(src, out)
        make_executable(os.path.join(dest, name))
    # Martin Riedl's builds are configured with --enable-gpl --enable-version3: GPL-3.0
    shutil.copy2(os.path.join(ROOT, "LICENSE"), os.path.join(lic, "FFmpeg-LICENSE-GPL-3.0.txt"))
    dmg = download(cfg["mkvtoolnix"])
    mnt = tempfile.mkdtemp(prefix="mkvtoolnix-")
    subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", mnt, dmg],
                   check=True, stdout=subprocess.DEVNULL)
    try:
        macos_dir = os.path.join(mnt, "MKVToolNix.app", "Contents", "MacOS")
        files = copy_mkvmerge_from_app(macos_dir, dest)
        print("  mkvmerge files: " + ", ".join(files))
        shutil.copy2(os.path.join(mnt, "COPYING.txt"), os.path.join(lic, "MKVToolNix-COPYING.txt"))
        # mkvmerge on macOS loads Qt Core (LGPL-3.0) from libs/
        shutil.copy2(os.path.join(HERE, "LGPL-3.0.txt"), os.path.join(lic, "Qt-LGPL-3.0.txt"))
        lic_src = os.path.join(mnt, "MKVToolNix.app", "Contents", "MacOS", "doc", "licenses")
        if os.path.isdir(lic_src):
            shutil.copytree(lic_src, os.path.join(lic, "MKVToolNix-components"), dirs_exist_ok=True)
    finally:
        subprocess.run(["hdiutil", "detach", mnt, "-force"], check=False, stdout=subprocess.DEVNULL)
    for f in ("ffmpeg", "ffprobe", "mkvmerge"):
        # downloaded files can carry the quarantine flag; the app bundle is signed later
        subprocess.run(["xattr", "-cr", os.path.join(dest, f)], check=False)


def verify(dest, target):
    """Run each staged tool once. Only possible when the target matches this machine."""
    if target != host_target():
        print(f"  (skipping run check: target {target} != host {host_target()})")
        return
    exe = ".exe" if target.startswith("windows") else ""
    for tool in ("ffmpeg", "ffprobe", "mkvmerge"):
        path = os.path.join(dest, tool + exe)
        flag = "-version" if tool != "mkvmerge" else "--version"
        p = subprocess.run([path, flag], capture_output=True, text=True, errors="replace")
        first = (p.stdout or p.stderr).splitlines()[0] if (p.stdout or p.stderr) else ""
        if p.returncode != 0:
            raise SystemExit(f"{tool} does not run: exit {p.returncode}\n{p.stderr[-800:]}")
        print(f"  ok      {first}")


def host_target():
    m = platform.machine().lower()
    if sys.platform == "win32":
        return "windows-x64"
    if sys.platform == "darwin":
        return "macos-arm64" if m == "arm64" else "macos-x86_64"
    return "linux-" + m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--dest", required=True)
    args = ap.parse_args()
    with open(os.path.join(HERE, "tools.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    target = host_target() if args.target == "auto" else args.target
    dest = os.path.abspath(args.dest)
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest)
    print(f"Staging {target} into {dest}")
    if target == "sources":
        for name, entry in cfg["sources"].items():
            shutil.copy2(download(entry), os.path.join(dest, entry["url"].split("/")[-1]))
        return
    if target not in cfg:
        raise SystemExit(f"unknown target {target}")
    if target.startswith("windows"):
        stage_windows(cfg[target], dest)
    else:
        stage_macos(cfg[target], dest)
    with open(os.path.join(dest, "VERSIONS.txt"), "w", encoding="utf-8") as fh:
        fh.write(f"FFmpeg {cfg['ffmpeg_version']} - built by {cfg[target]['ffmpeg']['builder']}\n")
        fh.write(f"  binary: {cfg[target]['ffmpeg']['url']}\n")
        fh.write(f"  source: {cfg['sources']['ffmpeg']['url']}\n")
        fh.write(f"MKVToolNix {cfg['mkvtoolnix_version']} (mkvmerge) - https://mkvtoolnix.download\n")
        fh.write(f"  binary: {cfg[target]['mkvtoolnix']['url']}\n")
        fh.write(f"  source: {cfg['sources']['mkvtoolnix']['url']}\n")
    verify(dest, target)
    print("Done.")


if __name__ == "__main__":
    main()
