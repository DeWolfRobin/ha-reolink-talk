"""Optional real FFmpeg and local self-signed TLS checks (no camera needed)."""

import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
import io
from pathlib import Path
import shutil
import ssl
import struct
import tempfile
import unittest
import wave
from unittest.mock import patch
from test_live_adpcm import talk, sine, snr_db


class FFmpegTests(unittest.IsolatedAsyncioTestCase):
    async def test_adpcm_against_independent_ffmpeg_decoder(self):
        binary = shutil.which("ffmpeg")
        if not binary and importlib.util.find_spec("imageio_ffmpeg"):
            import imageio_ffmpeg

            binary = imageio_ffmpeg.get_ffmpeg_exe()
        if not binary:
            self.skipTest("install FFmpeg or imageio-ffmpeg")
        block_size = 516
        samples_per_block = 1025
        samples = sine(samples_per_block * 80)
        encoded = talk.ima_adpcm_encode_dvi_blocks(
            struct.pack(f"<{len(samples)}h", *samples), full_block_size=block_size
        )
        # Wrap the identical mono IMA block layout in WAV for an independent
        # decoder, rather than trusting our own predictor/header interpretation.
        fmt = struct.pack(
            "<HHIIHHHH",
            17,
            1,
            16000,
            16000 * block_size // samples_per_block,
            block_size,
            4,
            2,
            samples_per_block,
        )
        body = (
            b"WAVEfmt "
            + struct.pack("<I", len(fmt))
            + fmt
            + b"data"
            + struct.pack("<I", len(encoded))
            + encoded
        )
        wav = b"RIFF" + struct.pack("<I", len(body)) + body
        with patch.object(talk.shutil, "which", return_value=binary):
            decoded = await talk.ffmpeg_to_pcm_s16le(wav, sample_rate=16000)
        output = struct.unpack(f"<{len(decoded) // 2}h", decoded)
        self.assertEqual(len(output), len(samples))
        self.assertGreater(snr_db(samples, output), 25)
        # Every header must be the corresponding INPUT sample, not the previous
        # block's final decoded sample.
        for index in range(80):
            self.assertEqual(
                struct.unpack_from("<h", encoded, index * block_size)[0],
                samples[index * samples_per_block],
            )

    async def test_stereo_48khz_to_mono_16khz_and_mute(self):
        binary = shutil.which("ffmpeg")
        if not binary and importlib.util.find_spec("imageio_ffmpeg"):
            import imageio_ffmpeg

            binary = imageio_ffmpeg.get_ffmpeg_exe()
        if not binary:
            self.skipTest("install FFmpeg or imageio-ffmpeg")
        samples = sine(4800, rate=48000)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(48000)
            wav.writeframes(b"".join(struct.pack("<hh", x, x) for x in samples))
        with patch.object(talk.shutil, "which", return_value=binary):
            pcm = await talk.ffmpeg_to_pcm_s16le(buffer.getvalue(), sample_rate=16000)
            muted = await talk.ffmpeg_to_pcm_s16le(
                buffer.getvalue(), sample_rate=16000, volume=0
            )
        self.assertEqual(len(pcm), 3200)
        self.assertEqual(muted, bytes(3200))
        self.assertGreater(max(struct.unpack("<1600h", pcm)), 10000)


@unittest.skipUnless(
    importlib.util.find_spec("cryptography"), "install cryptography for local TLS test"
)
class CertificateTests(unittest.IsolatedAsyncioTestCase):
    async def test_self_signed_rejected_when_enabled(self):
        import aiohttp
        from aiohttp import web
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .sign(key, hashes.SHA256())
        )
        with tempfile.TemporaryDirectory() as directory:
            cert_path, key_path = (
                Path(directory) / "cert.pem",
                Path(directory) / "key.pem",
            )
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert_path, key_path)
            app = web.Application()

            async def respond(request):
                return web.Response(text="ok")

            app.router.add_get("/", respond)
            runner = web.AppRunner(app)
            await runner.setup()
            server = await asyncio.get_running_loop().create_server(
                runner.server, "127.0.0.1", 0, ssl=context
            )
            url = f"https://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
            try:
                async with aiohttp.ClientSession() as session:
                    with self.assertRaises(aiohttp.ClientConnectorCertificateError):
                        await session.get(url)
                async with aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(ssl=False)
                ) as session:
                    async with session.get(url) as response:
                        self.assertEqual(await response.text(), "ok")
            finally:
                server.close()
                await server.wait_closed()
                await runner.cleanup()
