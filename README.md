<p align="center"><img src="docs/icon.png" width="96" height="96" alt=""></p>

<h1 align="center">Wick</h1>

<p align="center">
Get gigabytes back from the photos, videos and music already on your PC.
<br>
Free and open source for Windows 10 and 11.
<br><br>
<a href="https://github.com/Lanntern/Wick/releases/latest"><b>Download</b></a> ·
<a href="docs/features.md">Features</a> ·
<a href="PRIVACY.md">Privacy</a> ·
<a href="https://ko-fi.com/lanntern">Ko-fi</a>
</p>

Point Wick at a folder, or right-click one in File Explorer. It shows how much space you'd get back before it
touches anything, then stores each file in a smaller format. Your originals go to the Recycle Bin, and History
can put them back.

## What it does

| Page | Use it to |
|---|---|
| **Home** | See where the easy savings are in your Pictures, Videos, Downloads, Desktop and Documents |
| **Compress** | Shrink photos, videos, music and Office/PDF documents |
| **Convert** | Change file formats, e.g. HEIC to JPEG or a video to MP4 |
| **Metadata** | See what a photo or video gives away (location, camera, author) and remove it |
| **Clean Up** | Find duplicates, similar photos, old installers, caches, build folders and Windows leftovers |
| **Watch Folders** | Compress new files as they arrive, from the tray |
| **History** | Undo any run, or a single file |

## What it shrinks

| Your files | By default they become | Other choices |
|---|---|---|
| JPEG photos | JPEG XL, lossless: it converts back to the exact original JPEG | Smaller JPEG (same format), WebP, AVIF |
| PNG, GIF, BMP, TIFF | JPEG XL, pixel-identical | The same format made smaller, WebP, AVIF |
| HEIC, AVIF, WebP | JPEG XL (already compressed, so savings are smaller) | The same format, WebP, AVIF |
| Videos (H.264, MPEG, WMV, ...) | AV1 or HEVC, picked per video | Your choice of codec, resolution and frame rate |
| WAV, AIFF, APE, WavPack | FLAC, about half the size, sample-for-sample identical | Opus, AAC or MP3 |
| Word, Excel, PowerPoint, PDF | The same file with its pictures stored smaller | Scaling large pictures down |

Videos use your graphics card when it can (NVIDIA, Intel or AMD) and the processor otherwise. Everything Wick
needs comes in the download.

**Presets** cover the common cases: *Photo archive*, *Screenshots*, *For sharing*, *Maximum savings* and *Keep
compatible*. **Smart quality** tests each file and keeps the smallest version that still measures as
visually identical. [All features](docs/features.md)

## Your files stay safe

- **Nothing changes until you press Compress.** Scanning only reads.
- **Each result is checked before the original is replaced.** Lossless results must match the original pixel
  for pixel (or byte for byte); videos are test-played at the start and end.
- **Originals go to the Recycle Bin**, not straight to deletion. Or save the results next to them or in
  another folder instead.
- **Undo is in History.** JPEG XL photos can even be turned back into the exact original JPEG after the bin is
  emptied.
- **Wick leaves alone** program and game folders, drives without a Recycle Bin, Motion and Live Photos, Dolby
  Vision video, and files that wouldn't get meaningfully smaller.

> Wick is built not to lose files, but it comes with **no warranty**. Keep backups of files that matter, and
> check the results before you empty the Recycle Bin.

## Download

From the **[Releases page](https://github.com/Lanntern/Wick/releases/latest)** (64-bit Windows 10 or 11):

| File | What it is |
|---|---|
| `Wick-setup.exe` | Installer (recommended). Adds Start menu shortcuts and the File Explorer right-click menu. |
| `Wick-windows.zip` | Portable version. Unzip anywhere and run `Wick.exe`. |

Wick isn't code-signed yet, so Windows may show "Windows protected your PC" the first time. Choose
**More info → Run anyway**. To check a download, see [Verifying a release](docs/features.md#verifying-a-release).

To see `.jxl` photos in Photos and File Explorer, install Microsoft's free
[JPEG XL Image Extension](https://apps.microsoft.com/detail/9MZPRTH5C0TB). Most browsers and chat apps can't
open JPEG XL yet, so use WebP for pictures you send to people.

## Privacy

No account, no ads, no telemetry. Your files are processed on your PC and never uploaded. Wick goes online for
two things only: checking GitHub for a new version (weekly by default; you can turn it off), and downloading
Pandoc if you ask it to convert a document. Details are in the [privacy policy](PRIVACY.md).

## More

- **Command line:** `wick scan`, `wick run`, `wick undo` and more. See [Command line](docs/features.md#command-line).
- **AI assistants:** Claude and other MCP apps can plan and run compressions in folders you allow, and Wick
  asks you in its own window before any change. See [docs/mcp.md](docs/mcp.md).
- **Keyboard:** everything works without a mouse. Press `F1` in the app for the shortcuts.
- **Run from source:** see [CONTRIBUTING.md](CONTRIBUTING.md#running-from-source).
- **Security issues:** see [SECURITY.md](SECURITY.md).

## Support Wick

Wick is free, with no paid tier and nothing locked. If it saved you some space, a star here or a tip on
[Ko-fi](https://ko-fi.com/lanntern) is appreciated.

## License

Copyright © 2026 Lanntern. Wick is free software under the **GNU General Public License, version 3** only
([LICENSE](LICENSE)), and comes **without any warranty**, without even the implied warranty of merchantability
or fitness for a particular purpose.

The Windows build includes third-party programs and libraries (FFmpeg, libjxl, gifsicle, Qt and others). Their
licenses and source code links are in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). Contributions are
welcome under the [Contributor License Agreement](CLA.md); see [CONTRIBUTING.md](CONTRIBUTING.md).

**Name and logo:** the Wick name and icon identify the official releases from this repository and aren't
covered by the GPL. Forks are welcome under the GPL, but please give yours a different name and icon. "Based
on Wick" is fine.
