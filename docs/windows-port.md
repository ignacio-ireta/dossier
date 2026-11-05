# Windows port checklist

dossier is written so that only the platform backends remain for Windows.
Everything OS-specific sits behind the interfaces in
`src/dossier/platform/base.py`, and `tests/test_platform_neutrality.py` keeps the
rest of the code free of macOS-only calls (it also imports every module with
`sys.platform` set to `win32`).

## Already portable

| Area | Notes |
|---|---|
| Paths and state | Keys are POSIX-style, NFC-normalized relative paths; state written on one OS reads on the other. Locks use hard links with an `O_EXCL` fallback (NTFS supports both). |
| Conversion | python-docx, python-pptx, openpyxl, pdfplumber, pypdfium2, olefile: all pure Python or with Windows wheels. |
| Index and search | SQLite FTS5 and sqlite-vec (Windows wheels). |
| Models | HTTP to Ollama / llama.cpp / LM Studio, all available on Windows. |
| Secrets | `keyring` uses Windows Credential Manager. |
| Meetings | `faster-whisper` is the default ASR off Apple Silicon and is fed raw samples, so it needs no PyAV decoding; pyannote runs on CUDA or CPU. ffmpeg normalizes audio. |
| Documents | pandoc, typst, LibreOffice and draw.io ship for Windows; mermaid-cli via npm. The Mermaid browser lookup includes `chrome`, `msedge`, `chromium` and puppeteer's `chrome-headless-shell.exe`. |
| CLI and MCP | Typer, rich, the MCP SDK: portable. All orchestration is Python, not shell. |

## To implement (`src/dossier/platform/windows.py`)

| Capability | Windows backend | Until then |
|---|---|---|
| Process probe (stale locks) | `OpenProcess` + `GetExitCodeProcess` via ctypes | locks are never reclaimed automatically |
| OCR | `Windows.Media.Ocr` (winrt), or Tesseract | images and scanned PDFs are not OCR'd |
| Speech synthesis (test fixtures only) | SAPI via `pyttsx3` | synthetic meeting tests skip |
| Scheduler | Task Scheduler (`schtasks` or the COM API), daily trigger plus a folder watcher | run `dsr sync` manually |
| Notifications | toast notifications | messages go to the log |
| Keep awake | `SetThreadExecutionState` | long runs may be interrupted by sleep |
| Cloud placeholders | `FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS` / `RECALL_ON_OPEN` | OneDrive cloud-only files are read (downloaded) |

## Also to do

- A winget (or scoop) manifest mirroring the `Brewfile`: uv, ffmpeg, pandoc,
  typst, Node.js plus `npm install -g @mermaid-js/mermaid-cli`, draw.io,
  LibreOffice, Ollama, Bun.
- `ExternalTool` entries: Windows install paths for draw.io
  (`C:\Program Files\draw.io\draw.io.exe`) and LibreOffice
  (`C:\Program Files\LibreOffice\program\soffice.exe`), and winget install hints.
- The `meet` extra: drop `mlx-whisper` (already limited to macOS arm64 by an
  environment marker); check that a CUDA build of torch is chosen when a GPU exists.
- CI: run the unit tests on `windows-latest`; the e2e script needs Ollama and the
  system tools above.
