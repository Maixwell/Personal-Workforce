"""Database-free Decision 16 fingerprint and ID-boundary checks."""

import ast
import hashlib
import inspect
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from max_smart_db.application_validation import (  # noqa: E402
    ApplicationValidationError,
    require_explicit_positive_integer_id,
)
from max_smart_db.fingerprint import (  # noqa: E402
    FINGERPRINT_DOMAIN_TAG,
    FingerprintValidationError,
    fingerprint_v1,
)


class _IntSubclass(int):
    pass


class _StrSubclass(str):
    pass


_COMMON = {
    "operation_type": "message",
    "role_slug": "max-smart",
    "provider_id": "openai",
    "model_id": "gpt-5.6-sol",
}

_VECTORS = (
    {
        "owner_id": 1,
        "conversation_id": "00112233445566778899aabbccddeeff",
        "expected_version": 0,
        "request_content": "Hello, world!",
        "digest": "0fe626b8228f021342356f67de2c34e22c72ff92d4671f66e2e5942602737b2c",
    },
    {
        "owner_id": 42,
        "conversation_id": "0123456789abcdeffedcba9876543210",
        "expected_version": 7,
        "request_content": "こんにちは, Max 👋",
        "digest": "e69bf7eb31e0ed56f9b8ecc46715ab2795263f816245eb30cac077d05b46bc11",
    },
    {
        "owner_id": 7,
        "conversation_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "expected_version": 12,
        "request_content": " \tHello\u00a0 \n",
        "digest": "3f7bda0c831ae42d4954f4cb8f9131453cf4a7b45e87344ec42872b6ecf38556",
    },
    {
        "owner_id": 9,
        "conversation_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "expected_version": 3,
        "request_content": "line 1\r\nline 2\r\n",
        "digest": "e6c4501a5e2dbd0337dc03ad67ac926e130f0065e6f359ccb3631fca6c89e4ac",
    },
    {
        "owner_id": 9223372036854775807,
        "conversation_id": "ffffffffffffffffffffffffffffffff",
        "expected_version": 9223372036854775807,
        "request_content": "a" * 20_000,
        "digest": "cd75169e4b0a226382882bc57d1f62bc8e089f70a612bf206883c84ee2973af8",
    },
)


def _encode(vector, **overrides):
    arguments = {**_COMMON, **vector, **overrides}
    arguments.pop("digest", None)
    return fingerprint_v1(**arguments)


class FingerprintV1Tests(unittest.TestCase):
    def test_domain_tag_is_the_approved_v1_tag(self):
        self.assertEqual(FINGERPRINT_DOMAIN_TAG, "max-smart/op-fingerprint/v1")

    def test_original_vectors_match_original_digests(self):
        self.assertEqual(_VECTORS[2]["request_content"], " \tHello\u00a0 \n")
        self.assertEqual(_VECTORS[3]["request_content"], "line 1\r\nline 2\r\n")
        self.assertEqual(len(_VECTORS[4]["request_content"]), 20_000)
        for index, vector in enumerate(_VECTORS, start=1):
            with self.subTest(vector=index):
                digest = _encode(vector)
                self.assertEqual(digest, vector["digest"])
                self.assertEqual(digest, digest.lower())
                self.assertEqual(len(digest), 64)

    def test_zero_and_signed_maximum_are_deterministic(self):
        self.assertEqual(_encode(_VECTORS[0]), _encode(_VECTORS[0]))
        self.assertEqual(_encode(_VECTORS[4]), _VECTORS[4]["digest"])

    def test_integer_rejections_happen_before_hashing(self):
        rejected = (
            ("owner_id", -1),
            ("owner_id", 9223372036854775808),
            ("owner_id", False),
            ("owner_id", True),
            ("owner_id", _IntSubclass(1)),
            ("owner_id", 1.0),
            ("expected_version", -1),
            ("expected_version", 9223372036854775808),
            ("expected_version", False),
            ("expected_version", True),
            ("expected_version", _IntSubclass(0)),
            ("expected_version", "0"),
        )
        for field_name, value in rejected:
            with self.subTest(field=field_name, value=type(value).__name__):
                with patch(
                    "max_smart_db.fingerprint.hashlib.sha256",
                    side_effect=AssertionError("hashed"),
                ):
                    with self.assertRaises(FingerprintValidationError):
                        _encode(_VECTORS[0], **{field_name: value})

    def test_text_type_subclass_and_unicode_rejections(self):
        rejected = (
            ("operation_type", _StrSubclass("message")),
            ("role_slug", b"max-smart"),
            ("provider_id", None),
            ("model_id", _StrSubclass("gpt-5.6-sol")),
            ("request_content", "\ud800"),
            ("request_content", "Hello\udfff"),
            ("role_slug", "\ud800"),
        )
        for field_name, value in rejected:
            with self.subTest(field=field_name):
                with patch(
                    "max_smart_db.fingerprint.hashlib.sha256",
                    side_effect=AssertionError("hashed"),
                ):
                    with self.assertRaises(FingerprintValidationError):
                        _encode(_VECTORS[0], **{field_name: value})

    def test_invalid_conversation_ids_are_rejected_without_uuid_parsing(self):
        rejected = (
            "00112233-4455-6677-8899-aabbccddeeff",
            "00112233445566778899AABBCCDDEEFF",
            "00112233445566778899aabbccddeef",
            "00112233445566778899aabbccddeefff",
            "00112233445566778899aabbccddeefg",
            "{00112233445566778899aabbccddeeff}",
            "urn:uuid:00112233-4455-6677-8899-aabbccddeeff",
            "00112233445566778899aabbccddeef ",
            " 00112233445566778899aabbccddee",
            "00112233445566778899aabbccddee\x00",
        )
        for conversation_id in rejected:
            with self.subTest(conversation_id=conversation_id):
                with self.assertRaises(FingerprintValidationError) as caught:
                    _encode(_VECTORS[0], conversation_id=conversation_id)
                self.assertEqual(caught.exception.code, "conversation_id_format")

    def test_request_length_boundary(self):
        self.assertEqual(len(_VECTORS[4]["request_content"]), 20_000)
        self.assertEqual(_encode(_VECTORS[4]), _VECTORS[4]["digest"])
        with patch(
            "max_smart_db.fingerprint.hashlib.sha256",
            side_effect=AssertionError("hashed"),
        ):
            with self.assertRaises(FingerprintValidationError) as caught:
                _encode(_VECTORS[0], request_content="a" * 20_001)
        self.assertEqual(caught.exception.code, "request_content_length")

    def test_encoder_accepts_values_application_checks_may_reject(self):
        digest = _encode(_VECTORS[0], owner_id=0, request_content="")
        self.assertEqual(len(digest), 64)
        self.assertNotEqual(digest, _VECTORS[0]["digest"])

    def test_encoder_does_not_validate_idempotency_keys(self):
        parameters = tuple(inspect.signature(fingerprint_v1).parameters)
        self.assertNotIn("idempotency_key", parameters)
        source = (PROJECT_ROOT / "src" / "max_smart_db" / "fingerprint.py").read_text()
        tree = ast.parse(source)
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        self.assertNotIn("idempotency_key", names)
        self.assertNotIn("b64decode", names)

    def test_positive_integer_ids_are_separate_from_fingerprint_zero(self):
        self.assertEqual(require_explicit_positive_integer_id(1), 1)
        self.assertEqual(
            require_explicit_positive_integer_id(9223372036854775807),
            9223372036854775807,
        )
        zero_owner = _encode(_VECTORS[0], owner_id=0)
        self.assertEqual(len(zero_owner), 64)
        self.assertNotEqual(zero_owner, _VECTORS[0]["digest"])
        rejected = (
            None,
            0,
            -1,
            False,
            True,
            _IntSubclass(1),
            9223372036854775808,
            "1",
        )
        for value in rejected:
            with self.subTest(value=value):
                with self.assertRaises(ApplicationValidationError):
                    require_explicit_positive_integer_id(value)

    def test_published_short_frames_still_hash_to_the_original_digests(self):
        frames = {
            1: (
                "000000000000001b6d61782d736d6172742f6f702d66696e6765727072696e742f7631"
                "000000000000000131"
                "000000000000002430303131323233332d343435352d363637372d383839392d616162626363646465656666"
                "00000000000000076d657373616765"
                "000000000000000130"
                "00000000000000096d61782d736d617274"
                "00000000000000066f70656e6169"
                "000000000000000b6770742d352e362d736f6c"
                "000000000000000d48656c6c6f2c20776f726c6421"
            ),
            3: (
                "000000000000001b6d61782d736d6172742f6f702d66696e6765727072696e742f7631"
                "000000000000000137"
                "000000000000002461616161616161612d616161612d616161612d616161612d616161616161616161616161"
                "00000000000000076d657373616765"
                "00000000000000023132"
                "00000000000000096d61782d736d617274"
                "00000000000000066f70656e6169"
                "000000000000000b6770742d352e362d736f6c"
                "000000000000000b200948656c6c6fc2a0200a"
            ),
        }
        for index, framed in frames.items():
            digest = hashlib.sha256(bytes.fromhex(framed)).hexdigest()
            self.assertEqual(digest, _VECTORS[index - 1]["digest"])
            self.assertEqual(_encode(_VECTORS[index - 1]), digest)
