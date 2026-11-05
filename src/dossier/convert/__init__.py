"""Corpus conversion: keep every source folder mirrored as LLM-friendly files.

Each supported document becomes ``corpus/<source>/<relpath>.md`` (plus a
``.meta.json`` sidecar); spreadsheets also get one CSV per sheet. A per-source
snapshot of content hashes drives incremental runs: only added or changed files
are extracted, moves are relocated without re-extraction, and deletions are
archived, never silently dropped.
"""
