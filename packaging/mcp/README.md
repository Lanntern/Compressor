# Claude Desktop extension (MCP)

Wick's MCP server lives in the app (`Wick.exe mcp FOLDER...`, see `wick/mcp_server.py` and
[docs/mcp.md](../../docs/mcp.md)). The extension (`Wick.mcpb`) is a small bundle around it:

- `wick-mcp.c`: the launcher Claude Desktop starts. It finds the installed app (App Paths, or the
  "Wick.exe location" setting for the portable zip) and runs `Wick.exe mcp ...` on the same pipes.
- `../make_mcpb.py`: writes `manifest.json` (tools taken from the server itself) and zips the bundle.
- `check_extension.py`: CI's end-to-end check, run against the installed app and the portable build.
- `mcpb-manifest-v0.3.schema.json`: the manifest schema, copied unchanged from
  <https://github.com/modelcontextprotocol/mcpb> (`schemas/`; MIT / Apache-2.0, see that repository's LICENSE),
  used by `tests/test_mcp.py`.

CI builds all of it in the `package` job and attaches `Wick.mcpb` to releases.
