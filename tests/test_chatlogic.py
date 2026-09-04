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
# The chat can only ask for what is on disk, so every case here needs a disk to
# ask about. Titles are distinct on purpose: the search path must be able to
# fail to match, and to match too much.
LIBRARY = [{"n": n, "title": f"clip {n:02d} sample", "path": f"/lib/clip{n:02d}.mp4"}
           for n in range(1, 21)]


def fresh():
    return {"seq": 0, "items": []}, chatlogic.new_state()


def say(queue, state, user, text, now=1000.0, name="someone", verdicts=None, config=None,
        library=None):
    return chatlogic.handle(
        {"user_id": user, "username": name, "text": text}, queue, state, MODS, now,
        verdicts, config, None, LIBRARY if library is None else library,
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
    reply, changed = say(queue, state, "u1", "!play 1", now=1000)
    assert changed and reply.startswith("queued:")
    # a second add inside the cooldown is refused. A refusal now does change the
    # state, it records the rejection, so the property to assert is the one that
    # matters: nothing reached the queue.
    reply, changed = say(queue, state, "u1", "!play 2", now=1010)
    assert "wait" in reply and len(queue["items"]) == 1
    # and is allowed once the cooldown has passed
    reply, changed = say(queue, state, "u1", "!play 2", now=1100)
    assert changed, reply


def test_one_user_cannot_hold_the_whole_queue():
    queue, state = fresh()
    now = 1000.0
    for n in range(common.MAX_PENDING_PER_USER):
        _, changed = say(queue, state, "u1", f"!play {n + 3}", now=now)
        assert changed
        now += common.ADD_COOLDOWN_SECONDS + 1
    reply, changed = say(queue, state, "u1", "!play 9", now=now)
    assert "waiting" in reply and len(queue["items"]) == common.MAX_PENDING_PER_USER


def test_duplicates_are_refused():
    queue, state = fresh()
    say(queue, state, "u1", "!play 1", now=1000)
    # a different person, so the per-user cooldown is not what refuses this one
    reply, changed = say(queue, state, "u2", "!play 1", now=1000)
    assert reply == "already in the queue" and len(queue["items"]) == 1
    # and the same thing asked for by its words, not its number
    reply, changed = say(queue, state, "u3", "!play clip 01", now=1000)
    assert reply == "already in the queue" and len(queue["items"]) == 1


def test_a_vote_counts_once_per_user():
    queue, state = fresh()
    say(queue, state, "u1", "!play 1", now=1000)
    item = queue["items"][0]
    for _ in range(10):
        say(queue, state, "u2", f"!vote {item['id']}", now=1000)
    assert item["votes"] == ["u2"], item["votes"]


def test_vote_argument_is_not_trusted():
    queue, state = fresh()
    say(queue, state, "u1", "!play 1", now=1000)
    for junk in ("!vote abc", "!vote " + "9" * 40, "!vote -1", "!vote 99999", "!vote"):
        _, changed = say(queue, state, "u2", junk, now=1000)
        assert not changed, junk


def test_skip_needs_several_distinct_people():
    queue, state = fresh()
    # the same person shouting does not move the counter past one
    for _ in range(5):
        reply, _ = say(queue, state, "u1", "!skip", now=2000)
    assert reply != "skipping", reply
    assert len(state["skip_votes"]) == 1

    for n, user in enumerate(("u2", "u3")):
        reply, _ = say(queue, state, user, "!skip", now=2000 + n)
    assert reply == "skipping", reply


def test_skip_cannot_be_chained():
    queue, state = fresh()
    for user in ("u1", "u2", "u3"):
        reply, _ = say(queue, state, user, "!skip", now=2000)
    assert reply == "skipping"
    # immediately afterwards the cooldown swallows a second attempt
    for user in ("u1", "u2", "u3"):
        reply, changed = say(queue, state, user, "!skip", now=2005)
        assert reply != "skipping" and not changed


def test_stale_skip_votes_expire():
    queue, state = fresh()
    say(queue, state, "u1", "!skip", now=2000)
    say(queue, state, "u2", "!skip", now=2000)
    # the third vote arrives long after the window, so the first two are gone
    late = 2000 + common.SKIP_WINDOW_SECONDS + 10
    reply, _ = say(queue, state, "u3", "!skip", now=late)
    assert reply != "skipping", reply
    assert len(state["skip_votes"]) == 1


def test_moderator_powers_are_not_available_to_everyone():
    queue, state = fresh()
    reply, changed = say(queue, state, "randomuser", "!ban u9", now=1000)
    assert not changed and reply is None
    reply, changed = say(queue, state, "mod1", "!ban u9", now=1000)
    assert changed and "u9" in state["banned"]
    # a banned user is then ignored entirely
    _, changed = say(queue, state, "u9", "!play 1", now=1000)
    assert not changed
    # and a moderator skips alone
    reply, _ = say(queue, state, "mod1", "!skip", now=1000)
    assert reply == "skipping"


def test_noise_is_ignored_in_silence():
    queue, state = fresh()
    # a chat is not a shell prompt: unknown input gets no answer at all, which
    # is also why it cannot be used to farm replies. A malformed !play does
    # answer, so it lives in the rejection-budget test instead of here.
    for junk in ("hello", "", "!", "!unknown thing", "!!!!"):
        reply, changed = say(queue, state, "u1", junk, now=1000)
        assert not changed and reply is None, junk
    # an oversized message is dropped without being parsed
    _, changed = say(queue, state, "u1", "!play " + "a" * 6000, now=1000)
    assert not changed


def test_control_characters_never_survive():
    queue, state = fresh()
    say(queue, state, "u1", "!play 1", now=1000, name="ev\x00il\x1b[31m")
    stored = queue["items"][0]["by_name"]
    assert all(ord(c) >= 0x20 for c in stored), repr(stored)


def test_queue_file_stays_bounded():
    queue, state = fresh()
    queue["items"] = [{"id": n, "status": "played", "by": "u", "url": ""}
                      for n in range(common.MAX_QUEUE + 50)]
    queue["seq"] = len(queue["items"])
    say(queue, state, "u1", "!play 1", now=1000)
    assert len(queue["items"]) <= common.MAX_QUEUE + 1, len(queue["items"])


def test_most_voted_plays_first():
    queue, state = fresh()
    say(queue, state, "u1", "!play 1", now=1000)
    say(queue, state, "u2", "!play 2", now=1000)
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


def test_the_list_is_paged_and_numbered():
    queue, state = fresh()
    reply, changed = say(queue, state, "u1", "!vods", now=1000)
    assert not changed and reply.startswith("[page 1/"), reply
    assert "1. clip 01 sample" in reply, reply
    # a page past the end lands on the last one rather than answering nothing
    far, _ = say(queue, state, "u1", "!vods 99", now=1000)
    assert far.startswith("[page 4/4]"), far
    # and an empty library says so instead of pretending to be a page
    empty, _ = say(queue, state, "u1", "!vods", now=1000, library=[])
    assert empty == "the library is empty", empty


def test_a_request_by_words_needs_to_be_unambiguous():
    queue, state = fresh()
    # every title contains "clip", so this must refuse rather than pick one
    # a refusal records the rejection, so "changed" is true; the queue is the
    # property that has to hold
    reply, _ = say(queue, state, "u1", "!play clip", now=1000)
    assert "matches, be more precise" in reply, reply
    assert queue["items"] == []
    # narrow it and it goes through
    reply, changed = say(queue, state, "u2", "!play clip 07", now=1000)
    assert changed and reply.startswith("queued: clip 07"), reply
    # and words that match nothing are refused, not guessed at
    reply, _ = say(queue, state, "u3", "!play zzzz", now=1000)
    assert reply == "no match, try !vods", reply


def test_a_number_outside_the_library_is_refused():
    queue, state = fresh()
    for text in ("!play 0", "!play 21", "!play 9999"):
        reply, _ = say(queue, state, "u1", text, now=1000)
        assert reply == "no match, try !vods", (text, reply)
    assert queue["items"] == []


def test_next_reports_the_queue_and_never_guesses_the_screen():
    queue, state = fresh()
    reply, changed = say(queue, state, "u1", "!next", now=1000)
    assert not changed and "queue empty" in reply, reply
    say(queue, state, "u2", "!play 3", now=1000)
    reply, _ = say(queue, state, "u3", "!next", now=1000)
    assert reply.startswith("next up: clip 03") and "1 waiting" in reply, reply


def test_a_link_is_refused_instead_of_queued_to_die():
    """The old command took a link, answered "added", and the video never came.

    This host is refused the download, so a request naming a URL could only ever
    become a queue entry that fails later. Refusing it in the answer is the whole
    difference between a bot that works and one that lies.
    """
    queue, state = fresh()
    for link in ("https://youtu.be/dQw4w9WgXcQ",
                 "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                 "http://example.com/clip.mp4"):
        queue, state = fresh()
        reply, _ = say(queue, state, "u1", f"!play {link}", now=1000)
        assert reply == "links are not accepted, try !vods", reply
        assert queue["items"] == [], queue["items"]


def test_the_verdict_cache_is_unreachable_from_chat_now():
    """It gated URLs, and chat can no longer name one, so it decides nothing here.

    Kept as a control: a stale verdict must not leak into a library request and
    refuse something that is sitting on disk.
    """
    queue, state = fresh()
    verdicts = {"dQw4w9WgXcQ": {"ok": False, "reason": "channel not on the allowlist"}}
    reply, changed = say(queue, state, "u1", "!play 1", now=1000, verdicts=verdicts)
    assert changed and reply.startswith("queued:"), reply
    assert len(queue["items"]) == 1


def test_a_user_who_only_earns_refusals_stops_getting_answers():
    queue, state = fresh()
    now = 1000.0
    replies = []
    for n in range(common.MAX_REJECTS_IN_WINDOW + 3):
        reply, _ = say(queue, state, "u1", "!play x", now=now + n)
        replies.append(reply)
    answered = [r for r in replies if r is not None]
    assert len(answered) == common.MAX_REJECTS_IN_WINDOW - 1, answered
    assert replies[-1] is None, replies
    # muted means muted: even a valid link gets nothing while the silence lasts
    reply, changed = say(queue, state, "u1", "!play 1", now=now + 10)
    assert reply is None and not changed and queue["items"] == []
    # and it does expire rather than being a permanent ban
    reply, changed = say(queue, state, "u1", "!play 1",
                         now=now + common.REJECT_SILENCE_SECONDS + 20)
    assert changed and reply.startswith("queued:"), reply
    # one loud user must not silence anybody else
    reply, changed = say(queue, state, "u2", "!play 2", now=now + 5)
    assert changed and reply.startswith("queued:"), reply


def test_the_queue_cannot_be_filled_past_its_cap():
    """The cap that used to matter here counted items prep still owed a lookup.

    A library request costs no lookup: the file is already on disk, so there is
    nothing to bound on that side any more. What still has to hold is the total,
    since every live entry is memory and a line in the state file.
    """
    queue, state = fresh()
    now = 1000.0
    library = [{"n": n, "title": f"clip {n:03d}", "path": f"/lib/c{n:03d}.mp4"}
               for n in range(1, common.MAX_QUEUE + 3)]
    for n in range(common.MAX_QUEUE):
        _, changed = say(queue, state, f"u{n}", f"!play {n + 1}", now=now, library=library)
        assert changed, n
    reply, changed = say(queue, state, "flood", f"!play {common.MAX_QUEUE + 1}",
                         now=now, library=library)
    assert not changed and reply == "the queue is full", reply
    assert len(queue["items"]) == common.MAX_QUEUE


def test_the_ban_list_stays_bounded():
    queue, state = fresh()
    for n in range(common.MAX_BANNED + 25):
        say(queue, state, "mod1", f"!ban user{n}", now=1000)
    assert len(state["banned"]) == common.MAX_BANNED, len(state["banned"])
    # the newest bans are the ones kept, an evicted one is the oldest
    assert "user0" not in state["banned"]
    assert f"user{common.MAX_BANNED + 24}" in state["banned"]


def test_a_setting_from_the_panel_actually_changes_the_bot():
    def votes_needed(config):
        queue, state = fresh()
        for n in range(1, 9):
            reply, _ = say(queue, state, f"voter{n}", "!skip", now=2000 + n, config=config)
            if reply == "skipping":
                return n
        return None

    check = votes_needed(None)
    assert check == common.SKIP_MIN_VOTERS, check
    assert votes_needed({"SKIP_MIN_VOTERS": 2}) == 2
    assert votes_needed({"SKIP_MIN_VOTERS": 6}) == 6
    # anything the panel could not have written falls back to the constant, so a
    # broken file leaves the bot exactly as it was rather than half configured
    for junk in ({}, {"SKIP_MIN_VOTERS": "2"}, {"SKIP_MIN_VOTERS": None},
                 {"SKIP_MIN_VOTERS": True}, {"nonsense": 1}):
        assert votes_needed(junk) == common.SKIP_MIN_VOTERS, junk


def test_a_custom_command_answers_and_cannot_shadow_a_builtin():
    config = {"commands": {"!discord": "discord.gg/exemple", "!play": "detourne",
                           "!skip": "detourne"}}
    queue, state = fresh()
    reply, changed = say(queue, state, "u1", "!discord", now=1000, config=config)
    assert reply == "discord.gg/exemple" and not changed, reply

    # the built-ins are matched first, so a custom entry with the same name is
    # dead weight rather than a takeover
    reply, changed = say(queue, state, "u1", "!play 1", now=1000, config=config)
    assert reply.startswith("queued:") and len(queue["items"]) == 1, reply
    reply, _ = say(queue, state, "u2", "!skip", now=1000, config=config)
    assert reply != "detourne", reply

    # unknown stays silent even with commands configured
    reply, changed = say(queue, state, "u1", "!nothere", now=1000, config=config)
    assert reply is None and not changed

    # and a reply is text: control characters never reach the chat
    dirty = {"commands": {"!x": "prop\x00re\x1b[31m"}}
    reply, _ = say(queue, state, "u1", "!x", now=1000, config=dirty)
    assert "\x00" not in reply and "\x1b" not in reply, repr(reply)


def test_the_help_text_can_be_replaced_but_never_emptied():
    queue, state = fresh()
    assert say(queue, state, "u1", "!help", now=1000)[0] == chatlogic.HELP
    assert say(queue, state, "u1", "!help", now=1000,
               config={"HELP": "mon aide"})[0] == "mon aide"
    for empty in ("", "   ", None, 5):
        assert say(queue, state, "u1", "!help", now=1000,
                   config={"HELP": empty})[0] == chatlogic.HELP, repr(empty)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)}/{len(tests)} passent")
