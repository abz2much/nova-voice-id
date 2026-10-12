# Nova Voice ID

<img src="nova_voice_id/logo.png" alt="Nova Voice ID" width="250">

A Home Assistant app (formerly called an add-on) for
[Nova](https://github.com/abz2much/NOVA), the Home Assistant assistant.

## What it is

A speech to text app for Nova that also recognises who is speaking. You talk to
your voice satellite as usual. Whisper turns your speech into words, and this
app adds a quiet note saying whose voice it probably was.

## Why

When Nova knows who is talking, it can:

- answer the right person, by name;
- remember things for the right person ("remember that I like tea");
- tell the AI who it is talking to, so replies fit that person.

## How it works

```
voice satellite → Home Assistant → Nova → Nova Voice ID → Whisper
                                             (who is it?)   (what was said?)
```

The app sits between Nova and the official Whisper app. It passes your audio
straight to Whisper, piece by piece, as it arrives. Whisper's words come back
**exactly as Whisper wrote them**. While Whisper works, the app checks the
voice. If that takes too long, the words go back anyway, so the voice check
never slows you down.

## What it will never do

- **A voice is a hint, never a key.** Nothing here unlocks a door, opens a
  garage or disarms an alarm because of a voice. Nova still asks for a tap on
  your phone for those.
- **Unknown voices are never blocked.** Guests and anyone not enrolled are
  still heard, and their words still come back.
- **Voice data never leaves your machine.** Voice prints and recordings stay
  inside the app and are never included in Home Assistant backups. The app
  never logs your words, your audio or its token.

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
   `Nova Voice ID 0.3.0 listening on tcp://0f6d2792-nova-voice-id:10350`,
   and a line saying it reached Whisper.

The app's port is internal only. It is never opened to your network.

## Setting it up with Nova

1. **Connect.** Nova finds the app through Home Assistant discovery and fills
   in its address and token. In Nova, open **Settings → Voice & Speakers →
   Voice ID**: the status should say **Connected**. If it doesn't, paste the
   address from the app's log and the token from the app's **Configuration**
   tab (click the eye icon).
2. **Use it for speech to text.** In Home Assistant, open **Settings → Voice
   assistants**, pick your assistant, and set its speech to text to **Nova
   Voice ID**. In Nova's Voice ID card, pick a fallback (usually Whisper), so
   voice keeps working if the app is ever stopped.
3. **Record a voice.** In the Voice ID card, pick a satellite under **Record
   on** and press **Record voice** next to a person. Say the wake word, then
   the sentence Nova asks for. After 8 samples, Nova says it's done. Record
   each person on each satellite, because rooms sound different.
4. **Check the scores, then turn it on.** Nova's **Logs** tab shows a VOICE
   line for every voice command. Once each person's scores stay clearly apart
   (see Tuning in [DOCS.md](nova_voice_id/DOCS.md)), turn on **Use voice to
   tell who is speaking** in the Voice ID card.
5. **Learning (optional).** When your version of Nova supports it, Nova can
   add a few samples from everyday use, but only when the app already knew the
   voice. Learned samples are kept apart from your recordings, never replace
   them, and can be removed on their own.

## More

- [DOCS.md](nova_voice_id/DOCS.md): every option, tuning, learning, where voice
  data lives, troubleshooting, limits and licences.
- [docs/protocol.md](docs/protocol.md): the result block and the management
  events, for anyone writing a client.

## Licence

MIT. See [LICENSE](LICENSE). The voice models and the logo font have their own
licences, listed in [DOCS.md](nova_voice_id/DOCS.md#licences).
