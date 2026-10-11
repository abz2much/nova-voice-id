# Nova Voice ID

A Wyoming speech to text proxy for Nova. It passes your audio straight to the
Whisper app and returns Whisper's words unchanged. This version does no
speaker recognition yet.

When the app starts, its log shows the address to give Nova, for example
`tcp://0f6d2792-nova-voice-id:10350`.

## Options

### `upstream_uri`

The Wyoming address of the speech to text server that does the real work.

Default: `tcp://core-whisper:10300`, the official Whisper app.

### `log_level`

How much the app writes to its log: `debug`, `info`, `warning` or `error`.

Default: `warning`. At `info`, the app logs one line per request with how long
it took, how many seconds of audio it had, and the result.

The words you say and your audio are never logged, at any level. The
start-up lines (the address and whether Whisper answered) always show.
