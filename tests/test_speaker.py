"""Speaker recognition with the real models and the voice fixtures."""

import asyncio
import logging
from pathlib import Path

import numpy as np
import pytest
from conftest import (
    MODELS_DIR,
    TOKEN,
    FakeUpstream,
    ProxyServer,
    load_pcm,
    manage,
    split,
    transcribe,
    words_for,
)

from nova_voice_id.speaker import MODELS, Engine
from nova_voice_id.voiceid import VoiceId

PEOPLE = {"a": "person.alice", "b": "person.bob"}


async def enrol(port: int, voice: str) -> None:
    for n in (1, 2, 3):
        reply = await transcribe(port, load_pcm(f"voice_{voice}_enrol_{n}"))
        block = reply.data["nova_voice_id"]
        assert "sample_id" in block, block
        sample_id = block["sample_id"]
        result = await manage(
            port, "nvid-enrol-commit", token=TOKEN, sample_id=sample_id, person=PEOPLE[voice]
        )
        assert result.type == "nvid-enrol-commit-result"
        assert result.data["samples"] == n


async def test_each_voice_is_known_as_the_right_person(real_proxy: ProxyServer):
    await enrol(real_proxy.port, "a")
    await enrol(real_proxy.port, "b")

    for voice in ("a", "b"):
        for n in (1, 2):
            pcm = load_pcm(f"voice_{voice}_test_{n}")
            reply = await transcribe(real_proxy.port, pcm)
            assert reply.data["text"] == words_for(pcm)
            block = reply.data["nova_voice_id"]
            assert block["status"] == "known", (voice, n, block)
            assert block["person"] == PEOPLE[voice]
            assert block["model"] == MODELS[0]
            assert set(block) == {
                "v",
                "status",
                "person",
                "score",
                "second",
                "second_score",
                "margin",
                "speech_s",
                "sample_id",
                "model",
                "ms",
            }


async def test_never_known_as_the_wrong_person(real_proxy: ProxyServer):
    await enrol(real_proxy.port, "a")
    await enrol(real_proxy.port, "b")

    clips = [f"voice_{v}_test_{n}" for v in "abc" for n in (1, 2)]
    for clip in clips:
        block = (await transcribe(real_proxy.port, load_pcm(clip))).data["nova_voice_id"]
        if block["status"] == "known":
            voice = clip.split("_")[1]
            assert voice in PEOPLE, f"{clip} (not enrolled) came back known"
            assert block["person"] == PEOPLE[voice], clip
        else:
            assert "person" not in block


async def test_short_clip_is_short_with_no_embedding(real_proxy: ProxyServer):
    block = (await transcribe(real_proxy.port, load_pcm("voice_a_short"))).data["nova_voice_id"]
    assert block["status"] == "short"
    assert block["speech_s"] < 0.8
    assert "sample_id" not in block
    assert "score" not in block
    assert len(real_proxy.voiceid.pending) == 0


async def test_no_profiles(real_proxy: ProxyServer):
    block = (await transcribe(real_proxy.port, load_pcm("voice_a_test_1"))).data["nova_voice_id"]
    assert block["status"] == "no_profiles"
    assert "sample_id" in block  # so Nova can enrol the first person
    assert "score" not in block and "person" not in block


async def test_two_voices_at_once_never_swap(real_proxy: ProxyServer, upstream: FakeUpstream):
    await enrol(real_proxy.port, "a")
    await enrol(real_proxy.port, "b")

    for n in (1, 2):
        # Neither is answered until both are open, so they run at the same time.
        upstream.answer_after(2)
        pcm_a, pcm_b = load_pcm(f"voice_a_test_{n}"), load_pcm(f"voice_b_test_{n}")
        reply_a, reply_b = await asyncio.gather(
            transcribe(real_proxy.port, pcm_a), transcribe(real_proxy.port, pcm_b)
        )
        assert reply_a.data["text"] == words_for(pcm_a)
        assert reply_b.data["text"] == words_for(pcm_b)
        block_a, block_b = reply_a.data["nova_voice_id"], reply_b.data["nova_voice_id"]
        assert block_a.get("person") == PEOPLE["a"], block_a
        assert block_b.get("person") == PEOPLE["b"], block_b


async def test_audio_path_unchanged_with_real_models(
    real_proxy: ProxyServer, upstream: FakeUpstream
):
    pcm = load_pcm("voice_a_test_1")
    await transcribe(real_proxy.port, pcm)
    conn = upstream.connections[-1]
    sent = [e.payload for e in conn.events if e.type == "audio-chunk"]
    assert sent == split(pcm)
    assert not conn.stop_seen_before_first_chunk


async def test_model_change_re_embeds_from_recordings(
    real_proxy: ProxyServer, make_voiceid, data_dir: Path, caplog
):
    await enrol(real_proxy.port, "a")
    old = real_proxy.voiceid.profiles.get(PEOPLE["a"])
    assert old.model == MODELS[0] and old.embeddings.shape == (3, 256)

    caplog.set_level(logging.INFO)
    new_model = "nemo_en_titanet_small"
    voiceid: VoiceId = make_voiceid(Engine(MODELS_DIR, new_model))
    await voiceid.start()

    profile = voiceid.profiles.get(PEOPLE["a"])
    assert profile.model == new_model
    assert profile.embeddings.shape == (3, 192)
    assert profile.sample_ids == old.sample_ids
    assert "Re-embedded 3 samples from recordings for 1 profiles" in caplog.text
    with np.load(data_dir / "profiles" / f"{PEOPLE['a']}.npz") as saved:
        assert str(saved["model"]) == new_model


@pytest.mark.parametrize("model", MODELS)
async def test_every_model_loads_and_embeds(need_models, model):
    engine = Engine(MODELS_DIR, model)
    try:
        vector = await engine.run(engine.embed_recording, load_pcm("voice_a_test_1"))
        assert vector is not None
        assert abs(float(np.linalg.norm(vector)) - 1.0) < 1e-4
    finally:
        engine.close()
