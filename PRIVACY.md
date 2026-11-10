# Privacy policy

**Effective:** 10 November 2026 · **Applies to:** Compressor for Windows and Linux, every version and every way of
getting it (installer, portable zip, Microsoft Store, source code).

Compressor works entirely on your computer. It has no accounts, no telemetry, no analytics, no ads and no crash
reporting. It never sends your files, their names or anything about them anywhere, and it doesn't collect, sell or
share personal information. The developer (Lanntern) receives nothing from the app.

## What stays on your computer

Compressor keeps these in `%LOCALAPPDATA%\Compressor` (`~/.local/share/compressor` on Linux) and in its settings
(`HKEY_CURRENT_USER\Software\Compressor` on Windows):

| What | Why | Contains |
|---|---|---|
| Settings and presets | So your choices are remembered | Your choices |
| History | So compressions can be undone | Paths and sizes of the files that were changed, and where their originals went |
| Log | To help you find out what went wrong | What each run did, errors, file paths |
| Caches | Speed (e.g. which encoders work on your graphics card) | Technical details only |
| Downloaded tools | Only if you ask for them (Pandoc) | The tool itself |

None of it leaves your computer unless you send it yourself: **Copy diagnostics** (Settings) puts versions, the
encoders found and the latest log lines on your clipboard, and you decide where to paste them.

You control it: History can be turned off or limited in Settings, and uninstalling asks whether to delete all of
it. You can also delete the folder yourself at any time.

## When Compressor goes online

Only for these, and never with anything about your files:

* **Checking for a newer version.** Weekly by default; whenever you open it, monthly, or never (Settings → About,
  or in the installer). It's one anonymous request to GitHub's list of Compressor releases. Nothing is downloaded or
  installed by itself.
* **Downloading Pandoc**, only when you convert a document that needs it and only after you agree. It comes from
  Pandoc's own release page on GitHub, a fixed version checked against its SHA-256 fingerprint.

Like any website, GitHub sees your IP address and the request; its own
[privacy statement](https://docs.github.com/site-policy/privacy-policies/github-general-privacy-statement) applies.
Links you click in Compressor (Ko-fi, GitHub, the websites of included components) open in your web browser,
where those sites' own policies apply. Donating on Ko-fi is entirely optional and unlocks nothing in the app.

## Windows features

Compressor uses Windows' own features on your computer: notifications (shown by Windows, following your
notification and Do Not Disturb settings), File Explorer's thumbnails, the taskbar, and, if installed, the
*Everything* search app to find files faster. These stay on your computer.

## Children

Compressor doesn't collect personal information from anyone, including children.

## Security

Everything Compressor keeps is stored in your own user folder, protected by your Windows account. Updates and
downloads use HTTPS. To report a security problem, see [SECURITY.md](SECURITY.md).

## Changes and contact

Changes to this policy are published here, with the date above; the full history is in this repository. Questions:
**lanntern@proton.me**.
