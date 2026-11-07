from __future__ import annotations

import asyncio
import logging
import math
import shutil
import struct
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlparse

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import get_url

_LOGGER = logging.getLogger(__name__)


BC_MESSAGE_CLASS_1464: Final[bytes] = bytes.fromhex(
    "00001464"
)  # status_code=0, class=1464

# reolink-aio 0.21.17 removed baichuan.xmls.XML_HEADER, which every talk block
# was built on (issue #4). Kept here byte-for-byte so we no longer depend on a
# private constant of the library HA's Reolink integration pins for us.
BC_XML_HEADER: Final[str] = '<?xml version="1.0" encoding="UTF-8" ?>\n'


@dataclass(frozen=True)
class TalkAbility:
    duplex: str
    audio_stream_mode: str
    audio_type: str
    priority: int | None
    sample_rate: int
    sample_precision: int
    length_per_encoder: int
    sound_track: str


@dataclass(frozen=True)
class PlaybackOptions:
    """Opt-in experiments; defaults preserve the established playback path."""

    blocks_per_packet: int = 4
    initial_buffer_ms: int = 0
    pacing_factor: float = 1.0
    pacing_mode: str = "deadline"
    final_drain_ms: int = 0
    wait_for_ack: bool = True
    logging: bool = False
    attenuation_db: float = 0.0

    def __post_init__(self):
        if not 1 <= self.blocks_per_packet <= 16:
            raise ValueError("blocks_per_packet must be 1..16")
        if (
            not 0 <= self.initial_buffer_ms <= 500
            or not 0 <= self.final_drain_ms <= 2000
        ):
            raise ValueError("buffer/drain out of range")
        if (
            not math.isfinite(self.pacing_factor)
            or not 0.9 <= self.pacing_factor <= 1.1
        ):
            raise ValueError("pacing_factor must be .9..1.1")
        if self.pacing_mode not in ("deadline", "interval"):
            raise ValueError("unknown pacing mode")
        if (
            not math.isfinite(self.attenuation_db)
            or not -18 <= self.attenuation_db <= 0
        ):
            raise ValueError("attenuation_db must be -18..0")

    @classmethod
    def from_options(cls, options):
        return cls(
            **{
                key: options.get(f"diagnostic_{key}", field.default)
                for key, field in cls.__dataclass_fields__.items()
            }
        )


def talk_block_size(ability: TalkAbility) -> int:
    """Negotiated payload is lengthPerEncoder/2 bytes, plus 4 state bytes."""
    length = ability.length_per_encoder
    if length < 8 or length % 2 or ability.sample_rate <= 0:
        raise ValueError("Invalid negotiated ADPCM length/sample rate")
    if ability.sample_precision != 16 or ability.sound_track.lower() != "mono":
        raise ValueError("Only mono PCM16 talk is supported")
    return length // 2 + 4


def pcm_levels(pcm: bytes) -> dict[str, float | int]:
    """Measure post-FFmpeg PCM; rail hits suggest clipping but do not prove it."""
    if len(pcm) % 2:
        raise ValueError("PCM length must be even")
    count = len(pcm) // 2
    peak = rails = 0
    energy = 0
    for (sample,) in struct.iter_unpack("<h", pcm):
        peak = max(peak, abs(sample))
        energy += sample * sample
        rails += sample in (-32768, 32767)
    return {
        "samples": count,
        "peak": peak,
        "rail_samples": rails,
        "peak_dbfs": 20 * math.log10(peak / 32768) if peak else -math.inf,
        "rms_dbfs": 10 * math.log10(energy / count / 32768**2) if energy else -math.inf,
    }


def transport_backlog(bc):
    """Host buffers only; these values cannot measure the NVR speaker buffer."""
    connection = getattr(bc, "_connection", None)
    transport = getattr(connection, "_transport", None)
    size = None
    reader = getattr(transport, "get_write_buffer_size", None)
    if reader is not None:
        try:
            size = reader()
        except (AttributeError, NotImplementedError):
            pass
    return {
        "write_buffer_bytes": size,
        "udp_queued": len(getattr(connection, "_send_queue", ())),
        "udp_unacked": len(getattr(connection, "_send_packets", ())),
    }


def _first_text(root: ET.Element, path: str) -> str | None:
    el = root.find(path)
    if el is None or el.text is None:
        return None
    return el.text.strip()


def _all_texts(root: ET.Element, path: str) -> list[str]:
    out: list[str] = []
    for el in root.findall(path):
        if el is None or el.text is None:
            continue
        t = el.text.strip()
        if t:
            out.append(t)
    return out


def parse_talk_ability(xml: str) -> TalkAbility:
    root = ET.fromstring(xml)
    ta = root.find(".//TalkAbility")
    if ta is None:
        raise ValueError("TalkAbility not found in response")

    # Prefer "best" settings when lists are present:
    # - FDX: full duplex is typically what we want for talkback
    # - mixAudioStream: avoids dependency on the live video stream audio mode
    duplex_list = _all_texts(ta, ".//duplexList/duplex")
    stream_mode_list = _all_texts(ta, ".//audioStreamModeList/audioStreamMode")

    duplex = _first_text(ta, ".//duplex") or ""
    if "FDX" in duplex_list:
        duplex = "FDX"
    if not duplex:
        duplex = duplex_list[0] if duplex_list else "FDX"

    audio_stream_mode = _first_text(ta, ".//audioStreamMode") or ""
    if "mixAudioStream" in stream_mode_list:
        audio_stream_mode = "mixAudioStream"
    if not audio_stream_mode:
        audio_stream_mode = (
            stream_mode_list[0] if stream_mode_list else "followVideoStream"
        )

    ac = ta.find(".//audioConfig")
    if ac is None:
        raise ValueError("audioConfig not found in TalkAbility")

    audio_type = _first_text(ac, ".//audioType") or "adpcm"
    prio_txt = _first_text(ac, ".//priority")
    priority = int(prio_txt) if prio_txt and prio_txt.isdigit() else None
    sample_rate = int(_first_text(ac, ".//sampleRate") or "16000")
    sample_precision = int(_first_text(ac, ".//samplePrecision") or "16")
    length_per_encoder = int(_first_text(ac, ".//lengthPerEncoder") or "1024")
    sound_track = _first_text(ac, ".//soundTrack") or "mono"

    return TalkAbility(
        duplex=duplex,
        audio_stream_mode=audio_stream_mode,
        audio_type=audio_type,
        priority=priority,
        sample_rate=sample_rate,
        sample_precision=sample_precision,
        length_per_encoder=length_per_encoder,
        sound_track=sound_track,
    )


def build_talk_config_xml(channel: int, ability: TalkAbility) -> str:
    # Match the XML shapes documented by neolink.
    prio = (
        f"<priority>{ability.priority}</priority>\n"
        if ability.priority is not None
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" ?>\n'
        "<body>\n"
        '<TalkConfig version="1.1">\n'
        f"<channelId>{channel}</channelId>\n"
        f"<duplex>{ability.duplex}</duplex>\n"
        f"<audioStreamMode>{ability.audio_stream_mode}</audioStreamMode>\n"
        "<audioConfig>\n"
        + prio
        + f"<audioType>{ability.audio_type}</audioType>\n"
        + f"<sampleRate>{ability.sample_rate}</sampleRate>\n"
        + f"<samplePrecision>{ability.sample_precision}</samplePrecision>\n"
        + f"<lengthPerEncoder>{ability.length_per_encoder}</lengthPerEncoder>\n"
        + f"<soundTrack>{ability.sound_track}</soundTrack>\n"
        + "</audioConfig>\n"
        + "</TalkConfig>\n"
        + "</body>\n"
    )


def build_talk_config_variants(channel: int, ability: TalkAbility) -> list[str]:
    """Return a small set of TalkConfig XML variants for firmware quirks."""
    full = build_talk_config_xml(channel, ability)
    variants: list[str] = [full]

    # Some firmwares appear picky about the XML header.
    if full.lstrip().startswith("<?xml"):
        try:
            _, rest = full.split("\n", 1)
            variants.append(rest)
        except ValueError:
            pass

    # Some firmwares expect just the TalkConfig element (no <body> wrapper).
    start = full.find("<TalkConfig")
    end = full.rfind("</TalkConfig>")
    if start != -1 and end != -1:
        tc = full[start : end + len("</TalkConfig>")] + "\n"
        if tc not in variants:
            variants.append(tc)

    return variants


def _riff_chunks(wav: bytes):
    if len(wav) < 12 or wav[0:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise ValueError("Not a RIFF/WAVE file")
    off = 12
    while off + 8 <= len(wav):
        cid = wav[off : off + 4]
        size = int.from_bytes(wav[off + 4 : off + 8], "little")
        data_off = off + 8
        data_end = data_off + size
        # Be tolerant: ffmpeg output can occasionally be cut short (or the RIFF
        # size fields can be inconsistent). If we can still extract `fmt ` and
        # partial `data`, that is better than hard-failing the whole service call.
        truncated = data_end > len(wav)
        if truncated:
            data_end = len(wav)
        yield cid, wav[data_off:data_end]
        if truncated:
            break
        off = data_end + (size % 2)  # chunks are word-aligned


def extract_wav_fmt_and_data(wav: bytes) -> tuple[dict, bytes]:
    fmt: dict = {}
    data: bytes | None = None
    for cid, payload in _riff_chunks(wav):
        if cid == b"fmt ":
            if len(payload) < 16:
                raise ValueError("Invalid fmt chunk")
            fmt["audio_format"] = int.from_bytes(payload[0:2], "little")
            fmt["channels"] = int.from_bytes(payload[2:4], "little")
            fmt["sample_rate"] = int.from_bytes(payload[4:8], "little")
            fmt["byte_rate"] = int.from_bytes(payload[8:12], "little")
            fmt["block_align"] = int.from_bytes(payload[12:14], "little")
            fmt["bits_per_sample"] = int.from_bytes(payload[14:16], "little")
        elif cid == b"data":
            data = payload
    if not fmt or data is None:
        raise ValueError("WAV missing fmt or data chunk")
    return fmt, data


def bcmedia_adpcm_packet(block: bytes) -> bytes:
    # Port of neolink bcmedia_adpcm() + padding rules.
    # block must be: 4 bytes predictor state + N bytes adpcm payload.
    if len(block) < 5:
        raise ValueError("ADPCM block too small")
    payload_len = len(block) + 4  # + magic u16 + blocksize u16
    # Neolink format: "block size without header, halved" (DVI-4 payload bytes / 2).
    block_size = (len(block) - 4) // 2
    header = struct.pack(
        "<IHHHH",
        0x62773130,  # MAGIC_HEADER_BCMEDIA_ADPCM
        payload_len,
        payload_len,
        0x0100,  # MAGIC_HEADER_BCMEDIA_ADPCM_DATA
        block_size,
    )
    pad_len = (-len(block)) % 8
    return header + block + (b"\x00" * pad_len)


def talk_binary_payload(
    adpcm_bytes: bytes, full_block_size: int, blocks_per_payload: int = 4
) -> list[tuple[bytes, int]]:
    if full_block_size < 8 or blocks_per_payload < 1:
        raise ValueError("Invalid ADPCM packet dimensions")
    if len(adpcm_bytes) % full_block_size:
        raise ValueError("Incomplete ADPCM block; refusing to truncate audio")
    # Returns list of (binary_payload, blocks_in_payload).
    out: list[tuple[bytes, int]] = []
    blocks = [
        adpcm_bytes[i : i + full_block_size]
        for i in range(0, len(adpcm_bytes), full_block_size)
    ]
    for i in range(0, len(blocks), blocks_per_payload):
        group = blocks[i : i + blocks_per_payload]
        payload = b"".join(bcmedia_adpcm_packet(b) for b in group)
        out.append((payload, len(group)))
    return out


async def fetch_bytes(hass: HomeAssistant, url: str) -> bytes:
    session = async_get_clientsession(hass)
    fetch_url = url
    # If it's a Home Assistant local URL (TTS/media proxy), we need to sign it,
    # because we are fetching server-side (no browser cookies / auth headers).
    try:
        parsed = urlparse(url)
        if url.startswith("/"):
            path_q = url
        elif parsed.scheme in ("http", "https"):
            path_q = parsed.path + (("?" + parsed.query) if parsed.query else "")
            base = get_url(hass, allow_internal=True)
            base_netloc = urlparse(base).netloc
            if parsed.netloc != base_netloc:
                path_q = ""
        else:
            path_q = ""

        if path_q:
            from homeassistant.components.http.auth import async_sign_path

            base = get_url(hass, allow_internal=True)
            signed = async_sign_path(hass, path_q)
            fetch_url = f"{base}{signed}"
    except Exception:
        # Best-effort; fall back to raw URL.
        pass

    async with session.get(fetch_url, allow_redirects=True) as resp:
        resp.raise_for_status()
        return await resp.read()


# IMA/DVI ADPCM encoder tables (standard IMA ADPCM).
_IMA_INDEX_TABLE: Final[list[int]] = [
    -1,
    -1,
    -1,
    -1,
    2,
    4,
    6,
    8,
    -1,
    -1,
    -1,
    -1,
    2,
    4,
    6,
    8,
]
_IMA_STEP_TABLE: Final[list[int]] = [
    7,
    8,
    9,
    10,
    11,
    12,
    13,
    14,
    16,
    17,
    19,
    21,
    23,
    25,
    28,
    31,
    34,
    37,
    41,
    45,
    50,
    55,
    60,
    66,
    73,
    80,
    88,
    97,
    107,
    118,
    130,
    143,
    157,
    173,
    190,
    209,
    230,
    253,
    279,
    307,
    337,
    371,
    408,
    449,
    494,
    544,
    598,
    658,
    724,
    796,
    876,
    963,
    1060,
    1166,
    1282,
    1411,
    1552,
    1707,
    1878,
    2066,
    2272,
    2499,
    2749,
    3024,
    3327,
    3660,
    4026,
    4428,
    4871,
    5358,
    5894,
    6484,
    7132,
    7845,
    8630,
    9493,
    10442,
    11487,
    12635,
    13899,
    15289,
    16818,
    18500,
    20350,
    22385,
    24623,
    27086,
    29794,
    32767,
]


async def ffmpeg_to_pcm_s16le(
    input_bytes: bytes,
    *,
    sample_rate: int,
    volume: float = 1.0,
) -> bytes:
    """Decode arbitrary audio to mono 16-bit PCM (little-endian) at sample_rate."""
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-af",
        f"volume={max(0.0, float(volume))}",
        "-ac",
        "1",
        "-ar",
        str(int(sample_rate)),
        "-f",
        "s16le",
        "pipe:1",
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout
    try:
        out, err = await proc.communicate(input_bytes)
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
        await proc.communicate()
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {err.decode('utf-8', 'ignore')}")
    return out


def _ima_encode_nibble(
    sample: int, predictor: int, step_index: int
) -> tuple[int, int, int]:
    """Encode one PCM sample to a 4-bit IMA ADPCM nibble.

    Returns (nibble, new_predictor, new_step_index).
    """
    step = _IMA_STEP_TABLE[step_index]
    diff = sample - predictor
    sign = 0
    if diff < 0:
        sign = 8
        diff = -diff

    delta = 0
    vpdiff = step >> 3
    if diff >= step:
        delta |= 4
        diff -= step
        vpdiff += step
    if diff >= (step >> 1):
        delta |= 2
        diff -= step >> 1
        vpdiff += step >> 1
    if diff >= (step >> 2):
        delta |= 1
        vpdiff += step >> 2

    if sign:
        predictor -= vpdiff
    else:
        predictor += vpdiff

    predictor = max(-32768, min(32767, predictor))

    step_index += _IMA_INDEX_TABLE[delta | sign]
    step_index = max(0, min(88, step_index))

    return (delta | sign) & 0xF, predictor, step_index


def ima_adpcm_encode_dvi_blocks(pcm_s16le: bytes, *, full_block_size: int) -> bytes:
    """Encode PCM s16le into DVI-4 ADPCM blocks.

    Block layout expected by neolink talk:
    - 2 bytes: initial predictor sample (i16 LE)
    - 1 byte: step index
    - 1 byte: reserved (0)
    - (full_block_size - 4) bytes: packed nibbles, 2 samples per byte
    """
    if full_block_size < 8:
        raise ValueError("full_block_size too small")
    if len(pcm_s16le) % 2 != 0:
        raise ValueError("PCM length must be even (s16le)")

    if not pcm_s16le:
        return b""
    encoder = LiveAdpcmEncoder(full_block_size)
    samples_per_block = (full_block_size - 4) * 2 + 1
    # Pad only the final incomplete block. Every header represents a real
    # input sample, followed by two coded samples per payload byte.
    block_pcm_bytes = samples_per_block * 2
    padded = pcm_s16le + bytes((-len(pcm_s16le)) % block_pcm_bytes)
    return b"".join(encoder.feed(padded))


class LiveAdpcmEncoder:
    """Encode IMA/DVI blocks with one input sample in every block header.

    Match GStreamer's adpcmenc DVI layout: carry the adaptive step index
    between blocks, but seed each predictor with that block's first sample.
    """

    def __init__(self, full_block_size: int) -> None:
        if full_block_size < 8:
            raise ValueError("full_block_size too small")
        self.full_block_size = full_block_size
        self.payload_samples = (full_block_size - 4) * 2
        self.samples_per_block = self.payload_samples + 1
        self._predictor = 0
        self._step_index = 0
        self._pending: list[int] = []
        self._partial_byte = b""

    def feed(self, pcm_s16le: bytes) -> list[bytes]:
        """Accept arbitrary byte chunks without dropping split PCM samples."""
        pcm_s16le = self._partial_byte + pcm_s16le
        usable = len(pcm_s16le) - len(pcm_s16le) % 2
        self._partial_byte = pcm_s16le[usable:]
        if usable:
            self._pending.extend(struct.unpack(f"<{usable // 2}h", pcm_s16le[:usable]))
        blocks: list[bytes] = []
        while len(self._pending) >= self.samples_per_block:
            samples = self._pending[: self.samples_per_block]
            del self._pending[: self.samples_per_block]
            self._predictor = samples[0]
            blocks.append(self._encode_block(samples[1:]))
        return blocks

    def _encode_block(self, samples: list[int]) -> bytes:
        block = bytearray(struct.pack("<hBB", self._predictor, self._step_index, 0))
        acc = None
        for sample in samples:
            nib, self._predictor, self._step_index = _ima_encode_nibble(
                sample, self._predictor, self._step_index
            )
            if acc is None:
                acc = nib
            else:
                block.append(acc | (nib << 4))
                acc = None
        return bytes(block)


@contextmanager
def claim_talk_session(hass, entry_id: str, channel: int):
    """Reject overlapping talk on one channel, allowing different channels."""
    active = hass.data.setdefault("reolink_talk", {}).setdefault("active_talk", set())
    key = (entry_id, channel)
    if key in active:
        raise RuntimeError(
            "Talk already active on this channel; retry after it finishes"
        )
    active.add(key)
    try:
        yield
    finally:
        active.discard(key)


async def send_talk_binary(
    bc,  # reolink_aio.baichuan.Baichuan
    channel: int,
    binary_payload: bytes,
    *,
    mess_id: int | None = None,
    enc_type=None,
    wait_for_ack: bool = True,
) -> None:
    # Like reolink_aio Baichuan.send(), but:
    # - optionally waits for a command response (default), not audio completion
    # - encrypts only the Extension XML
    # - appends the BcMedia binary payload unencrypted
    from reolink_aio.baichuan import util as bc_util

    if not getattr(bc, "_logged_in", False):
        await bc.login()

    # reolink_aio internal fields have changed between versions. We only need a
    # monotonically increasing 24-bit message id for the Baichuan header.
    if not hasattr(bc, "_mess_id"):
        setattr(bc, "_mess_id", 0)

    # Map channel -> ch_id like reolink_aio does (1.. for channels)
    ch_id = channel + 1

    ext = (
        BC_XML_HEADER
        + '<Extension version="1.1">\n'
        + "<binaryData>1</binaryData>\n"
        + f"<channelId>{channel}</channelId>\n"
        + "</Extension>\n"
    )

    if mess_id is None:
        bc._mess_id = (bc._mess_id + 1) % 16777216
    else:
        bc._mess_id = mess_id

    # IMPORTANT: Baichuan TCP parsing uses:
    # - len_body (rec_len_body) to know how many bytes to consume after the header
    # - payload_offset to split "message" vs "payload" bytes
    #
    # For normal AES messages, reolink_aio sets both to the *plaintext* length
    # and sends only encrypted body bytes (no extra payload bytes).
    #
    # For talk/audio (cmd 202), we send:
    # - encrypted Extension XML ("message" bytes)
    # - raw, unencrypted BcMedia ADPCM bytes ("payload" bytes)
    #
    # Therefore, the header must use ON-WIRE byte lengths:
    # - payload_offset = len(enc_ext)
    # - mess_len = len(enc_ext) + len(binary_payload)
    if enc_type is None:
        enc_type = bc_util.EncType.AES

    if enc_type == bc_util.EncType.BC:
        enc_ext = bc_util.encrypt_baichuan(ext, ch_id)  # enc_offset = ch_id
    else:
        enc_ext = bc._aes_encrypt(ext.encode("utf-8"))
    payload_offset = len(enc_ext)
    mess_len = payload_offset + len(binary_payload)

    cmd_id = 202
    header = (
        bytes.fromhex(bc_util.HEADER_MAGIC)
        + int(cmd_id).to_bytes(4, "little")
        + int(mess_len).to_bytes(4, "little")
        + int(ch_id).to_bytes(1, "little")
        + int(bc._mess_id).to_bytes(3, "little")
        + BC_MESSAGE_CLASS_1464
        + int(payload_offset).to_bytes(4, "little")
    )

    packet = header + enc_ext + binary_payload
    # Modern reolink-aio owns response routing and UDP/TCP transport details.
    # Its connection.send propagates rejected packets and cleans pending futures.
    await bc._connect_if_needed()
    full_mess_id = int.from_bytes(header[12:16], "little")
    if not wait_for_ack:
        sender = getattr(bc._connection, "send_without_wait", None)
        if sender is None:
            raise RuntimeError(
                "Nonblocking diagnostic requires connection.send_without_wait"
            )
        await sender(packet, cmd_id=cmd_id)
        return
    sender = getattr(bc._connection, "send", None)
    if sender is not None:
        await sender(packet, cmd_id, full_mess_id, channel)
        return
    # Compatibility with older reolink-aio's protocol-owned response routing.
    proto = getattr(bc, "_protocol", None)
    loop = getattr(bc, "_loop", None)
    if proto is None or loop is None:
        if not getattr(bc, "_talk_ack_warning_logged", False):
            _LOGGER.warning(
                "Talk transport has no command-response API; packet acceptance cannot be confirmed"
            )
            bc._talk_ack_warning_logged = True
        async with bc._login_mutex:
            bc._connection._transport.write(packet)
        return

    receive_future = loop.create_future()
    proto.receive_futures.setdefault(cmd_id, {})[full_mess_id] = receive_future

    try:
        async with bc._login_mutex:
            bc._connection._transport.write(packet)
        async with asyncio.timeout(5):
            await receive_future
    finally:
        try:
            if not receive_future.done():
                receive_future.cancel()
        except Exception:
            pass
        futs = proto.receive_futures.get(cmd_id, {})
        futs.pop(full_mess_id, None)
        if not futs and cmd_id in proto.receive_futures:
            proto.receive_futures.pop(cmd_id, None)


async def talk_playback(
    bc,  # reolink_aio.baichuan.Baichuan
    channel: int,
    adpcm_bytes: bytes,
    ability: TalkAbility,
    *,
    block_align: int | None = None,
    options: PlaybackOptions | None = None,
) -> None:
    from reolink_aio.exceptions import ApiError
    from reolink_aio.baichuan import util as bc_util

    async def _send_with_fallback(cmd_id: int, *, body: str = "") -> bc_util.EncType:
        # Some firmwares expect BC encryption for talk commands (otherwise 400).
        for enc in (bc_util.EncType.AES, bc_util.EncType.BC):
            try:
                await bc.send(cmd_id=cmd_id, channel=channel, body=body, enc_type=enc)
                return enc
            except ApiError as err:
                # Retry with BC only for "bad request" style errors.
                if getattr(err, "rspCode", None) == 400 and enc == bc_util.EncType.AES:
                    continue
                raise
        return bc_util.EncType.BC

    options = options or PlaybackOptions()
    full_block_size = talk_block_size(ability)
    if block_align is not None and block_align != full_block_size:
        raise ValueError("ADPCM block alignment disagrees with TalkAbility")
    payloads = talk_binary_payload(
        adpcm_bytes, full_block_size, options.blocks_per_packet
    )

    enc_used = None
    last_err = None
    for talk_cfg in build_talk_config_variants(channel, ability):
        try:
            enc_used = await _send_with_fallback(201, body=talk_cfg)
            break
        except ApiError as err:
            last_err = err
            status = getattr(err, "rspCode", None)
            if status in (421, 422):
                raise RuntimeError(
                    f"Device rejected talk on channel {channel} (status {status}); "
                    "another talk stream may be active. Retry sequentially or use direct camera connections."
                ) from err
            if status != 400:
                raise
    if enc_used is None:
        raise RuntimeError(
            f"Device rejected talk configuration on channel {channel}"
        ) from last_err

    loop = asyncio.get_running_loop()
    started = deadline = loop.time()
    audio_seconds = 0.0
    lead = options.initial_buffer_ms / 1000
    lead_remaining = lead
    due = started
    previous_send = None
    count = logged = late_count = 0
    max_late = max_wait = max_gap = sum_wait = 0.0
    if options.logging:
        from importlib.metadata import version

        _LOGGER.debug(
            "Talk API reolink-aio=%s response_path=%s",
            version("reolink-aio"),
            "connection.send"
            if hasattr(getattr(bc, "_connection", None), "send")
            else "legacy",
        )
        _LOGGER.debug(
            "Talk experiment ch=%s length=%s block=%s samples_per_block=%s rate=%s options=%s transport=%s",
            channel,
            ability.length_per_encoder,
            full_block_size,
            (full_block_size - 4) * 2 + 1,
            ability.sample_rate,
            options,
            type(getattr(bc, "_connection", None)).__name__,
        )
    try:
        for payload, blocks_in_payload in payloads:
            sent_at = loop.time()
            lateness = max(0.0, sent_at - due)
            gap = sent_at - previous_send if previous_send is not None else 0.0
            previous_send = sent_at
            if options.wait_for_ack:
                await send_talk_binary(bc, channel, payload, enc_type=enc_used)
            else:
                await send_talk_binary(
                    bc, channel, payload, enc_type=enc_used, wait_for_ack=False
                )
            send_wait = loop.time() - sent_at
            samples_sent = ((full_block_size - 4) * 2 + 1) * blocks_in_payload
            play_length = samples_sent / ability.sample_rate
            audio_seconds += play_length
            count += 1
            late_count += lateness > 0.005
            max_late = max(max_late, lateness)
            max_wait = max(max_wait, send_wait)
            max_gap = max(max_gap, gap)
            sum_wait += send_wait
            if (
                options.logging
                and logged < 20
                and (
                    count <= 5
                    or count % 50 == 0
                    or send_wait > play_length
                    or lateness > 0.005
                )
            ):
                _LOGGER.debug(
                    "Talk packet ch=%s n=%s blocks=%s audio_ms=%.3f schedule_late_ms=%.3f send_wait_ms=%.3f gap_ms=%.3f ack_requested=%s host_backlog=%s",
                    channel,
                    count,
                    blocks_in_payload,
                    play_length * 1000,
                    lateness * 1000,
                    send_wait * 1000,
                    gap * 1000,
                    options.wait_for_ack,
                    transport_backlog(bc),
                )
                logged += 1
            if options.pacing_mode == "interval":
                # Reference Neolink talk_stream sleeps AFTER sending; its send
                # queues a message and does not await a cmd 202 response.
                interval = play_length * options.pacing_factor
                skip = min(lead_remaining, interval)
                lead_remaining -= skip
                deadline = loop.time() + interval - skip
                due = deadline
            else:
                deadline += play_length * options.pacing_factor
                due = max(started, deadline - lead)
            await asyncio.sleep(max(0.0, due - loop.time()))
        # Return any deliberate startup lead/faster pacing before stopping.
        # A command response is not evidence that the speaker has drained.
        if lead or options.pacing_factor < 1:
            await asyncio.sleep(max(0.0, started + audio_seconds - loop.time()))
        if options.final_drain_ms:
            await asyncio.sleep(options.final_drain_ms / 1000)
    finally:
        if options.logging:
            _LOGGER.debug(
                "Talk summary ch=%s packets=%s audio_s=%.3f elapsed_s=%.3f late_over_5ms=%s max_late_ms=%.3f mean_send_wait_ms=%.3f max_send_wait_ms=%.3f max_gap_ms=%.3f ack_requested=%s host_backlog=%s",
                channel,
                count,
                audio_seconds,
                loop.time() - started,
                late_count,
                max_late * 1000,
                sum_wait * 1000 / max(count, 1),
                max_wait * 1000,
                max_gap * 1000,
                options.wait_for_ack,
                transport_backlog(bc),
            )
        try:
            await bc.send(cmd_id=11, channel=channel, enc_type=enc_used)
        except Exception as err:
            _LOGGER.warning(
                "Talk stop failed on channel %s (%s)", channel, type(err).__name__
            )
