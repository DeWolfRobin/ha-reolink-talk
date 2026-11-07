"""Isolated timing experiments, block semantics and real library ACK routing."""

import asyncio
import ast
from functools import partial
from pathlib import Path
from dataclasses import replace
import json
import struct
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch
from test_live_adpcm import talk
from test_talk_binary import FakeBaichuan

ABILITY = talk.TalkAbility(
    "FDX", "mixAudioStream", "adpcm", None, 16000, 16, 1024, "mono"
)


class DimensionTests(TestCase):
    def test_negotiated_dimensions_and_framing(self):
        for length, physical, samples in (
            (512, 260, 513),
            (1024, 516, 1025),
            (2048, 1028, 2049),
        ):
            self.assertEqual(
                talk.talk_block_size(replace(ABILITY, length_per_encoder=length)),
                physical,
            )
            self.assertEqual((physical - 4) * 2 + 1, samples)
            block = bytes(physical)
            frame = talk.bcmedia_adpcm_packet(block)
            # Neolink ser.rs: magic, duplicate payload+4 lengths, data magic,
            # half payload bytes; physical block followed by 8-byte alignment.
            self.assertEqual(
                struct.unpack("<IHHHH", frame[:12]),
                (0x62773130, physical + 4, physical + 4, 0x100, (physical - 4) // 2),
            )
            self.assertEqual(frame[12 : 12 + physical], block)
            self.assertEqual(len(frame), 12 + physical + (-physical) % 8)
        with self.assertRaises(ValueError):
            talk.talk_binary_payload(bytes(515), 516)
        with self.assertRaises(ValueError):
            talk.talk_block_size(replace(ABILITY, length_per_encoder=1023))

    def test_option_defaults_persistence_and_bounds(self):
        self.assertEqual(talk.PlaybackOptions.from_options({}), talk.PlaybackOptions())
        options = {
            "diagnostic_blocks_per_packet": 1,
            "diagnostic_wait_for_ack": False,
            "diagnostic_initial_buffer_ms": 100,
        }
        self.assertEqual(
            talk.PlaybackOptions.from_options(options),
            talk.PlaybackOptions.from_options(json.loads(json.dumps(options))),
        )
        for kwargs in (
            {"blocks_per_packet": 0},
            {"initial_buffer_ms": 501},
            {"final_drain_ms": 2001},
            {"pacing_factor": float("nan")},
            {"attenuation_db": 1},
            {"pacing_mode": "burst"},
        ):
            with self.assertRaises(ValueError):
                talk.PlaybackOptions(**kwargs)

    def test_peak_and_rail_measurement(self):
        levels = talk.pcm_levels(struct.pack("<5h", 0, 32767, -32768, 16384, -16384))
        self.assertEqual(levels["peak"], 32768)
        self.assertEqual(levels["rail_samples"], 2)
        self.assertEqual(levels["peak_dbfs"], 0)
        self.assertAlmostEqual(
            talk.pcm_levels(struct.pack("<h", 16384))["peak_dbfs"], -6.0206, places=4
        )
        self.assertEqual(talk.pcm_levels(bytes(100))["rail_samples"], 0)
        with self.assertRaises(ValueError):
            talk.pcm_levels(b"x")


class ExperimentTests(IsolatedAsyncioTestCase):
    async def simulate(self, options=None, delays=None, blocks=12):
        options = options or talk.PlaybackOptions()
        now, sent, sleeps = [0.0], [], []
        delays = delays or []

        async def send(*args, **kwargs):
            sent.append(now[0])
            if len(sent) <= len(delays):
                now[0] += delays[len(sent) - 1]

        async def sleep(delay):
            self.assertGreaterEqual(delay, 0)
            sleeps.append(delay)
            now[0] += delay

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
            await talk.talk_playback(
                bc, 1, bytes(516 * blocks), ABILITY, options=options
            )
        return now[0], sent, sleeps, bc

    async def test_default_deadline_and_short_tail_packet(self):
        duration, sent, _, bc = await self.simulate(blocks=10)
        self.assertEqual(len(sent), 3)
        self.assertAlmostEqual(duration, 1025 * 10 / 16000)
        self.assertEqual(bc.send.call_args.kwargs["cmd_id"], 11)

    async def test_batch_size_only_changes_delivery_granularity(self):
        duration, sent, _, _ = await self.simulate(
            talk.PlaybackOptions(blocks_per_packet=1)
        )
        self.assertEqual(len(sent), 12)
        self.assertAlmostEqual(sent[1], 1025 / 16000)
        self.assertAlmostEqual(duration, 12 * 1025 / 16000)

    async def test_ack_stall_can_create_catchup_burst(self):
        _, sent, _, _ = await self.simulate(delays=[0.6])
        self.assertEqual(sent, [0, 0.6, 0.6])
        # This is host scheduling evidence, not proof of NVR buffer occupancy.

    async def test_initial_send_ahead_and_drain_are_isolated(self):
        base, baseline, _, _ = await self.simulate()
        duration, sent, _, _ = await self.simulate(
            talk.PlaybackOptions(initial_buffer_ms=100)
        )
        self.assertAlmostEqual(sent[1], baseline[1] - 0.1)
        self.assertAlmostEqual(duration, base)
        duration, sent, _, _ = await self.simulate(
            talk.PlaybackOptions(final_drain_ms=250)
        )
        self.assertEqual(sent, baseline)
        self.assertAlmostEqual(duration, base + 0.25)

    async def test_pacing_factor_and_interval_are_independent(self):
        duration, sent, _, _ = await self.simulate(
            talk.PlaybackOptions(pacing_factor=0.95)
        )
        self.assertAlmostEqual(sent[1], 4 * 1025 / 16000 * 0.95)
        self.assertAlmostEqual(duration, 12 * 1025 / 16000)  # queued audio is drained
        _, sent, _, _ = await self.simulate(
            talk.PlaybackOptions(pacing_mode="interval"), [0.05] * 3
        )
        self.assertAlmostEqual(sent[1], 0.05 + 4 * 1025 / 16000)

    async def test_interval_send_ahead_is_spent_only_once(self):
        duration, sent, _, _ = await self.simulate(
            talk.PlaybackOptions(pacing_mode="interval", initial_buffer_ms=100)
        )
        self.assertAlmostEqual(sent[1], 4 * 1025 / 16000 - 0.1)
        self.assertAlmostEqual(sent[2] - sent[1], 4 * 1025 / 16000)
        self.assertAlmostEqual(duration, 12 * 1025 / 16000)

    async def test_logs_are_bounded(self):
        with self.assertLogs(talk._LOGGER, level="DEBUG") as captured:
            await self.simulate(talk.PlaybackOptions(logging=True), blocks=4004)
        self.assertLessEqual(
            sum("Talk packet " in line for line in captured.output), 20
        )
        self.assertEqual(sum("Talk summary " in line for line in captured.output), 1)

    async def test_alignment_failure_precedes_device_commands(self):
        bc = SimpleNamespace(send=AsyncMock())
        with self.assertRaises(ValueError):
            await talk.talk_playback(bc, 1, bytes(516), ABILITY, block_align=1024)
        bc.send.assert_not_awaited()


class InstalledLibraryTests(IsolatedAsyncioTestCase):
    def connection(self, status=200, reply=True):
        from reolink_aio.baichuan.tcp_protocol import (
            BaichuanTcpConnection,
            BaichuanTcpClientProtocol,
        )

        loop = asyncio.get_running_loop()
        bc = FakeBaichuan()
        connection = BaichuanTcpConnection("test.invalid", 9000)
        protocol = BaichuanTcpClientProtocol(loop, "test.invalid")

        def write(packet):
            if reply:
                # Echo matching command/channel/message ID, modern 24-byte ACK.
                response = (
                    packet[:8]
                    + bytes(4)
                    + packet[12:16]
                    + status.to_bytes(2, "little")
                    + bytes.fromhex("1464")
                    + bytes(4)
                )
                loop.call_soon(protocol.data_received, response)

        connection._transport = SimpleNamespace(write=write)
        connection._protocol = protocol
        bc._connection = connection
        return bc, connection

    async def test_real_library_accepts_ack_and_propagates_rejection(self):
        from reolink_aio.exceptions import ApiError

        for status in (200, 422):
            bc, connection = self.connection(status)
            if status == 200:
                await talk.send_talk_binary(bc, 4, b"audio")
            else:
                with self.assertRaises(ApiError):
                    await talk.send_talk_binary(bc, 4, b"audio")
            self.assertFalse(connection.receive_futures)

    async def test_missing_ack_cancellation_cleans_future(self):
        bc, connection = self.connection(reply=False)
        task = asyncio.create_task(talk.send_talk_binary(bc, 1, b"audio"))
        await asyncio.sleep(0)
        self.assertTrue(connection.receive_futures)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(connection.receive_futures)

    async def test_real_library_timeout_cleans_future(self):
        from reolink_aio.exceptions import ReolinkTimeoutError
        from reolink_aio.baichuan import base_protocol

        bc, connection = self.connection(reply=False)
        with patch.object(base_protocol, "TIMEOUT", 0.01):
            with self.assertRaises(ReolinkTimeoutError):
                await talk.send_talk_binary(bc, 1, b"audio")
        self.assertFalse(connection.receive_futures)

    async def test_no_ack_experiment_uses_library_write_path(self):
        bc, connection = self.connection(reply=False)
        await talk.send_talk_binary(bc, 1, b"audio", wait_for_ack=False)
        self.assertFalse(connection.receive_futures)


class PlaybackWiringTests(IsolatedAsyncioTestCase):
    async def test_options_reach_transport_and_gain_without_volume_command(self):
        tree = ast.parse(
            Path("custom_components/reolink_talk/media_player.py").read_text()
        )
        cls = next(
            n
            for n in tree.body
            if isinstance(n, ast.ClassDef) and n.name == "ReolinkTalkPlayer"
        )
        method = next(
            n
            for n in cls.body
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_play_bytes"
        )
        ffmpeg, playback = AsyncMock(return_value=bytes(4)), AsyncMock()
        ns = {
            "asyncio": asyncio,
            "partial": partial,
            "talk_block_size": talk.talk_block_size,
            "ffmpeg_to_pcm_s16le": ffmpeg,
            "ima_adpcm_encode_dvi_blocks": lambda *a, **kw: bytes(516),
            "talk_playback": playback,
            "_LOGGER": SimpleNamespace(debug=lambda *a: None),
        }
        exec(
            compile(ast.Module(body=[method], type_ignores=[]), "playback", "exec"), ns
        )
        options = talk.PlaybackOptions(blocks_per_packet=1, attenuation_db=-6)
        target = SimpleNamespace(
            host="test.invalid",
            username="test",
            password="unused",
            http_port=443,
            use_https=True,
            port=9000,
            verify_ssl=True,
            channel=4,
            playback_options=options,
        )

        async def executor(func, *args):
            return func(*args)

        obj = SimpleNamespace(
            _target=target,
            _attr_volume_level=0.5,
            _probe_ability=AsyncMock(return_value=ABILITY),
            hass=SimpleNamespace(async_add_executor_job=executor),
        )
        host = SimpleNamespace(
            baichuan=SimpleNamespace(login=AsyncMock()), logout=AsyncMock()
        )
        with patch("reolink_aio.api.Host", return_value=host):
            await ns["_play_bytes"](obj, b"media")
        self.assertAlmostEqual(ffmpeg.call_args.kwargs["volume"], 0.5 * 10 ** (-6 / 20))
        self.assertEqual(playback.call_args.kwargs["block_align"], 516)
        self.assertEqual(playback.call_args.kwargs["options"], options)
        host.logout.assert_awaited_once()


class OptionsFlowTests(IsolatedAsyncioTestCase):
    async def test_diagnostic_form_defaults_and_saved_values(self):
        import voluptuous as vol

        tree = ast.parse(
            Path("custom_components/reolink_talk/config_flow.py").read_text()
        )
        cls = next(
            n
            for n in tree.body
            if isinstance(n, ast.ClassDef) and n.name == "ReolinkTalkOptionsFlowHandler"
        )
        cls.bases = []
        ns = {
            "vol": vol,
            "PlaybackOptions": talk.PlaybackOptions,
            "config_entries": SimpleNamespace(ConfigEntry=object),
            "CONF_REOLINK_ENTRY_IDS": "reolink_entry_ids",
            "CONF_VERIFY_SSL": "verify_ssl",
            "CONF_CHANNEL": "channel",
            "DEFAULT_CHANNEL": 0,
            "selector": SimpleNamespace(
                SelectSelector=lambda config: lambda value: value,
                SelectSelectorConfig=dict,
                SelectOptionDict=dict,
                SelectSelectorMode=SimpleNamespace(DROPDOWN="dropdown"),
            ),
        }
        exec(compile(ast.Module(body=[cls], type_ignores=[]), "options", "exec"), ns)
        entry = SimpleNamespace(
            options={"verify_ssl": False, "diagnostic_blocks_per_packet": 1}
        )
        flow = ns[cls.name](entry)
        flow.hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_entries=lambda domain: [])
        )
        flow.async_show_form = lambda **kw: kw
        flow.async_create_entry = lambda **kw: kw
        form = await flow.async_step_init()
        saved = form["data_schema"]({})
        self.assertFalse(saved["verify_ssl"])
        self.assertEqual(saved["diagnostic_blocks_per_packet"], 1)
        self.assertEqual(saved["diagnostic_pacing_factor"], 1)
        result = await flow.async_step_init(saved)
        self.assertEqual(result["data"], saved)
        form = await flow.async_step_init({"diagnostic_pacing_factor": float("nan")})
        self.assertEqual(form["errors"]["base"], "invalid_diagnostics")
