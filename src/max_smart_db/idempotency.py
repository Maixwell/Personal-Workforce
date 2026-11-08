"""Canonical idempotency-key validation and digesting."""

import base64
import binascii
import hashlib
import re


_CANONICAL_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_MINIMUM_KEY_BYTES = 16


class IdempotencyKeyError(ValueError):
    """An idempotency key is not canonical or sufficiently strong."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def idempotency_key_digest(value):
    """Return SHA-256(decoded canonical base64url bytes) as lowercase hex."""
    if type(value) is not str:
        raise IdempotencyKeyError("idempotency_key_type")
    if not _CANONICAL_BASE64URL.fullmatch(value):
        raise IdempotencyKeyError("idempotency_key_format")
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error):
        raise IdempotencyKeyError("idempotency_key_format") from None
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if canonical != value:
        raise IdempotencyKeyError("idempotency_key_canonical")
    if len(decoded) < _MINIMUM_KEY_BYTES:
        raise IdempotencyKeyError("idempotency_key_entropy")
    return hashlib.sha256(decoded).hexdigest()
