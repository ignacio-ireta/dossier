# Changelog

## 0.1.0 (2026-11-05)

First shared release.

### Corpus
- Converts DOCX, PPTX, XLSX/CSV (one CSV per sheet), PDF (with Apple Vision
  OCR for scans and images), EML/MSG, HTML, Markdown, text and JSON to
  Markdown, with dates and counts in front matter.
- `dsr sync` processes only what changed (content hashes), detects moves,
  archives deletions, guards against mass deletes and skips cloud-only files.
- `dsr schedule install` runs it daily and when a source folder changes
  (launchd).

### Index and search
- A summary card per document, a summary per folder and a map of the whole
  tree (`INDEX.md`, `TREE.md`, `index.json`), all from small local models.
- Hybrid search: SQLite full text plus vectors, fused by rank; optional qmd
  backend; `dsr models bench` measures fact recall and retrieval.

### Agents
- MCP server (`dsr mcp serve`, read-only by default), `--json` on every
  command, generated `AGENTS.md` and `llms.txt`, and a Claude Code skill.

### Meetings
- Transcription with MLX Whisper (Apple Silicon) or faster-whisper, speaker
  separation with pyannote, for any common audio or video format.
- Each meeting's language is detected among the workspace's languages
  (e.g. Spanish and English) or set per meeting; summaries are written in it.
- Speaker review: short clips of each unnamed speaker with an audio-quality
  check; name them in the terminal (`dsr meet identify`) or on a review page.
- Opt-in voiceprints, with recorded consent, suggest names in later meetings.
- Summaries with decisions, action items and open questions; every item
  cites a time checked against the transcript; owners and due dates only
  when stated.
- One transcript block per intervention; a per-account glossary with
  multi-word fixes and per-language tables.

### Documents
- DOCX, PPTX and PDF from Markdown with Mermaid and draw.io diagrams.
- Template packs per client or account (`extends`, kinds, cover tokens);
  `dsr templates import` turns a branded Word or PowerPoint file into a pack.

### Safety and operations
- Workspace policy gate: nothing is read until approved; external model
  providers only when the policy names them.
- Content-free logs and run reports; secrets in the system keychain.
- `dsr doctor` and `dsr setup` check and install what each feature needs.

### Known limitations
- macOS on Apple Silicon only for now; Windows backends are planned
  (`docs/windows-port.md`).
- The audio-quality thresholds were tuned on synthetic voices; real
  recordings may call for adjustments.
- Speaker review shows diarization mistakes but cannot correct them.
