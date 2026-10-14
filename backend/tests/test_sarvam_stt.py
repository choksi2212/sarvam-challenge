"""Tests for backend/sarvam_stt.py (A3).

HERMETIC: every HTTP interaction goes through httpx.MockTransport — no
live Sarvam calls, no .env reads (CONTRACT §8). The API key is faked via
the `_api_key` seam so the suite passes whether or not A7's
backend/config.py exists at integration time.

Run from the worktree root:

    python -m unittest discover -s backend/tests
"""

from __future__ import annotations

import json
import os
import re
import sys
import unittest
from unittest import mock

import httpx

# Make `backend` importable no matter how unittest discovery bootstraps
# sys.path (as top-level `test_sarvam_stt` or as `backend.tests....`).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from backend import sarvam_stt  # noqa: E402

# Fake WAV payload: deliberately free of CR/LF bytes so naive multipart
# parsing in these tests is byte-exact.
WAV_BYTES = b"RIFF_fake_wav_payload_for_unit_tests"
KEYTERMS = ["Sachin", "Tendulkar", "सचिन"]
FAKE_KEY = "test-key-123"

OK_BODY = {
    "request_id": "req-20261013-abc",
    "transcript": "सचिन Tendulkar",
    "language_code": "hi-IN",
    "probability": 0.97,
}


def _parse_multipart(request: httpx.Request) -> dict[str, bytes]:
    """Minimal multipart/form-data parser → {field name: raw value}."""
    content_type = request.headers["content-type"]
    assert content_type.startswith("multipart/form-data"), content_type
    boundary_token = (
        content_type.split("boundary=")[1].split(";")[0].strip().strip('"')
    )
    boundary = boundary_token.encode()
    fields: dict[str, bytes] = {}
    for chunk in request.content.split(b"--" + boundary)[1:]:
        if not chunk:  # preamble or empty
            continue
        chunk = chunk.removeprefix(b"\r\n")
        if chunk.startswith(b"--"):  # closing delimiter
            continue
        header_blob, _, value = chunk.partition(b"\r\n\r\n")
        # Every value is terminated by one CRLF that belongs to the framing
        # (it precedes the next boundary) — strip exactly that, not more.
        value = value.removesuffix(b"\r\n")
        match = re.search(rb'name="([^"]*)"', header_blob)
        if match:
            fields[match.group(1).decode()] = value
    return fields


class TranscribeAnswerTests(unittest.IsolatedAsyncioTestCase):
    """transcribe_answer behaviour over httpx.MockTransport."""

    def setUp(self) -> None:
        # Hermetic key for every test, independent of backend/config.py.
        patcher = mock.patch.object(sarvam_stt, "_api_key", return_value=FAKE_KEY)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- transport factories -------------------------------------------

    def _transport_returning(
        self, capture: list[httpx.Request], response: httpx.Response
    ) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            request.read()
            capture.append(request)
            return response

        return httpx.MockTransport(handler)

    def _transport_raising(
        self, capture: list[httpx.Request], exc: Exception
    ) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            request.read()
            capture.append(request)
            raise exc

        return httpx.MockTransport(handler)

    # -- success path ---------------------------------------------------

    async def test_200_parses_transcript_and_language_code(self) -> None:
        capture: list[httpx.Request] = []
        transport = self._transport_returning(
            capture, httpx.Response(200, json=OK_BODY)
        )
        with mock.patch.object(sarvam_stt, "_transport", transport):
            result = await sarvam_stt.transcribe_answer(WAV_BYTES)

        self.assertEqual(
            result,
            {
                "text": "सचिन Tendulkar",
                "language_code": "hi-IN",
                "raw": OK_BODY,  # full response, not a projection
            },
        )
        self.assertEqual(set(result), {"text", "language_code", "raw"})
        self.assertEqual(result["raw"]["request_id"], "req-20261013-abc")

    async def test_200_without_detected_language_code_yields_empty_string(self) -> None:
        capture: list[httpx.Request] = []
        transport = self._transport_returning(
            capture, httpx.Response(200, json={"transcript": "hello there"})
        )
        with mock.patch.object(sarvam_stt, "_transport", transport):
            result = await sarvam_stt.transcribe_answer(WAV_BYTES)
        self.assertEqual(result["text"], "hello there")
        self.assertEqual(result["language_code"], "")

    # -- request shape --------------------------------------------------

    async def test_request_is_correct_multipart_post(self) -> None:
        capture: list[httpx.Request] = []
        transport = self._transport_returning(
            capture, httpx.Response(200, json=OK_BODY)
        )
        with mock.patch.object(sarvam_stt, "_transport", transport):
            await sarvam_stt.transcribe_answer(WAV_BYTES)

        self.assertEqual(len(capture), 1)
        request = capture[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), "https://api.sarvam.ai/speech-to-text")

        content_type = request.headers["content-type"]
        self.assertTrue(
            content_type.startswith("multipart/form-data"), content_type
        )
        self.assertIn("boundary=", content_type)
        self.assertEqual(request.headers["api-subscription-key"], FAKE_KEY)

        # 12 s budget from CONTRACT §3 flows into the request.
        self.assertEqual(
            request.extensions.get("timeout"),
            {"connect": 12.0, "read": 12.0, "write": 12.0, "pool": 12.0},
        )

        fields = _parse_multipart(request)
        self.assertEqual(fields["model"], b"saaras:v4")
        self.assertEqual(fields["mode"], b"codemix")
        self.assertEqual(fields["language_code"], b"unknown")
        self.assertEqual(fields["file"], WAV_BYTES)
        self.assertIn(
            b'Content-Disposition: form-data; name="file"; filename="answer.wav"',
            request.content,
        )
        self.assertIn(b"Content-Type: audio/wav", request.content)
        self.assertNotIn("keyterms", fields)  # not provided → not sent

    async def test_keyterms_sent_as_single_json_encoded_field(self) -> None:
        capture: list[httpx.Request] = []
        transport = self._transport_returning(
            capture, httpx.Response(200, json=OK_BODY)
        )
        with mock.patch.object(sarvam_stt, "_transport", transport):
            await sarvam_stt.transcribe_answer(WAV_BYTES, KEYTERMS)

        fields = _parse_multipart(capture[0])
        self.assertIn("keyterms", fields)
        # ONE form field whose value is exactly the JSON encoding of the list…
        self.assertEqual(fields["keyterms"], json.dumps(KEYTERMS).encode())
        # …which round-trips back to the original keyterms.
        self.assertEqual(json.loads(fields["keyterms"]), KEYTERMS)
        # exactly one keyterms part, not one per term
        self.assertEqual(
            request_count := capture[0].content.count(b'name="keyterms"'), 1,
            f"expected 1 keyterms part, found {request_count}",
        )
        # the other probed fields are unchanged when keyterms ride along
        self.assertEqual(fields["model"], b"saaras:v4")
        self.assertEqual(fields["mode"], b"codemix")
        self.assertEqual(fields["language_code"], b"unknown")

    async def test_empty_keyterms_list_omits_the_field(self) -> None:
        capture: list[httpx.Request] = []
        transport = self._transport_returning(
            capture, httpx.Response(200, json=OK_BODY)
        )
        with mock.patch.object(sarvam_stt, "_transport", transport):
            await sarvam_stt.transcribe_answer(WAV_BYTES, [])
        self.assertNotIn("keyterms", _parse_multipart(capture[0]))

    # -- failures --------------------------------------------------------

    async def test_non_200_raises_stt_unavailable(self) -> None:
        for status in (401, 429, 500, 503):
            with self.subTest(status=status):
                capture: list[httpx.Request] = []
                transport = self._transport_returning(
                    capture,
                    httpx.Response(
                        status, json={"error": "server exploded"}
                    ),
                )
                with mock.patch.object(sarvam_stt, "_transport", transport):
                    with self.assertRaises(sarvam_stt.STTUnavailable) as ctx:
                        await sarvam_stt.transcribe_answer(WAV_BYTES)
                self.assertIn(str(status), str(ctx.exception))
                self.assertEqual(len(capture), 1)  # one attempt, no retries

    async def test_timeout_raises_stt_unavailable(self) -> None:
        capture: list[httpx.Request] = []
        transport = self._transport_raising(
            capture, httpx.ReadTimeout("read timed out")
        )
        with mock.patch.object(sarvam_stt, "_transport", transport):
            with self.assertRaises(sarvam_stt.STTUnavailable) as ctx:
                await sarvam_stt.transcribe_answer(WAV_BYTES)
        self.assertIn("timed out", str(ctx.exception))
        self.assertEqual(len(capture), 1)

    async def test_transport_error_raises_stt_unavailable(self) -> None:
        capture: list[httpx.Request] = []
        transport = self._transport_raising(
            capture, httpx.ConnectError("connection refused")
        )
        with mock.patch.object(sarvam_stt, "_transport", transport):
            with self.assertRaises(sarvam_stt.STTUnavailable) as ctx:
                await sarvam_stt.transcribe_answer(WAV_BYTES)
        self.assertIn("ConnectError", str(ctx.exception))

    async def test_empty_or_garbage_transcript_raises(self) -> None:
        bodies = [
            {"transcript": "", "language_code": "hi-IN"},
            {"transcript": "   ", "language_code": "hi-IN"},
            {"transcript": "x", "language_code": "hi-IN"},  # 1 char after strip
            {"language_code": "hi-IN"},  # transcript missing entirely
            {"transcript": None, "language_code": "hi-IN"},
            {"transcript": 12345, "language_code": "hi-IN"},  # not a string
        ]
        for body in bodies:
            with self.subTest(body=body):
                transport = self._transport_returning(
                    [], httpx.Response(200, json=body)
                )
                with mock.patch.object(sarvam_stt, "_transport", transport):
                    with self.assertRaises(sarvam_stt.STTUnavailable):
                        await sarvam_stt.transcribe_answer(WAV_BYTES)

    async def test_malformed_response_raises(self) -> None:
        responses = [
            httpx.Response(200, text="not json at all"),
            httpx.Response(200, json=[1, 2, 3]),  # JSON, but not an object
            httpx.Response(200, json="just a string"),
        ]
        for response in responses:
            with self.subTest(response=response.text[:20]):
                transport = self._transport_returning([], response)
                with mock.patch.object(sarvam_stt, "_transport", transport):
                    with self.assertRaises(sarvam_stt.STTUnavailable):
                        await sarvam_stt.transcribe_answer(WAV_BYTES)

    async def test_missing_api_key_raises_before_any_request(self) -> None:
        def unreachable(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request may be sent without a key")

        transport = httpx.MockTransport(unreachable)
        with mock.patch.object(sarvam_stt, "_api_key", return_value=""):
            with mock.patch.object(sarvam_stt, "_transport", transport):
                with self.assertRaises(sarvam_stt.STTUnavailable) as ctx:
                    await sarvam_stt.transcribe_answer(WAV_BYTES)
        self.assertIn("SARVAM_API_KEY", str(ctx.exception))

    async def test_empty_wav_bytes_raise_before_any_request(self) -> None:
        def unreachable(request: httpx.Request) -> httpx.Response:
            raise AssertionError("empty audio must not be sent")

        transport = httpx.MockTransport(unreachable)
        with mock.patch.object(sarvam_stt, "_transport", transport):
            with self.assertRaises(sarvam_stt.STTUnavailable):
                await sarvam_stt.transcribe_answer(b"")

    async def test_non_bytes_wav_raises_before_any_request(self) -> None:
        def unreachable(request: httpx.Request) -> httpx.Response:
            raise AssertionError("non-bytes audio must not be sent")

        transport = httpx.MockTransport(unreachable)
        with mock.patch.object(sarvam_stt, "_transport", transport):
            with self.assertRaises(sarvam_stt.STTUnavailable):
                await sarvam_stt.transcribe_answer("RIFF but a str")  # type: ignore[arg-type]


class APIKeyResolutionTests(unittest.TestCase):
    """_api_key fallback logic (no network, no .env reads)."""

    def test_env_fallback_when_config_not_importable(self) -> None:
        # Force the backend.config import to fail even at integration time
        # (must scrub BOTH sys.modules AND the cached package attribute --
        # any earlier test importing backend.config sets `backend.config`
        # as a package attribute, which defeats the sys.modules trick).
        import backend as _backend_pkg
        with mock.patch.object(_backend_pkg, "config", None, create=True),              mock.patch.dict(sys.modules, {"backend.config": None}):
            with mock.patch.dict(os.environ, {"SARVAM_API_KEY": "env-key-xyz"}):
                self.assertEqual(sarvam_stt._api_key(), "env-key-xyz")

            scrubbed = {
                k: v for k, v in os.environ.items() if k != "SARVAM_API_KEY"
            }
            with mock.patch.dict(os.environ, scrubbed, clear=True):
                self.assertEqual(sarvam_stt._api_key(), "")


if __name__ == "__main__":
    unittest.main()
