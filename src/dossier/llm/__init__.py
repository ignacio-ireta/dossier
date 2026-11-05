"""Model access built for small local models.

- ``providers``: Ollama, any OpenAI-compatible server, optional Anthropic.
- ``router``: per-role model routing from ``dossier.toml [models]`` + policy.
- ``structured``: constrained JSON, validate, repair once, deterministic fallback.
- ``budget``: token estimates and packing text into small context windows.
"""
