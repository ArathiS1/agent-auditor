from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import assistant
from ..insights import service as insights
from ..models import Agent, AgentVersion
from ..rootcause import service as rootcause
from .deps import get_db, require_api_key

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])


class StatusOut(BaseModel):
    enabled: bool  # an AI service is available (the built-in guide always is)
    model: str | None
    providers: list[str] = Field(default_factory=list)
    guide: bool = True


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=assistant.MAX_QUESTION_CHARS)
    context: dict[str, Any] = Field(default_factory=dict, description="What the dashboard is showing right now")
    history: list[Turn] = Field(default_factory=list, max_length=20)


class AnswerOut(BaseModel):
    answer: str
    source: str = "guide"  # the AI service that wrote it ("Gemini"), or "guide" for the built-in guide


class SuggestFixIn(BaseModel):
    environment: Literal["production", "canary", "test"] = "production"
    since_hours: float | None = Field(default=None, gt=0, le=24 * 365)


class FixOut(BaseModel):
    suggestion: str
    version: str | None
    failed_runs: int
    note: str


def _call(request: Request, system: str, messages: list[dict], max_tokens: int) -> str:
    s = request.app.state.settings
    try:
        request.app.state.rate_limiter.check()
        return assistant.ask_any(system=system, messages=messages, max_tokens=max_tokens, anthropic_key=s.anthropic_api_key,
                                 anthropic_model=s.assistant_model, transport=request.app.state.assistant_transport,
                                 dead=request.app.state.assistant_dead)[0]
    except assistant.RateLimited as exc:
        raise HTTPException(429, str(exc)) from exc
    except assistant.AssistantUnavailable as exc:
        raise HTTPException(503, "an AI key is needed for this: add GEMINI_API_KEY (free) or another key to .env and restart") from exc
    except assistant.AssistantError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/assistant/status", response_model=StatusOut)
def status(request: Request) -> StatusOut:
    s = request.app.state.settings
    names = assistant.configured_providers(anthropic_key=s.anthropic_api_key)
    return StatusOut(enabled=bool(names), model=s.assistant_model if "anthropic" in names else None, providers=names)


@router.post("/assistant/ask", response_model=AnswerOut)
def ask(body: AskIn, request: Request, db: Session = Depends(get_db)) -> AnswerOut:
    context = dict(body.context)
    agent = context.get("agent")
    if isinstance(agent, str) and agent:
        try:  # live numbers for the agent on screen, so "why are there so many failures?" can be answered
            context["live_overview"] = insights.overview(db, agent, 168)
        except Exception:  # noqa: BLE001 - the answer is still useful without them
            pass
    msgs = assistant.build_messages(body.question, context, [t.model_dump() for t in body.history])
    s = request.app.state.settings
    system = assistant.SYSTEM + "\n\nGuide to every page of the dashboard:\n" + assistant.guide_text()
    try:
        request.app.state.rate_limiter.check()
    except assistant.RateLimited as exc:
        raise HTTPException(429, str(exc)) from exc
    try:
        text, source = assistant.ask_any(system=system, messages=msgs, anthropic_key=s.anthropic_api_key, anthropic_model=s.assistant_model,
                                         transport=request.app.state.assistant_transport, dead=request.app.state.assistant_dead)
        return AnswerOut(answer=text, source=source)
    except assistant.AssistantUnavailable:
        return AnswerOut(answer=assistant.answer_from_guide(body.question), source="guide")
    except assistant.AssistantError as exc:
        note = "\n\n(The AI services did not answer, so this comes from the built-in guide. " + str(exc)[:300] + ")"
        return AnswerOut(answer=assistant.answer_from_guide(body.question) + note, source="guide")


@router.post("/agents/{name}/suggest-fix", response_model=FixOut)
def suggest_fix(name: str, body: SuggestFixIn, request: Request, db: Session = Depends(get_db)) -> FixOut:
    agent = db.scalar(select(Agent).where(Agent.name == name))
    if agent is None:
        raise HTTPException(404, f"agent '{name}' not found")
    try:
        report = rootcause.root_cause_report(db, agent=name, environment=body.environment, since_hours=body.since_hours,
                                             diagnose_first=False)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    active = db.scalar(select(AgentVersion).where(AgentVersion.agent_id == agent.id, AgentVersion.is_active.is_(True)))
    msgs = assistant.fix_messages(active.label if active else None, active.prompt if active else None,
                                  active.config if active else None, report)
    text = _call(request, assistant.FIX_SYSTEM, msgs, 900)
    return FixOut(suggestion=text, version=active.label if active else None, failed_runs=int(report.get("failed_runs") or 0),
                  note="A draft written by an AI model. Register it as a new version and run it through a canary before release.")
