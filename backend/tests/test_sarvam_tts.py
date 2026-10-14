"""Tests for backend/sarvam_tts.py — CONTRACT §3 (A1) + §4 probed facts.

NO live Sarvam API calls: the WS tests run against an in-process fake server
(``websockets.serve`` on 127.0.0.1) that replays the PROBED behaviors — 44-byte RIFF
header first chunk then raw PCM continuation, ``{type:"event",data:{event_type:"final"}}``
termination, close codes 1000/1003/1006/4401, mid-connection config re-send. The
REST/HTTP-stream tests use ``httpx.MockTransport``.

Run:  python -m unittest discover -s backend/tests   (from the worktree root)
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import sys
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/

import httpx  # noqa: E402
import websockets  # noqa: E402
from websockets.exceptions import ConnectionClosed  # noqa: E402
from websockets.frames import Close  # noqa: E402

import sarvam_tts  # noqa: E402

TEST_KEY = "test-api-key-123"
SPEAKER_A = "ratan_hi_customer_expressive"
SPEAKER_B = "ritu_hi_reels"
PCM1 = bytes(range(256)) * 128  # 32768 bytes of PCM-ish data
PCM2 = b"\xab\xcd" * 16384  # 32768 bytes
H44 = sarvam_tts.canonical_wav_header(24000, 1, 16, len(PCM1))
H44_B = sarvam_tts.canonical_wav_header(24000, 1, 16, len(PCM2))
DEFAULT_CHUNKS = [H44, PCM1]

EXPECTED_CONFIG_DATA = {
    "speaker": SPEAKER_A,
    "language_code": "hi-IN",
    "pace": 1.05,
    "speech_sample_rate": 24000,
    "output_audio_codec": "wav",
    "output_audio_bitrate": "128k",
    "min_buffer_size": 30,
    "max_chunk_length": 200,
}


class SleepRecorder:
    """Injectable asyncio.sleep replacement that records requested delays."""

    def __init__(self, real_delay: float = 0.0) -> None:
        self.calls: list[float] = []
        self._real_delay = real_delay

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)
        if self._real_delay:
            await asyncio.sleep(self._real_delay)


def audio_frame(chunk: bytes) -> str:
    return json.dumps({"type": "audio", "data": {"audio": base64.b64encode(chunk).decode()}})


FINAL_FRAME = json.dumps({"type": "event", "data": {"event_type": "final"}})
HEALTHY_FRAMES = [audio_frame(H44), audio_frame(PCM1), FINAL_FRAME]


class ScriptedConn:
    """Duck-typed websockets client connection for deterministic close-code tests.

    ``send()`` succeeds until the ``fail_send_from``-th message, then raises the REAL
    ``websockets.exceptions.ConnectionClosed`` with ``fail_code``. ``recv()`` returns the
    scripted frames, then raises ConnectionClosed with ``recv_close_code``. The channel
    only needs send/recv/close/close_code/close_reason — all provided.
    """

    def __init__(
        self,
        *,
        recv_frames: list | None = None,
        recv_close_code: int = 1006,
        recv_close_reason: str = "abnormal closure",
        fail_send_from: int | None = None,
        fail_code: int = 1006,
        fail_reason: str = "abnormal closure",
    ) -> None:
        self.send_log: list[str] = []
        self._frames = list(recv_frames) if recv_frames is not None else list(HEALTHY_FRAMES)
        self._recv_close_code = recv_close_code
        self._recv_close_reason = recv_close_reason
        self._fail_send_from = fail_send_from
        self._fail_code = fail_code
        self._fail_reason = fail_reason
        self.close_code: int | None = None
        self.close_reason: str = ""

    async def send(self, payload: str) -> None:
        if self._fail_send_from is not None and len(self.send_log) >= self._fail_send_from:
            self.close_code = self._fail_code
            self.close_reason = self._fail_reason
            raise ConnectionClosed(Close(code=self._fail_code, reason=self._fail_reason), None)
        self.send_log.append(payload)

    async def recv(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        self.close_code = self._recv_close_code
        self.close_reason = self._recv_close_reason
        raise ConnectionClosed(Close(code=self._recv_close_code, reason=self._recv_close_reason), None)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.close_code is None:
            self.close_code = code
            self.close_reason = reason


class ScriptedConnector:
    """Monkeypatched websockets.connect stand-in: returns ScriptedConn in sequence."""

    def __init__(self, *scripts: dict) -> None:
        self.scripts = list(scripts)
        self.calls = 0
        self.conns: list[ScriptedConn] = []
        self.urls: list[str] = []
        self.headers: list[dict] = []

    async def __call__(self, url: str, headers: dict) -> ScriptedConn:
        self.urls.append(url)
        self.headers.append(dict(headers))
        self.calls += 1
        if self.calls > len(self.scripts):
            raise OSError("scripted connector exhausted")
        conn = ScriptedConn(**self.scripts[self.calls - 1])
        self.conns.append(conn)
        return conn


class FakeSarvamWS:
    """In-process stand-in for Sarvam's TTS WebSocket (probed frame shapes).

    ``plans`` gives per-connection behavior (1-indexed by connection number);
    connections beyond the plan list get the default "serve" behavior: on every
    flush send the scripted chunks (audio frames) then the final completion event.

    Plan keys (mode defaults to "serve"):
      chunks / chunks_per_flush : list of bytes (or list-of-lists, per flush)
      no_final                  : suppress the final event (idle tests)
      abort_after_flush         : transport.abort() after chunks  → client sees 1006
      close_after_flush         : {"code": int, "reason": str} → clean-ish close
      error_after_flush         : send {type:"error"} then close 1011
      start_event_first         : send a non-final event frame before the audio
      mode="abort_on_open"      : abort immediately
      mode="close_on_open"      : close immediately with code/reason
      mode="abort_after_config" : abort as soon as a config message arrives
      mode="close_after_config" : close with code/reason as soon as config arrives
    """

    def __init__(self, plans: list | None = None, default_chunks: list | None = None) -> None:
        self.plans = list(plans or [])
        self.default_chunks = default_chunks or DEFAULT_CHUNKS
        self.received: list[list[dict]] = []  # parsed messages per connection
        self.headers: list[dict] = []  # upgrade headers per connection
        self.paths: list[str] = []  # request path per connection
        self.conn_count = 0
        self._server = None
        self.port: int | None = None

    @property
    def url(self) -> str:
        return (
            f"ws://127.0.0.1:{self.port}/text-to-speech/ws"
            f"?model=bulbul:v4-flash&send_completion_event=true"
        )

    async def start(self) -> str:
        self._server = await websockets.serve(
            self._handler, "127.0.0.1", 0, ping_interval=None, close_timeout=1.0
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self.url

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def plan_for(self, n: int) -> dict:
        return self.plans[n - 1] if n - 1 < len(self.plans) else {}

    def chunks_for(self, plan: dict, flush_no: int) -> list[bytes]:
        per_flush = plan.get("chunks_per_flush")
        if per_flush and flush_no - 1 < len(per_flush):
            return per_flush[flush_no - 1]
        if plan.get("chunks") is not None:
            return plan["chunks"]
        return self.default_chunks

    @staticmethod
    async def _send_audio(conn, chunk: bytes) -> None:
        await conn.send(
            json.dumps({"type": "audio", "data": {"audio": base64.b64encode(chunk).decode()}})
        )

    @staticmethod
    async def _send_event(conn, event_type: str) -> None:
        await conn.send(json.dumps({"type": "event", "data": {"event_type": event_type}}))

    async def _handler(self, conn) -> None:
        n = self.conn_count + 1
        self.conn_count = n
        self.received.append([])
        self.headers.append(dict(conn.request.headers))
        self.paths.append(conn.request.path)
        plan = self.plan_for(n)
        mode = plan.get("mode", "serve")
        flush_no = 0
        try:
            if mode == "abort_on_open":
                conn.transport.abort()
                return
            if mode == "close_on_open":
                await conn.close(code=plan.get("code", 1000), reason=plan.get("reason", ""))
                return
            async for raw in conn:
                msg = json.loads(raw)
                self.received[n - 1].append(msg)
                mtype = msg.get("type")
                if mtype == "config" and mode in ("abort_after_config", "close_after_config"):
                    if mode == "abort_after_config":
                        conn.transport.abort()
                    else:
                        await conn.close(
                            code=plan.get("code", 4401), reason=plan.get("reason", "Invalid API key")
                        )
                    return
                if mtype == "flush":
                    flush_no += 1
                    if plan.get("error_after_flush"):
                        await conn.send(
                            json.dumps(
                                {
                                    "type": "error",
                                    "data": {"message": plan.get("error_message", "boom")},
                                }
                            )
                        )
                        await conn.close(code=1011, reason=plan.get("error_message", "boom"))
                        return
                    if plan.get("start_event_first"):
                        await self._send_event(conn, "start")
                    for chunk in self.chunks_for(plan, flush_no):
                        await self._send_audio(conn, chunk)
                    if not plan.get("no_final"):
                        await self._send_event(conn, "final")
                    if plan.get("abort_after_flush"):
                        conn.transport.abort()
                        return
                    cc = plan.get("close_after_flush")
                    if cc:
                        await conn.close(code=cc.get("code", 1000), reason=cc.get("reason", ""))
                        return
        except Exception:  # noqa: BLE001 — fake server must never crash the tests
            pass


class VoiceChannelTestBase(unittest.IsolatedAsyncioTestCase):
    async def start_server(self, **kw) -> tuple[FakeSarvamWS, str]:
        server = FakeSarvamWS(**kw)
        self.addAsyncCleanup(server.stop)
        url = await server.start()
        return server, url

    def make_channel(self, url: str, **kw) -> sarvam_tts.VoiceChannel:
        channel = sarvam_tts.VoiceChannel(ws_url=url, api_key=TEST_KEY, **kw)
        self.addAsyncCleanup(channel.close)
        return channel

    @staticmethod
    async def collect(agen) -> list[bytes]:
        out: list[bytes] = []
        async for chunk in agen:
            out.append(chunk)
        return out

    async def collect_and_raise(self, agen, exc_type):
        out: list[bytes] = []
        with self.assertRaises(exc_type) as cm:
            async for chunk in agen:
                out.append(chunk)
        return out, cm.exception


class TestVoiceChannelProtocol(VoiceChannelTestBase):
    """connect / set_speaker / speak message sequences against the fake WS."""

    async def test_connect_sends_auth_header_and_url_params_then_config(self):
        server, url = await self.start_server()
        ch = self.make_channel(url)
        await ch.connect()
        await ch.set_speaker(SPEAKER_A, "hi-IN", 1.05)
        await asyncio.sleep(0.1)  # let the same-loop server handler record the frame
        self.assertEqual(server.conn_count, 1)
        self.assertEqual(server.headers[0]["api-subscription-key"], TEST_KEY)
        self.assertIn("model=bulbul:v4-flash", server.paths[0])
        self.assertIn("send_completion_event=true", server.paths[0])
        self.assertEqual(
            server.received[0][0], {"type": "config", "data": EXPECTED_CONFIG_DATA}
        )
        self.assertTrue(ch.connected)

    async def test_set_speaker_resends_config_mid_connection(self):
        # [PROBED] config re-send works mid-connection: speaker switch, no reconnect.
        server, url = await self.start_server()
        ch = self.make_channel(url)
        await ch.set_speaker(SPEAKER_A, "hi-IN", 1.0)
        first = await self.collect(ch.speak("राउंड एक शुरू!", "u1"))
        await ch.set_speaker(SPEAKER_B, "hi-IN", 1.15)
        second = await self.collect(ch.speak("राउंड तीन!", "u2"))

        self.assertEqual(server.conn_count, 1, "speaker switch must NOT reconnect")
        self.assertEqual(first, DEFAULT_CHUNKS)
        self.assertEqual(second, DEFAULT_CHUNKS)
        messages = server.received[0]
        self.assertEqual([m["type"] for m in messages], ["config", "text", "flush", "config", "text", "flush"])
        cfg_a, cfg_b = messages[0]["data"], messages[3]["data"]
        self.assertEqual(cfg_a["speaker"], SPEAKER_A)
        self.assertEqual(cfg_a["language_code"], "hi-IN")
        self.assertEqual(cfg_a["min_buffer_size"], 30)
        self.assertEqual(cfg_a["max_chunk_length"], 200)
        self.assertEqual(cfg_a["speech_sample_rate"], 24000)
        self.assertEqual(cfg_a["output_audio_codec"], "wav")
        self.assertEqual(cfg_a["output_audio_bitrate"], "128k")
        self.assertEqual(cfg_b["speaker"], SPEAKER_B)
        self.assertEqual(cfg_b["pace"], 1.15)
        self.assertEqual([m["data"]["text"] for m in messages if m["type"] == "text"],
                         ["राउंड एक शुरू!", "राउंड तीन!"])

    async def test_speak_sends_text_then_flush_sequence(self):
        server, url = await self.start_server()
        ch = self.make_channel(url)
        await ch.set_speaker(SPEAKER_A, "hi-IN")
        text = "नमस्ते! तैयार हो जाइए।"
        chunks = await self.collect(ch.speak(text, "u1"))
        self.assertEqual(chunks, DEFAULT_CHUNKS)
        messages = server.received[0]
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[0]["type"], "config")
        self.assertEqual(messages[1], {"type": "text", "data": {"text": text}})
        self.assertEqual(messages[2], {"type": "flush"})

    async def test_speak_chunk_shape_passthrough(self):
        # [PROBED] first chunk = 44-byte RIFF header ONLY, rest raw PCM — passed through.
        server, url = await self.start_server(plans=[{"chunks": [H44, PCM1, PCM2]}])
        ch = self.make_channel(url)
        await ch.set_speaker(SPEAKER_A, "hi-IN")
        chunks = await self.collect(ch.speak("chunk me", "u1"))
        self.assertEqual(chunks, [H44, PCM1, PCM2])
        self.assertEqual(len(chunks[0]), 44)
        self.assertTrue(chunks[0].startswith(b"RIFF"))
        self.assertFalse(chunks[1].startswith(b"RIFF"))

    async def test_speak_ignores_non_final_event_frames(self):
        server, url = await self.start_server(
            plans=[{"chunks": [H44, PCM1], "start_event_first": True}]
        )
        ch = self.make_channel(url)
        await ch.set_speaker(SPEAKER_A, "hi-IN")
        chunks = await self.collect(ch.speak("hello", "u1"))
        self.assertEqual(chunks, [H44, PCM1])

    async def test_first_chunk_fused_header_is_split(self):
        # Defensive normalization: oversized first chunk → 44-byte header + PCM remainder.
        fused = H44 + PCM2
        server, url = await self.start_server(plans=[{"chunks": [fused]}])
        ch = self.make_channel(url)
        await ch.set_speaker(SPEAKER_A, "hi-IN")
        chunks = await self.collect(ch.speak("fused", "u1"))
        self.assertEqual(chunks, [H44, PCM2])

    async def test_first_chunk_missing_riff_gets_canonical_header(self):
        # Defensive normalization: no RIFF magic → prepend a canonical 24 kHz header.
        server, url = await self.start_server(plans=[{"chunks": [PCM1]}])
        ch = self.make_channel(url)
        await ch.set_speaker(SPEAKER_A, "hi-IN")
        chunks = await self.collect(ch.speak("no header", "u1"))
        self.assertEqual(len(chunks), 2)
        header, pcm = chunks
        self.assertEqual(pcm, PCM1)
        self.assertEqual(len(header), 44)
        self.assertEqual(header[:4], b"RIFF")
        self.assertEqual(header[8:12], b"WAVE")
        (sample_rate,) = struct.unpack_from("<I", header, 24)
        self.assertEqual(sample_rate, 24000)

    async def test_continuation_chunk_with_fused_header_is_stripped(self):
        server, url = await self.start_server(plans=[{"chunks": [H44, H44_B + PCM2]}])
        ch = self.make_channel(url)
        await ch.set_speaker(SPEAKER_A, "hi-IN")
        chunks = await self.collect(ch.speak("strip", "u1"))
        self.assertEqual(chunks, [H44, PCM2])

    async def test_on_first_chunk_callback_variants(self):
        # TTFB stamp callback: fires exactly once, with the first (header) chunk.
        server, url = await self.start_server()
        ch = self.make_channel(url)
        await ch.set_speaker(SPEAKER_A, "hi-IN")

        stamps_arg: list = []
        stamps_zero: list = []
        stamps_async: list = []
        stamps_builtin: list = []

        async def async_cb(chunk: bytes) -> None:
            stamps_async.append(chunk)

        variants = [
            ("one_arg", lambda c: stamps_arg.append(c), stamps_arg, [H44]),
            ("zero_arg", lambda: stamps_zero.append("hit"), stamps_zero, ["hit"]),
            ("async_one_arg", async_cb, stamps_async, [H44]),
            ("builtin_append", stamps_builtin.append, stamps_builtin, [H44]),
        ]
        for name, cb, sink, expected in variants:
            with self.subTest(variant=name):
                sink.clear()
                chunks = await self.collect(ch.speak("stamp me", "u1", on_first_chunk=cb))
                self.assertEqual(chunks, DEFAULT_CHUNKS)
                self.assertEqual(list(sink), expected)

    async def test_speak_terminates_on_idle_without_final_event(self):
        server, url = await self.start_server(plans=[{"chunks": [H44], "no_final": True}])
        ch = self.make_channel(url, idle_timeout=0.15)
        await ch.set_speaker(SPEAKER_A, "hi-IN")
        t0 = time.monotonic()
        chunks = await self.collect(ch.speak("then silence", "u1"))
        elapsed = time.monotonic() - t0
        self.assertEqual(chunks, [H44])
        self.assertLess(elapsed, 3.0, "idle termination must not hang")

    async def test_ping_keepalive_task_sends_pings(self):
        # [PROBED] app-level pings keep the socket alive; the 20 s task runs by default.
        server, url = await self.start_server()
        ch = self.make_channel(url, ping_interval=0.05)
        await ch.set_speaker(SPEAKER_A, "hi-IN")
        await asyncio.sleep(0.25)
        pings = [m for m in server.received[0] if m.get("type") == "ping"]
        self.assertGreaterEqual(len(pings), 2, "keepalive task must ping periodically")
        self.assertTrue(ch.connected)

    async def test_public_ping_sends_one_frame(self):
        server, url = await self.start_server()
        ch = self.make_channel(url, ping_interval=999.0)  # no periodic drift
        await ch.set_speaker(SPEAKER_A, "hi-IN")
        await asyncio.sleep(0.1)  # let the server settle after set_speaker
        before = len([m for m in server.received[0] if m.get("type") == "ping"])
        self.assertEqual(before, 0)
        await ch.ping()  # public one-shot ping
        await asyncio.sleep(0.1)  # let the same-loop server handler record the frame
        pings = [m for m in server.received[0] if m.get("type") == "ping"]
        self.assertEqual(len(pings), 1)
        self.assertEqual(ch.ping_count, 1)

    async def test_speak_error_frame_raises_ttswserror(self):
        server, url = await self.start_server(
            plans=[{"error_after_flush": True, "error_message": "Invalid speaker"}]
        )
        ch = self.make_channel(url)
        await ch.set_speaker(SPEAKER_A, "hi-IN")
        chunks, exc = await self.collect_and_raise(ch.speak("oops", "u1"), sarvam_tts.TTSWSError)
        self.assertEqual(chunks, [])
        self.assertIn("Invalid speaker", str(exc))

    async def test_speak_requires_speaker_config_and_valid_text(self):
        server, url = await self.start_server()
        ch = self.make_channel(url)
        await ch.connect()
        with self.assertRaises(sarvam_tts.TTSConfigError):
            await self.collect(ch.speak("no speaker yet", "u1"))
        await ch.set_speaker(SPEAKER_A, "hi-IN")
        with self.assertRaises(ValueError):
            await self.collect(ch.speak("   ", "u2"))
        with self.assertRaises(ValueError):
            await self.collect(ch.speak("x" * 2501, "u3"))
        with self.assertRaises(sarvam_tts.TTSConfigError):
            ch2 = sarvam_tts.VoiceChannel(ws_url=url, api_key=TEST_KEY)
            self.addAsyncCleanup(ch2.close)
            await ch2.set_speaker("", "hi-IN")

    async def test_missing_api_key_raises_config_error(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SARVAM_API_KEY", None)
            ch = sarvam_tts.VoiceChannel(api_key=None)  # never contacts the network
            with self.assertRaises(sarvam_tts.TTSConfigError):
                await ch.connect()


class TestVoiceChannelReconnect(VoiceChannelTestBase):
    """Close-code policy: 1006 backoff ladder · 1003 cooldown · 1000 clean · 4xxx fatal.

    Deterministic: connections come from a scripted connector (monkeypatched
    websockets.connect stand-in) that fails/closes with exact codes — real RST
    sockets are inherently racy about un-read buffered frames. One real-socket
    test (below) proves websockets maps a TCP abort to client-observed 1006.
    """

    SCRIPT_URL = (
        "wss://scripted.invalid/text-to-speech/ws"
        "?model=bulbul:v4-flash&send_completion_event=true"
    )

    def make_scripted_channel(self, scripts, **kw):
        connector = ScriptedConnector(*scripts)
        channel = sarvam_tts.VoiceChannel(
            ws_url=self.SCRIPT_URL, api_key=TEST_KEY, connector=connector, **kw
        )
        self.addAsyncCleanup(channel.close)
        return channel, connector

    async def test_reconnect_on_1006(self):
        recorder = SleepRecorder()
        ch, connector = self.make_scripted_channel(
            [
                {"recv_frames": [audio_frame(H44), audio_frame(PCM1)], "recv_close_code": 1006},
                {},  # healthy: default frames
            ],
            sleep=recorder,
        )
        await ch.set_speaker(SPEAKER_A, "hi-IN")
        self.assertEqual(connector.calls, 1)
        self.assertEqual(connector.headers[0]["api-subscription-key"], TEST_KEY)
        self.assertIn("send_completion_event=true", connector.urls[0])
        self.assertIn("model=bulbul:v4-flash", connector.urls[0])

        chunks, exc = await self.collect_and_raise(ch.speak("drop me", "u1"), sarvam_tts.TTSConnectionLost)
        self.assertEqual(chunks, [H44, PCM1], "chunks received before the drop pass through")
        self.assertEqual(exc.close_code, 1006)

        second = await self.collect(ch.speak("second try", "u2"))
        self.assertEqual(second, DEFAULT_CHUNKS)
        self.assertEqual(recorder.calls, [0.5], "1006 → first backoff delay is 0.5 s")
        self.assertEqual(connector.calls, 2)
        self.assertEqual(
            json.loads(connector.conns[1].send_log[0]),
            {"type": "config", "data": dict(EXPECTED_CONFIG_DATA, pace=1.0)},
            "config must be re-sent as the first message after reconnect",
        )
        self.assertTrue(ch.connected)

    async def test_reconnect_1006_backoff_ladder_values(self):
        # 0.5 → 1.0 → 2.0 → 4.0 (capped), max 3 retries: two failures then success.
        recorder = SleepRecorder()
        ch, connector = self.make_scripted_channel(
            [
                {"fail_send_from": 0, "fail_code": 1006},
                {"fail_send_from": 0, "fail_code": 1006},
                {},  # healthy
            ],
            sleep=recorder,
        )
        await ch.set_speaker(SPEAKER_A, "hi-IN")  # first open fails → ladder runs here
        self.assertEqual(recorder.calls, [0.5, 1.0], "0.5 s then 1.0 s backoff")
        self.assertEqual(connector.calls, 3)
        self.assertTrue(ch.connected)
        chunks = await self.collect(ch.speak("after ladder", "u1"))
        self.assertEqual(chunks, DEFAULT_CHUNKS)
        self.assertEqual(connector.calls, 3, "healthy connection is reused after the ladder")

    async def test_reconnect_ladder_exhausts_then_fails_fast_and_rearms(self):
        recorder = SleepRecorder()
        ch, connector = self.make_scripted_channel(
            [{"fail_send_from": 0, "fail_code": 1006}] * 4 + [{}],
            sleep=recorder,
            backoff_s=(0.05, 0.06),
        )
        with self.assertRaises(sarvam_tts.TTSConnectionLost):
            await ch.set_speaker(SPEAKER_A, "hi-IN")
        self.assertEqual(recorder.calls, [0.05, 0.06, 0.06], "3 retries, delays capped at the ladder end")
        self.assertEqual(connector.calls, 4)

        _, exc = await self.collect_and_raise(ch.speak("too soon", "u1"), sarvam_tts.TTSConnectionLost)
        self.assertEqual(exc.close_code, 1006)
        self.assertEqual(recorder.calls, [0.05, 0.06, 0.06], "exhausted → instant raise, no new attempts")
        self.assertEqual(connector.calls, 4)

        await asyncio.sleep(0.08)  # past backoff_s[-1]=0.06 → the ladder re-arms
        chunks = await self.collect(ch.speak("re-armed", "u2"))
        self.assertEqual(chunks, DEFAULT_CHUNKS)
        self.assertEqual(connector.calls, 5)
        self.assertEqual(recorder.calls, [0.05, 0.06, 0.06, 0.05])

    async def test_cooldown_on_1003(self):
        recorder = SleepRecorder()
        ch, connector = self.make_scripted_channel(
            [
                {
                    "recv_frames": [audio_frame(H44), audio_frame(PCM1)],
                    "recv_close_code": 1003,
                    "recv_close_reason": "Rate limit exceeded",
                },
                {},  # healthy
            ],
            sleep=recorder,
            cooldown_1003_s=0.05,
        )
        await ch.set_speaker(SPEAKER_A, "hi-IN")
        chunks, exc = await self.collect_and_raise(ch.speak("throttled", "u1"), sarvam_tts.TTSConnectionLost)
        self.assertEqual(chunks, [H44, PCM1])
        self.assertEqual(exc.close_code, 1003)

        second = await self.collect(ch.speak("after cooldown", "u2"))
        self.assertEqual(second, DEFAULT_CHUNKS)
        self.assertEqual(recorder.calls, [0.05], "one cooldown sleep before the retry")
        self.assertEqual(connector.calls, 2)
        # frozen production defaults (task spec)
        self.assertEqual(sarvam_tts.COOLDOWN_1003_S, 5.0)
        self.assertEqual(sarvam_tts.RECONNECT_BACKOFF_S, (0.5, 1.0, 2.0, 4.0))
        self.assertEqual(sarvam_tts.RECONNECT_MAX_TRIES, 3)
        self.assertEqual(sarvam_tts.IDLE_TIMEOUT_S, 15.0)
        self.assertEqual(sarvam_tts.PING_INTERVAL_S, 20.0)

    async def test_fatal_on_4xxx_never_retries(self):
        recorder = SleepRecorder()
        ch, connector = self.make_scripted_channel(
            [{"fail_send_from": 0, "fail_code": 4401, "fail_reason": "Invalid API key"}],
            sleep=recorder,
        )
        with self.assertRaises(sarvam_tts.TTSFatalError):
            await ch.set_speaker(SPEAKER_A, "hi-IN")
        self.assertEqual(recorder.calls, [], "4xxx auth/quota close → no backoff attempts")
        self.assertEqual(connector.calls, 1)
        with self.assertRaises(sarvam_tts.TTSFatalError):
            await self.collect(ch.speak("still fatal", "u1"))
        with self.assertRaises(sarvam_tts.TTSFatalError):
            await ch.connect()
        self.assertEqual(recorder.calls, [])
        self.assertEqual(connector.calls, 1, "fatal close is never reconnected")
        self.assertIsNotNone(ch.fatal)
        self.assertFalse(ch.connected)

    async def test_clean_close_1000_reopens_without_backoff(self):
        recorder = SleepRecorder()
        ch, connector = self.make_scripted_channel(
            [
                {
                    "recv_frames": [audio_frame(H44), audio_frame(PCM1)],
                    "recv_close_code": 1000,
                    "recv_close_reason": "bye",
                },
                {},  # healthy
            ],
            sleep=recorder,
        )
        await ch.set_speaker(SPEAKER_A, "hi-IN")
        chunks, exc = await self.collect_and_raise(ch.speak("clean drop", "u1"), sarvam_tts.TTSConnectionLost)
        self.assertEqual(chunks, [H44, PCM1])
        self.assertEqual(exc.close_code, 1000)

        second = await self.collect(ch.speak("immediately", "u2"))
        self.assertEqual(second, DEFAULT_CHUNKS)
        self.assertEqual(recorder.calls, [], "1000/1001 clean close → no backoff sleep")
        self.assertEqual(connector.calls, 2)

    async def test_real_rst_abort_maps_to_client_1006(self):
        # Real-socket sanity: a TCP abort (no close frame) is classified 1006 by
        # websockets and by the channel; the channel then recovers via the ladder.
        # NOTE: an RST may discard un-read buffered frames, so no exact chunk count.
        server, url = await self.start_server(
            plans=[
                {"chunks": [H44, PCM1], "no_final": True, "abort_after_flush": True},
                {},  # default serve
            ]
        )
        recorder = SleepRecorder()
        ch = self.make_channel(url, sleep=recorder)
        await ch.set_speaker(SPEAKER_A, "hi-IN")
        chunks, exc = await self.collect_and_raise(ch.speak("rst drop", "u1"), sarvam_tts.TTSConnectionLost)
        self.assertEqual(exc.close_code, 1006, "RST abort must be classified abnormal (1006)")
        if chunks:
            self.assertEqual(chunks[0], H44, "the header chunk passes through before the drop")
        second = await self.collect(ch.speak("recover", "u2"))
        self.assertEqual(second, DEFAULT_CHUNKS)
        self.assertEqual(server.conn_count, 2)
        self.assertEqual(recorder.calls, [0.5])

    async def test_close_is_idempotent_and_stops_keepalive(self):
        server, url = await self.start_server()
        ch = self.make_channel(url, ping_interval=0.05)
        await ch.set_speaker(SPEAKER_A, "hi-IN")
        await asyncio.sleep(0.15)

        async def ping_count() -> int:
            return len([m for m in server.received[0] if m.get("type") == "ping"])

        self.assertGreaterEqual(await ping_count(), 1)
        await ch.close()
        await ch.close()  # idempotent
        self.assertFalse(ch.connected)
        before = await ping_count()
        await asyncio.sleep(0.15)
        self.assertEqual(await ping_count(), before, "close() must stop the ping keepalive task")
        with self.assertRaises((sarvam_tts.TTSConnectionLost, sarvam_tts.TTSError)):
            await ch.ping()

class TestWavBlobToChunks(unittest.TestCase):
    """wav_blob_to_chunks: cached blob → (44-byte header chunk, PCM chunk list)."""

    PCM = b"\x7f" * 70000  # 70000 bytes → 32768+32768+4464

    def _blob(self, pcm: bytes = PCM) -> bytes:
        return sarvam_tts.canonical_wav_header(24000, 1, 16, len(pcm)) + pcm

    def test_basic_chunking_matches_live_shape(self):
        pcm = self.PCM
        header, chunks = sarvam_tts.wav_blob_to_chunks(self._blob(pcm), chunk_pcm_bytes=32768)
        self.assertEqual(header, self._blob(pcm)[:44], "header chunk is exactly the 44-byte RIFF header")
        self.assertEqual([len(c) for c in chunks], [32768, 32768, 4464])
        self.assertEqual(b"".join(chunks), pcm)
        self.assertEqual(header[:4], b"RIFF")
        self.assertEqual(header[8:12], b"WAVE")
        (riff_size,) = struct.unpack_from("<I", header, 4)
        (data_size,) = struct.unpack_from("<I", header, 40)
        self.assertEqual(riff_size, 36 + len(pcm))
        self.assertEqual(data_size, len(pcm))
        (fmt_size, audio_format, channels, sample_rate, byte_rate, block_align, bits) = struct.unpack_from(
            "<IHHIIHH", header, 16
        )
        self.assertEqual((fmt_size, audio_format, channels, sample_rate, byte_rate, block_align, bits),
                         (16, 1, 1, 24000, 48000, 2, 16))

    def test_small_blob_single_chunk_and_empty_data(self):
        header, chunks = sarvam_tts.wav_blob_to_chunks(self._blob(b"\x00\x01"), chunk_pcm_bytes=32768)
        self.assertEqual(chunks, [b"\x00\x01"])
        header, chunks = sarvam_tts.wav_blob_to_chunks(self._blob(b""), chunk_pcm_bytes=32768)
        self.assertEqual(chunks, [])
        self.assertEqual(header, sarvam_tts.canonical_wav_header(24000, 1, 16, 0))

    def test_exact_multiple_boundary(self):
        pcm = b"\x77" * 65536
        _, chunks = sarvam_tts.wav_blob_to_chunks(self._blob(pcm), chunk_pcm_bytes=32768)
        self.assertEqual([len(c) for c in chunks], [32768, 32768])

    def test_non_canonical_blob_is_rebuilt_as_44_byte_header(self):
        # extra LIST chunk between RIFF and fmt → header must still come out canonical 44.
        pcm = b"\x10\x20" * 5000
        list_chunk = b"LIST" + struct.pack("<I", 4) + b"INFO"
        header, chunks = sarvam_tts.wav_blob_to_chunks(
            sarvam_tts.canonical_wav_header(24000, 1, 16, len(pcm)) + pcm + list_chunk,
            chunk_pcm_bytes=32768,
        )
        self.assertEqual(len(header), 44)
        self.assertEqual(b"".join(chunks), pcm)

    def test_invalid_inputs_raise(self):
        with self.assertRaises(ValueError):
            sarvam_tts.wav_blob_to_chunks(b"NOPE" + b"x" * 100)
        with self.assertRaises(ValueError):
            sarvam_tts.wav_blob_to_chunks(b"RIFF" + b"x" * 20)
        riff_wave_only = b"RIFF" + struct.pack("<I", 4) + b"WAVE"
        with self.assertRaises(ValueError):
            sarvam_tts.wav_blob_to_chunks(riff_wave_only + b"\x00" * 60)
        with self.assertRaises(ValueError):
            sarvam_tts.wav_blob_to_chunks(self._blob(), chunk_pcm_bytes=0)
        with self.assertRaises(ValueError):
            sarvam_tts.wav_blob_to_chunks(self._blob(), chunk_pcm_bytes=-8)


class TestRestAndStreamFallbacks(unittest.IsolatedAsyncioTestCase):
    """speak_via_stream / speak_via_rest via httpx.MockTransport."""

    REST_URL = "https://api.sarvam.ai/text-to-speech"
    STREAM_URL = "https://api.sarvam.ai/text-to-speech/stream"
    WAV_PAYLOAD = b"RIFF-and-actual-wav-bytes-\x00\x01" * 40

    def mock_client(self, handler):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        return client

    async def test_speak_via_rest_decodes_audios0(self):
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(
                {
                    "url": str(request.url),
                    "method": request.method,
                    "headers": dict(request.headers),
                    "body": json.loads(request.content.decode()),
                }
            )
            return httpx.Response(
                200,
                json={
                    "request_id": "20261013_test",
                    "audios": [
                        base64.b64encode(self.WAV_PAYLOAD).decode(),
                        base64.b64encode(b"second-input-text-audio").decode(),
                    ],
                },
            )

        result = await sarvam_tts.speak_via_rest(
            "जवाब दें!", SPEAKER_A, "hi-IN", pace=1.05,
            api_key=TEST_KEY, client=self.mock_client(handler),
        )
        self.assertEqual(result, self.WAV_PAYLOAD, "audios[0] must be base64-decoded to raw WAV bytes")
        self.assertEqual(len(captured), 1)
        req = captured[0]
        self.assertEqual(req["url"], self.REST_URL)
        self.assertEqual(req["method"], "POST")
        self.assertEqual(req["headers"]["api-subscription-key"], TEST_KEY)
        body = req["body"]
        self.assertEqual(body["text"], "जवाब दें!")
        self.assertEqual(body["model"], "bulbul:v4-flash", "model must be explicit everywhere")
        self.assertEqual(body["speaker"], SPEAKER_A)
        self.assertEqual(body["language_code"], "hi-IN")
        self.assertEqual(body["pace"], 1.05)
        self.assertEqual(body["speech_sample_rate"], 24000)
        self.assertEqual(body["output_audio_codec"], "wav")
        self.assertEqual(body["output_audio_bitrate"], "128k")
        self.assertIs(body["enable_preprocessing"], True)

    async def test_speak_via_rest_http_error_status_checked_before_body(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403, json={"error": {"code": "invalid_api_key_error", "message": "Invalid API key"}}
            )

        with self.assertRaises(sarvam_tts.TTSHTTPError) as cm:
            await sarvam_tts.speak_via_rest(
                "x", SPEAKER_A, "hi-IN", api_key=TEST_KEY, client=self.mock_client(handler)
            )
        self.assertEqual(cm.exception.status_code, 403)
        self.assertIn("invalid_api_key_error", str(cm.exception))

    async def test_speak_via_rest_missing_or_bad_audios(self):
        def missing(request):
            return httpx.Response(200, json={"request_id": "r"})

        def not_b64(request):
            return httpx.Response(200, json={"audios": ["!!!not-base64!!!"]})

        with self.assertRaises(sarvam_tts.TTSHTTPError):
            await sarvam_tts.speak_via_rest(
                "x", SPEAKER_A, "hi-IN", api_key=TEST_KEY, client=self.mock_client(missing)
            )
        with self.assertRaises(sarvam_tts.TTSHTTPError):
            await sarvam_tts.speak_via_rest(
                "x", SPEAKER_A, "hi-IN", api_key=TEST_KEY, client=self.mock_client(not_b64)
            )

    async def test_speak_via_stream_returns_raw_binary_and_sets_wav_explicitly(self):
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(
                {
                    "url": str(request.url),
                    "headers": dict(request.headers),
                    "body": json.loads(request.content.decode()),
                }
            )
            return httpx.Response(200, content=self.WAV_PAYLOAD)  # raw binary, not JSON

        result = await sarvam_tts.speak_via_stream(
            "स्ट्रीम करो", SPEAKER_B, "hi-IN", pace=1.1,
            api_key=TEST_KEY, client=self.mock_client(handler),
        )
        self.assertEqual(result, self.WAV_PAYLOAD)
        self.assertEqual(len(captured), 1)
        req = captured[0]
        self.assertEqual(req["url"], self.STREAM_URL)
        self.assertEqual(req["headers"]["api-subscription-key"], TEST_KEY)
        self.assertEqual(req["body"]["output_audio_codec"], "wav",
                         "stream defaults to mp3 — wav must be set explicitly [PROBED]")
        self.assertEqual(req["body"]["model"], "bulbul:v4-flash")
        self.assertEqual(req["body"]["speech_sample_rate"], 24000)
        self.assertIs(req["body"]["enable_preprocessing"], True)
        self.assertEqual(req["body"]["pace"], 1.1)

    async def test_speak_via_stream_http_error_status_checked_before_body(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                422, json={"error": {"code": "unprocessable_entity_error",
                                      "message": "Text exceeds maximum length of 3500 characters"}}
            )

        with self.assertRaises(sarvam_tts.TTSHTTPError) as cm:
            await sarvam_tts.speak_via_stream(
                "x", SPEAKER_A, "hi-IN", api_key=TEST_KEY, client=self.mock_client(handler)
            )
        self.assertEqual(cm.exception.status_code, 422)
        self.assertIn("exceeds maximum length", str(cm.exception))

    async def test_fallbacks_require_api_key(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SARVAM_API_KEY", None)
            with self.assertRaises(sarvam_tts.TTSConfigError):
                await sarvam_tts.speak_via_rest("x", SPEAKER_A, "hi-IN", api_key=None,
                                                 client=self.mock_client(lambda r: httpx.Response(200)))
            with self.assertRaises(sarvam_tts.TTSConfigError):
                await sarvam_tts.speak_via_stream("x", SPEAKER_A, "hi-IN", api_key=None,
                                                  client=self.mock_client(lambda r: httpx.Response(200)))

    async def test_fallback_text_validation(self):
        client = self.mock_client(lambda r: httpx.Response(200, json={"audios": [""]}))
        with self.assertRaises(ValueError):
            await sarvam_tts.speak_via_rest(" ", SPEAKER_A, "hi-IN", api_key=TEST_KEY, client=client)
        with self.assertRaises(ValueError):
            await sarvam_tts.speak_via_rest("x" * 2501, SPEAKER_A, "hi-IN", api_key=TEST_KEY, client=client)
        with self.assertRaises(ValueError):
            await sarvam_tts.speak_via_stream("x" * 3501, SPEAKER_A, "hi-IN", api_key=TEST_KEY, client=client)


class TestModuleConstants(unittest.TestCase):
    def test_frozen_constants(self):
        self.assertEqual(sarvam_tts.TTS_MODEL, "bulbul:v4-flash")
        self.assertEqual(
            sarvam_tts.TTS_WS_URL,
            "wss://api.sarvam.ai/text-to-speech/ws"
            "?model=bulbul:v4-flash&send_completion_event=true",
        )
        self.assertEqual(sarvam_tts.BASE_URL, "https://api.sarvam.ai")
        self.assertEqual(sarvam_tts.SAMPLE_RATE, 24000)
        self.assertEqual(sarvam_tts.AUDIO_BITRATE, "128k")
        self.assertEqual(sarvam_tts.MIN_BUFFER_SIZE, 30)
        self.assertEqual(sarvam_tts.MAX_CHUNK_LENGTH, 200)
        self.assertEqual(sarvam_tts.RECONNECT_BACKOFF_S, (0.5, 1.0, 2.0, 4.0))

    def test_build_config_shape(self):
        cfg = sarvam_tts.build_config(SPEAKER_A, "hi-IN", 1.0, dict_id="p_8eea3524")
        self.assertEqual(cfg["type"], "config")
        self.assertEqual(cfg["data"]["dict_id"], "p_8eea3524")
        cfg2 = sarvam_tts.build_config(SPEAKER_A, "en-IN", 1.0)
        self.assertNotIn("dict_id", cfg2["data"])
        self.assertEqual(cfg2["data"]["pace"], 1.0)


if __name__ == "__main__":
    unittest.main()
