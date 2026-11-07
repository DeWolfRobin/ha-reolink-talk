"""Deterministic waveform and fake-clock timing regressions."""

import math
import struct
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch
from test_live_adpcm import talk, decode, sine, snr_db


class WaveformTests(TestCase):
    def test_synthetic_signals(self):
        count = 16001
        signals = {
            "silence": [0] * count,
            "sine": sine(count),
            "transient": [12000 if i == 8000 else 0 for i in range(count)],
            "speech_like": [
                int(5000 * math.sin(i * 0.06) * (1 + math.sin(i * 0.001)))
                for i in range(count)
            ],
            "doorbell": [
                int(
                    9000
                    * math.exp(-i / 8000)
                    * (math.sin(i * 0.16) + 0.3 * math.sin(i * 0.21))
                )
                for i in range(count)
            ],
        }
        for name, samples in signals.items():
            with self.subTest(name=name):
                data = talk.ima_adpcm_encode_dvi_blocks(
                    struct.pack(f"<{count}h", *samples), full_block_size=260
                )
                blocks = [data[i : i + 260] for i in range(0, len(data), 260)]
                output = decode(blocks)[:count]
                self.assertEqual(len(output), count)
                if name == "silence":
                    self.assertEqual(set(output), {0})
                elif name != "transient":
                    self.assertGreater(snr_db(samples, output), 15)
                self.assertEqual(
                    len(talk.talk_binary_payload(data, 260)), math.ceil(len(blocks) / 4)
                )


class PacingTests(IsolatedAsyncioTestCase):
    async def test_ack_latency_is_inside_playback_budget(self):
        now = [0.0]

        async def send(*args, **kwargs):
            now[0] += 0.05

        async def sleep(delay):
            now[0] += delay

        ability = talk.TalkAbility(
            "FDX", "mixAudioStream", "adpcm", None, 16000, 16, 512, "mono"
        )
        bc = SimpleNamespace(send=AsyncMock())
        with (
            patch.object(talk, "send_talk_binary", side_effect=send),
            patch.object(talk.asyncio, "sleep", side_effect=sleep),
            patch.object(
                talk.asyncio,
                "get_running_loop",
                return_value=SimpleNamespace(time=lambda: now[0]),
            ),
        ):
            await talk.talk_playback(bc, 1, bytes(260 * 40), ability, block_align=260)
        self.assertAlmostEqual(now[0], 40 * 513 / 16000)
        self.assertEqual(bc.send.call_args.kwargs["cmd_id"], 11)
