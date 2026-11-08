"""Decision 16 fingerprint V1 encoder.

Integer components accept exact ints from 0 through the signed 64-bit
maximum. This module does not validate idempotency keys or repository IDs.
"""

import hashlib
import struct


FINGERPRINT_DOMAIN_TAG = "max-smart/op-fingerprint/v1"
FINGERPRINT_VERSION = 1
SIGNED_INT64_MAX = 9223372036854775807
REQUEST_MAX_CODE_POINTS = 20_000

_LOWER_HEX = frozenset("0123456789abcdef")


class FingerprintValidationError(ValueError):
    """The encoder rejected a component before framing or hashing."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def fingerprint_v1(
    owner_id,
    conversation_id,
    operation_type,
    expected_version,
    role_slug,
    provider_id,
    model_id,
    request_content,
):
    """Return the lowercase SHA-256 hex digest of fingerprint V1."""
    _require_exact_int(owner_id, "owner_id")
    _require_exact_str(conversation_id, "conversation_id")
    _require_conversation(conversation_id)
    _require_exact_str(operation_type, "operation_type")
    _require_exact_str(role_slug, "role_slug")
    _require_exact_str(provider_id, "provider_id")
    _require_exact_str(model_id, "model_id")
    _require_exact_int(expected_version, "expected_version")
    _require_exact_str(request_content, "request_content")
    _require_request(request_content)

    canonical_conversation = (
        conversation_id[0:8]
        + "-"
        + conversation_id[8:12]
        + "-"
        + conversation_id[12:16]
        + "-"
        + conversation_id[16:20]
        + "-"
        + conversation_id[20:32]
    )
    components = (
        FINGERPRINT_DOMAIN_TAG,
        str(owner_id),
        canonical_conversation,
        operation_type,
        str(expected_version),
        role_slug,
        provider_id,
        model_id,
        request_content,
    )
    framed = bytearray()
    for component in components:
        encoded = component.encode("utf-8")
        framed += struct.pack(">Q", len(encoded))
        framed += encoded
    return hashlib.sha256(framed).hexdigest()


def _require_exact_int(value, name):
    if type(value) is not int:
        raise FingerprintValidationError(f"{name}_type")
    if value < 0 or value > SIGNED_INT64_MAX:
        raise FingerprintValidationError(f"{name}_range")


def _require_exact_str(value, name):
    if type(value) is not str:
        raise FingerprintValidationError(f"{name}_type")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise FingerprintValidationError(f"{name}_unicode") from None


def _require_conversation(value):
    if len(value) != 32 or any(character not in _LOWER_HEX for character in value):
        raise FingerprintValidationError("conversation_id_format")


def _require_request(value):
    if len(value) > REQUEST_MAX_CODE_POINTS:
        raise FingerprintValidationError("request_content_length")
