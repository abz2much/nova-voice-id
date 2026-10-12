"""Learned samples: kept apart from enrolled ones, with their own limits."""

import logging
from pathlib import Path

import numpy as np
import pytest
from conftest import TOKEN, FakeEngine, ProxyServer, load_pcm, manage, transcribe, tree

from nova_voice_id.profiles import (
    DAY_S,
    ENROLLED,
    LEARNED,
    MAX_PER_KIND,
    ProfileStore,
)
from nova_voice_id.speaker import KNOWN, NOT_SURE, unit
from nova_voice_id.voiceid import VoiceId

ALICE, BOB = "person.alice", "person.bob"
PCM = b"\x01\x02" * 24000  # 1.5 s
_counter = [0]


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(proxy: ProxyServer) -> Clock:
    clock = Clock()
    proxy.voiceid.profiles._clock = clock
    return clock


def pending(proxy: ProxyServer, status: str = KNOWN, person: str | None = ALICE) -> str:
    """A pending sample with a chosen result, as a request would leave it."""
    _counter[0] += 1
    vector = unit(np.random.default_rng(_counter[0]).standard_normal(8).astype(np.float32))
    return proxy.voiceid.pending.add(
        vector,
        PCM,
        proxy.voiceid.model,
        frozenset({person} if person else ()),
        status,
        person if status == KNOWN else None,
    )


async def commit(proxy: ProxyServer, sample_id: str, person: str = ALICE, source=None):
    data = {"token": TOKEN, "sample_id": sample_id, "person": person}
    if source is not None:
        data["source"] = source
    return await manage(proxy.port, "nvid-enrol-commit", **data)


async def enrol(proxy: ProxyServer, count: int, person: str = ALICE) -> list[str]:
    ids = []
    for _ in range(count):
        sample_id = pending(proxy, NOT_SURE, None)
        reply = await commit(proxy, sample_id, person)
        assert reply.type == "nvid-enrol-commit-result", reply.data
        ids.append(sample_id)
    return ids


async def learn(proxy: ProxyServer, person: str = ALICE):
    return await commit(proxy, pending(proxy, KNOWN, person), person, "learned")


def kinds_on_disk(data_dir: Path, person: str = ALICE) -> list[str]:
    with np.load(data_dir / "profiles" / f"{person}.npz") as data:
        return [str(s) for s in data["sources"]]


def wavs(data_dir: Path, person: str = ALICE) -> set[str]:
    folder = data_dir / "recordings" / person
    return {p.stem for p in folder.iterdir()} if folder.exists() else set()


# Refusals ---------------------------------------------------------------


@pytest.mark.parametrize(
    "status, known_as",
    [(NOT_SURE, ALICE), (KNOWN, BOB), ("unknown", None), ("no_profiles", None)],
)
async def test_learned_refused_unless_known_for_that_person(
    proxy: ProxyServer, clock, data_dir: Path, status, known_as
):
    await enrol(proxy, 3)
    before = tree(data_dir)
    sample_id = pending(proxy, status, known_as)

    reply = await commit(proxy, sample_id, ALICE, "learned")

    assert reply.type == "error" and reply.data["code"] == "refused"
    assert tree(data_dir) == before
    assert proxy.voiceid.pending.get(sample_id) is not None  # still there


async def test_learned_refused_with_fewer_than_3_enrolled(
    proxy: ProxyServer, clock, data_dir: Path
):
    await enrol(proxy, 2)
    before = tree(data_dir)
    reply = await learn(proxy)
    assert reply.type == "error" and reply.data["code"] == "refused"
    assert tree(data_dir) == before


async def test_learned_refused_with_no_profile(proxy: ProxyServer, clock, data_dir: Path):
    reply = await learn(proxy)
    assert reply.type == "error" and reply.data["code"] == "refused"
    assert not (data_dir / "profiles" / f"{ALICE}.npz").exists()
    assert wavs(data_dir) == set()


async def test_only_3_learned_in_24_hours(proxy: ProxyServer, clock: Clock, data_dir: Path):
    await enrol(proxy, 3)
    for n in range(3):
        clock.now += 3600
        reply = await learn(proxy)
        assert reply.type == "nvid-enrol-commit-result", reply.data
        assert reply.data["learned"] == n + 1

    before = tree(data_dir)
    clock.now += 3600
    reply = await learn(proxy)
    assert reply.type == "error" and reply.data["code"] == "refused"
    assert tree(data_dir) == before

    # 24 hours after the first learned sample, one more is allowed.
    clock.now += DAY_S - 3 * 3600
    assert (await learn(proxy)).type == "nvid-enrol-commit-result"


async def test_24_hour_limit_survives_a_restart(
    proxy: ProxyServer, clock: Clock, data_dir: Path, make_voiceid
):
    await enrol(proxy, 3)
    for _ in range(3):
        assert (await learn(proxy)).type == "nvid-enrol-commit-result"

    restarted: VoiceId = make_voiceid(FakeEngine())
    restarted.profiles._clock = clock
    await restarted.start()
    server = ProxyServer(proxy._upstream_uri, restarted)
    await server.start()
    try:
        reply = await commit(server, pending(server), ALICE, "learned")
        assert reply.type == "error" and reply.data["code"] == "refused"
        clock.now += DAY_S + 1
        reply = await commit(server, pending(server), ALICE, "learned")
        assert reply.type == "nvid-enrol-commit-result"
    finally:
        await server.stop()


async def test_bad_source_is_bad_request(proxy: ProxyServer, data_dir: Path):
    await enrol(proxy, 3)
    before = tree(data_dir)
    for source in ("nova", "LEARNED", "", 1, None):
        if source is None:
            continue
        reply = await commit(proxy, pending(proxy), ALICE, source)
        assert reply.type == "error" and reply.data["code"] == "bad_request", source
    assert tree(data_dir) == before


# Caps -------------------------------------------------------------------


async def test_learned_cap_replaces_oldest_learned_never_enrolled(
    proxy: ProxyServer, clock: Clock, data_dir: Path
):
    enrolled = await enrol(proxy, MAX_PER_KIND[ENROLLED])
    learned = []
    for _ in range(MAX_PER_KIND[LEARNED] + 3):
        clock.now += DAY_S / 2  # stay under 3 a day
        sample_id = pending(proxy)
        reply = await commit(proxy, sample_id, ALICE, "learned")
        assert reply.type == "nvid-enrol-commit-result", reply.data
        learned.append(sample_id)

    profile = proxy.voiceid.profiles.get(ALICE)
    assert profile.count(ENROLLED) == MAX_PER_KIND[ENROLLED]
    assert profile.count(LEARNED) == MAX_PER_KIND[LEARNED]
    kept_learned = [
        i for i, k in zip(profile.sample_ids, profile.kinds, strict=True) if k == LEARNED
    ]
    assert kept_learned == learned[3:]  # the 3 oldest learned went
    kept_enrolled = [
        i for i, k in zip(profile.sample_ids, profile.kinds, strict=True) if k == ENROLLED
    ]
    assert kept_enrolled == enrolled  # every enrolled sample is still there
    assert wavs(data_dir) == set(enrolled) | set(learned[3:])
    assert reply.data == {
        "person": ALICE,
        "samples": MAX_PER_KIND[ENROLLED] + MAX_PER_KIND[LEARNED],
        "enrolled": MAX_PER_KIND[ENROLLED],
        "learned": MAX_PER_KIND[LEARNED],
    }


async def test_enrolled_cap_never_removes_learned(proxy: ProxyServer, clock: Clock, data_dir: Path):
    await enrol(proxy, 3)
    learned = []
    for _ in range(2):
        sample_id = pending(proxy)
        await commit(proxy, sample_id, ALICE, "learned")
        learned.append(sample_id)
    clock.now += 1
    await enrol(proxy, MAX_PER_KIND[ENROLLED] + 2)

    profile = proxy.voiceid.profiles.get(ALICE)
    assert profile.count(ENROLLED) == MAX_PER_KIND[ENROLLED]
    assert [
        i for i, k in zip(profile.sample_ids, profile.kinds, strict=True) if k == LEARNED
    ] == learned


# Migration, delete, list ---------------------------------------------


def test_existing_nova_samples_load_as_enrolled_without_a_rewrite(tmp_path: Path):
    profiles = tmp_path / "profiles"
    profiles.mkdir(parents=True)
    path = profiles / f"{ALICE}.npz"
    np.savez(
        path,
        embeddings=np.eye(3, 8, dtype=np.float32),
        sample_ids=np.array(["s1", "s2", "s3"], dtype=str),
        created=np.array([1.0, 2.0, 3.0]),
        sources=np.array(["nova", "nova", "nova"], dtype=str),
        model=np.array("fake_model"),
    )
    before = path.read_bytes()

    store = ProfileStore(tmp_path)
    store.load()

    profile = store.get(ALICE)
    assert profile.kinds == (ENROLLED, ENROLLED, ENROLLED)
    assert profile.count(ENROLLED) == 3 and profile.count(LEARNED) == 0
    assert path.read_bytes() == before  # not rewritten just for loading


async def test_delete_learned_keeps_profile_and_enrolled(
    proxy: ProxyServer, clock: Clock, data_dir: Path
):
    enrolled = await enrol(proxy, 3)
    for _ in range(2):
        await learn(proxy)
    await enrol(proxy, 3, BOB)
    bob_before = (data_dir / "profiles" / f"{BOB}.npz").read_bytes()

    reply = await manage(
        proxy.port, "nvid-profile-delete", token=TOKEN, person=ALICE, source="learned"
    )

    assert reply.type == "nvid-profile-delete-result"
    assert reply.data == {"deleted": [ALICE], "all": False, "source": "learned", "removed": 2}
    assert kinds_on_disk(data_dir) == [ENROLLED] * 3
    assert wavs(data_dir) == set(enrolled)
    assert proxy.voiceid.profiles.get(ALICE).sample_ids == tuple(enrolled)
    assert (data_dir / "profiles" / f"{BOB}.npz").read_bytes() == bob_before


async def test_delete_learned_for_everyone(proxy: ProxyServer, clock: Clock, data_dir: Path):
    alice = await enrol(proxy, 3)
    bob = await enrol(proxy, 3, BOB)
    await learn(proxy)
    await learn(proxy, BOB)
    await learn(proxy, BOB)

    reply = await manage(proxy.port, "nvid-profile-delete", token=TOKEN, all=True, source="learned")
    assert reply.data == {"deleted": [ALICE, BOB], "all": True, "source": "learned", "removed": 3}
    assert wavs(data_dir) == set(alice)
    assert wavs(data_dir, BOB) == set(bob)


async def test_delete_with_bad_source_is_bad_request(proxy: ProxyServer, data_dir: Path):
    await enrol(proxy, 3)
    before = tree(data_dir)
    reply = await manage(
        proxy.port, "nvid-profile-delete", token=TOKEN, person=ALICE, source="enrolled"
    )
    assert reply.type == "error" and reply.data["code"] == "bad_request"
    assert tree(data_dir) == before


async def test_list_gives_counts_per_kind(proxy: ProxyServer, clock: Clock):
    await enrol(proxy, 4)
    await learn(proxy)
    await enrol(proxy, 3, BOB)
    reply = await manage(proxy.port, "nvid-profile-list", token=TOKEN)
    rows = {row["person"]: row for row in reply.data["profiles"]}
    assert {k: rows[ALICE][k] for k in ("samples", "enrolled", "learned")} == {
        "samples": 5,
        "enrolled": 4,
        "learned": 1,
    }
    assert {k: rows[BOB][k] for k in ("samples", "enrolled", "learned")} == {
        "samples": 3,
        "enrolled": 3,
        "learned": 0,
    }


async def test_model_change_keeps_each_kind(
    proxy: ProxyServer, clock: Clock, data_dir: Path, make_voiceid, caplog
):
    await enrol(proxy, 3)
    await learn(proxy)
    caplog.set_level(logging.INFO)

    changed: VoiceId = make_voiceid(FakeEngine(model="other_model"))
    await changed.start()

    profile = changed.profiles.get(ALICE)
    assert profile.model == "other_model"
    assert profile.kinds == (ENROLLED, ENROLLED, ENROLLED, LEARNED)
    assert kinds_on_disk(data_dir) == [ENROLLED, ENROLLED, ENROLLED, LEARNED]
    assert "Re-embedded 4 samples" in caplog.text


# With the real models -------------------------------------------------


async def test_learning_from_a_real_known_voice(real_proxy: ProxyServer):
    for n in (1, 2, 3):
        reply = await transcribe(real_proxy.port, load_pcm(f"voice_a_enrol_{n}"))
        await commit(real_proxy, reply.data["nova_voice_id"]["sample_id"], ALICE)

    other = await transcribe(real_proxy.port, load_pcm("voice_b_test_1"))
    assert other.data["nova_voice_id"]["status"] != "known"
    refused = await commit(real_proxy, other.data["nova_voice_id"]["sample_id"], ALICE, "learned")
    assert refused.type == "error" and refused.data["code"] == "refused"

    own = await transcribe(real_proxy.port, load_pcm("voice_a_test_1"))
    block = own.data["nova_voice_id"]
    assert block["status"] == "known" and block["person"] == ALICE
    saved = await commit(real_proxy, block["sample_id"], ALICE, "learned")
    assert saved.data == {"person": ALICE, "samples": 4, "enrolled": 3, "learned": 1}
