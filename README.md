<p align="center"><img src="docs/icon.png" width="96" height="96" alt=""></p>

<h1 align="center">Compressor</h1>

<p align="center">
Shrink photos, videos and music on Windows. Every result is verified, every change can be undone, and nothing leaves your computer.
<br><br>
<a href="https://github.com/Lanntern/Compressor/releases/latest"><b>Download for Windows</b></a> ·
<a href="#safety-first">Safety</a> ·
<a href="#privacy">Privacy</a> ·
<a href="https://ko-fi.com/lanntern">Support on Ko-fi</a>
</p>

One place for real disk savings. Drop folders or files on it (or right-click them in Explorer) and it shrinks photos, videos and music:

| Source | Targets | Engines |
|---|---|---|
| JPEG | **Keep JPEG** (lossless mozjpeg repack, or lossy with jpegli), **JPEG XL** (lossless & reversible, or lossy), WebP, AVIF | mozjpeg, cjpegli, cjxl |
| PNG / APNG, BMP, TIFF (multi-page), GIF (animated) | **Keep format** (oxipng, libimagequant, gifsicle, Deflate TIFF), **JPEG XL**, WebP, AVIF (animations kept) | oxipng, imagequant, gifsicle, cjxl, libwebp, libavif |
| HEIC, AVIF, WebP (incl. animated) | Keep format, JPEG XL, WebP, AVIF | libheif, libavif, libwebp, cjxl |
| Videos (H.264, MPEG-2/4, WMV, …) | **AV1** or **HEVC** | ffmpeg: on the GPU (NVIDIA NVENC, Intel Quick Sync, AMD AMF), or SVT-AV1/x265 on the CPU |
| Music and audio: WAV, AIFF, FLAC, ALAC, APE, WavPack | **FLAC** (lossless, verified sample for sample), or Opus / AAC / MP3 | ffmpeg (flac, libopus, aac, LAME) |
| MP3, AAC, Vorbis, WMA, Opus | Kept by default; optionally re-encoded to Opus / AAC / MP3 when the bitrate is well above the target | ffmpeg |

Also: **resize** (long edge, fit box, or percent, never enlarging), **metadata control** (keep, remove GPS location, or remove everything except rotation and colour profile), **JPEG tuning** (progressive or baseline, chroma subsampling), **before/after preview** with a zoomable split view, **output choices** (replace with originals to the Recycle Bin, save with a suffix, or save to another folder keeping the structure), **watch folders** (new files compressed automatically, running in the tray), **retry failed**, **thread count and low-priority mode**, **light/dark theme**.

## Music and audio

* **Lossless files become FLAC** by default: WAV/AIFF shrink to about half, and the result is decoded and compared with the original **sample for sample** before the original is touched. Tags and album art are kept.
* **Identical channels → mono**: "stereo" files whose left and right channels are the same (voice memos, old recordings) are stored as mono, halving them with no audible change.
* **Hi-res to CD quality** (optional): 88.2–192 kHz / 24-bit audio becomes 44.1/48 kHz, 16-bit with dither. This is the part of "removing what people can't hear" that actually saves space: ultrasonic content and dynamic range beyond human hearing.
* **Efficient lossy formats**: Opus (smallest for its quality), AAC (Apple devices) or MP3 (plays everywhere), at High / Balanced / Small. These encoders already discard inaudible detail through their psychoacoustic models, so there's no separate filtering step.
* Already-compressed music (MP3, AAC…) is left alone unless you allow re-encoding, and even then only when its bitrate is well above the target, since every lossy generation costs a little quality.
* Convert also turns audio into MP3, AAC, Opus, FLAC or WAV.

## Presets and Smart quality

* **Presets** bundle the compression options: *Photo archive* (lossless, reversible JPEG XL), *Screenshots*, *For sharing* (WebP, 2560 px, no location or personal info, 1080p HEVC), *Maximum savings* (Smart quality aiming for "good", AV1) and *Keep compatible* (same file types, visually identical). Save your own from the options panel. Use them on the Compress page, per **watched folder**, from Explorer's **Compress with Compressor** submenu, or with `--preset` on the command line.
* **Smart quality** (optional) searches per file for the smallest result that still *measures* as visually identical, high or good: SSIMULACRA2 for images (libjxl's `ssimulacra2` when present, otherwise a built-in implementation scored on the most detailed area) and VMAF for videos (when ffmpeg has libvmaf; SSIM otherwise). The chosen setting and score are shown per file and in the preview. It's slower, since every file is encoded several times.
* **Video codec**: *Automatic* (default) uses HEVC for videos straight from cameras and phones (AV1 encoders smooth away their grain and fine detail) and AV1 for everything else.
* **Visually identical** video quality test-encodes short stretches, measures them against the original (VMAF, else SSIM) and uses the smallest setting that still looks the same. Every encoded video reports how close it is to the original.
* **Only my languages**: audio and subtitle tracks in other languages are left out (the main soundtrack and untagged tracks always stay); videos already in an efficient codec are just remuxed. Your languages are saved, or taken from the computer's display languages and keyboard layouts.
* **Documents**: Word, PowerPoint, Excel and OpenDocument files and PDFs get their pictures stored smaller with identical pixels (PNG optimised, JPEG optimised without re-encoding), the rest byte-for-byte unchanged and checked; PDFs are also restructured losslessly by qpdf. Optional: scale pictures above 2560 px down. Digitally signed and password-protected documents are never changed.
* **Music for your phone** (Watch Folders): a smaller Opus/AAC/MP3 copy of your music library that keeps tags and album art, copies lossy songs as they are, only redoes what changed and removes copies of songs you deleted (never other files).
* **Video options**: maximum resolution (short side, never upscaled; portrait videos handled), frame-rate cap, and audio re-encoding at a set bitrate. Even HEVC/AV1 files are converted when one of these applies. **Preview** a video to compare a frame from a short sample encode, with the estimated final size and quality score.

It's private by design: no accounts, no telemetry, and your files never leave your computer. It goes online only to check for a newer version (weekly by default, or whenever you open it, monthly or never: Settings → About; one anonymous request to GitHub's release list) and, when you ask for it, to download Pandoc for converting documents (a pinned version, checked against its SHA-256). Nothing is downloaded or installed by itself.

## Home, Convert and Metadata

* **Home** (the start page) analyses your Pictures, Videos, Downloads, Desktop and Documents folders in the background (headers only) and lists the best opportunities ranked by *expected saving × confidence* (High: uncompressed images, old video codecs, reversible JPEG → JPEG XL; Low: already-lossy HEIC/AVIF). It also shows what's already efficient (HEVC/AV1/VP9 video, HEIC/AVIF, JPEG XL) so you don't waste time on it. Send the selection to Compress with one click.
* **Convert** changes formats without caring about size: images to PNG, JPEG, WebP, AVIF, JPEG XL, HEIC, TIFF, BMP or GIF (animations kept where the format allows; transparency flattened onto white for JPEG/BMP), videos to MP4 (H.264 or HEVC), MKV (AV1) or WebM (VP9). Results go next to the originals by default; optional "only keep if smaller".
* **Metadata** shows what each photo or video reveals (location with coordinates, camera, owner/author, date taken, comments) and removes it **without re-encoding**: JPEG/PNG/WebP/JPEG XL are edited at the container level and the compressed image data is verified byte for byte; videos are remuxed with streams copied. Choose: location · location and personal info · everything except the date taken · everything.

Everywhere: **Keep original file dates** (on by default) gives results the original's modified/created dates, and **Only keep results that save at least N % and N KB** (compression and watch folders) leaves the original untouched otherwise.

## Clean Up

A second page finds space Windows doesn't free for you. Each tool scans first, pre-selects only what's verified safe, and every change can be undone from History.

| Tool | What it finds | Action |
|---|---|---|
| Similar Photos | Re-saved, resized or edited copies and (optionally) burst shots, found by perceptual hash; the best copy (most pixels, then sharpest, then largest) stays | Recycle Bin (only true copies pre-selected) |
| Duplicates | Byte-for-byte identical files (size → partial → full checksum); keep the oldest, newest, shortest path or a copy in a chosen folder | Recycle Bin, or **replace with hard links** (same drive; paths keep working, data stored once) |
| Archives | .zip/.7z/.tar archives whose every file exists *and matches* (CRC-32 / SHA-256) in an extracted folder next to them | Recycle Bin (partial or changed extractions are only flagged) |
| Compress Apps | Installed games (Steam, Epic, GOG) and folders you add | Windows' built-in transparent **LZX** compression (`compact /exe:lzx`); undo decompresses |
| Google Takeout | Merges Google Photos Takeout archives (.zip/.tgz/folders) into one folder with each photo's taken date from its JSON (all of Google's naming quirks), album copies as hard links, edited versions kept or not; resumable | Copies (archives untouched); History can remove the merged copy |
| Caches | Browser, Discord, Spotify, Teams, Slack and Telegram caches, WhatsApp media (not pre-selected), shader caches, Lightroom previews (never Smart Previews) and old catalog backups, Adobe's media cache, crash dumps and driver installers | Recycle Bin |
| Developer | Rebuildable folders in every project (`node_modules`, `.next`, Gradle/Xcode/.NET/CMake build output, `target`, `Pods`, virtualenvs, Unity `Library`, Unreal `Intermediate`, `.terraform`…) and shared caches (npm, pnpm, pip, uv, Gradle, Cargo, Go, NuGet, DerivedData, JetBrains…). Listed only when the file that recreates it sits beside it; `dist`/`build`/`coverage` only when your `.gitignore` ignores them too. Never anything committed to git or inside an installed app. Sizes count only space that really comes back (not pnpm hard links or cloud-only files). Projects with recent edits, git activity, or something running in them (a terminal, a dev server, a Docker container) aren't pre-selected; protect a project (remembered by its git remote) from the right-click menu, and switch whole kinds off under Show. Docker: build cache, untagged images, images and stopped containers nothing uses (unique size only; volumes never listed) Also compacts WSL and Docker Desktop virtual disks (shuts WSL down after a clear warning) | Recycle Bin, then optionally free just those items; Docker data is removed by Docker; disks compacted by Windows' diskpart |
| Windows | The previous Windows installation, Windows Update leftovers, Delivery Optimization cache, error reports, memory dumps, upgrade logs, old driver versions, Compact OS (reversible); your temporary files older than a week | Removed by Windows' own tools (Disk Cleanup handlers, DISM, Delivery Optimization) with one administrator prompt; your own files to the Recycle Bin |
| Installers | `.exe`/`.msi`/… and disk images older than N days (marks installers for programs that look installed) | Recycle Bin (disk images never pre-selected) |
| Empty Items | Empty folder trees and zero-byte files (keeps meaningful ones like `__init__.py`, `.gitkeep`) | Recycle Bin (zero-byte files never pre-selected) |

## Safety first

> **Keep backups of files that matter.** Compressor is built to never lose a file (below), but it changes and
> replaces files, and it comes with **no warranty** (see [License](#license)). Run it on files you also have
> elsewhere, and check the results before you empty the Recycle Bin.

* **Nothing changes until you press Compress.** The scan only reads files and estimates savings.
* **Every result is verified before the original is touched.**
  * Lossless JPEG → JPEG XL: the original JPEG is rebuilt from the `.jxl` and compared **byte for byte**.
  * Every other lossless conversion (PNG/GIF/BMP/TIFF/WebP → anything, JPEG → JPEG repack): every decoded frame must be pixel-identical.
  * Lossy images: the output must decode with the same dimensions.
  * Videos: codec, duration, resolution and audio-track count are checked, and the start and end are test-decoded.
* **Originals go to the Recycle Bin / Trash**, never deleted outright, unless you explicitly allow permanent deletion for drives without a bin. Space is freed once you empty the bin. Or keep originals untouched by saving to a folder or with a suffix.
* **Undo** any run or single file from the History tab (or `compressor undo <run>`). Lossless JPEG XL files can be turned back into the *exact* original JPEG even after the bin has been emptied.
* Files are skipped, never half-done, if the saving is below a threshold (default 5%), if they're already efficient (HEVC/AV1/VP9 video), if the drive has no Recycle Bin (USB sticks, network drives), or if they live in program/game folders (any folder containing `.dll`/`.so` files, plus `Program Files`, `AppData`, `steamapps`, …) where converting files would break things.
* Modified/created dates are carried over to the new files.
* **Motion Photos** (a short video stored inside a Google/Samsung JPEG or HEIC) and **Live Photos** (an iPhone photo with its .MOV) are left alone, so the video part is never lost.
* **HDR video** keeps 10-bit colour and its HDR10/HLG colour description; HDR10 mastering and light-level metadata are carried over on the CPU encoders. Dolby Vision files are skipped. Interlaced video is deinterlaced; repeated frames are removed only on constant-frame-rate video.
* Size estimates learn from your real results, per kind of conversion.

## Download

Get the latest version from the **[Releases page](https://github.com/Lanntern/Compressor/releases/latest)** (Windows 10 or 11, 64-bit):

* **`Compressor-setup.exe`**: the installer (recommended). It can add Start menu and desktop shortcuts and the right-click menu in File Explorer, and uninstalls cleanly from Settings → Apps.
* **`Compressor-windows.zip`**: the portable version. Unzip it anywhere and run `Compressor.exe`.

Everything it needs, encoders included, comes in the download. Compressor isn't code-signed yet, so the first time you run it Windows may say "Windows protected your PC": choose **More info → Run anyway**.

**Checking a download (optional):** each release lists the files' SHA-256 checksums in `SHA256SUMS.txt`
(compare with `Get-FileHash Compressor-setup.exe` in PowerShell), and carries GitHub's build provenance, so
`gh attestation verify Compressor-setup.exe -R Lanntern/Compressor` confirms it was built by this repository's
workflow from its source.

To view `.jxl` files in Windows 11 Photos/Explorer, install Microsoft's free **[JPEG XL Image Extension](https://apps.microsoft.com/detail/9MZPRTH5C0TB)** from the Microsoft Store. Browsers and most chat apps don't open JPEG XL yet; use WebP for images you share.

## Run from source

Requires Python 3.11 or newer. (On Windows, the [download](#download) is simpler.)

```bash
pip install -e .
```

Then get the encoders:

* **Windows:** `python scripts/fetch_tools.py` downloads ffmpeg/ffprobe, cjxl/djxl/cjpegli and gifsicle into `tools/`.
* **Linux:** `sudo apt install ffmpeg gifsicle`, then `python scripts/fetch_tools.py --libjxl` for cjxl/djxl/cjpegli 0.11 (distro libjxl is often too old).

Start the app:

```bash
compressor
```

### Command line

```bash
compressor scan "D:\Photos"                                  # what would happen (changes nothing)
compressor run "D:\Photos" --video hevc                      # compress (asks first; -y to skip)
compressor run "D:\Photos" --images keep --jpeg lossy --quality 85 --resize-long-edge 2560 --metadata strip_gps
compressor run a.png b.gif --images webp --output folder --output-dir "D:\Small"
compressor run "D:\Videos" --preset "For sharing" --output suffix   # a preset; output and rules still apply
compressor run "D:\Photos" --images jxl --jpeg lossy --smart high     # Smart quality
compressor run "D:\Videos" --max-height 1080 --max-fps 30 --audio-bitrate 128
compressor run "D:\Music" --audio-lossless flac --cd-quality          # lossless FLAC, hi-res to CD quality
compressor run "D:\Music" --audio-lossy reencode --audio-format opus  # also shrink high-bitrate MP3s
compressor presets                          # list presets
compressor history -v                       # past runs
compressor undo 20261104-123011-023         # restore a run's originals
compressor tools                            # which encoders / GPU encoders are available
```

Run `compressor run --help` for all options (image format, lossy quality, AV1/HEVC, GPU/CPU, quality preset, thresholds).

## Video settings

| Preset | NVIDIA AV1 / HEVC (CQ) | Intel AV1 / HEVC (ICQ) | AMD AV1 / HEVC (QP) | CPU SVT-AV1 / x265 (CRF) |
|---|---|---|---|---|
| High quality | 26 / 22 | 24 / 22 | 100 / 22 | 26 / 20 |
| Balanced | 32 / 27 | 30 / 26 | 124 / 26 | 32 / 24 |
| Smallest | 38 / 32 | 36 / 30 | 148 / 30 | 38 / 28 |

(AMD's AV1 encoder counts quality on a 0–255 scale, hence the larger numbers.)

AV1 is encoded in 10-bit (better compression, no banding). HEVC stays 8-bit unless the source is 10-bit, and is tagged `hvc1` in MP4 so Windows and Apple players open it. MP4/MOV sources stay `.mp4`. MKV keeps every track, subtitle and attachment. Audio is copied as-is (converted to AAC only when MP4 can't hold it), unless an audio bitrate is set (AAC in MP4, Opus in MKV).
GPU encoding works on NVIDIA (HEVC from GTX 10-series, AV1 from RTX 40-series), Intel (HEVC on most Core CPUs with graphics from 6th generation, AV1 on Arc and Core Ultra) and AMD (HEVC on most Radeon cards, AV1 from RX 7000). **Auto** tries them in that order with a short test encode, using the first that works, and otherwise the CPU.

## Speed

Images and audio are processed one file per CPU thread (capped by memory), which measured about 25% faster than the old "half the threads" rule. With an NVIDIA RTX 40-series GPU, which has two video encoders, two videos are encoded at once. **Settings → Diagnostics → Speed Test** (or `compressor speedtest`) compares the old and new settings on generated samples, without touching your files or History.

## Keyboard

Everything works without a mouse. `F1` lists every shortcut. The essentials: `Ctrl+1`…`7` switch pages (focus lands in the page's list), `Ctrl+O` adds files to the page you're on, `Ctrl+A` / `Ctrl+Shift+A` tick all / none, `Space` ticks a row (Compress: preview), the Menu key or `Shift+F10` opens a file's actions, `Ctrl+Return` runs the page's main action and `Esc` stops it.

## Security updates

Compressor opens your files with decoders it bundles (FFmpeg, libjxl, Pillow with its WebP/JPEG libraries,
libheif, qpdf), and those get security fixes. Every release ships exact, checksum-verified versions of them,
which are checked every week for newer releases and known vulnerabilities. The app checks for a newer Compressor
weekly by default (Settings → About: whenever you open it, weekly, monthly or never), and Settings → About →
About Compressor lists every component's version. To report a security problem, see [SECURITY.md](SECURITY.md).

## Use it from an AI assistant

Compressor works with Claude and other assistants that support the Model Context Protocol: ask *"how much space
could I save in my Pictures?"* and approve what it proposes. It only uses the folders you choose, asks you in
Compressor's own window before changing anything, and every run can be undone. See [docs/mcp.md](docs/mcp.md).

## Privacy

Compressor works entirely on your computer. It has no accounts, no telemetry and no ads, and it never sends your
files or anything about them anywhere. It goes online only to:

* **check for a newer version**, weekly by default: one anonymous request to GitHub's list of releases (GitHub
  sees your IP address, like any website). Choose how often (whenever you open it, weekly, monthly or never) in
  Settings → About, or in the installer.
* **download Pandoc**, only when you convert a document and only after you agree, from Pandoc's own release page.

Your settings, history (what can be undone), presets and a small log stay in `%LOCALAPPDATA%\Compressor`
(`~/.local/share/compressor` on Linux). Uninstalling asks whether to remove them. The full
[privacy policy](PRIVACY.md) has the details.

## Support Compressor

Compressor is free, with no ads, no paid tier and nothing locked. If it saved you space or time, you can support
it on **[Ko-fi](https://ko-fi.com/lanntern)**. Donations are entirely optional and don't buy support or features;
they help keep the project going.

## License

Copyright © 2026 Lanntern.

Compressor is free software: you can redistribute it and/or modify it under the terms of the
**GNU General Public License, version 3** only, as published by the Free Software Foundation ([LICENSE](LICENSE)).

Compressor is distributed in the hope that it will be useful, but **WITHOUT ANY WARRANTY**; without even the
implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for
more details.

The Windows build includes unmodified third-party programs (FFmpeg, libjxl, gifsicle) in `tools/`, which
Compressor runs as separate programs, and libraries such as Qt (LGPL) and libimagequant (GPL-3.0). Their
licences, and where to get their source code, are in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md); the
licence files of the tools are next to them in `tools/`.

Contributions are welcome under the [Contributor License Agreement](CLA.md); see [CONTRIBUTING.md](CONTRIBUTING.md).
Security problems: [SECURITY.md](SECURITY.md).

## Name and logo

The Compressor name and icon identify the official releases from this repository and are not licensed under
the GPL. You're welcome to fork and redistribute the code under the GPL, but please give your version a
different name and icon so nobody mistakes it for the official app. Saying that it is "based on Compressor"
is fine.
