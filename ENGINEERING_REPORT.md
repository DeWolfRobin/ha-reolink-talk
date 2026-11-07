# Reolink Talk fixes and verification

## Baseline and scope

Baseline: `77cebd8`, integration 1.1.6, clean working tree. Python 3.14 on Windows.
The initial suite passed four tests and skipped the reolink-aio framing test.
No Home Assistant runtime, Docker host, NVR or camera was available to this work.
Nothing has been installed on the user's Home Assistant or published upstream.

## Changes and root causes

1. **Lovelace** (`1aa79b6`): `__init__.py` assumed all resource collections implement
   `async_create_item`. Read-only/YAML collections do not. The registration path
   checks write capability, recognizes existing query-versioned URLs, serializes
   resource creation, catches unavailable collection errors, and gives manual YAML
   instructions. It retries at HA started and on subsequent entry setup. Static
   paths and live views remain registered once per HA process. Resources are not
   deleted on unload: they may be user-owned and are needed again on reload.
   Script injection through extra_js_url was not chosen because dashboard loading
   must await custom-element registration. No extra_js_url fallback is claimed.
2. **SSL** (`76231f3`): four device Host callbacks previously selected HA's default
   verified session unconditionally. `verify_ssl` is now a persistent options-flow
   boolean, default true, with English labels and a security explanation.
   `const.py`, `config_flow.py`, `strings.json`, `translations/en.json`,
   `media_player.py` and `__init__.py` implement selection and reload. Existing
   entries need no migration; entity unique IDs, discovery and channels are unchanged.
   Each entity snapshots the option and is recreated on options reload. Device
   sessions use `async_get_clientsession(..., verify_ssl=...)`; HA owns their lifetime.
   Media URLs retain normal certificate validation. Live talk uses the official
   integration's existing Baichuan connection (no new HTTP request); this option
   does not change the official integration's independent connection settings.
3. **Audio** (`74c61ae`): acknowledgement latency was added to every packet's full
   playback sleep, causing cumulative timing drift. Playback now uses monotonic
   deadlines, retaining the existing four-block batching and codec/framing for
   device compatibility. A 40-block fake-clock test with 50 ms transport delay
   now completes in the audio budget (1.2825 s rather than 1.7825 s).
   Zero volume no longer falls back to full volume. Cancellation kills and reaps
   FFmpeg. Debug messages report encoding sizes and late delivery without media
   URLs or credentials. This is evidence of software defects, **not proof that
   the reported hardware crackling is eliminated**.
4. **Concurrency** (`2db9b28`): players already have per-entity locks and separate
   Host instances; there was no global playback lock. Newer reolink-aio moved
   response routing into its connection object, so the legacy sender silently
   bypassed acknowledgements. It now uses connection.send when available, with
   the older response-routing path retained. Legacy response IDs now come from
   the packet, avoiding a race when another command allocates an ID during connection. Busy/rejected TalkConfig responses
   propagate meaningful failures instead of issuing stop commands and retrying
   potentially over another stream. Local guards protect the same entry/channel
   across live and file playback; different channels remain independent. Repeated
   calls to one player queue. Entity removal cancels active and queued playback.
   Cleanup remains per channel. No global serialization or automatic sequential
   fallback is imposed. Live relay failures close the WebSocket with an error.

A device may acknowledge audio yet remain silent. Software cannot infer audible
output from acknowledgement alone. Firmware concurrency limits and direct-camera
alternatives remain unverified; a 421/422 response may also indicate a configuration
problem, so errors describe rejection as well as a possible busy stream.

## Pipeline findings

Local media-source files are read in an executor under the configured media
folder. Other sources are resolved by HA and fetched with normal verified HTTP.
FFmpeg decodes to mono signed little-endian PCM16 at the advertised sample rate,
applying software volume. The existing IMA encoder carries predictor/index across
blocks within a file. Four-byte state headers, low-nibble-first payloads and BcMedia
framing are retained. Partial final PCM is zero padded by the encoder. Four ADPCM
blocks are sent per payload, then paced against a monotonic playback deadline.
File ADPCM encoding runs in the HA executor to avoid blocking concurrent players
and the event loop. Stop is issued in finally after a successfully configured stream; logout follows.
No evidence justifies changing ADPCM layout, gain, or sample-rate negotiation yet.
The existing live codec tests use a decoder that treats headers as state, not output
samples; camera interpretation of the header and the legacy +1 sample pacing term
still need hardware confirmation. They were intentionally not changed speculatively.

## Validation

Install test tools: `python -m pip install -r requirements-dev.txt`.
Run `python -m unittest discover -s tests -v`.

Final result: **20 tests passed, no skips** with development dependencies installed.
Added `test_resources.py`, `test_ssl_options.py`, `test_audio_diagnostics.py`,
`test_sessions.py`, and `test_external_pipeline.py`. They cover read-only/storage
resource surfaces, concurrent/idempotent registration, failures, all four SSL
callbacks and default values, reload hooks, silence/sine/transient/speech-like/
doorbell-like waveforms, codec SNR, framing counts, pacing, independent channels,
same-player queues, one failure beside a success, cancellation, teardown, and reuse.
Real FFmpeg converts 48 kHz stereo WAV to 16 kHz mono and correctly mutes it.
A real local self-signed HTTPS endpoint fails with verification enabled and succeeds
with it disabled. This TLS test checks aiohttp behavior; callback tests check the
integration's selection. It is not a full HA config-flow or storage-persistence test.
Resource, option and entity tests isolate code with AST extraction/minimal surfaces;
they do not prove initialization against a complete HA installation.

`python -m compileall -q custom_components` passes.
`python -m ruff check --isolated --select F custom_components tests` passes.
The machine's inherited default Ruff rules report existing broad-exception/style
issues; they are not a clean full-project lint baseline. No type checker or HA test
environment is configured. Real startup, translated options UI and reload behavior
must be exercised in HA 2026.10.2 before release.

## Hardware acceptance procedure

1. Back up the HA configuration and install as below. Keep the existing official
   Reolink integration and all entity registrations. Confirm all NVR channels and
   live-talk buttons return after restart. Reload Reolink Talk twice and check for
   duplicate resources and entities. With YAML resources, no auto-create error
   should occur; with storage resources exactly one module entry should appear.
2. Leave verification enabled, try device discovery/volume against the untrusted
   NVR certificate, and inspect failure class in logs. Disable **Verify SSL
   certificates** in Settings > Devices & services > Reolink Talk > Configure.
   Confirm entry reload and device HTTP success; restart and confirm the saved
   option. Baichuan-only playback is not a valid certificate-verification test.
3. Play the existing WAV at fixed volume on each camera separately, then compare
   baseline/new recordings from the same microphone placement. Include silence,
   440 Hz sine at moderate amplitude, isolated transient, speech and doorbell-like
   audio. Listen for periodic clicks, compare duration, boundary artifacts and
   clipping, and confirm normal speech intelligibility. Start/interrupt/repeat ten
   times; ensure no stuck session. Also test a zero-volume playback.
4. Start the livingroom (NVR channel 1) and camera1_2 (channel 4) actions in HA
   parallel branches, each with the same local WAV. Check both physical speakers,
   service results and logs. Repeat while one camera is offline; the other must
   finish. Interrupt one stream and repeat. Test live microphone plus file playback
   on the same and different channels. Same-channel live overlap should fail
   clearly; multiple file requests on one player should queue.
5. If one NVR channel fails or is silent only during overlap, repeat through two
   independent direct camera integrations if network/PoE topology permits. This
   may require making NVR-attached cameras reachable. Do not assume support.
   Record NVR/camera models and firmware, statuses and channel numbers. Use an HA
   automation with consecutive (not parallel) play_media actions as the explicit
   sequential fallback. Each call waits for playback completion.

Temporary diagnostics in configuration.yaml:

```yaml
logger:
  logs:
    custom_components.reolink_talk: debug
```

Disable debug logging after the test. Do not share raw device API traces containing
credentials or tokens.

## Home Assistant Docker installation

These commands preserve the existing config entry and entities. Substitute your
Docker SSH host and container name; `homeassistant` below is an example. They copy
only this integration. There is no need to replace or reinstall the official one.

On this Windows checkout after the commits are complete:

```powershell
git archive --format=tar -o reolink-talk-update.tar HEAD custom_components/reolink_talk
scp reolink-talk-update.tar USER@DOCKER_HOST:/tmp/
```

On the Linux Docker host (replace the container name if needed):

```sh
container=homeassistant
backup="$HOME/reolink-talk-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup"
docker cp "$container":/config/custom_components/reolink_talk "$backup/"
staging=$(mktemp -d)
tar -xf /tmp/reolink-talk-update.tar -C "$staging"
docker stop "$container"
docker cp "$staging/custom_components/reolink_talk/." "$container":/config/custom_components/reolink_talk/
docker start "$container"
docker logs --since 5m "$container"
```

For YAML-managed resources merge this into the existing resources list; do not
replace other resources or add a duplicate `lovelace` key:

```yaml
lovelace:
  resources:
    - url: /reolink_talk/reolink-talk-button.js
      type: module
```

Restart HA after a YAML edit and hard-refresh the browser. For the trusted local
NVR's self-signed certificate, turn off verification in Reolink Talk's options
only if needed. This choice reduces connection security. There are no mandatory
migration steps, new credentials, addresses, or entity-ID changes. For rollback,
stop the container, copy the backed-up integration contents back to the same path,
and start it again. Keep the backup path from the commands above.

## Suggested independent upstream PRs

- Handle YAML-managed Lovelace resources without breaking integration startup
- Add a configurable SSL certificate verification option for device HTTP sessions
- Account for transport latency in talk playback pacing
- Propagate talk rejections and preserve concurrent channel isolation

## API references inspected

- [HA resource collections](https://github.com/home-assistant/core/blob/dev/homeassistant/components/lovelace/resources.py)
- [HA aiohttp session helper](https://github.com/home-assistant/core/blob/dev/homeassistant/helpers/aiohttp_client.py)
- [Neolink talk setup](https://github.com/thirtythreeforty/neolink/blob/master/src/talk/mod.rs)

These sources and installed reolink-aio were used to inspect API behavior; the
moving dev branches do not substitute for testing against the deployed HA version.
