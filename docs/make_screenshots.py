"""Regenerate the README / release-note screenshots (docs/screenshot-<theme>.png).

    python docs/make_screenshots.py

Needs: ffmpeg + MKVToolNix, numpy, Pillow, Playwright (`pip install playwright`) and
Google Chrome (or run `playwright install chromium` and pass --chromium).
Builds a small synthetic demo show (no real videos), runs the app, ticks tracks, merges,
and captures each theme at 2x pixel density. Everything it creates is removed afterwards.
"""
import os
import shutil
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from PIL import Image  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from kizuna import engine, selftest, server, system  # noqa: E402

WIDTH, SCALE = 1100, 2
DEMO = {"darwin": "/Users/Shared/KizunaDemo", "win32": r"C:\Users\Public\KizunaDemo"}.get(
    sys.platform, "/tmp/KizunaDemo")


def _srt(path, lines):
    with open(path, "w", encoding="utf-8") as fh:
        for i, (t, s) in enumerate(lines, 1):
            fh.write(f"{i}\n{engine._srt_f(t)} --> {engine._srt_f(t + 2)}\n{s}\n\n")


def build_demo(root):
    a, b = os.path.join(root, "My Show (Blu-ray)"), os.path.join(root, "My Show (Streaming)")
    os.makedirs(a)
    os.makedirs(b)
    for ep in (1, 2, 3):
        t = tempfile.mkdtemp()
        selftest.build_case(t, "recut")
        _srt(f"{t}/fr.srt", [(20, "Bonjour"), (60, "Au revoir")])
        _srt(f"{t}/en.srt", [(25, "Hello"), (64.5, "Goodbye")])
        _srt(f"{t}/en_signs.srt", [(30, "[SIGN] Station")])
        _srt(f"{t}/es.srt", [(25, "Hola"), (64.5, "Adiós")])
        selftest._ffmpeg([
            "-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=24:d=90", "-i", f"{t}/jpn.wav", "-i", f"{t}/eng_ref.wav",
            "-i", f"{t}/fr.srt", "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:s",
            "-c:v", "mpeg4", "-q:v", "31", "-c:a:0", "flac", "-c:a:1", "aac", "-c:s", "srt",
            "-metadata:s:a:0", "language=jpn", "-metadata:s:a:0", "title=Japanese 2.0",
            "-metadata:s:a:1", "language=fre", "-metadata:s:a:1", "title=Français",
            "-metadata:s:s:0", "language=fre", "-metadata:s:s:0", "title=Français",
            os.path.join(a, f"My Show - S01E0{ep} - 1080p BluRay.mkv")])
        selftest._ffmpeg([
            "-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=24:d=100", "-i", f"{t}/d_eng.wav", "-i", f"{t}/d_jpn.wav",
            "-i", f"{t}/en.srt", "-i", f"{t}/en_signs.srt", "-i", f"{t}/es.srt",
            "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:s", "-map", "4:s", "-map", "5:s",
            "-c:v", "mpeg4", "-q:v", "31", "-c:a", "aac", "-c:s", "srt",
            "-metadata:s:a:0", "language=eng", "-metadata:s:a:0", "title=English",
            "-metadata:s:a:1", "language=jpn", "-metadata:s:a:1", "title=Japanese",
            "-metadata:s:s:0", "language=eng", "-metadata:s:s:0", "title=English",
            "-metadata:s:s:1", "language=eng", "-metadata:s:s:1", "title=English (Signs)",
            "-metadata:s:s:2", "language=spa", "-metadata:s:s:2", "title=Español",
            os.path.join(b, f"[Web] My Show - 0{ep} [1080p].mkv")])
        shutil.rmtree(t)
    return a, b


def main():
    use_chromium = "--chromium" in sys.argv
    if os.path.exists(DEMO):
        raise SystemExit(f"{DEMO} already exists; remove it first")
    data = tempfile.mkdtemp(prefix="tm-shots-data-")
    system.data_dir = lambda: data          # keep the user's own settings out of it
    try:
        print("building demo show ...")
        a, b = build_demo(DEMO)
        srv, url = server.make_server()
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        with sync_playwright() as p:
            browser = p.chromium.launch(**({} if use_chromium else {"channel": "chrome"}))
            page = browser.new_page(viewport={"width": WIDTH, "height": 900}, device_scale_factor=SCALE,
                                    color_scheme="light")
            page.goto(url)

            def shot(selector, name, pad=18):
                """Close-up of one part of the page (Playwright captures exactly that element),
                then a margin in the page's background colour is added around it."""
                out = os.path.join(HERE, name)
                page.locator(selector).screenshot(path=out)
                img = Image.open(out).convert("RGB")
                bg = img.getpixel((0, 0))           # a rounded card's corner shows the page background
                p = pad * SCALE
                framed = Image.new("RGB", (img.width + 2 * p, img.height + 2 * p), bg)
                framed.paste(img, (p, p))
                framed.save(out, optimize=True)
                print(f"  {out}  {framed.width}x{framed.height}")

            def load(k, path):
                page.fill(f'[data-path="{k}"]', path)
                page.dispatch_event(f'[data-path="{k}"]', "change")
                page.wait_for_selector(f'#card-{k} .chip')

            load("A", a)                                      # one-folder clean-up mode
            page.wait_for_selector("#pairing:not(.hidden)")
            page.mouse.move(0, 0)
            shot(".folders", "feature-one-folder.png")
            load("B", b)
            page.locator("#pair-summary", has_text="matched").wait_for()
            page.click('[data-chip="B:audio:eng"]')
            page.click('[data-chip="B:subtitles:eng"]')
            page.select_option("#def-audio", "B:eng")
            page.select_option("#def-subs", "B:eng:full")
            page.check("#export-subs")
            page.mouse.move(0, 0)
            shot("#options", "feature-options.png")
            page.click("#start")
            # (the app's Content-Security-Policy blocks eval, so wait on elements, not JS predicates)
            try:
                page.locator("#prog-count", has_text="left").wait_for(timeout=60000)
                shot("#progress", "feature-progress-running.png")
            except Exception:  # noqa: BLE001 - the estimate only shows briefly on fast machines
                print("  (time estimate not captured this run)")
            page.locator("#prog-title", has_text="All done").wait_for(timeout=120000)
            page.mouse.move(0, 0)
            page.wait_for_timeout(900)                        # let the progress bar finish filling
            shot("#progress", "feature-progress-done.png")
            for theme in ("light", "dark", "kon", "urushi"):
                page.click(f'[data-theme-btn="{theme}"]')
                page.wait_for_timeout(600)                 # let colour transitions finish
                out = os.path.join(HERE, f"screenshot-{theme}.png")
                page.screenshot(path=out, full_page=True)
                img = Image.open(out)
                img.save(out, optimize=True)
                print(f"  {out}  {img.size[0]}x{img.size[1]}  {os.path.getsize(out) // 1024} KB")
            browser.close()
        srv.shutdown()
    finally:
        shutil.rmtree(DEMO, ignore_errors=True)
        shutil.rmtree(data, ignore_errors=True)


if __name__ == "__main__":
    main()
