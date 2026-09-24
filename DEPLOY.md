# Deploying the voice pair on a new server

Two services, one URL for MiniClosedAI to register:

| | Repo | Port | Serves |
|---|---|---|---|
| **Front** | `miniclosedai-voice` (this repo) | 8090 | English TTS (Chatterbox Turbo), Whisper ASR, WebRTC call mode, and the merged voice catalog |
| **Spanish** | [`latinavoicepod`](https://github.com/Interdata-Research/latinavoicepod) | 8088 | Spanish voices (VoxCPM2 cloning), Whisper `large-v3` Spanish ASR |

The front service proxies any voice it does not have locally to the Spanish one
(`VOICE_UPSTREAM_URL`). **Register only the front service in MiniClosedAI.**

## What the box needs

- Linux, NVIDIA driver (CUDA 11.8 / 12.x / 13.x — both setup scripts detect it),
  `python3.11+`, `git`, `ffmpeg`, `openssl`.
- **~11 GB of VRAM** for both services: Chatterbox ~3 GB + local Whisper ~1.5 GB
  here, VoxCPM2 ~4 GB + Whisper large-v3 ~3 GB + turbo ~1.6 GB there. Less if you
  run one Whisper: see *Trimming* below.
- **~25 GB of disk** for the two virtualenvs and the model weights. Put the model
  cache on the big/persistent disk with `HF_HOME`.
- CPU-only works but is far too slow for calls.

## 1. Spanish service (start with this one — the front service probes it)

```bash
git clone https://github.com/Interdata-Research/latinavoicepod.git latina_voice_tts
cd latina_voice_tts
cat > .env <<'EOF'
HF_HOME=/path/to/persistent/hf-cache     # or the weights re-download on every boot
LATINA_HOST=0.0.0.0
LATINA_PORT=8088
LATINA_OPTIMIZE=1                        # torch.compile: +70 s startup, faster synth.
                                         # Set 0 if warmup never finishes (GB10/DGX).
LATINA_ASR=1                             # /transcribe; 0 saves ~4.5 GB of VRAM
EOF
./start.sh                               # builds .venv on first run (~3 GB), then serves
```

Ready when `curl -s localhost:8088/health` reports `"ok": true` (first run also
downloads ~5 GB of weights). `curl -s localhost:8088/voices` must list your
Spanish voices. Add voices by dropping `<id>.wav` (+ optional `<id>.txt`
transcript and `<id>.json` `{"name","language","gender"}`) into `voices/`, or
through the GUI at `http://<host>:8088/studio/`.

## 2. Front service (this repo)

```bash
git clone https://github.com/Interdata-Research/miniclosedai-voice.git
cd miniclosedai-voice
./setup.sh                               # venv at ./env, torch wheel matched to the driver
VOICE_UPSTREAM_URL=http://127.0.0.1:8088 \
  VOICE_ASR_MODEL=medium.en \
  ./start.sh -d                          # background; log → /tmp/voice.log
```

Verify the pairing — this is the check that proves both halves are talking:

```bash
curl -s localhost:8090/health  | python3 -m json.tool | grep -A4 upstream
curl -s localhost:8090/voices  | python3 -m json.tool      # en (local) + es (upstream)
curl -s -X POST localhost:8090/speak -H 'Content-Type: application/json' \
     -d '{"text":"Hola, ¿en qué le puedo ayudar?","voice":"carla"}' --output es.wav
```

If `upstream.ok` is `false`, read `upstream.error`: it is the literal exception
from the probe (connection refused → the Spanish service isn't up yet or the URL
is wrong; 401 → set `VOICE_UPSTREAM_KEY` to the upstream's `LATINA_API_KEY`).

## 3. Register in MiniClosedAI

**Settings → + Add endpoint → Kind: Voice**, URL = the front service
(`http://<host>:8090`, or the public URL if it is remote) → **Test** → **Save**.
Every bot's voice picker now lists both languages. Per bot you can also set
**ASR: Auto / English / Spanish**, which decides the language the microphone is
transcribed in.

Do **not** also register the Spanish service — you would see its voices twice.
(Registering it separately instead of as an upstream is a valid layout too; you
then get Spanish TTS without call mode. Pick one.)

## What has been verified, and what has not

Tested 2026-09-24 with this repo in front of a live latina_voice_tts (front
service on CPU, upstream on an RTX A6000):

| Path | Result |
|---|---|
| `GET /voices` | merged: `en/default` local, `es/carla,es_f_19,romina` upstream; upstream's own `default` correctly shadowed by the local one |
| `GET /health` → `upstream` | `ok: true` with the upstream's voice list; a wrong URL surfaces the literal connection error |
| `POST /speak` (Spanish voice) | 200, 3.2 s `audio/wav`, **without loading the local TTS model at all** |
| `POST /speak/stream` (Spanish voice) | 17 chunks at 48 kHz, terminal `{"done": true}`, round-tripped back through `/transcribe` word for word |
| `POST /transcribe` `language=es` with `VOICE_ASR_MODEL=tiny.en` | routed upstream, answered by `whisper-large-v3` |
| `POST /transcribe` `language=en` | stayed local (`tiny.en`) |
| `TTS.synthesize_stream()` with an upstream voice — the exact call `call.py` makes | 2.56 s of Spanish audio, local engine untouched |
| Upstream returning an error | propagated as a clear `RuntimeError` naming the upstream, not a silent empty stream |

**Not yet verified:** local English synthesis through this pairing (the test box
had no disk left for the Chatterbox weights — the engine is unchanged by this
feature, but the combination is untested), and a real browser WebRTC call using
a Spanish voice. The call path's synthesis seam is covered by the last row
above; the WebRTC transport around it is not.

## Behind a proxy / on RunPod

Both services bind `0.0.0.0` and speak plain HTTP when `RUNPOD_POD_ID` is set,
because the RunPod proxy terminates TLS itself and cannot talk to an HTTPS
listener. Expose **8090** publicly; 8088 can stay private, since only the front
service talks to it. Public URLs look like
`https://<POD_ID>-8090.proxy.runpod.net`, and they change if the pod is
recreated — update the endpoint URL in MiniClosedAI when that happens.

Neither service authenticates by default. If either is reachable from the
internet, set `VOICE_API_KEY` here and `LATINA_API_KEY` there (then
`VOICE_UPSTREAM_KEY` so this service can still reach it), and put the tokens in
the endpoint's API-key field in MiniClosedAI.

## Keeping them up

Nothing restarts these on its own, and neither has a systemd unit. A one-minute
cron watchdog is enough:

```cron
* * * * * root curl -sf http://127.0.0.1:8088/health >/dev/null || (cd /path/to/latina_voice_tts && nohup ./start.sh >> latina.log 2>&1 &)
* * * * * root curl -sf http://127.0.0.1:8090/health >/dev/null || (cd /path/to/miniclosedai-voice && ./start.sh -d)
```

Allow a start-up grace period before restarting a service that is merely still
loading: latina needs ~2 minutes (torch.compile + two Whisper models) and does
not open its port until it is ready.

## Trimming

- **One Whisper instead of two.** Keep `VOICE_ASR_MODEL` English-only here and
  let Spanish route to the upstream (the default behaviour), or set
  `LATINA_ASR=0` there and `VOICE_ASR_MODEL=large-v3` here — then all ASR is
  local and nothing is forwarded.
- **Separate machines.** `VOICE_UPSTREAM_URL` can be any reachable URL; the
  services do not need to share a host. Expect the network round trip per
  synthesized sentence.
- **No Spanish?** Leave `VOICE_UPSTREAM_URL` unset and run this repo alone.
- **No calls / no English?** Run `latinavoicepod` alone and register *it* in
  MiniClosedAI; it implements the same backend contract minus `/call/*`.
