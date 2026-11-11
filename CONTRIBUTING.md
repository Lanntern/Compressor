# Contributing to Wick

Thanks for helping! Bug reports, ideas and pull requests are all welcome.

## Running from source

Requires Python 3.11 or newer.

```bash
pip install -e .
```

Then get the encoders:

* **Windows:** `python scripts/fetch_tools.py` downloads ffmpeg/ffprobe, cjxl/djxl/cjpegli and gifsicle into `tools/`.
* **Linux:** `sudo apt install ffmpeg gifsicle`, then `python scripts/fetch_tools.py --libjxl` for cjxl/djxl/cjpegli 0.11
  (distro libjxl is often too old).

Start the app with `wick`.

## Reporting problems

- **Bugs:** open an issue with what you did, what happened, and what you expected. Settings → About → About
  Wick → **Copy Versions** gives the version details worth pasting in.
- **Security problems:** please don't open a public issue; see [SECURITY.md](SECURITY.md).

## Pull requests

1. **Sign the Contributor License Agreement.** On your first pull request a bot asks you to sign
   [CLA.md](CLA.md) by posting a comment; that one comment covers all your future contributions (signatures are
   kept on the `cla-signatures` branch). You keep the
   copyright in your work. The agreement lets the maintainer keep the project's licensing flexible (for
   example, move to a newer license) without tracking down every contributor. Pull requests can't be merged
   until it's signed.
2. **Keep it focused.** One change per pull request, with a short description of what it changes and why.
   For a bigger change, open an issue first so we can agree on the approach.
3. **Run the checks** (CI runs them too):

   ```bash
   pip install --require-hashes -r requirements.txt
   pip install --no-deps . pytest ruff==0.16.9 mypy==2.3.1
   ruff check .
   mypy wick
   python -m pytest
   ```

   Tests that need the encoders skip themselves when the tools aren't installed;
   `python scripts/fetch_tools.py` downloads them (see the README).
4. **Add a test** for a bug fix or new behavior where you can.
5. **Be careful with people's files.** Wick replaces and deletes files, so anything touching that path
   needs to keep the existing guarantees: verify before replacing, originals to the Recycle Bin, everything
   undoable.

## Development notes

```bash
pip install -e ".[dev]"
python -m pytest          # unit tests + real round trips (needs the encoders)
```

CI (`.github/workflows/build.yml`) lints, then runs the tests on Linux and on Windows (including the real Recycle Bin restore and offscreen GUI tests that drive the main window). Pull requests stop there. Pushes to `main`, `v*` tags and manual runs also build the `.exe` with PyInstaller and the installer, smoke-test both, and keep them as run artifacts; a `v*` tag attaches them to the GitHub release.

Data lives in `%LOCALAPPDATA%\Wick` (`~/.local/share/wick` on Linux): run history (`runs/`), presets (`presets.json`), the file index that makes repeat scans fast (`index.sqlite`, safe to delete) and a small rotating log (`logs/`). Settings → Diagnostics opens the log folder or copies a summary for bug reports. `WICK_DATA_DIR` and `WICK_SETTINGS_FILE` override the locations (used by the tests).

Layout:

```
wick/
  scanner.py       walk folders, skip system/program folders, probe videos
  index.py         cache of probed file details (fast repeat scans)
  presets.py       built-in and user presets
  quality.py       SSIMULACRA2 / VMAF / SSIM scoring and the Smart quality search
  log.py           log file, crash hooks, diagnostics
  plan.py          per-file decision + savings estimate
  images.py        all image routes (cjxl, cjpegli, mozjpeg, oxipng, imagequant, gifsicle, Pillow) + verification
  metadata.py      lossless EXIF/XMP editing in JPEG, PNG, WebP and JPEG XL
  video.py         ffmpeg AV1/HEVC (NVENC or CPU) + verification
  audio.py         FLAC (sample-exact check), Opus/AAC/MP3, mono detection, CD quality, tags and album art
  engine.py        run a job: convert → verify → swap → journal; undo
  trash_restore.py restore from the Windows Recycle Bin / freedesktop Trash
  journal.py       run history (JSON lines in %LOCALAPPDATA%\Wick)
  cli.py           command line
  gui/             PySide6 desktop app (compress, preview, watch folders, history, settings, tray)
scripts/fetch_tools.py   download Windows encoders
```

## How bundled components are kept up to date

Wick opens files with decoders it bundles (FFmpeg, libjxl, Pillow with its WebP/JPEG libraries, libheif,
qpdf). Those get security fixes, so:

* **Builds are pinned and audited.** `requirements.txt` holds every Python library's exact version and checksum
  (installed with `--require-hashes`); `pip-audit` checks them for known vulnerabilities whenever they change
  and every week.
  The command-line tools are exact releases checked against recorded SHA-256 checksums
  (`scripts/fetch_tools.py`); a changed download is refused.
* **Maintainers hear about new versions.** Dependabot opens pull requests for new library versions, and a weekly job
  (`scripts/check_updates.py`) opens an issue when FFmpeg, libjxl or gifsicle has a newer release or a library
  has a new advisory.

## License

Wick is licensed under the GNU General Public License, version 3 only ([LICENSE](LICENSE)). By
contributing you agree to [CLA.md](CLA.md).

The Wick name and icon are not covered by the code's license; see the end of the
[README](README.md#license).
