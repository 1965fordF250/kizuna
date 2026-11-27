"""Command-line entry point.

  kizuna                  start the app (opens your browser)
  kizuna --no-browser     start without opening a browser (prints the address)
  kizuna --self-test [report.txt]   run the built-in end-to-end test
  kizuna --version
"""
import sys

from . import __version__


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--version"]:
        _print(f"Kizuna {__version__}")
        return 0
    if argv[:1] == ["--pick-folder"]:
        # helper process used by system.pick_folder(): prompt, start dir, output file
        from . import system
        prompt, start, out = (argv[1:] + ["", "", ""])[:3]
        system.folder_dialog_main(prompt, start, out)
        return 0
    if argv[:1] == ["--self-test"]:
        from . import selftest
        return 0 if selftest.run(argv[1] if len(argv) > 1 else None) else 1
    from . import server, system
    open_browser = "--no-browser" not in argv
    if system.IS_MAC and system.FROZEN and "--serve" not in argv:
        # macOS treats a running .app as "already open" and ignores further double-clicks.
        # So the app itself only starts a background server process and exits; opening
        # the app again then simply reopens the page.
        url = server.already_running()
        if url:
            system.open_url(url)
        else:
            system.spawn_detached([sys.executable, "--serve"] + ([] if open_browser else ["--no-browser"]))
        return 0
    return server.main(open_browser=open_browser)


def _print(text):
    try:
        print(text)
    except Exception:  # noqa: BLE001 - windowed builds have no console
        pass


if __name__ == "__main__":
    sys.exit(main())
