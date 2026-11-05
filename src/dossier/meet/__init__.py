"""Meetings: transcription, diarization, speaker suggestions, summaries and actions.

Recordings in any common audio/video format are normalized with ffmpeg,
transcribed (MLX Whisper on Apple Silicon, faster-whisper elsewhere), split by
speaker (pyannote), and summarized window by window by the ``meet`` model role.
Speaker names are never asserted: they are suggestions until a person confirms
them. Every summary item cites a timestamp that is checked against the
transcript.
"""
