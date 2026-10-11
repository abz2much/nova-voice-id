# Changelog

## 0.2.0

- Speaker recognition. Each transcript's `nova_voice_id` block now says who is
  probably speaking (`known`, `not_sure`, `unknown`), or why not (`short`,
  `no_profiles`, `error`, `timeout`). The words are unchanged and never wait
  more than 400 ms for this.
- Nova can save a recent sample to a person, delete a person or everyone,
  list profiles, and read score rows for tuning. These need the app's token.
- New options: `model`, `t_known`, `t_maybe`, `m_gap`, `min_speech_s`,
  `sure_speech_s`, `trim_start_ms`. Changing the model rebuilds profiles from
  their recordings.
- Discovery for Nova, with the address and token. The token is also shown,
  masked, in the Configuration tab.
- Speaker and VAD models are built into the image and checked against fixed
  SHA-256 hashes. Nothing is downloaded at runtime.
- Voice data is kept out of backups.
- Moved the s6 user bundle to `user-bundles.d`, which ends the s6-overlay
  deprecation warning.

## 0.1.0

- First release. A Wyoming speech to text proxy that sits in front of the
  Whisper app and passes audio straight through. It does no speaker
  recognition yet.
