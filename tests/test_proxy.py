"""Tests for the proxy, against a fake upstream. No Home Assistant needed."""

import asyncio
import logging
import socket

from conftest import (
    CHANNELS,
    RATE,
    WIDTH,
    Client,
    FakeEngine,
    FakeUpstream,
    ProxyServer,
    chunk,
    transcribe,
    words_for,
)
from wyoming.asr import Transcribe
from wyoming.audio import AudioStart, AudioStop
from wyoming.info import Describe, Info

from nova_voice_id.health import answers_describe
from nova_voice_id.upstream import DescribeCache

# 1.6 s of audio, enough for the fake engine to make an embedding.
AUDIO = [bytes([n]) * 6400 for n in range(1, 9)]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_audio_reaches_upstream_byte_for_byte(upstream: FakeUpstream, proxy: ProxyServer):
    await transcribe(proxy.port, AUDIO)

    events = upstream.connections[-1].events
    assert [e.type for e in events] == (
        ["transcribe", "audio-start"] + ["audio-chunk"] * len(AUDIO) + ["audio-stop"]
    )
    chunks = [e for e in events if e.type == "audio-chunk"]
    assert [c.payload for c in chunks] == AUDIO


async def test_first_chunk_arrives_before_audio_stop(upstream: FakeUpstream, proxy: ProxyServer):
    client = await Client.connect(proxy.port)
    await client.send(Transcribe(language="en").event())
    await client.send(AudioStart(rate=RATE, width=WIDTH, channels=CHANNELS).event())
    await client.send(chunk(AUDIO[0]))

    # The client has not sent AudioStop, so the chunk must already be upstream.
    await asyncio.wait_for(upstream.connected.wait(), timeout=2)
    await asyncio.wait_for(upstream.connections[-1].first_chunk.wait(), timeout=2)

    await client.send(AudioStop().event())
    reply = await client.read()
    await client.close()
    assert reply is not None and reply.type == "transcript"
    assert not upstream.connections[-1].stop_seen_before_first_chunk


async def test_transcript_unchanged_plus_result_block(upstream: FakeUpstream, proxy: ProxyServer):
    reply = await transcribe(proxy.port, AUDIO, language="fr")

    assert reply.type == "transcript"
    assert set(reply.data) == {"text", "language", "nova_voice_id"}
    assert reply.data["text"] == words_for(b"".join(AUDIO))
    assert reply.data["language"] == "fr"
    block = reply.data["nova_voice_id"]
    assert block["v"] == 1
    assert block["status"] == "no_profiles"
    assert block["model"] == "fake_model"


async def test_two_requests_in_parallel_get_their_own_transcript(
    upstream: FakeUpstream, proxy: ProxyServer
):
    # The fake upstream answers no one until both requests are open, so this
    # only passes if the two run at the same time.
    upstream.wait_for_connections = 2
    audio_a = [b"\xaa" * 640] * 5
    audio_b = [b"\xbb" * 640] * 5

    reply_a, reply_b = await asyncio.gather(
        transcribe(proxy.port, audio_a), transcribe(proxy.port, audio_b)
    )

    assert reply_a.data["text"] == words_for(b"".join(audio_a))
    assert reply_b.data["text"] == words_for(b"".join(audio_b))


async def test_upstream_error_is_forwarded(upstream: FakeUpstream, proxy: ProxyServer):
    upstream.reply_error = True
    reply = await transcribe(proxy.port, AUDIO)

    assert reply is not None
    assert reply.type == "error"
    assert reply.data == {"text": "boom", "code": "test_error"}


async def test_upstream_closed_early_sends_error(upstream: FakeUpstream, proxy: ProxyServer):
    upstream.close_after_audio_start = True
    client = await Client.connect(proxy.port)
    await client.send(Transcribe(language="en").event())
    await client.send(AudioStart(rate=RATE, width=WIDTH, channels=CHANNELS).event())

    reply = await client.read()
    assert reply is not None
    assert reply.type == "error"
    assert reply.data["code"] == "upstream_unavailable"
    # Then the proxy closes the connection.
    assert await client.read() is None


async def test_upstream_down_sends_error_and_server_keeps_serving(make_voiceid):
    voiceid = make_voiceid(FakeEngine())
    await voiceid.start()
    proxy = ProxyServer(f"tcp://127.0.0.1:{free_port()}", voiceid)
    await proxy.start()
    try:
        reply = await transcribe(proxy.port, AUDIO)
        assert reply is not None
        assert reply.type == "error"
        assert reply.data["code"] == "upstream_unavailable"

        # The server still answers new connections.
        reply = await transcribe(proxy.port, AUDIO)
        assert reply is not None and reply.data["code"] == "upstream_unavailable"

        client = await Client.connect(proxy.port)
        await client.send(Describe().event())
        info = Info.from_event(await client.read())
        await client.close()
        assert info.asr[0].name == "Nova Voice ID"
        assert info.asr[0].models == []
    finally:
        await proxy.stop()


async def test_describe_uses_upstream_languages_under_our_name(
    upstream: FakeUpstream, proxy: ProxyServer
):
    client = await Client.connect(proxy.port)
    await client.send(Describe().event())
    info = Info.from_event(await client.read())
    await client.close()

    program = info.asr[0]
    assert program.name == "Nova Voice ID"
    assert [m.name for m in program.models] == ["base-int8"]
    assert program.models[0].languages == ["en", "fr", "de"]
    # Settings HA reads from the first program come from the upstream.
    assert program.requires_external_vad is False


async def test_describe_is_cached_for_60_seconds(upstream: FakeUpstream):
    now = [1000.0]
    cache = DescribeCache(upstream.uri, clock=lambda: now[0])

    await cache.get()
    now[0] += 59
    await cache.get()
    assert upstream.describes == 1

    now[0] += 2
    await cache.get()
    assert upstream.describes == 2


async def test_no_transcript_text_in_logs(upstream: FakeUpstream, proxy: ProxyServer, caplog):
    caplog.set_level(logging.DEBUG)
    reply = await transcribe(proxy.port, AUDIO)
    upstream.reply_error = True
    await transcribe(proxy.port, AUDIO)

    text = reply.data["text"]
    assert text.startswith("words-")  # the test really had words to look for
    assert text not in caplog.text
    assert text[len("words-") :] not in caplog.text
    assert "Request:" in caplog.text  # and there were log lines to look at


async def test_health_check(proxy: ProxyServer):
    assert await answers_describe(f"tcp://127.0.0.1:{proxy.port}")
    assert not await answers_describe(f"tcp://127.0.0.1:{free_port()}")


async def test_second_request_on_same_connection(upstream: FakeUpstream, proxy: ProxyServer):
    client = await Client.connect(proxy.port)
    for audio in ([b"\x11" * 640], [b"\x22" * 640]):
        # The second request skips transcribe and starts with audio-start.
        if audio[0][0] == 0x11:
            await client.send(Transcribe(language="en").event())
        await client.send(AudioStart(rate=RATE, width=WIDTH, channels=CHANNELS).event())
        await client.send(chunk(audio[0]))
        await client.send(AudioStop().event())
        reply = await client.read()
        assert reply.data["text"] == words_for(audio[0])
    await client.close()
