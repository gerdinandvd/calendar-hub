"""Create, verify, list, and revoke agent PATs."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import AgentToken
from app.services.agent.types import (
    SCOPE_READ,
    SCOPE_WRITE,
    VALID_SCOPES,
    AgentError,
    invalid,
)
from app.sync.apply import utc_now

_TOKEN_BYTES = 32
_PREFIX_LEN = 8


@dataclass(frozen=True)
class VerifiedToken:
    id: str
    name: str
    scopes: frozenset[str]


def create_token(
    engine: Engine, *, name: str, scopes: list[str]
) -> tuple[dict, str] | AgentError:
    cleaned = name.strip()
    if not cleaned or len(cleaned) > 120:
        return invalid("Enter a name.")
    normalized = _normalize_scopes(scopes)
    if isinstance(normalized, AgentError):
        return normalized
    plain = f"ch_{secrets.token_urlsafe(_TOKEN_BYTES)}"
    row_id = str(uuid.uuid4())
    when = utc_now()
    with Session(engine) as session, session.begin():
        session.add(
            AgentToken(
                id=row_id,
                name=cleaned,
                token_hash=_hash(plain),
                token_prefix=plain[:_PREFIX_LEN],
                scopes=",".join(sorted(normalized)),
                created_at=when,
                revoked_at=None,
            )
        )
    return (
        {
            "id": row_id,
            "name": cleaned,
            "scopes": sorted(normalized),
            "token_prefix": plain[:_PREFIX_LEN],
            "created_at": when,
            "revoked_at": None,
        },
        plain,
    )


def list_tokens(engine: Engine) -> list[dict]:
    with Session(engine) as session:
        rows = session.scalars(
            select(AgentToken).order_by(AgentToken.created_at, AgentToken.id)
        ).all()
        return [_public(row) for row in rows]


def revoke_token(engine: Engine, token_id: str) -> dict | AgentError:
    with Session(engine) as session, session.begin():
        row = session.get(AgentToken, token_id)
        if row is None:
            return AgentError(404, "not_found", "Not found.")
        if row.revoked_at is None:
            row.revoked_at = utc_now()
        return _public(row)


def verify_bearer(
    engine: Engine, authorization: str | None
) -> VerifiedToken | AgentError:
    if authorization is None or not authorization.startswith("Bearer "):
        return AgentError(401, "unauthorized", "Authentication required.")
    plain = authorization[7:].strip()
    if not plain:
        return AgentError(401, "unauthorized", "Authentication required.")
    digest = _hash(plain)
    with Session(engine) as session:
        row = session.scalar(select(AgentToken).where(AgentToken.token_hash == digest))
        if row is None or row.revoked_at is not None:
            return AgentError(401, "unauthorized", "Authentication required.")
        scopes = frozenset(
            part for part in row.scopes.split(",") if part in VALID_SCOPES
        )
        return VerifiedToken(id=row.id, name=row.name, scopes=scopes)


def require_scope(token: VerifiedToken, scope: str) -> AgentError | None:
    if scope not in token.scopes:
        return AgentError(403, "forbidden", "Missing required scope.")
    return None


def _normalize_scopes(scopes: list[str]) -> frozenset[str] | AgentError:
    if not scopes:
        return invalid("Choose at least one scope.")
    cleaned = frozenset(scopes)
    if not cleaned or cleaned - VALID_SCOPES:
        return invalid("Choose valid scopes.")
    if SCOPE_READ not in cleaned and SCOPE_WRITE not in cleaned:
        return invalid("Choose valid scopes.")
    return cleaned


def _hash(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def _public(row: AgentToken) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "scopes": [part for part in row.scopes.split(",") if part],
        "token_prefix": row.token_prefix,
        "created_at": row.created_at,
        "revoked_at": row.revoked_at,
    }


def constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
