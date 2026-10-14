"""Webhook source operations."""

import secrets

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import Source, SourceSecret
from app.log import log_mutation
from app.radicale.client import RadicaleClient
from app.secrets import SecretKeyError, decrypt, encrypt, equal
from app.services.sources.present import _new_secret, _now
from app.services.sources.types import (
    SourceFailure,
    WebhookApplyError,
    WebhookPayloadError,
    WebhookSecretError,
    _failure,
)
from app.sync.runner import SyncFailed, run_webhook
from app.sync.webhook import ParseError, parse


def apply_webhook_payload(
    engine: Engine,
    radicale: RadicaleClient,
    source: dict,
    payload: object,
) -> None:
    """Parse a webhook body and apply it. Raises WebhookPayloadError or WebhookApplyError."""

    try:
        command = parse(payload, source["timezone"])
    except ParseError as exc:
        raise WebhookPayloadError(exc.detail) from None
    try:
        run_webhook(engine, radicale, source, command)
    except SyncFailed as exc:
        raise WebhookApplyError(exc.message) from None


def rotate_webhook_secret(
    engine: Engine,
    source_id: str,
    *,
    reveal: bool = False,
) -> dict | SourceFailure:
    with Session(engine) as session:
        row = session.get(Source, source_id)
        if row is None:
            return _failure(404, "sentence.not_found")
        if row.kind != "webhook":
            return _failure(
                422,
                "sentence.rotate_webhook",
                error="rotate_not_supported",
            )
    try:
        plaintext = secrets.token_urlsafe(32)
        ciphertext = encrypt(plaintext)
    except SecretKeyError:
        return _failure(500, "sentence.secret_key")
    with Session(engine) as session, session.begin():
        row = session.get(Source, source_id)
        if row is None or row.kind != "webhook":
            return _failure(404, "sentence.not_found")
        secret = session.get(SourceSecret, source_id)
        if secret is None:
            secret = _new_secret(source_id, ciphertext)
            session.add(secret)
        else:
            secret.webhook_secret = ciphertext
        row.updated_at = _now()
    log_mutation(actor="owner", source_id=source_id, message="rotated webhook secret")
    payload: dict = {"id": source_id, "rotated": True}
    if reveal:
        payload["webhook_secret"] = plaintext
    return payload


def webhook_secret_matches(engine: Engine, source_id: str, provided: str) -> bool:
    with Session(engine) as session:
        row = session.get(SourceSecret, source_id)
        stored = row.webhook_secret if row is not None else None
    try:
        if not stored:
            equal(provided, "missing-webhook-secret")
            return False
        return equal(decrypt(stored), provided)
    except SecretKeyError as exc:
        raise WebhookSecretError(str(exc)) from None
