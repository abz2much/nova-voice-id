"""What must never reach the logs."""

import logging

from conftest import TOKEN, ProxyServer, load_pcm, manage, transcribe
from test_speaker import PEOPLE, enrol


async def test_no_words_names_scores_or_token_in_logs(real_proxy: ProxyServer, caplog):
    caplog.set_level(logging.DEBUG)
    await enrol(real_proxy.port, "a")
    await enrol(real_proxy.port, "b")

    words, blocks = [], []
    for clip in ("voice_a_test_1", "voice_b_test_1", "voice_c_test_1", "voice_a_short"):
        reply = await transcribe(real_proxy.port, load_pcm(clip))
        words.append(reply.data["text"])
        blocks.append(reply.data["nova_voice_id"])
    await manage(real_proxy.port, "nvid-profile-list", token=TOKEN)
    await manage(real_proxy.port, "nvid-stats", token=TOKEN, days=1)
    await manage(real_proxy.port, "nvid-profile-list", token="not-the-token-xyz")
    await manage(real_proxy.port, "nvid-profile-delete", token=TOKEN, person=PEOPLE["a"])

    text = caplog.text
    assert "Request:" in text and "speaker known" in text  # there were lines to check
    assert TOKEN not in text
    for spoken in words:
        assert spoken not in text
    for block in blocks:
        for key in ("score", "second_score", "margin"):
            if key in block:
                assert str(block[key]) not in text
        if "sample_id" in block:
            assert block["sample_id"] not in text
    # No names at info or above, so never a name next to a score.
    info_and_up = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.INFO)
    for person in PEOPLE.values():
        assert person not in info_and_up
