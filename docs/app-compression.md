# App and game compression

Wick uses Windows WOF compression through the system's `compact.exe`. Files keep the same contents and
paths. Windows expands their data when read. This requires NTFS; it is separate from video/image conversion.

| Algorithm | Starting point |
|---|---|
| XPRESS4K | Fast reads, usually less space saved; loading-sensitive games. |
| XPRESS8K | Balanced default. |
| XPRESS16K | Stronger compression with more read work. |
| LZX | Most compact; prefer infrequently used apps or cold data. |

Choose an algorithm and excluded file extensions globally or for one checked folder. Folder overrides and
the algorithm used are recorded for future update runs. Already-compressed media/archive exclusions are
editable. Exclusions are honored in administrator runs as well.

Before/current disk sizes are measurements, not estimates. Historical before sizes may refer to an older
game version. Logical size is the uncompressed length of the current files. Tracking uses local History;
deleting that history also removes the tracking baseline. Decompress works for folders compressed by
other tools too; choose an algorithm to compress again afterward.

## Loading and performance

DLL names can indicate DirectStorage, but do not prove that a game uses it. Wick leaves those folders
unselected and skips them in automatic work. Streaming assets are also flagged. Running folders are
checked again before changing them. Missing a warning does not prove that compression is performance-neutral.

Wick cannot predict FPS or load-time effects from a folder listing. Compare the same game scene/loading
sequence before and after compression; keep the same hardware, cache conditions and settings. Undo in
History decompresses the folder without changing file contents.

## Estimates and updates

Steam estimates can use CompactGUI's public results after an explicit database download. Game matching
happens locally; no installed-game list or compression results are uploaded. Community figures are not
performance benchmarks and may refer to different versions and exclusions. The source/contribution link
opens the upstream information in a browser.

Optional sampling uses temporary copies of small portions of up to eight files, with the selected WOF
algorithm, and deletes them afterward. Samples are imperfect estimates. Without public data or sampling,
the UI explicitly labels the result a rough estimate.

While Wick is open, recursive Windows change notifications watch tracked folders. Changes are allowed to
settle for a minute; a five-minute input-idle requirement is enabled by default. Periodic 15-minute checks
cover unavailable notifications. Battery policy, running folders, DirectStorage markers and administrator
requirements can postpone a run. Only eligible uncompressed files are recompressed. A metadata fingerprint
catches updates with preserved old modification times and avoids retrying an unchanged set of files.
Tray watching is opt-in, and Quit stops it. Windows startup is also optional. There is no separate service.

## CompactGUI comparison

Reviewed against the upstream README and settings on 2026-11-12:

| Capability | Wick |
|---|---|
| Four WOF algorithms | Global and per-folder choices with tradeoffs. |
| File-type exclusions | Editable globally and per folder. |
| Folder analysis and statistics | Current logical/disk sizes, first recorded before size, History and CSV export. |
| Compress/decompress and progress | Explicit folder actions, live status, cancellation and a global activity dashboard. |
| Steam/community estimates | Optional public database download, local search and Steam-ID matching. |
| Non-Steam estimates | Optional bounded native samples, otherwise clearly marked rough estimates. |
| Updates in the background | Recursive change notifications plus periodic fallback; idle/battery guards and saved folder settings. |
| Explorer integration | App/game analysis entry in Wick's classic folder submenu; no automatic compression on launch. |
| DirectStorage warnings | DLL heuristic and streaming-asset notices; no promised performance prediction. |
| Backend | System `compact.exe` in bounded batches, rather than CompactGUI's direct Win32 engine. |

Differences remain: CompactGUI's direct Win32 compression/thread scheduler is not duplicated. Wick uses
sequential bounded batches to limit disk contention. Contribution is through the upstream link/local
report export, not an integrated uploader. The dedicated app/game
Explorer command is currently in the classic submenu. This comparison is not a claim of exhaustive
feature parity with every CompactGUI release or beta.

Sources: [CompactGUI README](https://github.com/IridiumIO/CompactGUI),
[CompactGUI settings](https://github.com/IridiumIO/CompactGUI/blob/master/CompactGUI.Core/Settings/Settings.cs),
[Microsoft compact reference](https://learn.microsoft.com/windows-server/administration/windows-commands/compact).
