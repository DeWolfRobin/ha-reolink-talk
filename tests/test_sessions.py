"""Channel isolation, rejection propagation, cancellation, and player queues."""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch
from test_live_adpcm import talk
from test_talk_binary import FakeBaichuan


class SessionTests(IsolatedAsyncioTestCase):
    async def test_independent_channels_and_cancelled_reservation(self):
        hass = SimpleNamespace(data={})
        entered = asyncio.Event()

        async def hold():
            with talk.claim_talk_session(hass, "nvr", 1):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(hold())
        await entered.wait()
        with talk.claim_talk_session(hass, "nvr", 4):
            with self.assertRaises(RuntimeError):
                with talk.claim_talk_session(hass, "nvr", 1):
                    pass
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with talk.claim_talk_session(hass, "nvr", 1):
            pass
        self.assertFalse(hass.data["reolink_talk"]["active_talk"])

    async def test_connection_sender_rejection_is_not_success(self):
        from reolink_aio.exceptions import ApiError

        bc = FakeBaichuan()
        bc._connection.send = AsyncMock(side_effect=ApiError("busy", rspCode=422))
        with self.assertRaises(ApiError):
            await talk.send_talk_binary(bc, 4, b"data")
        self.assertFalse(bc.written)
        self.assertEqual(bc._connection.send.call_args.args[3], 4)

    async def test_legacy_response_uses_packet_id_after_connect_yields(self):
        bc = FakeBaichuan()
        bc._loop = asyncio.get_running_loop()
        bc._protocol = SimpleNamespace(receive_futures={})

        async def connect():
            bc._mess_id += 1  # another command allocated an ID while connecting

        bc._connect_if_needed = connect

        def write(packet):
            message_id = int.from_bytes(packet[12:16], "little")
            bc._protocol.receive_futures[202][message_id].set_result(None)

        bc._connection._transport.write = write
        await talk.send_talk_binary(bc, 4, b"data")
        self.assertFalse(bc._protocol.receive_futures)

    async def test_busy_config_does_not_stop_another_stream(self):
        from reolink_aio.exceptions import ApiError

        bc = SimpleNamespace(send=AsyncMock(side_effect=ApiError("busy", rspCode=422)))
        ability = talk.TalkAbility(
            "FDX", "mixAudioStream", "adpcm", None, 16000, 16, 512, "mono"
        )
        with self.assertRaisesRegex(RuntimeError, "sequentially"):
            await talk.talk_playback(bc, 4, bytes(260), ability)
        self.assertTrue(all(c.kwargs["cmd_id"] == 201 for c in bc.send.call_args_list))

    async def test_cancelled_stream_stops_its_channel_then_can_repeat(self):
        entered = asyncio.Event()

        async def send(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        bc = SimpleNamespace(send=AsyncMock())
        ability = talk.TalkAbility(
            "FDX", "mixAudioStream", "adpcm", None, 16000, 16, 512, "mono"
        )
        with patch.object(talk, "send_talk_binary", side_effect=send):
            task = asyncio.create_task(talk.talk_playback(bc, 4, bytes(260), ability))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(bc.send.call_args.kwargs["cmd_id"], 11)
        self.assertEqual(bc.send.call_args.kwargs["channel"], 4)
        with patch.object(talk, "send_talk_binary", new=AsyncMock()):
            await talk.talk_playback(bc, 4, bytes(260), ability)

    async def test_players_queue_same_channel_and_isolate_failure(self):
        tree = ast.parse(
            Path("custom_components/reolink_talk/media_player.py").read_text()
        )
        cls = next(
            n
            for n in tree.body
            if isinstance(n, ast.ClassDef) and n.name == "ReolinkTalkPlayer"
        )
        cls.bases = []
        cls.body = [
            n
            for n in cls.body
            if isinstance(n, ast.AsyncFunctionDef)
            and n.name in ("async_play_media", "_async_play_media")
        ]
        ns = dict(
            asyncio=asyncio,
            claim_talk_session=talk.claim_talk_session,
            MediaPlayerState=SimpleNamespace(PLAYING="playing", IDLE="idle"),
            _LOGGER=Mock(),
        )
        exec(compile(ast.Module(body=[cls], type_ignores=[]), "player", "exec"), ns)
        hass = SimpleNamespace(data={})
        active = set()
        entered = asyncio.Event()
        release = asyncio.Event()

        def player(channel):
            obj = ns[cls.name]()
            obj.hass, obj._reolink_entry_id = hass, "nvr"
            obj._target = SimpleNamespace(channel=channel)
            obj._lock = asyncio.Lock()
            obj._playback_tasks = set()
            obj.entity_id = str(channel)
            obj.async_write_ha_state = Mock()
            obj._resolve_media_bytes = AsyncMock(return_value=b"audio")

            async def play(data):
                self.assertNotIn(channel, active)
                active.add(channel)
                if len(active) == 2:
                    entered.set()
                try:
                    await release.wait()
                    if channel == 1:
                        raise RuntimeError("device failed")
                finally:
                    active.remove(channel)

            obj._play_bytes = play
            return obj

        a, b = player(1), player(4)
        tasks = [
            asyncio.create_task(p.async_play_media("audio/wav", "local"))
            for p in (a, a, b)
        ]
        await asyncio.wait_for(entered.wait(), 2)
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertIsInstance(results[0], RuntimeError)
        self.assertIsInstance(results[1], RuntimeError)
        self.assertIsNone(results[2])
        self.assertEqual(a._attr_state, "idle")
        self.assertEqual(b._attr_state, "idle")
        self.assertFalse(active)
