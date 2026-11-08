"""Repository-boundary checks that stay outside fingerprint encoding.

Fingerprint integers may be zero. An explicitly supplied application integer
ID is positive and stops at the signed 64-bit maximum.
"""


SIGNED_INT64_MAX = 9223372036854775807


class ApplicationValidationError(ValueError):
    """A repository-boundary value was rejected before any database call."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def require_explicit_positive_integer_id(value):
    """Require an exact int in 1..9223372036854775807."""
    if type(value) is not int:
        raise ApplicationValidationError("positive_integer_id_type")
    if value < 1 or value > SIGNED_INT64_MAX:
        raise ApplicationValidationError("positive_integer_id_range")
    return value
