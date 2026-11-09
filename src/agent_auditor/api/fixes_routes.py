"""Propose fix candidates for an agent's failures. The server only proposes; the SDK replays them
(the agent runs in the caller's process) and judges the result, see Auditor.verify_fixes."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import assistant
from ..fixes import propose
from ..fixes.propose import candidate_id
from ..models import Agent, AgentVersion
from ..rootcause import service as rootcause
from .deps import get_db, require_api_key

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])


class ProposeIn(BaseModel):
    environment: Literal["production", "canary", "test"] = "production"
    since_hours: float | None = Field(default=None, gt=0, le=24 * 365)
    base_version: str | None = Field(default=None, description="Version label to improve; defaults to the active version")
    use_ai: bool = Field(default=False, description="Also ask the AI helper for a rewritten prompt (needs ANTHROPIC_API_KEY)")
    max_candidates: int = Field(default=6, ge=1, le=12)


def _ai_prompt_candidate(request: Request, prompt: str | None, report: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    s = request.app.state.settings
    if not s.anthropic_api_key:
        return None, "AI prompt rewrite skipped: ANTHROPIC_API_KEY is not set on the server (the rule-based candidates are unaffected)"
    try:
        request.app.state.rate_limiter.check()
        text = assistant.complete(api_key=s.anthropic_api_key, model=s.assistant_model, system=assistant.REWRITE_SYSTEM,
                                  messages=assistant.rewrite_messages(prompt, report), max_tokens=1500,
                                  transport=request.app.state.assistant_transport)
    except (assistant.AssistantError, assistant.AssistantUnavailable, assistant.RateLimited) as exc:
        return None, f"AI prompt rewrite skipped: {exc}"
    text = text.strip().strip("`").strip()
    if not text or text == (prompt or "").strip():
        return None, "AI prompt rewrite skipped: it returned no change"
    changes = [{"field": "prompt", "from": None, "to": "rewritten by the AI helper"}]
    return ({"id": candidate_id("prompt-ai", changes, text), "kind": "prompt", "verifiable": True, "cause": "multiple", "source": "ai",
             "title": "Prompt rewritten by the AI helper", "rationale": "Drafted from the top failure causes; it must pass the regression replay like any other candidate.",
             "changes": changes, "prompt": text, "config": None}, None)


@router.post("/agents/{name}/fixes/propose")
def propose_fixes(name: str, body: ProposeIn, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    agent = db.scalar(select(Agent).where(Agent.name == name))
    if agent is None:
        raise HTTPException(404, f"agent '{name}' not found")
    stmt = select(AgentVersion).where(AgentVersion.agent_id == agent.id)
    stmt = stmt.where(AgentVersion.label == body.base_version) if body.base_version else stmt.where(AgentVersion.is_active.is_(True))
    base = db.scalar(stmt)
    if base is None:
        raise HTTPException(404 if body.base_version else 422,
                            f"version '{body.base_version}' not found" if body.base_version
                            else "this agent has no active version; register one so fixes have something to change")
    try:
        report = rootcause.root_cause_report(db, agent=name, environment=body.environment, since_hours=body.since_hours,
                                             version=body.base_version, diagnose_first=True)  # no version chosen: look at the failures of every version
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    db.commit()
    notes = list(report.get("notes", []))
    candidates = propose(base.prompt, base.config, report, max_candidates=body.max_candidates)
    if body.use_ai and report.get("failed_runs"):
        ai, why = _ai_prompt_candidate(request, base.prompt, report)
        if ai:
            candidates = [ai, *candidates][: body.max_candidates]
        if why:
            notes.append(why)
    if not report.get("failed_runs"):
        notes.append("no failed runs in this window, so there is nothing to fix yet")
    return {"agent": name, "base_version": {"label": base.label, "prompt": base.prompt, "config": base.config},
            "failed_runs": report.get("failed_runs", 0), "failure_rate": report.get("failure_rate"),
            "causes": report.get("causes", []), "suspect_sources": report.get("suspect_sources", []),
            "candidates": candidates, "notes": notes}
