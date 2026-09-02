#!/usr/bin/env python3
"""Adversarial tests for the chat surface. Run: python3 tests/test_chatlogic.py

Every case here is something a stranger in chat can actually send.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

import chatlogic  # noqa: E402
import common  # noqa: E402

MODS = ("mod1",)


def fresh():
    return {"seq": 0, "items": []}, chatlogic.new_state()


def say(queue, state, user, text, now=1000.0, name="someone"):
    return chatlogic.handle(
        {"user_id": user, "username": name, "text": text}, queue, state, MODS, now
    )


def test_url_rejects_everything_that_is_not_a_youtube_video():
    hostile = [
        "file:///etc/passwd",
        "http://127.0.0.1:8770/api/skip",
        "http://169.254.169.254/latest/meta-data/",   # cloud metadata
        "https://evil.example.com/watch?v=AAAAAAAAAAA",
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/@somechannel",
        "https://www.youtube.com/watch?v=short",       # id too short
        "https://www.youtube.com/watch?v=AAAAAAAAAAAA",  # id too long
        "not a url at all",
        "https://www.youtube.com/watch?v=AAAAAAAAAAA\nhttps://evil.com",
        "-rf",
        "--config-location=/tmp/evil",
        "https://youtu.be/AAAAAAAAAAA/../../etc",
        "x" * 400,
    ]
    for candidate in hostile:
        video_id, _ = common.canonical_youtube_url(candidate)
        assert video_id is None, f"accepted hostile input: {candidate!r}"


def test_url_accepts_the_real_shapes_and_always_rebuilds_https():
    good = [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&t=42",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
        "dQw4w9WgXcQ",
    ]
    for candidate in good:
        video_id, url = common.canonical_youtube_url(candidate)
        assert video_id == "dQw4w9WgXcQ", candidate
        # the playlist in the second case must not survive canonicalisation
        assert url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ", url

    # an id beginning with a dash must never come back out as a bare argument
    video_id, url = common.canonical_youtube_url("-ve3gv07vIQ")
    assert video_id == "-ve3gv07vIQ"
    assert url.startswith("https://"), url


def test_one_user_cannot_flood():
    queue, state = fresh()
    reply, changed = say(queue, state, "u1", "!play dQw4w9WgXcQ", now=1000)
    assert changed and reply == "ajoute"
    # a second add inside the cooldown is refused
    reply, changed = say(queue, state, "u1", "!play AAAAAAAAAAA", now=1010)
    assert not changed and "attends" in reply
    # and is allowed once the cooldown has passed
    reply, changed = say(queue, state, "u1", "!play AAAAAAAAAAA", now=1100)
    assert changed, reply


def test_one_user_cannot_hold_the_whole_queue():
    queue, state = fresh()
    now = 1000.0
    for n in range(common.MAX_PENDING_PER_USER):
        _, changed = say(queue, state, "u1", f"!play {'a' * 10}{n}", now=now)
        assert changed
        now += common.ADD_COOLDOWN_SECONDS + 1
    reply, changed = say(queue, state, "u1", "!play bbbbbbbbbb9", now=now)
    assert not changed and "attente" in reply


def test_duplicates_are_refused():
    queue, state = fresh()
    say(queue, state, "u1", "!play dQw4w9WgXcQ", now=1000)
    reply, changed = say(queue, state, "u2", "!play https://youtu.be/dQw4w9WgXcQ", now=1000)
    assert not changed and reply == "deja dans la file"


def test_a_vote_counts_once_per_user():
    queue, state = fresh()
    say(queue, state, "u1", "!play dQw4w9WgXcQ", now=1000)
    item = queue["items"][0]
    for _ in range(10):
        say(queue, state, "u2", f"!vote {item['id']}", now=1000)
    assert item["votes"] == ["u2"], item["votes"]


def test_vote_argument_is_not_trusted():
    queue, state = fresh()
    say(queue, state, "u1", "!play dQw4w9WgXcQ", now=1000)
    for junk in ("!vote abc", "!vote " + "9" * 40, "!vote -1", "!vote 99999", "!vote"):
        _, changed = say(queue, state, "u2", junk, now=1000)
        assert not changed, junk


def test_skip_needs_several_distinct_people():
    queue, state = fresh()
    # the same person shouting does not move the counter past one
    for _ in range(5):
        reply, _ = say(queue, state, "u1", "!skip", now=2000)
    assert reply != "passe", reply
    assert len(state["skip_votes"]) == 1

    for n, user in enumerate(("u2", "u3")):
        reply, _ = say(queue, state, user, "!skip", now=2000 + n)
    assert reply == "passe", reply


def test_skip_cannot_be_chained():
    queue, state = fresh()
    for user in ("u1", "u2", "u3"):
        reply, _ = say(queue, state, user, "!skip", now=2000)
    assert reply == "passe"
    # immediately afterwards the cooldown swallows a second attempt
    for user in ("u1", "u2", "u3"):
        reply, changed = say(queue, state, user, "!skip", now=2005)
        assert reply != "passe" and not changed


def test_stale_skip_votes_expire():
    queue, state = fresh()
    say(queue, state, "u1", "!skip", now=2000)
    say(queue, state, "u2", "!skip", now=2000)
    # the third vote arrives long after the window, so the first two are gone
    late = 2000 + common.SKIP_WINDOW_SECONDS + 10
    reply, _ = say(queue, state, "u3", "!skip", now=late)
    assert reply != "passe", reply
    assert len(state["skip_votes"]) == 1


def test_moderator_powers_are_not_available_to_everyone():
    queue, state = fresh()
    reply, changed = say(queue, state, "randomuser", "!ban u9", now=1000)
    assert not changed and reply is None
    reply, changed = say(queue, state, "mod1", "!ban u9", now=1000)
    assert changed and "u9" in state["banned"]
    # a banned user is then ignored entirely
    _, changed = say(queue, state, "u9", "!play dQw4w9WgXcQ", now=1000)
    assert not changed
    # and a moderator skips alone
    reply, _ = say(queue, state, "mod1", "!skip", now=1000)
    assert reply == "passe"


def test_noise_is_ignored_in_silence():
    queue, state = fresh()
    for junk in ("hello", "", "!", "!unknown thing", "!!!!", " !play x"):
        reply, changed = say(queue, state, "u1", junk, now=1000)
        assert not changed, junk
    # an oversized message is dropped without being parsed
    _, changed = say(queue, state, "u1", "!play " + "a" * 6000, now=1000)
    assert not changed


def test_control_characters_never_survive():
    queue, state = fresh()
    say(queue, state, "u1", "!play dQw4w9WgXcQ", now=1000, name="ev\x00il\x1b[31m")
    stored = queue["items"][0]["by_name"]
    assert all(ord(c) >= 0x20 for c in stored), repr(stored)


def test_queue_file_stays_bounded():
    queue, state = fresh()
    queue["items"] = [{"id": n, "status": "played", "by": "u", "url": ""}
                      for n in range(common.MAX_QUEUE + 50)]
    queue["seq"] = len(queue["items"])
    say(queue, state, "u1", "!play dQw4w9WgXcQ", now=1000)
    assert len(queue["items"]) <= common.MAX_QUEUE + 1, len(queue["items"])


def test_most_voted_plays_first():
    queue, state = fresh()
    say(queue, state, "u1", "!play dQw4w9WgXcQ", now=1000)
    say(queue, state, "u2", "!play AAAAAAAAAAA", now=1000)
    second = queue["items"][1]
    say(queue, state, "u3", f"!vote {second['id']}", now=1000)
    assert chatlogic.playback_order(queue)[0]["id"] == second["id"]


def test_a_real_bus_envelope_is_unwrapped():
    """Regression: the payload sits under "data", not at the top level.

    Reading the envelope as the payload finds no sender, and the command is then
    dropped without a word. The chat looks connected and simply never answers,
    which is exactly how this shipped once.
    """
    import chat

    envelope = {
        "id": "01M1J4Z2EMK3VM0VY8RE104SGD",
        "type": "chat.message.sent",
        "version": "1",
        "broadcaster": "127469285",
        "received_at": "2026-09-02T22:48:49Z",
        "data": {
            "message_id": "52f6ac83-0668-4282-bd73-e358c0a64db7",
            "content": "!help",
            "sender": {"user_id": 127469285, "username": "CX247_CX"},
            "broadcaster": {"user_id": 127469285, "username": "CX247_CX"},
        },
    }
    message = chat.as_message(chat.unwrap(envelope))
    assert message["user_id"] == 127469285, message
    assert message["text"] == "!help", message

    queue, state = fresh()
    reply, _ = chatlogic.handle(message, queue, state, MODS, 1000.0)
    assert reply == chatlogic.HELP, reply

    # a bare payload, with no envelope, must keep working too
    assert chat.as_message(chat.unwrap(envelope["data"]))["text"] == "!help"


def _with_allowlist(content):
    """Point the allowlist at a temporary file holding exactly `content`."""
    import tempfile
    path = pathlib.Path(tempfile.mkdtemp()) / "allowed_channels.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    common.ALLOWLIST = path
    return path


def test_allowlist_is_closed_by_default():
    real = common.ALLOWLIST
    try:
        # missing file, empty file, malformed json, wrong shape: all allow nothing
        for content in (None, "", "{", '{"channels": []}', '{"channels": null}', "{}"):
            _with_allowlist(content)
            assert common.load_allowlist() == {}, repr(content)
            assert not common.channel_allowed("UC" + "a" * 22), repr(content)
    finally:
        common.ALLOWLIST = real


def test_allowlist_only_admits_what_is_listed():
    real = common.ALLOWLIST
    try:
        good = "UC" + "a" * 22
        other = "UC" + "b" * 22
        _with_allowlist(json.dumps({"channels": {good: "an approved channel"}}))
        assert common.channel_allowed(good)
        assert not common.channel_allowed(other)
        # and an empty or absent id never slips through
        for junk in ("", None, "UC", "not-an-id", good + "extra"):
            assert not common.channel_allowed(junk), repr(junk)
    finally:
        common.ALLOWLIST = real


def test_allowlist_ignores_malformed_entries():
    real = common.ALLOWLIST
    try:
        good = "UC" + "a" * 22
        _with_allowlist(json.dumps({"channels": {
            good: "kept", "UC-too-short": "dropped", "": "dropped",
            "../../etc/passwd": "dropped",
        }}))
        assert list(common.load_allowlist()) == [good]
    finally:
        common.ALLOWLIST = real


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)}/{len(tests)} passent")
