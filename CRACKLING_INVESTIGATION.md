# Remaining crackling: evidence and controlled experiments

**Result:** no remaining encoder mismatch was found in 15 real GStreamer cases.
Transport scheduling is the next discriminating test, not a proven hardware root
cause. The production encoder is unchanged in this investigation. No deployment,
physical playback, or automation changes were performed.

## Evidence

- Baseline `8b604a5`: 23/23 regression tests passed before edits.
- Local installed **reolink-aio 0.22.0**: connection.send registers a matching
  command/message/channel response future, writes through its TCP/UDP implementation,
  propagates negative replies, and clears futures on timeout/cancellation. A command
  response indicates acceptance, not completion of audible playback. UDP delivery
  ACK/retry handling is separate from the Baichuan command response. The diagnostic
  no-wait path still uses connection.send_without_wait, retaining UDP framing/retries.
- **Neolink master `186d58046271870ca0f67afca6e18a797696551c` talk_stream**:
  one ADPCM block per message; send serializes/writes/flushes without waiting for
  cmd 202 responses; sleep is 0.95 times the block's duration. TalkConfig and stop
  do wait for responses. Its file-oriented talk() uses four blocks and full-duration
  sleeps. Our default remains four blocks, awaited cmd 202 responses and monotonic
  deadlines. The earlier comment claiming Neolink waits for each audio ACK was wrong.
- A deterministic 600 ms response stall produces default send starts
  **0, 600, 600 ms** for three four-block packets (516-byte blocks). Thus host
  scheduling can produce a gap followed by a catch-up burst. This is not a measured
  arrival trace or proof of NVR underruns. Interval pacing removes catch-up, but can
  instead accumulate transport delay; it is an experiment, not a new default.
- The complete file is decoded/encoded before TalkConfig, so FFmpeg does not starve
  the transmission loop mid-file. Default stop follows the final scheduled sleep,
  with no additional speaker-drain confirmation. After late delivery this can leave
  less time for a final packet to play; final-drain experiments distinguish tail
  truncation from crackling throughout a clip. Cancellation/failure stops immediately
  without an artificial drain, then logs out.

## Block audit and reference comparison

A single validated talk_block_size() now supplies file playback, live talk and
transport validation. Mismatched block alignment or incomplete encoded blocks fail
before TalkConfig rather than silently dropping bytes. The unused alternate FFmpeg
ADPCM-WAV helper that confused lengthPerEncoder with physical bytes was removed.
The existing encoder, nibble order, BcMedia headers and defaults were retained.

| lengthPerEncoder | Physical block bytes | Header bytes | PCM samples/block | At 16 kHz |
|---:|---:|---:|---:|---:|
| 512 | 260 | 4 | 513 | 32.0625 ms |
| 1024 | 516 | 4 | 1025 | 64.0625 ms |
| 2048 | 1028 | 4 | 2049 | 128.0625 ms |

Formula: physical bytes = lengthPerEncoder // 2 + 4; samples =
(physical bytes - 4) * 2 + 1. Four 516-byte blocks carry 256.25 ms of audio.
BcMedia uses a 12-byte envelope, duplicate physical+4 length fields, data magic
0x0100, (physical-4)//2 as its size field, then padding of the physical block to
an 8-byte boundary. Tests check these against Neolink serializer values.
Historical research/reolink_talk_debug.py contains independent old codec/framing
copies; it is not imported by the integration or used for these experiments.

**Real GStreamer adpcmenc 1.24.2**, DVI layout, ran through WSL using identical
16 kHz mono PCM16 inputs. Five signals (silence, 440 Hz sine, isolated impulses,
speech-like and doorbell-like) x three block sizes x 32 complete blocks:

- **0 differing encoded bytes** in all 15 cases; every header/boundary matches.
- FFmpeg independently decoded both encoders: **identical PCM and sample counts**.
- At 516 bytes: sine SNR 34.40 dB, speech-like 41.38 dB, doorbell-like 28.82 dB.
  Isolated impulses score only 0.006 dB in BOTH encoders: limited IMA adaptation to
  abrupt transients remains intrinsic codec distortion, not an implementation mismatch.
- Full blocks were used to avoid EOS differences: GStreamer discards an incomplete
  final block, whereas our file path deliberately pads it with zero PCM samples.

Machine-readable results: [adpcm_reference_results.json](research/adpcm_reference_results.json).
Reproduce on a system with GStreamer tools/adpcmenc and FFmpeg:

```sh
python -m pip install -r requirements-dev.txt
python research/compare_adpcm_reference.py --output /tmp/talk-reference
```

The harness creates input WAVs, both encoded streams, independently decoded WAVs
and results.json. On this Windows machine the reference run used extracted Ubuntu
packages in WSL (no privileged install or system package changes):

```powershell
python research/compare_adpcm_reference.py --output "$env:TEMP/reolink-adpcm-reference" --wsl-root /home/haywire/.cache/reolink-talk-gst/root
```

## Home Assistant test matrix

Install the updated custom component and restart HA using ENGINEERING_REPORT.md.
Open **Settings > Devices & services > Reolink Talk > Configure**. The new fields
are prefixed Diagnostic and persist/reload like the SSL setting. They affect file
playback only. Enable Diagnostic bounded logging and this logger for every run:

```yaml
logger:
  logs:
    custom_components.reolink_talk: debug
```

Use one camera and the SAME file, fixed HA slider and physical speaker setting.
Manually issue the existing play_media action twice per row; do not re-enable an
automation. Restore all baseline values before every row. Also play a moderate
440 Hz sine and silence if available; no format conversion comparison is needed.

Baseline values: blocks 4; initial send-ahead 0 ms; pacing factor 1.0;
pacing mode deadline; extra final drain 0 ms; wait for command responses ON;
attenuation 0 dB. Diagnostic logging is OFF by default, ON for this matrix.

| Run | Change from baseline ONLY | Observation that matters |
|---|---|---|
| A | None | Crackle throughout, at boundaries, or only start/end? |
| B | Blocks per packet = 1 | Improvement implicates burst size/granularity. |
| C | Wait for command responses = OFF | Improvement with reduced gaps implicates response gating; acceptance is unconfirmed in this run. |
| D | Initial send-ahead = 100 ms | Improvement implicates lack of delivery headroom; it is not a guaranteed NVR prebuffer. |
| E | Pacing multiplier = 0.95 | Improvement suggests delivery-rate/buffer sensitivity; no default change is implied. |
| F | Pacing mode = interval | Tests catch-up bursts versus accumulated response delay. |
| G | Extra final drain = 300 ms | Improvement ONLY at the end implicates teardown/tail truncation. |
| H | Software attenuation = -6 dB | Improvement implicates level sensitivity; keep physical volume unchanged. |

If useful, approximate Neolink talk_stream in a SECOND sequence, comparing each
step with the previous one: A -> blocks 1 -> responses OFF -> interval -> factor
0.95. Each step changes only one variable. This sequence includes a final nominal
playback-duration wait to avoid intentionally truncating faster delivery.

Initial send-ahead consumes its budget once, not on every packet. Final drain is
additional time after normal pacing; faster pacing/send-ahead returns to at least
the nominal audio end before stop. Neither setting can command the speaker to wait
before playing; the device may start immediately and its buffer capacity is unknown.
Do not increase several values together or infer success from service completion.

## What to return and how to interpret it

For each row return camera model/firmware, row label, two listening outcomes, and
whether clicks are periodic, tied to loud transients, or confined to start/end.
Save the Talk API, experiment, physical volume, PCM, packet and summary log lines.
These contain no media URL, credentials or tokens. Logging emits at most 20 packet
records (early/periodic/anomalous), plus per-playback metadata and a summary over
ALL completed sends. Disable diagnostics/debug afterward.

- send_wait_ms is time in the send API, including connection/lock/write/response
  waiting. With response waits enabled it is an **upper bound on ACK latency**,
  not a wire RTT. With waits disabled it measures local enqueue/write only.
- schedule_late_ms and gap_ms are host send-call timestamps, not NVR arrivals.
  Growing gaps, response waits exceeding packet audio time, then near-zero gaps
  support timing sensitivity. A wire trace is needed to prove network delivery jitter.
- host_backlog reports local TCP write-buffer bytes and UDP queue/unacknowledged
  packet counts when available. Zero backlog cannot establish an empty/full NVR buffer.
- PCM peak/RMS dBFS and full-scale rail counts are measured AFTER software gain
  in the executor. Rail hits suggest clipping; no rail hits cannot exclude clipping
  already embedded in the source. We do not have the actual user WAV to measure.
- Physical speak volume is read with GetAudioCfg before playback when diagnostics
  are enabled; unsupported/failed reads are explicitly logged. The HA slider sets
  both software gain and, where supported, hardware speaker level. For a separate
  amplifier test restore A and change ONLY speaker volume in the Reolink app;
  leave the HA slider fixed. H changes software gain without a hardware write.
- No limiter was added: the reference match and synthetic tests do not establish
  that a limiter would help the user's source or physical speaker.

| Diagnosis | Current evidence |
|---|---|
| Incorrect encoded audio | Not reproduced in 15 exact GStreamer comparisons; transient quantization remains. |
| Incorrect BcMedia framing | Matches reference fields/padding in tests; firmware-specific interpretation unverified. |
| Timing/buffer underruns | Host gap/catch-up behavior reproduced; NVR occupancy and arrivals unmeasured. |
| Software clipping | Measurements and isolated attenuation added; actual user WAV unmeasured. |
| NVR/camera/speaker limitation | Remains possible; repeat only the best discriminating row on another channel/direct camera if reachable. |

## Changes and verification

Changed talk.py (options, block validation, bounded telemetry and experimental
scheduling), media_player.py (option wiring, PCM analysis, read-only speaker query,
optional attenuation), config_flow.py, English strings, and talk_live_view.py
(shared block calculation only). Added test_transport_diagnostics.py, reference
harness/results and this report. Lovelace/SSL implementations and entity IDs are
unchanged; no automation, frontend script or channel selection was edited.

**40/40 tests passed**, retaining all 23 baseline regressions. New tests cover
all isolated schedules, short tail packets, option persistence/form defaults,
SSL-option preservation, attenuation wiring, dimensions/padding, clipping metrics,
bounded logs, and the REAL installed library TCP response parser for success,
rejection, timeout, cancellation and no-response mode. GStreamer comparison is a
separate real-tool run, not a mocked regression. Ruff F checks, formatting on
changed Python files, compileall and git diff --check pass. A complete HA runtime
and physical NVR/camera were not available; no claim of audible resolution is made.

References: [Neolink talk_stream](https://github.com/thirtythreeforty/neolink/blob/186d58046271870ca0f67afca6e18a797696551c/crates/core/src/bc_protocol/talk.rs),
[subscription send](https://github.com/thirtythreeforty/neolink/blob/186d58046271870ca0f67afca6e18a797696551c/crates/core/src/bc_protocol/connection/bcsub.rs),
[connection write](https://github.com/thirtythreeforty/neolink/blob/186d58046271870ca0f67afca6e18a797696551c/crates/core/src/bc_protocol/connection/bcconn.rs),
[BcMedia serialization](https://github.com/thirtythreeforty/neolink/blob/186d58046271870ca0f67afca6e18a797696551c/crates/core/src/bcmedia/ser.rs),
[GStreamer encoder](https://github.com/GStreamer/gst-plugins-bad/blob/master/gst/adpcmenc/adpcmenc.c).
