"""Documents: render Markdown to DOCX, PPTX and PDF from per-account template packs.

A template pack is a folder with ``pack.toml`` and the files it names: Word
reference documents (styles, headers, footers), an optional cover page,
PowerPoint templates (layouts, theme), and a Typst theme for PDF. Packs live in
the workspace (``templates/<pack>/``, where client branding belongs), in the
user's data folder, or ship with dossier (only ``neutral``). A pack can extend
another and override only what differs.
"""
