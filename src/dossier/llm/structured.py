"""Structured output that a 2B-class model can reliably produce.

Recipe, applied to every LLM task in dossier:

1. Ask for one narrow thing, with a small flat JSON schema (``$defs`` inlined,
   titles removed). The schema is always written into the prompt, and the
   server also enforces it through constrained decoding when it can; some
   builds cannot (they answer HTTP 501), and the provider then falls back to
   the prompted schema alone.
2. Validate with pydantic.
3. On failure, repair once: show the model its answer and the validation error.
4. If that still fails, use a deterministic fallback so the pipeline never
   blocks on a weak model; the result is marked ``degraded`` for later redo.

Transport failures (server down) are *not* degraded: they raise
:class:`ProviderUnavailableError` so a run stops instead of silently filling the
index with fallbacks.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ValidationError

from dossier.core.errors import ProviderResponseError
from dossier.llm.router import RoleClient
from dossier.llm.types import JsonSchema, Message

log = logging.getLogger("dossier.llm")

_MAX_ECHO_CHARS = 2000


class Outcome(StrEnum):
    OK = "ok"
    REPAIRED = "repaired"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class StructuredResult[T: BaseModel]:
    value: T
    outcome: Outcome
    attempts: int
    error: str | None = None
    seconds: float = 0.0
    constrained: bool | None = None


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            target = copy.deepcopy(defs[ref.removeprefix("#/$defs/")])
            merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
            return _inline_refs(merged, defs)
        # Drop "title" annotations (strings), never a property that is named "title".
        return {
            key: _inline_refs(value, defs)
            for key, value in node.items()
            if key != "$defs" and not (key == "title" and isinstance(value, str))
        }
    if isinstance(node, list):
        return [_inline_refs(item, defs) for item in node]
    return node


def schema_for(model: type[BaseModel]) -> JsonSchema:
    """A flat JSON schema: refs inlined, titles dropped, no extra properties."""
    raw = model.model_json_schema()
    flat: JsonSchema = _inline_refs(raw, raw.get("$defs", {}))
    flat["additionalProperties"] = False
    return flat


_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def extract_json(text: str) -> Any:
    """Parse the first JSON object in ``text`` (tolerates code fences and chatter)."""
    candidate = text.strip()
    fenced = _FENCE.match(candidate)
    if fenced:
        candidate = fenced.group(1)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    start = candidate.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for position in range(start, len(candidate)):
            char = candidate[position]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(candidate[start : position + 1])
                    except json.JSONDecodeError:
                        break
        start = candidate.find("{", start + 1)
    raise ValueError("no JSON object found in model output")


def example_for(schema: JsonSchema) -> Any:
    """A placeholder instance of a (flat) JSON schema; small models copy shapes well."""
    kind = schema.get("type")
    if "enum" in schema:
        return schema["enum"][0]
    if "anyOf" in schema:
        options = [s for s in schema["anyOf"] if s.get("type") != "null"]
        return example_for(options[0]) if options else None
    if kind == "object":
        return {key: example_for(sub) for key, sub in schema.get("properties", {}).items()}
    if kind == "array":
        return [example_for(schema.get("items", {}))]
    if kind == "boolean":
        return True
    if kind in {"integer", "number"}:
        return 0
    if "default" in schema and isinstance(schema["default"], str) and schema["default"]:
        return schema["default"]
    if schema.get("description"):
        return f"<{schema['description']}>"
    return "..."


def field_notes(schema: JsonSchema) -> str:
    """One line per field with its description and length limit, when there are any."""
    lines = []
    for key, sub in schema.get("properties", {}).items():
        parts = []
        if sub.get("description"):
            parts.append(str(sub["description"]))
        if sub.get("maxItems"):
            parts.append(f"at most {sub['maxItems']} items")
        if sub.get("maxLength"):
            parts.append(f"at most {sub['maxLength']} characters")
        if parts:
            lines.append(f"- {key}: {'; '.join(parts)}")
    return ("\nFields:\n" + "\n".join(lines)) if lines else ""


def unwrap(data: Any, schema: JsonSchema) -> Any:
    """Recover an answer a model nested inside a copy of the schema's ``properties``."""
    required = set(schema.get("required", []))
    if not isinstance(data, dict) or not required or required <= set(data):
        return data
    nested = data.get("properties")
    if isinstance(nested, dict) and required <= set(nested):
        values = list(nested.values())
        if not all(isinstance(v, dict) and "type" in v for v in values):  # not a bare schema
            return nested
    return data


def _validation_message(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = [
            f"{'.'.join(str(p) for p in error['loc']) or 'root'}: {error['msg']}"
            for error in exc.errors()[:4]
        ]
        return "; ".join(parts)
    return str(exc)[:300]


def generate_structured[T: BaseModel](
    client: RoleClient,
    *,
    system: str,
    user: str,
    schema: type[T],
    fallback: Callable[[], T],
    images: tuple[bytes, ...] = (),
) -> StructuredResult[T]:
    json_schema = schema_for(schema)
    instructions = (
        f"{system}\nReply with one JSON object only, shaped like this example "
        f"(replace the placeholder values):\n"
        f"{json.dumps(example_for(json_schema), ensure_ascii=False)}" + field_notes(json_schema)
    )
    messages = [Message("system", instructions), Message("user", user, images=images)]
    error: str | None = None
    seconds = 0.0
    constrained: bool | None = None
    for attempt in (1, 2):
        try:
            response = client.chat(messages, json_schema=json_schema)
        except ProviderResponseError as exc:
            error = f"provider_error: {exc.message[:200]}"
            log.warning("role=%s model=%s provider error; degrading", client.role, client.model)
            break
        seconds += response.seconds
        constrained = response.constrained
        try:
            value = schema.model_validate(unwrap(extract_json(response.text), json_schema))
        except (ValueError, ValidationError) as exc:
            error = _validation_message(exc)
            log.info("role=%s attempt=%d invalid output: %s", client.role, attempt, error)
            messages = [
                *messages,
                Message("assistant", response.text[:_MAX_ECHO_CHARS]),
                Message(
                    "user",
                    f"That reply was invalid ({error}). Return one corrected JSON object "
                    "that matches the schema, and nothing else.",
                ),
            ]
            continue
        outcome = Outcome.OK if attempt == 1 else Outcome.REPAIRED
        return StructuredResult(value, outcome, attempt, None, round(seconds, 3), constrained)
    return StructuredResult(fallback(), Outcome.DEGRADED, 2, error, round(seconds, 3), constrained)
