from __future__ import annotations

import hmac
from collections.abc import Iterator

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session


def get_db(request: Request) -> Iterator[Session]:
    factory = request.app.state.session_factory
    with factory() as session:
        yield session


# Declared as a security scheme so the Swagger page (/docs) shows one Authorize button.
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(request: Request, api_key: str | None = Security(_api_key_header)) -> None:
    expected = request.app.state.settings.api_key
    if expected and not hmac.compare_digest((api_key or "").encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid or missing API key")
