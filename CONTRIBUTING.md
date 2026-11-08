# Contributing to Compressor

Thanks for helping! Bug reports, ideas and pull requests are all welcome.

## Reporting problems

- **Bugs:** open an issue with what you did, what happened, and what you expected. Settings → About → About
  Compressor → **Copy Versions** gives the version details worth pasting in.
- **Security problems:** please don't open a public issue; see [SECURITY.md](SECURITY.md).

## Pull requests

1. **Sign the Contributor License Agreement.** On your first pull request a bot asks you to sign
   [CLA.md](CLA.md) by posting a comment; that one comment covers all your future contributions. You keep the
   copyright in your work. The agreement lets the maintainer keep the project's licensing flexible (for
   example, move to a newer licence) without tracking down every contributor. Pull requests can't be merged
   until it's signed.
2. **Keep it focused.** One change per pull request, with a short description of what it changes and why.
   For a bigger change, open an issue first so we can agree on the approach.
3. **Run the checks** (CI runs them too):

   ```bash
   pip install --require-hashes -r requirements.txt
   pip install --no-deps . pytest ruff==0.16.9 mypy==2.3.1
   ruff check .
   mypy compressor
   python -m pytest
   ```

   Tests that need the encoders skip themselves when the tools aren't installed;
   `python scripts/fetch_tools.py` downloads them (see the README).
4. **Add a test** for a bug fix or new behaviour where you can.
5. **Be careful with people's files.** Compressor replaces and deletes files, so anything touching that path
   needs to keep the existing guarantees: verify before replacing, originals to the Recycle Bin, everything
   undoable.

## Licence

Compressor is licensed under the GNU General Public License, version 3 only ([LICENSE](LICENSE)). By
contributing you agree to [CLA.md](CLA.md).

The Compressor name and icon are not covered by the code's licence; see "Name and logo" in the
[README](README.md#name-and-logo).
