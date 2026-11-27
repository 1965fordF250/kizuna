# Third-party software

Kizuna is free software licensed under the **GNU General Public License v3.0 or later**
(see `LICENSE`). The installers published on the Releases page also contain the
third-party programs below. Each keeps its own license; the license texts are included in
the installed app (in the `tools/licenses` folder on Windows and in
`Kizuna.app/Contents/Resources/licenses` on macOS).

## FFmpeg (ffmpeg, ffprobe) 9.1

- Project: <https://ffmpeg.org>. Licensed under the GPL-3.0 in the builds we ship,
  because they are configured with `--enable-gpl --enable-version3`.
- Windows builds by Gyan Doshi: <https://www.gyan.dev/ffmpeg/builds/>,
  downloaded from <https://github.com/GyanD/codexffmpeg/releases/tag/9.1>.
- macOS builds by Martin Riedl: <https://ffmpeg.martin-riedl.de>
  (build scripts: <https://gitlab.com/martinr92/ffmpeg>).
- Corresponding source code: `ffmpeg-9.1.tar.xz` is attached to every Kizuna
  release and is also available from <https://ffmpeg.org/releases/>. The build
  configuration of each binary is printed by `ffmpeg -version`. The third-party libraries
  those builds link to are listed on each builder's page above, with links to their sources.

## MKVToolNix (mkvmerge) 104.0

- Project: <https://mkvtoolnix.download>, by Moritz Bunkus and contributors. GPL-2.0.
- Binaries are the official Windows portable package and macOS universal DMG.
- Corresponding source code: `mkvtoolnix-104.0.tar.xz` is attached to every Track
  Merger release and is also available from <https://mkvtoolnix.download/sources/>.
- On macOS, `mkvmerge` uses the Qt Core library (LGPL-3.0, <https://www.qt.io>), which is
  included unmodified next to it in `libs/`. Qt sources: <https://download.qt.io/archive/qt/>.

## Python runtime and libraries (packaged builds)

- Python, PSF License: <https://www.python.org>
- NumPy, BSD-3-Clause: <https://numpy.org>
- PyInstaller bootloader, GPL-2.0 with a bootloader exception: <https://pyinstaller.org>

## Written offer

If any link above stops working, open an issue on this repository and we will provide the
corresponding source code of the bundled third-party programs, for at least three years
after the release that contained them.
