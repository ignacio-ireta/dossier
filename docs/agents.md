# Agent access

Agents reach a workspace three ways: the MCP server, the `dsr` CLI with
`--json`, and the generated navigation files.

## MCP server

```bash
dsr mcp serve                                   # stdio server for this workspace
dsr mcp install                                 # prints `claude mcp add ...` for Claude Code
dsr mcp install --yes --scope user              # runs it
dsr mcp install --client json                   # mcpServers snippet for other clients
```

Tools (all read-only; results include document keys and line ranges):

| Tool | Returns |
|---|---|
| `status` | Workspace, policy, documents indexed, index freshness, latest run |
| `overview` | `index/INDEX.md` |
| `tree(folder="")` | `index/TREE.md`, or one folder's page |
| `search(query, limit, source, path_prefix, doc_type, kind)` | Hybrid search hits: doc, title, heading, lines, snippet, card, corpus path |
| `card(doc)` | The document card: summary, key points, outline with line numbers |
| `read(doc, lines, max_chars)` | Converted text, optionally a line range |

Meeting and template tools are added when those features are present. A tool
that writes (rendering a document) exists only when `[agent] allow_render = true`.

The server re-reads the policy on every call; with `processing = "prohibited"`
every tool refuses. Tool errors carry dossier's error code and message.

## CLI for agents

Every command accepts `--json` (before the command name):

```bash
dsr --json status
dsr --json search "approved budget" -k 5
dsr --json read main/plans/charter.docx --lines 20-40
dsr --json tree main/plans
```

Errors are JSON too (`{"error": {"code": ..., "message": ..., "exit_status": ...}}`),
with stable exit statuses: 78 policy, 75 needs attention or locked, 69 a model
or tool unavailable, 2 usage.

## Navigation files

Each index run writes, at the workspace root:

- `AGENTS.md`: layout, how to work, how to cite, rules (policy, no editing
  generated folders, no inferring missing facts).
- `llms.txt`: links to INDEX, TREE, the agent guide, folder pages and cards.

A hand-written `AGENTS.md` (one without dossier's marker line) is never overwritten.

## Claude Code skill

```bash
dsr skill install          # copies the dossier skill to ~/.claude/skills/dossier
dsr skill path             # where the bundled skill lives
```
