# Nova Voice ID

A Wyoming speech to text proxy for Nova. It passes your audio straight to the
Whisper app and returns Whisper's words unchanged. Alongside the words it says
who is probably speaking: known, not sure, or unknown. It only learns a voice
when Nova tells it to.

When the app starts, its log shows its address, for example
`tcp://0f6d2792-nova-voice-id:10350`. Nova normally finds the app on its own
through Home Assistant discovery.

## Options

You set these in the app's **Configuration** tab. Restart the app after a
change.

### `upstream_uri`

The Wyoming address of the speech to text server that does the real work.
Default: `tcp://core-whisper:10300`, the official Whisper app.

**When to change it:** only if your Whisper runs somewhere else, for example
on another machine.

### `log_level`

How much the app writes to its log: `debug`, `info`, `warning` or `error`.
Default: `warning`. At `info`, the app logs one line per request with how long
it took, how many seconds of audio it had, and the speaker status (for example
`speaker known`), never a name.

The words you say, your audio and the token are never logged, at any level.

**When to change it:** set `info` for a while if you want to see each request,
or `debug` when you're chasing a problem. Go back to `warning` after.

### `model`

The voice model. Default: `wespeaker_en_voxceleb_resnet34_LM`. The others are
`wespeaker_en_voxceleb_CAM++_LM` and `nemo_en_titanet_small`. When you change
it, the app rebuilds every voice profile from its saved recordings at the next
start, and logs how many samples it redid. Each sample keeps its kind
(enrolled or learned).

**When to change it:** usually never. `nemo_en_titanet_small` is several times
faster, which may help on slow hardware such as a Raspberry Pi, if many
requests come back `timeout`. Scores differ between models, so tune the
thresholds again after a change. In our tests `wespeaker_en_voxceleb_CAM++_LM`
told voices apart less well than the other two.

### `t_known`, `t_maybe`, `m_gap`

How sure the app must be. Each is a number from 0 to 1.

- `t_known` (default 0.55): a score at least this high can be **known**.
- `t_maybe` (default 0.40): a score at least this high, but not good enough
  for known, is **not sure**. Lower is **unknown**.
- `m_gap` (default 0.10): for known, the best match must beat the second best
  by at least this much.

These are starting guesses. Tune them from your own scores (see **Tuning**).

**When to change them:** once you have a few days of VOICE lines in Nova's
Logs tab. Raise `t_known` if someone else is ever shown as known. Raise
`m_gap` if two people get mixed up.

### `min_speech_s`, `sure_speech_s`

Seconds of actual speech, not counting silence.

- `min_speech_s` (default 0.8): with less speech than this, the app doesn't
  try and says **short**.
- `sure_speech_s` (default 1.2): known needs at least this much speech.

**When to change them:** rarely. Very short commands ("lights off") are
naturally `short` or `not_sure`. Lowering these makes short commands guess
more, and guess wrong more often.

### `trim_start_ms`

Milliseconds cut from the start of each recording before the voice is checked,
to skip the wake sound and the end of the wake word. Default 300.

**When to change it:** if your satellite's wake sound is longer, raise it a
little. If most requests are `short`, check this isn't cutting off speech.

### `token`

You don't set this. The app fills it in when it starts, so you can see it:
click the eye icon. Nova needs it to save and delete voices. Discovery gives it
to Nova automatically; you only need to copy it if you set Nova up by hand.
If you change it, the app puts the real token back at the next start.

## Tuning

### What a score is

For each request, the app makes a voice print of what you said and compares it
with every saved sample of each person. A person's **score** is the average of
their 3 closest samples. It runs from about 0 (nothing alike) to 1 (the same).
Your own voice usually scores well above other people's, but the exact numbers
depend on the model, the room and the microphone.

### What the statuses mean

| Status | Meaning |
|---|---|
| `known` | The best score is at least `t_known`, it beats the second best by at least `m_gap`, and there was at least `sure_speech_s` of speech |
| `not_sure` | The best score is at least `t_maybe`, but one of the checks for known failed |
| `unknown` | The best score is below `t_maybe`: probably someone not enrolled |
| `short` | Less than `min_speech_s` of speech, so the app didn't try |
| `no_profiles` | Nobody is enrolled yet |
| `error` | The voice check failed. The words still came back |
| `timeout` | The voice check took more than 400 ms after Whisper's words. The words came back on time without it |

### Reading Nova's VOICE lines

Nova's **Logs** tab has one VOICE line per voice command, for example:

```
status=not_sure person=- score=0.61 second=person.bob margin=0.18 speech_s=2.10 ms=96 device=…
```

- `status`: the app's answer, from the table above.
- `person`: only filled in when the status is `known`.
- `score`: the best match's score. `second` is who came second, and `margin`
  is how far ahead the best match was.
- `speech_s`: seconds of speech the app heard. `ms`: how long the check took.

Your words are never in these lines.

### A worked example

Say you look at a week of VOICE lines and see:

- your own commands score between **0.73 and 0.86**;
- a visitor, who isn't enrolled, scored **0.58** against you once.

The default `t_known` of 0.55 would have called the visitor **you**. Set
`t_known` between the two, with room on both sides, for example **0.65**.
Now the visitor comes back `not_sure`, and you are still `known`.

### Start strict, loosen slowly

It's better for Nova to say "not sure" than to name the wrong person. Start
with `t_known` high, so that only clear matches count. Lower it in small steps
(about 0.02 at a time) only while every `known` line still names the right
person. Recording each person on every satellite helps more than lowering
thresholds.

## Learning

Learning needs a version of Nova that supports it.

**What it adds.** Your recorded samples come from one sitting. Voices change
with the time of day, a cold or the room. Learning lets Nova add a few samples
from everyday commands, so the app keeps up. Nova decides when to ask; the
app decides whether to accept.

**The limits.** The app accepts a learned sample only when:

- that same request was already `known` as that same person;
- the person has at least 3 recorded samples;
- fewer than 3 learned samples were saved for them in the last 24 hours.

Each person keeps at most **16 learned** samples. Past that, a new one
replaces the oldest learned one. Learned samples are kept apart from your
recordings: they never replace or push out a sample you recorded (at most 24
per person).

**Removing learned samples.** The app can remove just a person's learned
samples, or everyone's, and keep the recordings. Nova does this for you once
your version of Nova supports learning. If the app starts naming the wrong
person after learning, remove the learned samples first.

## Where voice data lives

Everything is in the app's own data folder, readable only by the app:

- `profiles/` holds one file per person with their voice prints.
- `recordings/` holds the short recordings each voice print came from
  (at most 24 recorded and 16 learned per person), so profiles can be rebuilt
  for another model.
- `scores.db` keeps one row per request for 30 days (time, status, names
  and scores, never audio or words), for tuning.
- `token` is the key Nova uses.

Recent samples Nova hasn't saved yet are kept in memory only, for 10 minutes,
and never written to disk.

**Voice data is never included in backups.** `profiles/`, `recordings/` and
`scores.db` are left out. After restoring a backup, enrol voices again. The
token is in backups, so Nova keeps working.

**To delete a person's voice**, use **Delete voice** in Nova's Voice ID card.
It removes their profile, recordings and score rows. **To delete everything**,
use **Delete all voice data** there.

## Troubleshooting

**Nova says it can't reach the app.** Check the app is started and its log
shows `listening on tcp://…`. Compare that address with the one in Nova's
Voice ID card. The first part of the name depends on the repository address
you added, so copy it from the log rather than typing it.

**Every request comes back `short`.** Look at `speech_s` in the VOICE lines.
If it is always tiny, check that `trim_start_ms` isn't too high, and that the
satellite doesn't stop listening too early. Very short commands are normally
`short`; longer ones shouldn't be.

**Everything is `not_sure`.** Compare the scores with `t_known`, the margins
with `m_gap`, and `speech_s` with `sure_speech_s`: one of them is failing.
Record more samples on the satellite you use most. Then, if needed, lower
`t_known` a little (see **Tuning**).

**Someone else shows as `known`.** Raise `t_known` above the scores you see
for the wrong person, and raise `m_gap` if two enrolled people get mixed up.
If the person who was recorded was the wrong one, delete their voice in Nova
and record again. If it started after learning, remove learned samples.

**Nova says the token doesn't match.** Open this app's **Configuration** tab,
click the eye icon on **Token**, and copy it into Nova's Voice ID card. The
app's token file is the real copy; the field just shows it.

## Limits

- **One model at a time.** All profiles use the model you chose. Changing it
  rebuilds them from the recordings.
- **Accuracy drops** with distance from the microphone, background noise, a
  cold or a sore throat, and whispering. The words are not affected.
- **What the default model learned from.** The default model was trained on
  VoxCeleb2: speech from 5,994 speakers in online videos. Voices very unlike
  those, such as young children, may score less reliably.
- A voice is a hint, never a key. Nothing in the app can unlock, open or
  disarm anything.

## Licences

The app's own code and artwork are MIT licensed.

### Models

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

### Font

The logo's text is set in **Nunito**, Copyright 2014 The Nunito Project
Authors, under the [SIL Open Font License 1.1](http://scripts.sil.org/OFL).
Only the rendered logo is included; the font itself is not.
