"""End-to-end check of the MCP extension, as Claude Desktop would run it: start the server command, talk to it
over stdin/stdout, and check the answers. Used in CI against the built launcher and the installed app (which
proves the windowed app really serves MCP over the pipes it's given).

    python packaging/mcp/check_extension.py FOLDER -- COMMAND [ARGS...]

FOLDER is passed as the allowed folder; a few test pictures are made in it. Nothing is compressed (that needs a
person's approval, which can't be given here): the check stops at the plan.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if "--" not in argv or argv.index("--") != 1:
        sys.exit(__doc__)
    folder, command = Path(argv[0]).resolve(), argv[2:]
    folder.mkdir(parents=True, exist_ok=True)
    from PIL import Image

    for i in range(3):
        Image.new("RGB", (1200, 800), (i * 60, 100, 180)).save(folder / f"check {i}.png", compress_level=0)
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "ci", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "list_allowed_folders", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "plan_compression", "arguments": {"paths": [str(folder)], "list_files": 1}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "plan_compression", "arguments": {"paths": [str(folder.parent)]}}},
    ]
    proc = subprocess.run([*command, "--keep-originals=false", "--max-files=1000", "--", str(folder)],
                          input="".join(json.dumps(m) + "\n" for m in messages).encode(), capture_output=True,
                          timeout=180)
    replies = {}
    for line in proc.stdout.decode("utf-8").splitlines():
        if line.strip():
            msg = json.loads(line)                     # (anything but JSON-RPC on stdout fails here)
            replies[msg.get("id")] = msg
    print("stderr:", proc.stderr.decode("utf-8", "replace")[-2000:])

    def text(i):
        result = replies[i]["result"]
        return result["content"][0]["text"], bool(result.get("isError"))

    assert replies[1]["result"]["serverInfo"]["name"] == "compressor", replies
    names = {t["name"] for t in replies[2]["result"]["tools"]}
    assert names == {"list_allowed_folders", "plan_compression", "compress_planned_files", "list_history",
                     "undo_run"}, names
    allowed, err = text(3)
    assert not err and json.loads(allowed)["allowed_folders"], allowed
    plan, err = text(4)
    assert not err and json.loads(plan)["files_to_compress"] == 3, plan
    outside, err = text(5)
    assert err and "outside" in outside, outside             # the parent folder isn't allowed
    assert all((folder / f"check {i}.png").exists() for i in range(3))
    print(f"OK: {len(names)} tools; plan: {json.loads(plan)['estimated_saving']} to save; outside refused; "
          f"exit code {proc.returncode}")
    return 0 if proc.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
