# The corpus: conversion and sync

`dsr convert` (and the first stage of `dsr sync`) mirrors every `[[sources]]`
folder into `corpus/<source>/` as LLM-friendly files. It never modifies sources.

## What each file becomes

| Source | Output |
|---|---|
| `.docx` `.docm` | Markdown: headings, paragraphs, lists, tables, links, image markers |
| `.pptx` `.pptm` | One `## Slide N: title` section per slide, plus speaker notes |
| `.xlsx` `.xlsm` `.csv` `.tsv` | A Markdown preview per sheet (size + first rows, `value (=formula)`) and the full sheet as CSV in `<file>.sheets/` |
| `.pdf` | One `## Page N` section per page; tables detected; large lines become headings. Pages with no text layer are OCR'd |
| `.eml` `.msg` | Subject as title, From/To/Cc/Date, body, attachment names |
| `.html` `.htm` | Headings, paragraphs, lists, tables, code |
| `.md` `.txt` `.rst` `.log` `.json` | Text kept as-is (JSON pretty-printed) |
| Images (`.png` `.jpg` `.tiff` `.heic`...) | OCR text |
| Audio and video | Not converted; counted as `media` for the meetings stage |

Every output is `corpus/<source>/<path>.<ext>.md` with front matter
(`source`, `sha256`, `type`, `extractor`, `title`, ...) and a
`<path>.<ext>.meta.json` sidecar (sections, warnings, sheets, word count).

## How a run decides what to do

1. Walk the source folder (hidden files, Office lock files, `exclude` globs and
   cloud-only placeholders are skipped; directory symlinks are not followed).
2. Hash files whose size or modification time changed (`--rehash` hashes all).
3. Compare with the snapshot in `.dossier/state/convert/<source>.json`:
   - **added / updated**: extract and write outputs;
   - **moved** (same content, new path): move outputs, no re-extraction;
   - **deleted**: move outputs to `.dossier/archive/<run>/`;
   - **failed**: recorded in `<source>.errors.json`; retried when the file
     changes or with `--retry-failed`.
4. Changing an extractor's version re-extracts the files it handles.

## Safety

- Nothing runs until `dsr policy approve`.
- A missing source folder (offline drive, unsynced cloud folder) is reported and
  skipped; nothing is deleted.
- An emptied folder, or a run that would delete more than
  `convert.mass_delete_ratio` of a source, is blocked (exit 75) until you re-run
  with `--confirm-deletes`.
- `--dry-run` shows the plan and writes nothing.

## Settings (`[convert]`)

| Key | Default | Meaning |
|---|---|---|
| `ocr` | `auto` | `auto`: OCR images and pages without text; `force`: OCR every PDF page; `off` |
| `max_file_mb` | 200 | Larger documents are skipped |
| `max_sheet_rows` | 50000 | Rows kept per sheet |
| `preview_rows` | 20 | Rows shown per sheet in the Markdown |
| `mass_delete_ratio` | 0.5 | Share of a source a run may delete without confirmation |

## Scheduling

```bash
dsr schedule install --at 07:30 --watch   # daily, plus whenever a source folder changes
dsr schedule status
dsr schedule remove
```

Scheduled runs use `dsr sync --scheduled`: local models only (unless the policy
allows external ones for unattended runs), the machine is kept awake while the
run lasts, and a desktop notification is shown when a run fails or needs attention.
