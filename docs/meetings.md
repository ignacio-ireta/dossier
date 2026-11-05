# Meetings

```bash
uv tool install --reinstall --python 3.12 "<path-or-url>[all]"   # includes the meet extra
dsr secrets set hf-token          # once; see "Diarization model" below
dsr meet add "2026-11-02 Rollout review.m4a" --attendee Sam --deck slides.pptx
dsr meet add "2026-11-03 Revisión semanal.m4a" --language es   # or let it be detected
dsr meet list                      # with each meeting's language
dsr meet show <id>                 # summary; --transcript for the transcript
```

Recordings dropped into a source folder are transcribed automatically by
`dsr sync` (the `meetings` stage), once per recording.

## What a meeting produces

`meetings/<date>-<title>-<hash>/`:

| File | Contents |
|---|---|
| `transcript.md` | One block per intervention (`**[mm:ss] Speaker:** text`), speaker list, duration, language |
| `summary.md` | Overview, topics, decisions, action items (task, owner, due, time), open questions |
| `actions.json` | The action items, machine-readable |
| `brief.md` | Written by `dsr meet brief`: summary items, slides, related documents |
| `deck.md` | Text of the slides shown, when `--deck` was given |
| `meeting.json` | Title, date, language, speakers, models used, stage status |
| `machine/` | Words as heard (`words.json`), diarization, turns after glossary fixes, `.srt`, `.vtt` |

A transcript block holds everything a person says until someone else speaks,
however long they pause and however the diarization cut their speech. Labels
confirmed as the same person (a voice split in two) count as one person. Long
blocks get a time-stamped paragraph (`[12:05] ...`) about every minute and
after long silences, so quotes stay easy to locate. The finer turns remain in
`machine/turns.json` and the subtitle files.

Transcripts and summaries are indexed like documents: search them with
`dsr search "..." --kind meeting`, and agents get the MCP tools `meetings`,
`transcript` (time ranges) and `actions`.

## How it works

1. **Audio**: ffmpeg converts any audio or video (m4a, mp3, wav, aac, flac,
   ogg/opus, webm, mp4, mov, mkv...) to 16 kHz mono with loudness normalization.
2. **Speech recognition**: MLX Whisper on Apple Silicon, faster-whisper
   elsewhere (`[meet] asr_backend`, `asr_model`), in the meeting's language
   (see below), with the glossary's terms as a hint. Word timestamps are kept.
3. **Diarization**: pyannote splits the audio by speaker and gives each speaker
   a voice embedding.
4. **Speakers**: the glossary's replacements fix known mishearings, then words
   are assigned to speakers. Speakers keep labels (`SPEAKER_00`, ...) until a
   person confirms names.
5. **Summary**: the `meet` model reads the transcript in windows that fit its
   context. Each window yields a short summary plus the decisions, actions and
   open questions stated in it, each with the time it is said. A last call writes
   the overview. Notes are written in the meeting's language.

Stages resume: re-running `dsr meet add` on the same recording only does what
is missing. `--force asr` re-runs a stage and everything after it; after
editing the glossary's replacements, `--force assign` applies them without
transcribing again (new `terms` need `--force asr`).

## Language

Each meeting has one spoken language. It is either set or detected:

| Setting | Effect |
|---|---|
| `dsr meet add <file> --language es` | This meeting is in Spanish. |
| `--language auto` | Detect it for this meeting. |
| `[meet] language = "en"` | The default for `dsr meet add` and for recordings `dsr sync` finds. |
| neither | Detect when `[workspace] languages` lists several (`["es", "en"]`); otherwise use that one language. |

Detection scores up to three 30-second stretches of speech, from the start,
middle and end of the recording, and picks the likeliest of the workspace's
languages. Restricting the choice matters: on short or accented audio Whisper
sometimes hears Spanish as Portuguese or Galician. With a single workspace
language and `--language auto`, any language Whisper knows can be chosen.

`dsr meet list` shows each meeting's language and whether it was detected
(`Spanish (detected, 0.98)`). A log warning flags an unsure detection, or a
recording that sounds like a language outside the workspace's. To correct a
meeting, add it again with the right language; it is transcribed again:

```bash
dsr meet add "2026-11-03 Revisión semanal.m4a" --language es
```

Whisper transcribes; it does not translate. A transcript is in the language
spoken, and its summary is written in the same language, which keeps quoted
owners and due dates ("el viernes") checkable against the transcript. A
meeting that switches language partway is transcribed in its main language;
the other language's passages come out less accurately.

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

Point `[meet] glossary` at a TOML file (one per workspace, so each account
keeps its own vocabulary):

```toml
terms = ["Lakehouse", "DataOps", "Falcon"]   # spelled as they should appear

[replacements]                               # heard -> correct, for every meeting
"data ops" = "DataOps"
"lake house" = "Lakehouse"

[es]                                         # only for meetings in Spanish
terms = ["facturación electrónica"]
[es.replacements]
"cuberneti" = "Kubernetes"
"comité de dirección" = "Comité de Dirección"
```

- **Terms** bias recognition: they go into Whisper's initial prompt, labelled
  in the meeting's language ("Glossary: ...", "Glosario: ..."). The prompt is
  limited to about 800 characters, so list the words that are actually
  misheard: product and system names, acronyms, people's and places' names,
  words from another language.
- **Replacements** fix known mishearings after recognition. They ignore case,
  match whole words, and may span several words: `"data ops"` becomes one word
  `DataOps` that keeps the timing of both. Keys are what Whisper wrote, values
  the correct form.
- **Language tables** (`[es]`, `[en]`, any language code) add terms and
  replacements used only for meetings in that language; on a conflict they win
  over the shared ones. Mishearings differ by language, so a term often needs a
  different fix in each.

`dsr meet eval <id>` scores a transcript against the glossary (with the
meeting's language table): `100 * term recall - 5 * mishearings - 0.5 * words
in repetition loops`. Use it to see which terms are still missed.

## Diarization model

pyannote's `speaker-diarization-community-1` is gated on Hugging Face: accept
its terms on the model page with your account, create a read token, and store it
with `dsr secrets set hf-token` (macOS Keychain). Without a token, a copy already
in the local Hugging Face cache is used offline. `--no-diarize` treats the
recording as a single speaker.
