# Meetings

```bash
uv tool install --reinstall --python 3.12 "<path-or-url>[all]"   # includes the meet extra
dsr secrets set hf-token          # once; see "Diarization model" below
dsr meet add "2026-11-02 Rollout review.m4a" --attendee Sam --deck slides.pptx
dsr meet list
dsr meet show <id>                 # summary; --transcript for the transcript
```

Recordings dropped into a source folder are transcribed automatically by
`dsr sync` (the `meetings` stage), once per recording.

## What a meeting produces

`meetings/<date>-<title>-<hash>/`:

| File | Contents |
|---|---|
| `transcript.md` | Turns as `**[mm:ss] Speaker:** text`, speaker list, duration |
| `summary.md` | Overview, topics, decisions, action items (task, owner, due, time), open questions |
| `actions.json` | The action items, machine-readable |
| `brief.md` | Written by `dsr meet brief`: summary items, slides, related documents |
| `deck.md` | Text of the slides shown, when `--deck` was given |
| `meeting.json` | Title, date, speakers, models used, stage status |
| `machine/` | Words with timestamps, diarization, turns, `.srt`, `.vtt` |

Transcripts and summaries are indexed like documents: search them with
`dsr search "..." --kind meeting`, and agents get the MCP tools `meetings`,
`transcript` (time ranges) and `actions`.

## How it works

1. **Audio**: ffmpeg converts any audio or video (m4a, mp3, wav, aac, flac,
   ogg/opus, webm, mp4, mov, mkv...) to 16 kHz mono with loudness normalization.
2. **Speech recognition**: MLX Whisper on Apple Silicon, faster-whisper
   elsewhere (`[meet] asr_backend`, `asr_model`). Word timestamps are kept.
3. **Diarization**: pyannote splits the audio by speaker and gives each speaker
   a voice embedding.
4. **Speakers**: labels (`SPEAKER_00`, ...) until a person confirms names.
5. **Summary**: the `meet` model reads the transcript in windows that fit its
   context. Each window yields a short summary plus the decisions, actions and
   open questions stated in it, each with the time it is said. A last call writes
   the overview.

Stages resume: re-running `dsr meet add` on the same recording only does what
is missing. `--force asr` re-runs a stage and everything after it.

## Summary guarantees

- Every decision, action and question cites a time, and that time is checked
  against the transcript; items with an invented time are dropped.
- An action's owner or due date is kept only if those words occur in the part
  of the meeting it comes from; otherwise it is `UNKNOWN`.
- The model sees speaker labels, never suggested names.
- If the model is not reachable, the transcript is still written and the
  summary is marked incomplete; `dsr meet summarize <id>` redoes it.

## Speaker names

Names are suggestions until a person confirms them:

```bash
dsr meet speakers <id>                              # who is who
dsr meet speakers <id> --set SPEAKER_01="Sam"       # confirm
```

With opt-in voiceprints (`[meet] speaker_voiceprints = true`), confirmed
speakers can be recognized in later meetings:

```bash
dsr people consent "Sam" --note "agreed by email, 2026-11-02"
dsr meet speakers <id> --set SPEAKER_01="Sam"       # now also stores Sam's voiceprint
dsr people list
dsr people forget "Sam"                             # deletes consent and voiceprint
```

Later transcripts show `SPEAKER_00 (likely Sam, 0.91)` until confirmed.
`--attendee` limits suggestions to the people present. Voiceprints are
biometric data: they are off by default, stored only in this workspace's
`people/voiceprints.json`, and created only after consent is recorded.

## Glossary

Point `[meet] glossary` at a TOML file:

```toml
terms = ["Lakehouse", "DataOps", "Falcon"]

[replacements]
"data ops" = "DataOps"
"lake house" = "Lakehouse"
```

Terms bias recognition (Whisper's initial prompt); replacements fix frequent
mishearings afterwards. `dsr meet eval <id>` scores a transcript against the
glossary: `100 * term recall - 5 * mishearings - 0.5 * words in repetition loops`.

## Diarization model

pyannote's `speaker-diarization-community-1` is gated on Hugging Face: accept
its terms on the model page with your account, create a read token, and store it
with `dsr secrets set hf-token` (macOS Keychain). Without a token, a copy already
in the local Hugging Face cache is used offline. `--no-diarize` treats the
recording as a single speaker.
