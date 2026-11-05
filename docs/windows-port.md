# Windows port

dossier is written to run on Windows; only the platform backends remain. All
OS-specific behavior sits behind the interfaces in `src/dossier/platform/base.py`,
and `tests/test_platform_neutrality.py` keeps the rest of the code base free of
macOS-only calls.

## Backends to implement (`src/dossier/platform/windows.py`)

| Capability | macOS backend | Windows backend |
|---|---|---|
| Process probe (stale locks) | `os.kill(pid, 0)` | `OpenProcess` + `GetExitCodeProcess` via ctypes. Until then, locks are never auto-reclaimed |
| OCR | Apple Vision (`ocrmac`) | `Windows.Media.Ocr` (winrt), or Tesseract |
| Speech synthesis (test fixtures) | `say` | SAPI (`pyttsx3`) |
| Scheduler | launchd user agent | Task Scheduler (`schtasks` or the COM API) |
| Notifications | `osascript` | toast notifications |
| Keep awake | `caffeinate -i` | `SetThreadExecutionState` |
| Cloud placeholders | APFS dataless flag | `FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS` / `RECALL_ON_OPEN` |
| Secrets | keyring (Keychain) | keyring (Credential Manager), already portable |

## Already portable

- Paths are keyed by POSIX-style, NFC-normalized relative paths, so state written on
  one OS stays valid on the other.
- Locks use hard links with an `O_EXCL` fallback; both work on NTFS.
- Model access is HTTP to Ollama / OpenAI-compatible servers, which run on Windows.
- Orchestration is Python, not shell.

## System dependencies

A winget or scoop manifest equivalent to the `Brewfile` will list ffmpeg, pandoc,
typst, mermaid-cli (via npm), draw.io, LibreOffice, Ollama and Bun.
