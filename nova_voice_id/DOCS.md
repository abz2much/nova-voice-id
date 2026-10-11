# Nova Voice ID

A Wyoming speech to text proxy for Nova. It passes your audio straight to the
Whisper app and returns Whisper's words unchanged. Alongside the words it says
who is probably speaking: known, not sure, or unknown. It only learns a voice
when Nova tells it to.

When the app starts, its log shows its address, for example
`tcp://0f6d2792-nova-voice-id:10350`. Nova normally finds the app on its own
through Home Assistant discovery.

## Options

### `upstream_uri`

The Wyoming address of the speech to text server that does the real work.
Default: `tcp://core-whisper:10300`, the official Whisper app.

### `log_level`

How much the app writes to its log: `debug`, `info`, `warning` or `error`.
Default: `warning`. At `info`, the app logs one line per request with how long
it took, how many seconds of audio it had, and the speaker status (for example
`speaker known`), never a name.

The words you say, your audio and the token are never logged, at any level.

### `model`

The voice model. Default: `wespeaker_en_voxceleb_resnet34_LM`. The others are
`wespeaker_en_voxceleb_CAM++_LM` and `nemo_en_titanet_small`. When you change
it, the app rebuilds every voice profile from its saved recordings at the next
start, and logs how many samples it redid.

### `t_known`, `t_maybe`, `m_gap`

How sure the app must be. Each is a number from 0 to 1.

- `t_known` (default 0.55): a score at least this high can be **known**.
- `t_maybe` (default 0.40): a score at least this high, but not good enough
  for known, is **not sure**. Lower is **unknown**.
- `m_gap` (default 0.10): for known, the best match must beat the second best
  by at least this much.

These are starting guesses. They will be tuned from real scores.

### `min_speech_s`, `sure_speech_s`

Seconds of actual speech, not counting silence.

- `min_speech_s` (default 0.8): with less speech than this, the app doesn't
  try and says **short**.
- `sure_speech_s` (default 1.2): known needs at least this much speech.

### `trim_start_ms`

Milliseconds cut from the start of each recording before the voice is checked,
to skip the wake sound and the end of the wake word. Default 300.

### `token`

You don't set this. The app fills it in when it starts, so you can see it:
click the eye icon. Nova needs it to save and delete voices. Discovery gives it
to Nova automatically; you only need to copy it if you set Nova up by hand.
If you change it, the app puts the real token back at the next start.

## Where voice data lives

Everything is in the app's own data folder, readable only by the app:

- `profiles/` holds one file per person with their voice prints.
- `recordings/` holds the short recordings each voice print came from
  (at most 24 per person), so profiles can be rebuilt for another model.
- `scores.db` keeps one row per request for 30 days (time, status, names
  and scores, never audio or words), for tuning.
- `token` is the key Nova uses.

Recent samples Nova hasn't saved yet are kept in memory only, for 10 minutes,
and never written to disk.

**Voice data is never included in backups.** After restoring a backup, enrol
voices again. The token is in backups, so Nova keeps working.

To delete a person's voice, use Nova. It removes their profile, recordings
and score rows.

## Models and licences

The image includes these models, downloaded unchanged and checked against
fixed SHA-256 hashes. Full details and licence texts are in
`/usr/src/models/MODEL_LICENCES` inside the app.

- **ResNet34-LM** and **CAM++-LM** speaker models by the
  [WeSpeaker](https://github.com/wenet-e2e/wespeaker) project, trained on
  VoxCeleb, under
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
- **TitaNet-S** speaker model by NVIDIA (NeMo), under the Apache License 2.0.
- **Silero VAD** by the Silero Team, under the MIT licence.

The ONNX files come from
[csukuangfj/speaker-embedding-models](https://huggingface.co/csukuangfj/speaker-embedding-models)
and the [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) releases.
