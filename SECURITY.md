# Security policy

Wick opens files from anywhere (photos, videos, PDFs, archives, documents) with bundled decoders, so
security problems matter here.

## Reporting a vulnerability

Please **don't open a public issue.** Email **lanntern@proton.me** with:

- what the problem is and what an attacker could do with it,
- how to reproduce it (a sample file helps; mention it before sending anything large),
- the Wick version (Settings → About → About Wick → **Copy Versions**).

I aim to reply within a week. Please give a reasonable time to fix it (normally up to 90 days) before
disclosing it publicly; you'll be credited in the release notes unless you'd rather not be.

## Supported versions

Only the newest release gets security fixes. Installed copies check for a newer version (weekly by default; the
interval can be changed in Settings), and a release that fixes a security problem says so.

## Bundled components

Many problems turn out to be in a bundled component (FFmpeg, libjxl, Pillow, pikepdf, Qt, …). Report those
here too; if a fix is needed upstream, it will be reported there and Wick updated once it's released.
Components are pinned and checked weekly for known vulnerabilities (see "How bundled components are kept up to
date" in [CONTRIBUTING.md](CONTRIBUTING.md)).
