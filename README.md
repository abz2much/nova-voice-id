# Nova Voice ID

A Home Assistant app (formerly called an add-on) for
[Nova](https://github.com/abz2much/NOVA).

It is a Wyoming speech to text proxy. It sits in front of the official Whisper
app, passes your audio straight through to it, and returns Whisper's words
unchanged. It also works out who is speaking.

**New in 0.2.0:** the app starts recognising voices. The words still come
back exactly as Whisper gives them, and alongside them the app says who is
probably speaking: known, not sure, or unknown. It learns a voice only when
Nova tells it to, and deletes it on request. Voice data stays in the app and is
never included in backups.

## What you need

- Nova, with its Nova Voice ID speech to text entity.
- The official Whisper app, installed and running.
- Home Assistant OS or Supervised, so you can install apps. The app runs on
  amd64 and aarch64.

## Install

1. In Home Assistant, go to **Settings → Apps → App store**.
2. Open the menu (top right) and choose **Repositories**.
3. Add `https://github.com/abz2much/nova-voice-id` and close the dialog.
4. Find **Nova Voice ID** in the store, install it, and start it.
5. Open the app's **Log** tab. It shows the app's address, like
   `Nova Voice ID 0.2.0 listening on tcp://0f6d2792-nova-voice-id:10350`,
   and a line saying it reached Whisper. Nova finds the app through
   discovery.

The app's port is internal only. It is never opened to your network.

See [DOCS.md](nova_voice_id/DOCS.md) for the options, where voice data lives,
and the models' licences. [docs/protocol.md](docs/protocol.md) describes the
result block and the management events, for client authors.

## Licence

MIT. See [LICENSE](LICENSE).
