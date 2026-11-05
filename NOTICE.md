# Notice

dossier generalizes six earlier single-project command-line tools, all
MIT-licensed. Modules derived from them, and what they derive from:

| dossier module | Derived from |
|---|---|
| `dossier.core.io`, `dossier.core.lock` | `docmd` (document conversion), `meeting-context` |
| `dossier.convert` | `docmd` |
| `dossier.index` | `knowledge-index` |
| `dossier.llm` | `knowledge-index`, `meeting-context` |
| `dossier.meet` | `meeting-transcription`, `meeting-context` |
| `dossier.docs` | `markdown-docx-styler`, `md2pptx` |

Project-specific vocabularies, glossaries, templates and test material in those
tools were not carried over. dossier ships only generic code, a neutral template
pack, and synthetic test fixtures.
