# Wick features

The full list of what Wick can do, and how. For an overview, see the [README](../README.md).

- [Photos and pictures](#photos-and-pictures)
- [Videos](#videos)
- [Music and audio](#music-and-audio)
- [Documents](#documents)
- [Presets and Smart quality](#presets-and-smart-quality)
- [Home, Convert and Metadata](#home-convert-and-metadata)
- [Clean Up](#clean-up)
- [Safety checks](#safety-checks)
- [Command line](#command-line)
- [Keyboard](#keyboard)
- [Updates and verifying a release](#updates-and-verifying-a-release)

## Photos and pictures

| Source | Can become | Encoders |
|---|---|---|
| JPEG | JPEG XL (lossless and reversible, or lossy), a smaller JPEG (lossless repack, or lossy), WebP, AVIF | cjxl, mozjpeg, cjpegli |
| PNG, APNG, BMP, TIFF (multi-page), GIF (animated) | JPEG XL, the same format made smaller, WebP, AVIF; animations are kept | cjxl, oxipng, libimagequant, gifsicle, libwebp, libavif |
| HEIC, AVIF, WebP (including animated) | JPEG XL, the same format, WebP, AVIF | cjxl, libheif, libavif, libwebp |

Options:

| Option | Choices |
|---|---|
| Resize | Long edge, fit inside a box, or percent. Never enlarges. |
| Metadata | Keep it all, remove GPS location, or remove everything except rotation and color profile |
| JPEG output | Progressive or baseline; chroma subsampling |
| Where results go | Replace the original (original to the Recycle Bin), save next to it with a suffix, or save to another folder with the same structure |
| Only keep worthwhile results | Keep a result only if it saves at least N % and N KB |
| File dates | Results keep the original's created and modified dates (on by default) |

**Preview** any file to compare before and after in a zoomable split view, with the expected size.

## Videos

- **Codec:** *Automatic* uses HEVC for videos straight from cameras and phones (AV1 encoders tend to smooth
  away their grain and fine detail) and AV1 for everything else. Videos already in HEVC, AV1 or VP9 are skipped.
- **Graphics card or processor:** *Auto* tries NVIDIA (NVENC), Intel (Quick Sync) and AMD (AMF) with a short
  test encode, and uses the processor (SVT-AV1 or x265) if none works.

  | GPU | HEVC | AV1 |
  |---|---|---|
  | NVIDIA | GTX 10-series and newer | RTX 40-series and newer |
  | Intel | Most Core CPUs with graphics, 6th generation and newer | Arc, Core Ultra |
  | AMD | Most Radeon cards | RX 7000 and newer |

- **Quality presets:**

  | Preset | NVIDIA AV1 / HEVC (CQ) | Intel AV1 / HEVC (ICQ) | AMD AV1 / HEVC (QP) | Processor SVT-AV1 / x265 (CRF) |
  |---|---|---|---|---|
  | High quality | 26 / 22 | 24 / 22 | 100 / 22 | 26 / 20 |
  | Balanced | 32 / 27 | 30 / 26 | 124 / 26 | 32 / 24 |
  | Smallest | 38 / 32 | 36 / 30 | 148 / 30 | 38 / 28 |

  AMD's AV1 encoder uses a 0 to 255 scale, hence the larger numbers.

- **Visually identical:** test-encodes short stretches, measures them against the original, and uses the
  smallest setting that still looks the same. Every encoded video reports its score.
- **Quality measure:** VMAF by default. SSIMULACRA2, Butteraugli or XPSNR (FFmpeg 7.1 and newer) can be
  chosen instead; if the chosen one isn't available, VMAF is used.
- **Scene by scene** (processor encodes of videos over 90 seconds): the video is split at scene changes, the
  parts are encoded side by side, and they're joined at the original frame times. With Smart quality, each scene
  gets the quality it needs. If encoding stops (you stop it, the app crashes, or the PC restarts), it picks up
  where it left off next time. Unused parts are removed after 14 days.
- **Film grain** (AV1 on the processor): grain is removed before encoding and similar grain is re-created during
  playback, which saves a lot on grainy footage. The grain level is measured per video; clean videos are left as
  they are.
- **Only my languages:** leaves out audio and subtitle tracks in other languages. The main soundtrack and
  untagged tracks always stay.
- **Limits:** maximum resolution (never upscales), maximum frame rate, and an audio bitrate.
- **Format details:** AV1 is 10-bit. HEVC stays 8-bit unless the source is 10-bit, and is tagged so Windows and
  Apple players open it. MP4 and MOV stay MP4; MKV keeps every track, subtitle and attachment. HDR10 and HLG
  video keep 10-bit color and their HDR information. Interlaced video is deinterlaced.
- **Two at once:** on GPUs with two video encoders (NVIDIA RTX 40-series), two videos are encoded at the same
  time. Pictures and audio use one file per processor thread.

## Music and audio

| Source | Default | Options |
|---|---|---|
| WAV, AIFF, FLAC, ALAC, APE, WavPack | FLAC, about half the size of WAV/AIFF, checked sample for sample. Tags and album art are kept. | Opus, AAC or MP3 at High, Balanced or Small |
| MP3, AAC, Vorbis, WMA, Opus | Left alone | Re-encoded, but only when the bitrate is well above the target |

- **Identical channels become mono:** "stereo" files whose two channels are the same (voice memos, old
  recordings) are stored as mono, at half the size with no audible change.
- **Hi-res to CD quality** (optional): 88.2 to 192 kHz, 24-bit audio becomes 44.1 or 48 kHz, 16-bit with dither.
- **Music for your phone** (Watch Folders): keeps a smaller Opus, AAC or MP3 copy of your music library, with tags
  and album art. Only changed songs are redone, and copies of songs you deleted are removed.

## Documents

Word, PowerPoint, Excel, OpenDocument and PDF files get their pictures stored smaller with identical pixels;
everything else in the file stays byte-for-byte the same and is checked. PDFs are also restructured
losslessly with qpdf. Optionally, pictures larger than 2560 px are scaled down. Signed and password-protected
documents are never changed.

## Presets and Smart quality

| Preset | For |
|---|---|
| Photo archive | Lossless, reversible JPEG XL |
| Screenshots | Screen captures |
| For sharing | WebP at 2560 px without location or personal info, 1080p HEVC video |
| Maximum savings | Smart quality aiming for "good", AV1 video |
| Keep compatible | Same file types, visually identical |

Save your own from the options panel. Presets work on the Compress page, per watched folder, from the
**Compress with Wick** menu in File Explorer, and with `--preset` on the command line.

**Smart quality** tries several settings per file and keeps the smallest result that still measures as
visually identical, high or good. Pictures are measured with SSIMULACRA2 and videos with VMAF. It's slower,
because each file is encoded several times. The chosen setting and score are shown for each file.

## Home, Convert and Metadata

- **Home** reads the headers of the files in your Pictures, Videos, Downloads, Desktop and Documents folders in
  the background and ranks the best savings by size and confidence. It also shows what's already efficient, so
  you don't spend time on it.
- **Convert** changes formats regardless of size. Pictures: PNG, JPEG, WebP, AVIF, JPEG XL, HEIC, TIFF, BMP or GIF.
  Videos: MP4 (H.264 or HEVC), MKV (AV1) or WebM (VP9). Audio: MP3, AAC, Opus, FLAC or WAV.
- **Metadata** shows what each photo or video reveals (location, camera, author, date taken, comments) and
  removes it without re-encoding. Choose: location; location and personal info; everything except the date
  taken; or everything.

## Clean Up

Each tool scans first and only preselects what's safe. Everything can be undone from History.

| Tool | Finds | What happens |
|---|---|---|
| Similar Photos | Resized, re-saved or edited copies, and optionally burst shots. The best copy stays. | Recycle Bin |
| Duplicates | Identical files | Recycle Bin, or replaced with hard links (same drive, data stored once) |
| Archives | .zip, .7z and .tar files that were already extracted, with every file matching | Recycle Bin |
| Compress Apps | Installed games (Steam, Epic, GOG) and folders you add | Windows' built-in transparent compression |
| Google Takeout | Google Photos exports | Merged into one folder with the correct dates and albums |
| Caches | Browser, Discord, Spotify, Teams, Slack, Telegram, shader and Adobe caches, crash dumps, driver installers | Recycle Bin |
| Developer | `node_modules`, build output, virtual environments and package caches that can be rebuilt; Docker leftovers; WSL and Docker disks | Recycle Bin; Docker data removed by Docker; disks compacted |
| Windows | Previous Windows installation, update leftovers, error reports, old drivers, old temporary files | Removed with Windows' own tools |
| Installers | Installers and disk images older than N days | Recycle Bin |
| Empty Items | Empty folders and empty files | Recycle Bin |

**Developer** only lists a folder when the file that recreates it is next to it, never touches anything
committed to git, and doesn't preselect projects you've worked on recently. You can protect a project from the
right-click menu.

## Safety checks

Before an original is replaced:

| Result | Check |
|---|---|
| Lossless JPEG XL from JPEG | The original JPEG is rebuilt from it and compared byte for byte |
| Other lossless pictures | Every frame decodes pixel-identical |
| Lossy pictures | Decodes at the right size |
| FLAC | Decoded and compared sample for sample |
| Videos | Codec, length, resolution and audio tracks checked; start and end test-decoded |

Wick skips:

- files in program and game folders (`Program Files`, `AppData`, `steamapps`, any folder with `.dll` files);
- drives without a Recycle Bin (USB sticks, network drives), unless you allow permanent deletion;
- Motion Photos and Live Photos, so their video part isn't lost;
- Dolby Vision video;
- results that save less than the threshold (5% by default).

Size estimates learn from your actual results.

## Command line

```bash
wick scan "D:\Photos"                                   # what would happen (changes nothing)
wick run "D:\Photos" --video hevc                       # compress (asks first; -y to skip)
wick run "D:\Photos" --images keep --jpeg lossy --quality 85 --resize-long-edge 2560 --metadata strip_gps
wick run a.png b.gif --images webp --output folder --output-dir "D:\Small"
wick run "D:\Videos" --preset "For sharing" --output suffix
wick run "D:\Photos" --images jxl --jpeg lossy --smart high
wick run "D:\Videos" --max-height 1080 --max-fps 30 --audio-bitrate 128
wick run "D:\Music" --audio-lossless flac --cd-quality
wick run "D:\Music" --audio-lossy reencode --audio-format opus
wick presets                          # list presets
wick history -v                       # past runs
wick undo 20261104-123011-023         # restore a run's originals
wick tools                            # available encoders and GPUs
wick speedtest                        # time compression on generated samples
```

`wick run --help` lists every option.

## Keyboard

| Keys | Action |
|---|---|
| `F1` | List every shortcut |
| `Ctrl+1` to `Ctrl+7` | Switch pages |
| `Ctrl+O` | Add files |
| `Ctrl+A` / `Ctrl+Shift+A` | Select all / none |
| `Space` | Select a row (on Compress: preview it) |
| Menu key or `Shift+F10` | A file's actions |
| `Ctrl+Enter` | Run the page's main action |
| `Esc` | Stop |

## Updates and verifying a release

Wick checks GitHub for a new version weekly by default. Change this (on every launch, weekly, monthly or never)
in Settings → About. The bundled decoders (FFmpeg, libjxl, Pillow, libheif, qpdf) are pinned, checksum-verified
versions, checked weekly for new releases and security advisories. Settings → About → About Wick lists each
component's version.

### Verifying a release

Each release lists SHA-256 checksums in `SHA256SUMS.txt`. Compare with:

```powershell
Get-FileHash Wick-setup.exe
```

Releases also carry GitHub build provenance. This confirms a file was built by this repository's workflow:

```bash
gh attestation verify Wick-setup.exe -R Lanntern/Wick
```
