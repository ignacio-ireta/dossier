"""Index: document cards, folder summaries, tree documents, and hybrid search.

Cards and folder summaries are written for people and agents to read
(``index/``); chunks, full-text and vector indexes live in
``.dossier/search.db`` for retrieval. Everything is incremental: only documents
whose converted output changed are re-summarized, and a folder is re-summarized
only when something inside it changed.
"""
