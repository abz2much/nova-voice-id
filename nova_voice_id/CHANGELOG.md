# Changelog

## 0.3.0

- Learned samples. Nova can now save a sample from everyday use with
  `source: "learned"`. Learned samples are kept apart from the ones you
  recorded: at most 16 per person, never replacing a recorded one, at most 3
  a day, and only from a request that was already known as that person.
  Anything else is refused with the new error code `refused`.
- Nova can remove just a person's learned samples (or everyone's) and keep
  the recordings. Profile lists and commit replies now give `enrolled` and
  `learned` counts.
- Samples saved by 0.2.0 count as recorded (enrolled). Their files are not
  rewritten until they next change.
- An icon and a logo for the app store, and a store page.
- Rewritten README and much fuller documentation: every option, tuning,
  learning, troubleshooting and limits.

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
