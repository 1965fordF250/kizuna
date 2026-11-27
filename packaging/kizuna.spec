# PyInstaller spec for Kizuna.  Run from the repository root:
#     pyinstaller --noconfirm --clean packaging/kizuna.spec
# Expects build/tools (from fetch_tools.py) and build/icon.* (from make_icon.py).
# Output: dist/Kizuna/ (Windows) or dist/Kizuna.app (macOS), with the tools inside.
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821 - SPECPATH is provided by PyInstaller
NAME = "Kizuna"
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"
TOOLS = os.path.join(ROOT, "build", "tools")
with open(os.path.join(ROOT, "src", "kizuna", "__init__.py"), encoding="utf-8") as fh:
    VERSION = re.search(r'__version__ = "([^"]+)"', fh.read()).group(1)

if not os.path.isfile(os.path.join(TOOLS, "mkvmerge.exe" if IS_WIN else "mkvmerge")):
    raise SystemExit("build/tools is missing: run packaging/fetch_tools.py first")

a = Analysis(  # noqa: F821
    [os.path.join(SPECPATH, "launcher.py")],  # noqa: F821
    pathex=[os.path.join(ROOT, "src")],
    datas=[(os.path.join(ROOT, "src", "kizuna", "web"), "web")],
    excludes=["tkinter", "_tkinter"] if IS_MAC else [],   # macOS uses the native AppleScript folder dialog
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

version_file = None
if IS_WIN:
    nums = tuple(int(x) for x in (VERSION.split(".") + ["0", "0", "0"])[:4])
    version_file = os.path.join(ROOT, "build", "version_info.txt")
    with open(version_file, "w", encoding="utf-8") as fh:
        fh.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Kizuna contributors'),
      StringStruct('FileDescription', 'Kizuna'),
      StringStruct('FileVersion', '{VERSION}'),
      StringStruct('InternalName', 'Kizuna'),
      StringStruct('LegalCopyright', 'GPL-3.0-or-later'),
      StringStruct('OriginalFilename', 'Kizuna.exe'),
      StringStruct('ProductName', 'Kizuna'),
      StringStruct('ProductVersion', '{VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
""")

exe = EXE(  # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=NAME,
    console=False,
    icon=os.path.join(ROOT, "build", "icon.ico" if IS_WIN else "icon.icns"),
    version=version_file,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)  # noqa: F821

DIST = os.path.join(ROOT, "dist")
EXTRA_DOCS = [os.path.join(ROOT, f) for f in ("LICENSE", "THIRD_PARTY_NOTICES.md")]

if IS_MAC:
    app = BUNDLE(  # noqa: F821
        coll,
        name=f"{NAME}.app",
        icon=os.path.join(ROOT, "build", "icon.icns"),
        bundle_identifier="io.github.1965fordf250.kizuna",
        version=VERSION,
        info_plist={
            "CFBundleDisplayName": NAME,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSUIElement": True,                 # no Dock icon: the window is the browser page
            "LSMinimumSystemVersion": "13.0",     # MKVToolNix requires macOS 13+
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": "GPL-3.0-or-later. Includes FFmpeg and MKVToolNix.",
        },
    )
    app_path = os.path.join(DIST, f"{NAME}.app")
    helpers = os.path.join(app_path, "Contents", "Helpers", "tools")
    resources = os.path.join(app_path, "Contents", "Resources")
    # only code goes in Helpers (codesign rule); licenses and VERSIONS.txt go in Resources
    shutil.copytree(TOOLS, helpers, symlinks=True,
                    ignore=shutil.ignore_patterns("licenses", "VERSIONS.txt"))
    shutil.copytree(os.path.join(TOOLS, "licenses"), os.path.join(resources, "licenses"))
    shutil.copy2(os.path.join(TOOLS, "VERSIONS.txt"), os.path.join(resources, "licenses", "VERSIONS.txt"))
    for doc in EXTRA_DOCS:
        shutil.copy2(doc, resources)
    # Re-sign the whole bundle (ad-hoc unless a Developer ID identity is provided) so the
    # added helper tools are covered by the app's signature.
    identity = os.environ.get("MACOS_SIGN_IDENTITY", "-")
    if identity == "-":          # ad-hoc signature (no Apple Developer account)
        sign = ["codesign", "--force", "--timestamp=none", "-s", "-"]
        app_extra = []
    else:                        # Developer ID: hardened runtime, ready for notarization
        sign = ["codesign", "--force", "--timestamp", "--options", "runtime", "-s", identity]
        app_extra = ["--entitlements", os.path.join(SPECPATH, "macos", "entitlements.plist")]  # noqa: F821
    for root, _, files in os.walk(helpers):
        for f in files:
            p = os.path.join(root, f)
            if not os.path.islink(p) and (f.endswith(".dylib") or os.access(p, os.X_OK)):
                subprocess.run(sign + [p], check=True)
    subprocess.run(sign + app_extra + [app_path], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", app_path], check=True)
elif IS_WIN:
    out = os.path.join(DIST, NAME)
    shutil.copytree(TOOLS, os.path.join(out, "tools"))
    for doc in EXTRA_DOCS:
        shutil.copy2(doc, out)
