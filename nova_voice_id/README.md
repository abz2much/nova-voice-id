# Nova Voice ID

Speech to text for the Nova assistant that also recognises who is speaking.

It passes what you say to the Whisper app and gives back Whisper's words,
unchanged and without delay. Alongside the words, it tells Nova whose voice it
probably heard, so Nova can answer the right person.

## What you need

- [Nova](https://github.com/abz2much/NOVA), the Home Assistant assistant.
- The official **Whisper** app, installed and running.

## What stays private

- Your voice prints and recordings stay inside this app, on your own
  machine. They are never sent anywhere and never included in backups.
- The app never logs your words, your audio or its token.
- A voice is only a hint. It never unlocks, opens or disarms anything.

## Learn more

Read the full documentation in the **Documentation** tab, or on
[GitHub](https://github.com/abz2much/nova-voice-id).
