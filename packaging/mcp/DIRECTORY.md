# Anthropic Software Directory: how the extension complies

Checked against the [Anthropic Software Directory Policy](https://support.claude.com/en/articles/13145358-anthropic-software-directory-policy)
and [Terms](https://support.claude.com/en/articles/13145338-anthropic-software-directory-terms) (read November
2026), the [MCPB 0.3 manifest spec](https://github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md) and the
MCP specification (2025-11-25, tools: security considerations). Re-check before each submission: the policy
changes.

## 1. Safety and security

| Rule | How |
|---|---|
| A, B: Usage Policy; high-risk uses need a human in the loop | Not a high-risk use; every change still needs the user's approval in Wick's own dialog |
| C: no evading Claude's guardrails | Tool descriptions and server instructions only describe Wick's tools |
| D: protect privacy | Runs locally; sends nothing; [PRIVACY.md](../../PRIVACY.md) says what reaches the conversation |
| E: only the data needed; no conversation data, even in logs | Plans list at most `list_files` files (10 by default); the log records the tool name only |
| F: no IP infringement | GPL-3.0 code by the maintainer; bundled tools' licences in THIRD-PARTY-NOTICES.md |
| G: no reading Claude's memory or chats | Never |

## 2. Compatibility (tool descriptions)

| Rule | How |
|---|---|
| A, B: narrow, accurate descriptions | Each says what it does, whether it changes files, and when to use it; checked against behaviour by tests |
| C: no confusion with other software | Names are specific (`plan_compression`, `compress_planned_files`, ...) |
| D, E: no steering Claude to other tools or away from them | Descriptions mention only Wick's own tools |
| F: no instructions pulled from elsewhere | None; file names are passed as data, cleaned of hidden/control characters |
| G: nothing hidden or encoded | All text is plain and in the source |

## 3. Developer requirements

| Rule | How / to do |
|---|---|
| A: privacy policy | `privacy_policies` in the manifest → PRIVACY.md (AI assistants section) |
| B: contact and support | `support` → GitHub issues; lanntern@proton.me (SECURITY.md, PRIVACY.md). **Submit with a verified address.** |
| C: documentation and troubleshooting | [docs/mcp.md](../../docs/mcp.md) (`documentation` in the manifest) |
| D: test account with sample data | No account needed. Sample data: `check_extension.py` makes test pictures; docs/mcp.md says how to try it |
| E: three example prompts | docs/mcp.md, *Examples* |
| F: own every endpoint | No network endpoints: the launcher only starts the local app |
| G, H: maintain; report material changes | **To do after listing:** tell Anthropic when tools, descriptions or data handling change |
| I: agree to the Directory Terms | **To do at submission** (includes keeping a way to receive security reports: SECURITY.md) |

## 4. Unsupported uses

None apply: no payments, no generated media, no ads, no adult content.

## 5. MCP servers

| Rule | How |
|---|---|
| A: helpful errors | Tool errors are results with `isError` and say what to do next; protocol errors use JSON-RPC codes |
| B: frugal with tokens | Compact JSON; totals by kind; the file list is optional (`list_files`, 0 to 100) |
| C: names ≤ 64 characters | Longest is 22; checked by a test |
| D, F: OAuth, Streamable HTTP | Remote servers only; this one is local (stdio) |
| E: `readOnlyHint`, `destructiveHint`, `title` on every tool | All five tools have them, plus `idempotentHint` and `openWorldHint`; checked by a test |
| G: current dependencies | No dependencies beyond the app itself (Python standard library); the launcher is plain C, built in CI |

## MCP specification (server MUSTs)

- **Validate all inputs:** types, ranges and unknown or missing arguments are checked against each tool's schema.
- **Access controls:** allowed folders only, fully resolved; system, program, app-data and History locations
  refused; a person approves every change outside the agent's reach.
- **Rate limits:** per tool (30 plans a minute, 10 runs and 10 undos an hour); one job at a time.
- **Sanitise outputs:** control, bidirectional and zero-width characters replaced; lengths bounded.

## Submitting (after the rename)

1. Take `Wick.mcpb` from the release (CI builds it and checks it against the installed and portable app).
2. Submit it through Claude's extension submission form, with: the privacy policy URL, a verified contact address,
   the docs link, the three example prompts, and "no account needed; sample data: see docs/mcp.md".
3. Note for reviewers: changes require the user's approval in a Wick dialog, so a reviewer testing
   `compress_planned_files` or `undo_run` needs to click OK in it.
