"""OpenTelemetry ingestion: the standard OTLP/HTTP trace endpoint, so an OpenTelemetry exporter can point at the Auditor.

  OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:8000        (the exporter adds /v1/traces)
  OTEL_EXPORTER_OTLP_HEADERS=x-api-key=<key>,x-auditor-agent=support-bot

JSON bodies work out of the box. Protobuf bodies (what most exporters send by default) need: pip install "agent-auditor[otel]".
"""
from __future__ import annotations

import gzip
import json
import zlib

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from ..importers import otel
from ..importers.service import import_traces
from .deps import get_db, require_api_key

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])
MAX_BODY = 32 * 1024 * 1024


@router.post("/traces", summary="Receive OpenTelemetry traces (OTLP/HTTP, JSON or protobuf)")
async def receive_traces(request: Request, db: Session = Depends(get_db)):
    q, h = request.query_params, request.headers
    agent = q.get("agent") or h.get("x-auditor-agent")
    environment = q.get("environment") or h.get("x-auditor-environment")
    version = q.get("version") or h.get("x-auditor-version")
    if environment and environment not in ("production", "canary", "test"):
        raise HTTPException(422, "environment must be production, canary or test")

    body = await request.body()
    if h.get("content-encoding", "").lower() == "gzip":
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError, zlib.error) as exc:
            raise HTTPException(400, "the body says it is gzip but could not be decompressed") from exc
    if len(body) > MAX_BODY:
        raise HTTPException(413, "request too large; send smaller batches")

    ctype = h.get("content-type", "").split(";")[0].strip().lower()
    protobuf = ctype in ("application/x-protobuf", "application/protobuf")
    try:
        if protobuf:
            payload = otel.from_protobuf(body)
        else:
            try:
                payload = json.loads(body)
            except ValueError as exc:
                raise HTTPException(415, "send OTLP as JSON (application/json) or protobuf (application/x-protobuf)") from exc
        result = import_traces(db, payload, agent=agent, environment=environment, version=version)
    except ImportError as exc:
        raise HTTPException(415, str(exc)) from exc
    except otel.OtlpError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    if protobuf:  # an empty ExportTraceServiceResponse is zero bytes
        return Response(content=b"", media_type="application/x-protobuf")
    return {"partialSuccess": {}, "imported": result}
