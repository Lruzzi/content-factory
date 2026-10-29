"""Four rooms in one Telegram group (topics): approval, alerts, monitoring, commands. Where each kind of message
goes, how the ids are found, and that without a group everything still goes to your private chat."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fake_telegram_server import fake_telegram

import config_schema as cs
from common import alerts, rooms
from common.telegram import Telegram

TOKEN = "123456789:FAKE-token_for_tests"  # noqa: S105  # gitleaks:allow
GROUP = -1001234567890
TOPICS = {"approval": 11, "alerts": 12, "monitoring": 13, "commands": 14}


def file(tmp_path: Path, **raw) -> Path:
    path = tmp_path / "telegram.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


# ---------- the settings file ---------------------------------------------------------------------
def test_no_file_or_an_empty_one_means_no_group_and_everything_goes_to_private_chats(tmp_path):
    for loaded in (rooms.load(tmp_path / "missing.yaml"), rooms.load(file(tmp_path, chat_id=None, topics={}))):
        assert not loaded.enabled
        assert loaded.where("alerts", [111, 222]) == [rooms.Destination(111), rooms.Destination(222)]


def test_with_a_group_each_kind_of_message_goes_to_its_topic(tmp_path):
    loaded = rooms.load(file(tmp_path, chat_id=GROUP, topics=TOPICS))
    assert loaded.enabled and loaded.where("approval", [111]) == [rooms.Destination(GROUP, 11)]
    assert loaded.where("alerts", [111]) == [rooms.Destination(GROUP, 12)]
    assert loaded.where("monitoring", [111]) == [rooms.Destination(GROUP, 13)]


def test_a_topic_that_is_not_set_falls_back_to_the_groups_general_topic(tmp_path):
    loaded = rooms.load(file(tmp_path, chat_id=GROUP, topics={"approval": 11}))
    assert loaded.where("alerts", [111]) == [rooms.Destination(GROUP, None)]


def test_the_commands_room_is_the_commands_topic_of_the_group_and_nothing_else(tmp_path):
    loaded = rooms.load(file(tmp_path, chat_id=GROUP, topics=TOPICS))
    assert loaded.is_commands_room(GROUP, 14)
    assert not loaded.is_commands_room(GROUP, 11) and not loaded.is_commands_room(GROUP, None)
    assert not loaded.is_commands_room(-100999, 14)


@pytest.mark.parametrize(
    "bad",
    [
        {"chat_id": 5, "topics": {}},  # a group id is negative
        {"chat_id": "x", "topics": {}},
        {"chat_id": GROUP, "topics": {"gossip": 3}},  # not one of the four rooms
        {"chat_id": GROUP, "topics": {"alerts": 0}},
        {"chat_id": GROUP, "topics": {"alerts": "7"}},
        {"chat_id": GROUP, "topics": {"alerts": 7}, "extra": 1},
    ],
)
def test_a_wrong_file_is_refused_with_a_clear_message(tmp_path, bad):
    with pytest.raises(ValueError, match="telegram.yaml"):
        rooms.load(file(tmp_path, **bad))


def test_the_repo_file_is_valid_whether_or_not_the_group_is_set_up_yet():
    loaded = rooms.load(cs.ROOT / "telegram.yaml")
    assert isinstance(loaded.enabled, bool)


# ---------- the Telegram client -------------------------------------------------------------------
@pytest.fixture
def bot(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"clip" * 100)
    with fake_telegram() as (fake, url):
        yield Telegram(TOKEN, base_url=url), fake, clip


def test_a_message_a_video_and_a_document_can_be_sent_into_a_topic(bot):
    client, fake, clip = bot
    client.send_message(GROUP, "hi", thread_id=12)
    client.send_video(GROUP, clip, "caption", [], thread_id=11)
    client.send_document(GROUP, clip, "caption", thread_id=11)
    assert fake.calls_to("sendMessage")[0].fields["message_thread_id"] == 12
    assert fake.calls_to("sendVideo")[0].fields["message_thread_id"] == "11"
    assert fake.calls_to("sendDocument")[0].fields["message_thread_id"] == "11"


def test_without_a_topic_no_thread_id_is_sent(bot):
    client, fake, clip = bot
    client.send_message(42, "hi")
    client.send_video(42, clip, "c", [])
    assert "message_thread_id" not in fake.calls_to("sendMessage")[0].fields
    assert "message_thread_id" not in fake.calls_to("sendVideo")[0].fields


def test_a_topic_can_be_created_and_its_id_comes_back(bot):
    client, fake, _ = bot
    assert client.create_forum_topic(GROUP, "Alerts") == 7
    assert fake.calls_to("createForumTopic")[0].fields == {"chat_id": GROUP, "name": "Alerts"}


# ---------- alerts --------------------------------------------------------------------------------
def alerter(rooms_value, allowed="111,222"):
    with fake_telegram() as (fake, url):
        bot = Telegram(TOKEN, base_url=url)
        send = alerts.make_alerter(
            "render", {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ALLOWED_USERS": allowed}, bot=bot, rooms=rooms_value
        )
        send("a clip failed")
        return fake.calls_to("sendMessage")


def test_alerts_go_to_the_alerts_topic_once_not_to_each_user(tmp_path):
    calls = alerter(rooms.load(file(tmp_path, chat_id=GROUP, topics=TOPICS)))
    assert len(calls) == 1 and calls[0].fields["chat_id"] == GROUP and calls[0].fields["message_thread_id"] == 12
    assert "render" in calls[0].fields["text"] and "a clip failed" in calls[0].fields["text"]


def test_without_a_group_alerts_still_go_to_every_allowed_user():
    calls = alerter(rooms.Rooms(None, {}))
    assert sorted(c.fields["chat_id"] for c in calls) == [111, 222] and all(
        "message_thread_id" not in c.fields for c in calls
    )


# ---------- finding the ids -----------------------------------------------------------------------
def topic_message(thread, name, text="hi", chat=GROUP, title="Content Factory", forum=True):
    root = {"message_id": thread, "forum_topic_created": {"name": name}}
    return {
        "update_id": thread,
        "message": {
            "message_id": thread + 100, "message_thread_id": thread, "is_topic_message": True, "text": text,
            "chat": {"id": chat, "type": "supergroup", "title": title, "is_forum": forum},
            "from": {"id": 111}, "reply_to_message": root,
        },
    }  # fmt: skip


def test_the_group_and_its_topics_are_found_from_messages_you_posted_in_them():
    updates = [
        topic_message(11, "Approval"), topic_message(12, "Warning and error"), topic_message(13, "Monitoring"),
        topic_message(14, "Command"),
    ]  # fmt: skip
    chat, found = rooms.plan(updates)
    assert chat == GROUP and found == TOPICS


def test_topic_creation_messages_work_too():
    created = {
        "update_id": 1,
        "message": {
            "message_id": 11, "message_thread_id": 11, "forum_topic_created": {"name": "Approval room"},
            "chat": {"id": GROUP, "type": "supergroup", "title": "Content Factory", "is_forum": True},
        },
    }  # fmt: skip
    assert rooms.plan([created])[1] == {"approval": 11}


def test_names_are_matched_by_what_they_mean_not_exactly():
    updates = [
        topic_message(1, "✅ APPROVALS"),
        topic_message(2, "alerts"),
        topic_message(3, "monitor"),
        topic_message(4, "Cmds & commands"),
    ]
    assert rooms.plan(updates)[1] == {"approval": 1, "alerts": 2, "monitoring": 3, "commands": 4}


def test_a_topic_with_an_unrelated_name_is_left_out():
    assert rooms.plan([topic_message(5, "Random chat")])[1] == {}


def test_a_group_that_is_not_a_forum_or_private_chats_are_ignored():
    private = {"update_id": 1, "message": {"message_id": 1, "text": "hi", "chat": {"id": 111, "type": "private"}}}
    plain_group = topic_message(1, "Approval", chat=-5, forum=False)
    with pytest.raises(rooms.SetupError, match="no group"):
        rooms.plan([private, plain_group])


def test_two_groups_are_an_error_unless_you_name_the_one_you_mean():
    updates = [topic_message(11, "Approval"), topic_message(21, "Approval", chat=-1009, title="Other")]
    with pytest.raises(rooms.SetupError, match="more than one"):
        rooms.plan(updates)
    assert rooms.plan(updates, title="content factory")[0] == GROUP


def test_the_found_ids_are_written_to_the_file_and_read_back(tmp_path):
    path = tmp_path / "telegram.yaml"
    rooms.write(path, GROUP, TOPICS)
    loaded = rooms.load(path)
    assert loaded.chat_id == GROUP and loaded.topics == TOPICS


# ---------- the setup command -------------------------------------------------------------------------------------
def test_the_setup_does_not_create_topics_unless_asked_and_says_which_are_missing(bot, tmp_path, capsys):
    from services.review import rooms_setup

    client, fake, _ = bot
    fake.updates = [topic_message(14, "Command"), topic_message(11, "Approval")]
    path = tmp_path / "telegram.yaml"
    assert rooms_setup.main([], bot=client, path=path) == 2  # partly done
    loaded = rooms.load(path)
    assert loaded.chat_id == GROUP and loaded.topics == {"approval": 11, "commands": 14}  # what was found is kept
    assert fake.calls_to("createForumTopic") == []
    out = capsys.readouterr().out
    assert "alerts" in out and "monitoring" in out and "--create" in out
    assert {c.fields["message_thread_id"] for c in fake.calls_to("sendMessage")} == {11, 14}  # hello only where known


def test_the_setup_with_create_makes_the_missing_topics_and_says_hello_in_each_room(bot, tmp_path, capsys):
    from services.review import rooms_setup

    client, fake, _ = bot
    fake.updates = [
        topic_message(11, "Approval"),
        topic_message(12, "Warning and error"),
        topic_message(13, "Monitoring"),
    ]
    path = tmp_path / "telegram.yaml"
    assert rooms_setup.main(["--create"], bot=client, path=path) == 0
    loaded = rooms.load(path)
    assert loaded.chat_id == GROUP and loaded.topics == {"approval": 11, "alerts": 12, "monitoring": 13, "commands": 7}
    assert [c.fields["name"] for c in fake.calls_to("createForumTopic")] == ["Commands"]
    hellos = {c.fields["message_thread_id"]: c.fields["text"] for c in fake.calls_to("sendMessage")}
    assert set(hellos) == {11, 12, 13, 7} and all("connected" in t for k, t in hellos.items() if k != 7)
    assert "/status" in hellos[7]  # the new commands room opens with the list of commands
    assert "created the Commands topic" in capsys.readouterr().out


def test_the_group_name_is_still_taken_as_an_argument_next_to_the_flag(bot, tmp_path):
    from services.review import rooms_setup

    client, fake, _ = bot
    fake.updates = [topic_message(11, "Approval"), topic_message(21, "Approval", chat=-1009, title="Other")]
    assert rooms_setup.main(["content factory"], bot=client, path=tmp_path / "t.yaml") == 2
    assert rooms.load(tmp_path / "t.yaml").chat_id == GROUP


def test_the_setup_says_what_to_check_when_it_finds_no_group_and_writes_nothing(bot, tmp_path, capsys):
    from services.review import rooms_setup

    client, fake, _ = bot
    path = tmp_path / "telegram.yaml"
    assert rooms_setup.main([], bot=client, path=path) == 1
    assert not path.exists() and "admin" in capsys.readouterr().out


def test_the_setup_reads_the_updates_without_marking_them_as_read(bot, tmp_path):
    from services.review import rooms_setup

    client, fake, _ = bot
    fake.updates = [topic_message(11, "Approval")]
    rooms_setup.main([], bot=client, path=tmp_path / "t.yaml")
    assert "offset" not in fake.calls_to("getUpdates")[0].fields


def test_a_room_that_was_already_connected_is_not_greeted_again_and_the_commands_room_gets_the_command_list(
    bot, tmp_path
):
    from services.review import rooms_setup

    client, fake, _ = bot
    path = tmp_path / "telegram.yaml"
    rooms.write(path, GROUP, {"approval": 11, "alerts": 12, "commands": 14})  # set up on an earlier run
    names = {11: "Approval", 12: "Alerts", 13: "Monitoring", 14: "Command"}
    fake.updates = [topic_message(thread, name) for thread, name in names.items()]
    assert rooms_setup.main([], bot=client, path=path) == 0
    hellos = {c.fields["message_thread_id"]: c.fields["text"] for c in fake.calls_to("sendMessage")}
    assert set(hellos) == {13} and "monitoring room is connected" in hellos[13]  # only the new one


def test_a_commands_room_that_is_new_is_greeted_with_the_list_of_commands(bot, tmp_path):
    from services.review import commands, rooms_setup

    client, fake, _ = bot
    fake.updates = [topic_message(14, "Command")]
    rooms_setup.main([], bot=client, path=tmp_path / "t.yaml")
    text = fake.calls_to("sendMessage")[0].fields["text"]
    assert all(f"/{name}" in text for name, _ in commands.MENU)
