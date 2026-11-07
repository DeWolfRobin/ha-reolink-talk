"""Compare production encoder against real GStreamer and independent FFmpeg.

Native: python research/compare_adpcm_reference.py --output /tmp/talk-reference
WSL: add --wsl-root /path/to/extracted/gstreamer/root --wsl-distro Ubuntu-24.04
The root must have plugins/adpcmenc+coreelements and usr/bin/gst-launch-1.0.
No network or camera access. Only synthetic audio is generated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_live_adpcm import talk  # minimal HA import shim, actual production encoder


def decode(ffmpeg, encoded, block_size):
    samples = (block_size - 4) * 2 + 1
    fmt = struct.pack(
        "<HHIIHHHH",
        17,
        1,
        16000,
        16000 * block_size // samples,
        block_size,
        4,
        2,
        samples,
    )
    body = (
        b"WAVEfmt "
        + struct.pack("<I", len(fmt))
        + fmt
        + b"data"
        + struct.pack("<I", len(encoded))
        + encoded
    )
    return subprocess.run(
        [ffmpeg, "-v", "error", "-i", "pipe:0", "-f", "s16le", "pipe:1"],
        input=b"RIFF" + struct.pack("<I", len(body)) + body,
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout


def snr(reference, decoded):
    got = struct.unpack(f"<{len(decoded) // 2}h", decoded)
    error = sum((a - b) ** 2 for a, b in zip(reference, got))
    signal = sum(a * a for a in reference)
    return 10 * math.log10(signal / error) if signal and error else None


def run(args):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    prefix = [args.gst]
    if args.wsl_root:
        root = args.wsl_root.rstrip("/")
        prefix = [
            "wsl",
            "-d",
            args.wsl_distro,
            "--",
            "env",
            "GST_PLUGIN_SYSTEM_PATH=",
            f"GST_PLUGIN_PATH={root}/plugins",
            f"GST_REGISTRY={root}/registry.bin",
            f"LD_LIBRARY_PATH={root}/usr/lib/x86_64-linux-gnu",
            f"{root}/usr/bin/gst-launch-1.0",
        ]
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    version = (
        subprocess.run(prefix + ["--version"], capture_output=True, check=True)
        .stdout.decode()
        .strip()
    )
    results = {
        "gstreamer": version,
        "rate": 16000,
        "blocks_per_signal": 32,
        "cases": [],
    }
    for block_size in (260, 516, 1028):
        count = ((block_size - 4) * 2 + 1) * 32
        signals = {
            "silence": [0] * count,
            "sine": [
                int(12000 * math.sin(2 * math.pi * 440 * i / 16000))
                for i in range(count)
            ],
            "transient": [16000 if i % 4000 == 2000 else 0 for i in range(count)],
            "speech_like": [
                int(
                    5000
                    * (math.sin(i * 0.06) + 0.5 * math.sin(i * 0.13))
                    * (1 + math.sin(i * 0.001))
                )
                for i in range(count)
            ],
            "doorbell": [
                int(
                    9000
                    * math.exp(-(i % 16000) / 8000)
                    * (math.sin(i * 0.16) + 0.3 * math.sin(i * 0.21))
                )
                for i in range(count)
            ],
        }
        for name, samples in signals.items():
            pcm = struct.pack(f"<{count}h", *samples)
            ours = talk.ima_adpcm_encode_dvi_blocks(pcm, full_block_size=block_size)
            reference = subprocess.run(
                prefix
                + [
                    "-q",
                    "fdsrc",
                    "!",
                    "audio/x-raw,format=S16LE,channels=1,rate=16000,layout=interleaved",
                    "!",
                    "adpcmenc",
                    f"blockalign={block_size}",
                    "layout=dvi",
                    "!",
                    "fdsink",
                    "fd=1",
                    "sync=false",
                ],
                input=pcm,
                capture_output=True,
                check=True,
                timeout=30,
            ).stdout
            our_pcm, gst_pcm = (
                decode(ffmpeg, ours, block_size),
                decode(ffmpeg, reference, block_size),
            )
            tag = f"{name}-{block_size}"
            for suffix, data in (("ours.adpcm", ours), ("gst.adpcm", reference)):
                (output / f"{tag}-{suffix}").write_bytes(data)
            for suffix, data in (("input", pcm), ("ours", our_pcm), ("gst", gst_pcm)):
                with wave.open(str(output / f"{tag}-{suffix}.wav"), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(16000)
                    wav.writeframes(data)
            headers_equal = len(ours) == len(reference) and all(
                ours[i : i + 4] == reference[i : i + 4]
                for i in range(0, len(ours), block_size)
            )
            case = {
                "signal": name,
                "block_bytes": block_size,
                "input_samples": count,
                "ours_bytes": len(ours),
                "gst_bytes": len(reference),
                "different_bytes": sum(a != b for a, b in zip(ours, reference))
                + abs(len(ours) - len(reference)),
                "headers_equal": headers_equal,
                "decoded_pcm_equal": our_pcm == gst_pcm,
                "decoded_samples": len(gst_pcm) // 2,
                "ours_snr_db": snr(samples, our_pcm),
                "gst_snr_db": snr(samples, gst_pcm),
                "sha256": hashlib.sha256(reference).hexdigest(),
            }
            results["cases"].append(case)
            print(json.dumps(case), flush=True)
    (output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    if any(
        c["different_bytes"] or not c["headers_equal"] or not c["decoded_pcm_equal"]
        for c in results["cases"]
    ):
        raise SystemExit("Reference difference detected; inspect results.json and WAVs")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--gst", default="gst-launch-1.0")
    parser.add_argument("--wsl-root")
    parser.add_argument("--wsl-distro", default="Ubuntu-24.04")
    run(parser.parse_args())
