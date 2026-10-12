"""Management events, storage, the token, pending samples and timing."""

import asyncio
import logging
import stat
import time
from pathlib import Path

import pytest
from conftest import (
    TOKEN,
    FakeEngine,
    FakeUpstream,
    ProxyServer,
    manage,
    transcribe,
    tree,
    words_for,
)

from nova_voice_id import auth
from nova_voice_id.pending import PendingStore
from nova_voice_id.profiles import ENROLLED, MAX_PER_KIND
from nova_voice_id.voiceid import RESULT_WAIT_S

LONG = b"\x01\x02" * 24000  # 1.5 s: enough speech for the fake engine after the trim


def clip(n: int) -> bytes:
    """A distinct 1.5 s clip, so each gives a different embedding."""
    return bytes([n % 251 + 1, n // 251 + 1]) * 24000


async def new_sample(port: int, n: int = 0) -> str:
    reply = await transcribe(port, clip(n))
    return reply.data["nova_voice_id"]["sample_id"]


async def commit(port: int, sample_id: str, person: str = "person.alice"):
    return await manage(port, "nvid-enrol-commit", token=TOKEN, sample_id=sample_id, person=person)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


async def test_commit_then_delete_removes_files_and_rows(proxy: ProxyServer, data_dir: Path):
    sample_id = await new_sample(proxy.port)
    reply = await commit(proxy.port, sample_id)
    assert reply.type == "nvid-enrol-commit-result"
    assert reply.data == {"person": "person.alice", "samples": 1, "enrolled": 1, "learned": 0}

    profile = data_dir / "profiles" / "person.alice.npz"
    wav = data_dir / "recordings" / "person.alice" / f"{sample_id}.wav"
    assert profile.exists() and wav.exists()
    assert mode(profile) == 0o600 and mode(wav) == 0o600
    assert mode(data_dir / "scores.db") == 0o600

    # A request that scores against alice adds a score row naming her.
    await transcribe(proxy.port, clip(1))
    scores = proxy.voiceid.scores
    await proxy.voiceid.storage(lambda: None)  # let queued row writes finish
    assert await proxy.voiceid.storage(scores.count) >= 1

    listed = await manage(proxy.port, "nvid-profile-list", token=TOKEN)
    assert listed.data["profiles"][0]["person"] == "person.alice"
    assert listed.data["profiles"][0]["samples"] == 1

    reply = await manage(proxy.port, "nvid-profile-delete", token=TOKEN, person="person.alice")
    assert reply.type == "nvid-profile-delete-result"
    assert reply.data["deleted"] == ["person.alice"]
    assert not profile.exists()
    assert not (data_dir / "recordings" / "person.alice").exists()
    rows, _ = await proxy.voiceid.storage(scores.rows, 30)
    assert not [r for r in rows if "person.alice" in (r["person"], r["second"])]
    assert proxy.voiceid.profiles.all() == []


async def test_delete_all(proxy: ProxyServer, data_dir: Path):
    await commit(proxy.port, await new_sample(proxy.port, 1), "person.alice")
    await commit(proxy.port, await new_sample(proxy.port, 2), "person.bob")
    await new_sample(proxy.port, 3)  # a pending sample

    reply = await manage(proxy.port, "nvid-profile-delete", token=TOKEN, all=True)
    assert sorted(reply.data["deleted"]) == ["person.alice", "person.bob"]
    assert list((data_dir / "profiles").iterdir()) == []
    assert list((data_dir / "recordings").iterdir()) == []
    assert len(proxy.voiceid.pending) == 0
    assert await proxy.voiceid.storage(proxy.voiceid.scores.count) == 0


@pytest.mark.parametrize(
    "event_type, data",
    [
        ("nvid-enrol-commit", {"sample_id": "x", "person": "person.alice"}),
        ("nvid-profile-delete", {"person": "person.alice"}),
        ("nvid-profile-delete", {"all": True}),
        ("nvid-profile-list", {}),
        ("nvid-stats", {"days": 7}),
    ],
)
@pytest.mark.parametrize("token", [None, "", "not-the-token-xyz", TOKEN[:-1] + "c", 12345])
async def test_bad_token_is_rejected_and_changes_nothing(
    proxy: ProxyServer, data_dir: Path, caplog, event_type, data, token
):
    await commit(proxy.port, await new_sample(proxy.port), "person.alice")
    before = tree(data_dir)
    caplog.set_level(logging.DEBUG)

    sent = dict(data)
    if token is not None:
        sent["token"] = token
    reply = await manage(proxy.port, event_type, **sent)

    assert reply.type == "error"
    assert reply.data["code"] == "unauthorised"
    assert tree(data_dir) == before
    assert "missing or wrong token" in caplog.text
    assert TOKEN not in caplog.text
    if isinstance(token, str) and token:
        assert token not in caplog.text


@pytest.mark.parametrize(
    "person",
    [
        "alice",
        "person.",
        "person.Alice",
        "person.../x",
        "person.a/../../etc",
        "person.a b",
        "../person.a",
        "person.a\n",
        "person." + "a" * 200,
        None,
        7,
    ],
)
async def test_bad_person_is_rejected_and_changes_nothing(
    proxy: ProxyServer, data_dir: Path, person
):
    sample_id = await new_sample(proxy.port)
    await commit(proxy.port, await new_sample(proxy.port, 5), "person.alice")
    before = tree(data_dir)

    reply = await commit(proxy.port, sample_id, person)
    assert reply.type == "error" and reply.data["code"] == "bad_request"
    reply = await manage(proxy.port, "nvid-profile-delete", token=TOKEN, person=person)
    assert reply.type == "error" and reply.data["code"] == "bad_request"

    assert tree(data_dir) == before
    assert proxy.voiceid.pending.get(sample_id) is not None  # still there to commit


async def test_unknown_or_expired_sample_is_not_found(proxy: ProxyServer):
    reply = await commit(proxy.port, "nosuchsample")
    assert reply.type == "error" and reply.data["code"] == "not_found"


async def test_committing_twice_fails_the_second_time(proxy: ProxyServer):
    sample_id = await new_sample(proxy.port)
    assert (await commit(proxy.port, sample_id)).type == "nvid-enrol-commit-result"
    reply = await commit(proxy.port, sample_id)
    assert reply.type == "error" and reply.data["code"] == "not_found"


async def test_24_enrolled_cap_replaces_the_oldest_enrolled(proxy: ProxyServer, data_dir: Path):
    ids = []
    for n in range(MAX_PER_KIND[ENROLLED] + 2):
        sample_id = await new_sample(proxy.port, n)
        ids.append(sample_id)
        reply = await commit(proxy.port, sample_id)
        assert reply.data["samples"] == min(n + 1, MAX_PER_KIND[ENROLLED])
        await asyncio.sleep(0.001)  # distinct commit times

    profile = proxy.voiceid.profiles.get("person.alice")
    assert list(profile.sample_ids) == ids[2:]
    wavs = sorted(p.stem for p in (data_dir / "recordings" / "person.alice").iterdir())
    assert wavs == sorted(ids[2:])


async def test_stats_rows(proxy: ProxyServer):
    await transcribe(proxy.port, LONG)
    await proxy.voiceid.storage(lambda: None)
    reply = await manage(proxy.port, "nvid-stats", token=TOKEN, days=1)
    assert reply.type == "nvid-stats-result"
    row = reply.data["rows"][0]
    assert set(row) == {
        "time",
        "status",
        "person",
        "score",
        "second",
        "second_score",
        "margin",
        "speech_s",
        "model",
    }
    assert row["status"] == "no_profiles"
    bad = await manage(proxy.port, "nvid-stats", token=TOKEN, days="seven")
    assert bad.type == "error" and bad.data["code"] == "bad_request"


def test_pending_samples_expire_after_10_minutes_and_cap_at_50():
    now = [0.0]
    store = PendingStore(clock=lambda: now[0])
    first = store.add(None, b"", "m", frozenset())
    now[0] = 599.0
    assert store.get(first) is not None
    now[0] = 601.0
    assert store.get(first) is None

    ids = [store.add(None, b"", "m", frozenset()) for _ in range(60)]
    assert len(store) == 50
    assert store.get(ids[9]) is None  # oldest dropped first
    assert store.get(ids[10]) is not None


async def test_expired_pending_sample_cannot_be_committed(proxy: ProxyServer):
    now = [time.monotonic()]
    proxy.voiceid.pending._clock = lambda: now[0]
    sample_id = await new_sample(proxy.port)
    now[0] += 601
    reply = await commit(proxy.port, sample_id)
    assert reply.type == "error" and reply.data["code"] == "not_found"


def test_token_file_is_private_and_kept(tmp_path: Path):
    path = tmp_path / "token"
    token = auth.load_or_create(path)
    assert len(bytes.fromhex(token)) == 32
    assert mode(path) == 0o600
    assert auth.load_or_create(path) == token
    assert auth.matches(token, token)
    assert not auth.matches(token.upper(), token)
    assert not auth.matches(None, token)


async def test_slow_embedding_gives_timeout_and_words_on_time(
    proxy: ProxyServer, fake_engine: FakeEngine, upstream: FakeUpstream
):
    fake_engine.delay = 2.0
    upstream.decode_delay = 0.05
    started = time.monotonic()
    reply = await transcribe(proxy.port, LONG)
    received = time.monotonic()

    assert reply.data["text"] == words_for(LONG)
    block = reply.data["nova_voice_id"]
    assert block["status"] == "timeout"
    assert "sample_id" not in block and "score" not in block
    whisper_at = upstream.connections[-1].transcript_sent_at
    assert received - whisper_at < RESULT_WAIT_S + 0.15, received - whisper_at
    assert received - started < 1.5  # never waited for the 2 s embedding


async def test_crashing_embedding_gives_error_and_words(
    proxy: ProxyServer, fake_engine: FakeEngine
):
    fake_engine.fail = True
    reply = await transcribe(proxy.port, LONG)
    assert reply.data["text"] == words_for(LONG)
    assert reply.data["nova_voice_id"]["status"] == "error"

    fake_engine.fail = False  # and the next request is fine
    reply = await transcribe(proxy.port, LONG)
    assert reply.data["nova_voice_id"]["status"] == "no_profiles"


async def test_slow_embedding_does_not_hold_up_other_rooms(
    proxy: ProxyServer, fake_engine: FakeEngine
):
    # Each embedding takes 0.25 s. Run side by side, both are back inside the
    # 0.4 s window. With one worker, the second would need 0.5 s and time out.
    fake_engine.delay = 0.25
    replies = await asyncio.gather(transcribe(proxy.port, clip(1)), transcribe(proxy.port, clip(2)))
    assert [r.data["nova_voice_id"]["status"] for r in replies] == ["no_profiles"] * 2
