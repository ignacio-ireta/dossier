"""``dsr models``: list roles and test them with a tiny probe (no workspace content)."""

from __future__ import annotations

import time
from dataclasses import dataclass

from pydantic import BaseModel, Field

from dossier.core.config import ROLE_NAMES, WorkspaceConfig
from dossier.core.errors import DossierError
from dossier.llm.router import ModelRouter
from dossier.llm.structured import Outcome, generate_structured


class _Probe(BaseModel):
    ready: bool
    word: str = Field(max_length=20)


@dataclass(frozen=True)
class RoleTest:
    role: str
    model: str
    status: str  # ok | repaired | degraded | failed | disabled | skipped
    seconds: float
    detail: str = ""


def describe_roles(config: WorkspaceConfig) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for role in ROLE_NAMES:
        route = config.models.get(role)
        if route is None:
            rows.append({"role": role, "configured": False})
            continue
        rows.append(
            {
                "role": role,
                "configured": True,
                "enabled": route.enabled,
                "provider": route.provider,
                "model": route.model,
                "context_tokens": route.context_tokens,
                "max_output_tokens": route.max_output_tokens,
                "think": route.think,
            }
        )
    return rows


def probe_role(router: ModelRouter, role: str) -> RoleTest:
    route = router.config.models.get(role)
    if route is None or not route.enabled:
        return RoleTest(role, route.model if route else "", "disabled", 0.0)
    start = time.monotonic()
    try:
        client = router.client(role)
        if role == "embed":
            vectors = client.embed(["dossier probe sentence"])
            detail = f"{len(vectors[0])} dimensions" if vectors else "no vector returned"
            status = "ok" if vectors and vectors[0] else "failed"
        elif role == "rerank":
            ranked = client.rerank("probe", ["probe text", "unrelated"])
            status, detail = ("ok", f"{len(ranked)} scores") if ranked else ("failed", "empty")
        elif role == "vision":
            status, detail = "skipped", "image probe arrives with slide captioning"
        else:
            result = generate_structured(
                client,
                system="You are a connectivity probe.",
                user='Set "ready" to true and "word" to "ready".',
                schema=_Probe,
                fallback=lambda: _Probe(ready=False, word=""),
            )
            status = result.outcome.value
            ok = result.outcome is not Outcome.DEGRADED and result.value.ready
            if not ok:
                detail = result.error or "probe answer was wrong"
            elif result.constrained is False:
                detail = "valid JSON (prompted only; server cannot constrain this model)"
            elif result.constrained:
                detail = "valid JSON (constrained)"
            else:
                detail = "valid JSON"
            if result.outcome is not Outcome.DEGRADED and not result.value.ready:
                status = "degraded"
    except DossierError as exc:
        return RoleTest(
            role, route.model, "failed", round(time.monotonic() - start, 2), exc.message
        )
    return RoleTest(role, route.model, status, round(time.monotonic() - start, 2), detail)
