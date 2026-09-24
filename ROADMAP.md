# Notes for the next version

Written 2026-09-24, when `upstream.py` (Spanish via a paired
[latina_voice_tts](https://github.com/edantonio505/latinavoicepod)) landed. This
is the context a future session needs before changing that integration: what is
deliberately missing, what will break if touched carelessly, and what is worth
building next.

## The shape of the thing

Everything that produces audio — `/speak`, `/speak/stream`, and the WebRTC call
handler in `call.py` — goes through **`tts.synthesize_stream(text, voice_id,
language, speed)`**, a blocking generator of `(pcm16_bytes, sample_rate)`.
`upstream.py` plugs into that single method, which is why call mode got Spanish
without touching `call.py` at all. **Keep that property.** A change that makes
any path synthesize some other way (a new endpoint calling the model directly, a
batch mode, a cache layer) must route through the same seam or Spanish silently
stops working on that path only.

Two related invariants:

- **Local voices win an id clash.** `tts.synthesize_stream` checks
  `self._wav_for(voice_id)` before `upstream.owns(voice_id)`. Both services ship
  a `default`; reversing that order would make every install start speaking the
  upstream's English clip.
- **The upstream is never fatal.** `catalog()` swallows every exception and
  returns the last good catalog. Anything new that talks to the upstream should
  do the same: a dead Spanish service must degrade to an English-only service,
  never to a 500.

## Known limitations

| Limitation | Why it is that way | Where to start |
|---|---|---|
| One upstream only | `VOICE_UPSTREAM_URL` is a single URL; two Spanish services or a French one need a list, per-upstream catalogs and id→upstream routing | `upstream.py` module state is a single `_cache`; make it a dict keyed by URL |
| Upstream voices can't be cloned from the Voice Studio GUI | `POST /voices` / `DELETE /voices/{id}` write to the local `voices/` dir only. Uploading a Spanish clip here produces a voice Chatterbox will read, not VoxCPM2 | `server.py` upload handler; would need to forward multipart to the upstream's `/api/voices/upload` and mark the row |
| Sample-rate mixing | Local Chatterbox emits 22 050 Hz, the upstream 48 000 Hz. Every frame carries its own `sample_rate` and both MiniClosedAI and FastRTC honour it, but nothing resamples, so a single utterance must not mix engines | `upstream.synthesize_stream` yields the upstream's rate verbatim — keep it that way |
| Spanish call latency has an extra hop | Call audio goes browser → here → upstream → back. Measured ~0.1–0.3 s to first chunk locally; a remote upstream adds its RTT per sentence | consider co-locating, or a persistent HTTP/2 connection instead of a new `requests` POST per sentence |
| ASR routing is a heuristic | Non-English + local model is `*.en` + upstream configured → forward. A multilingual local model never forwards, even when the upstream's `large-v3` is better | `ASR._route_upstream` in `asr.py`; an explicit `VOICE_ASR_ROUTE=upstream\|local\|auto` would be clearer |
| No auth between the two services by default | `VOICE_UPSTREAM_KEY` exists but is empty; anyone who can reach 8088 can use the GPU | set `LATINA_API_KEY` upstream and the key here; consider binding the upstream to loopback |
| `_call_config` is still one global | Pre-existing: one active call per instance, unrelated to this feature | `server.py` `_call_config`; scope per WebRTC session id |

## Worth building next

1. **A pairing self-test.** `./test.sh` covers the local engine. Add a mode that,
   with `VOICE_UPSTREAM_URL` set, asserts: the merged catalog contains both
   languages, an upstream voice synthesizes through `/speak/stream` with a
   terminal `done` frame, and a round trip through `/transcribe` returns the
   words. That is the check a new deployment actually needs, and it is what
   DEPLOY.md currently asks people to do by hand.
2. **Health-driven catalog invalidation.** The catalog is time-cached (30 s), so
   a voice added upstream takes up to 30 s to appear and a dead upstream's voices
   linger that long. A `POST /voices/refresh` (or honouring the upstream's
   `loaded_at`) would make the GUI feel immediate.
3. **Per-language default voices.** `_DEFAULT_VOICES` in `server.py` still names
   Piper ids (`es_MX-claude-high`) that no longer exist anywhere — dead config
   that only survives because callers always pass a voice. Derive the default
   from the merged catalog instead.
4. **Streaming `/transcribe`.** Both services buffer the whole clip. For call
   mode the VAD already knows when the utterance ended, so the win is small; for
   long push-to-talk clips it is real.
5. **One install script for the pair.** DEPLOY.md is two clones and two setup
   scripts. A `pair-up.sh` that does both, writes the env files, and runs the
   verification above would remove most of the deployment surface.

## If you are replacing the Spanish engine

`upstream.py` talks the *miniclosedai voice-backend contract*, not anything
VoxCPM2-specific: `GET /voices` keyed by language, `POST /speak/stream` emitting
`{chunk_b64, sample_rate}` frames and a terminal frame with `done` (or `end`),
`POST /transcribe` returning `{text}`. Any service speaking that contract can be
the upstream — including a second copy of this repo. The latina-specific details
live entirely in that repo.
