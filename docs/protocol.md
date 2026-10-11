# Nova Voice ID protocol

For anyone writing a client, such as Nova. Everything runs over the app's
Wyoming connection (`tcp://<host>:10350`).

## Speech to text

Exactly as any Wyoming speech to text server: `transcribe`, `audio-start`,
`audio-chunk`…, `audio-stop`, then a `transcript`. The words are Whisper's,
unchanged. The `transcript` event's data has one extra key:

```json
"nova_voice_id": {
  "v": 1,
  "status": "known",
  "person": "person.x",
  "score": 0.71,
  "second": "person.y",
  "second_score": 0.48,
  "margin": 0.23,
  "speech_s": 2.4,
  "sample_id": "random id",
  "model": "wespeaker_en_voxceleb_resnet34_LM",
  "ms": 85
}
```

| status | Meaning |
|---|---|
| `known` | Score ≥ `t_known`, margin over the second best ≥ `m_gap`, and at least `sure_speech_s` of speech |
| `not_sure` | Score ≥ `t_maybe`, but one of the `known` checks failed |
| `unknown` | Score below `t_maybe` |
| `short` | Less than `min_speech_s` of speech. No embedding was made |
| `no_profiles` | Nobody is enrolled |
| `error` | The speaker step failed. The words still come back |
| `timeout` | The speaker step took more than 400 ms after Whisper's words arrived |

- `person` is only there when the status is `known`.
- `score` is there when scores were computed. `second`, `second_score` and
  `margin` are there when at least two people are enrolled.
- `speech_s` is there when the speech was measured.
- `sample_id` is there whenever an embedding was made. The sample is kept in
  memory for 10 minutes (at most 50 samples) so it can be committed.
- `ms` is how long the speaker step took.

## Management events

Each needs `token` in its data. A missing or wrong token gets an `error`
event with code `unauthorised`. `person` must match `^person\.[a-z0-9_]+$`.

Replies are `<event type>-result`, or an `error` event with one of these
codes: `unauthorised`, `bad_request`, `not_found`, `failed`.

### nvid-enrol-commit

Data: `token`, `sample_id`, `person`. Saves that pending sample to the
person: its embedding to the profile, its audio as a WAV. A person keeps at
most 24 samples; past that the oldest is replaced.

Reply `nvid-enrol-commit-result`: `{"person": "person.x", "samples": 3}`.
`not_found` if the sample expired, was already committed, or was made with
another model.

### nvid-profile-delete

Data: `token`, and `person` or `"all": true`. Removes the profile file,
recordings, pending samples that scored best or second best against that
person, and score log rows naming them. The reply comes once they're gone
from disk.

Reply `nvid-profile-delete-result`: `{"deleted": ["person.x"], "all": false}`.

### nvid-profile-list

Data: `token`.

Reply `nvid-profile-list-result`:
`{"profiles": [{"person": "person.x", "samples": 3, "updated": 1760000000.0}], "model": "…"}`.
`updated` is a Unix time.

### nvid-stats

Data: `token`, `days` (default 7, at most 30).

Reply `nvid-stats-result`: `{"days": 7, "rows": [...], "truncated": false}`.
Each row has `time`, `status`, `person` (the best match, even when not
known), `score`, `second`, `second_score`, `margin`, `speech_s`, `model`.
Newest first, at most 5,000 rows.

## Discovery

On start the app sends Supervisor discovery for service `nova` with
`{"uri": "tcp://<hostname>:10350", "token": "<token>"}`.
