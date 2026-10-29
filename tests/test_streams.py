"""Redis Streams hand-offs: one consumer per message, reclaim, dead-letter (FR-18, TECH.md recovery)."""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest

from common.streams import Consumer, publish

pytestmark = pytest.mark.integration

FAST = 0.3  # visibility timeout in seconds, short so tests do not wait long
PAST_FAST = FAST + 0.15


def job() -> str:
    return str(uuid.uuid4())


def consumer(r, prefix, name, **kw):
    options: dict[str, Any] = {"visibility_timeout_s": FAST, "max_deliveries": 3, "dlq": f"{prefix}.dlq"}
    return Consumer(r, f"{prefix}.jobs", "workers", name, **(options | kw))


def test_a_published_job_is_delivered_once_and_then_nothing(redis_client, stream_prefix):
    job_id = job()
    publish(redis_client, f"{stream_prefix}.jobs", job_id)
    c = consumer(redis_client, stream_prefix, "a")
    delivery = c.read()
    assert delivery is not None
    assert delivery.job_id == job_id
    assert delivery.deliveries == 1
    c.ack(delivery)
    assert c.read() is None


def test_reading_an_empty_stream_returns_nothing(redis_client, stream_prefix):
    assert consumer(redis_client, stream_prefix, "a").read() is None


def test_two_workers_never_receive_the_same_job(redis_client, stream_prefix):
    ids = {job() for _ in range(30)}
    for job_id in ids:
        publish(redis_client, f"{stream_prefix}.jobs", job_id)
    a, b = consumer(redis_client, stream_prefix, "a"), consumer(redis_client, stream_prefix, "b")
    got: dict[str, list[str]] = {"a": [], "b": []}
    for _ in range(40):
        for name, worker in (("a", a), ("b", b)):
            delivery = worker.read()
            if delivery:
                got[name].append(delivery.job_id)
                worker.ack(delivery)
    assert set(got["a"]) | set(got["b"]) == ids
    assert not set(got["a"]) & set(got["b"])
    assert len(got["a"]) + len(got["b"]) == 30


def test_an_unacked_job_is_not_taken_before_the_visibility_timeout(redis_client, stream_prefix):
    publish(redis_client, f"{stream_prefix}.jobs", job())
    assert consumer(redis_client, stream_prefix, "a", visibility_timeout_s=30).read() is not None
    assert consumer(redis_client, stream_prefix, "b", visibility_timeout_s=30).read() is None


def test_a_silent_workers_job_is_claimed_by_another_worker(redis_client, stream_prefix):
    job_id = job()
    publish(redis_client, f"{stream_prefix}.jobs", job_id)
    assert consumer(redis_client, stream_prefix, "crashed").read() is not None
    time.sleep(PAST_FAST)
    delivery = consumer(redis_client, stream_prefix, "healthy").read()
    assert delivery is not None
    assert delivery.job_id == job_id
    assert delivery.deliveries == 2


def test_a_claimed_job_is_not_claimed_a_second_time_at_once(redis_client, stream_prefix):
    publish(redis_client, f"{stream_prefix}.jobs", job())
    consumer(redis_client, stream_prefix, "crashed").read()
    time.sleep(PAST_FAST)
    assert consumer(redis_client, stream_prefix, "b").read() is not None
    assert consumer(redis_client, stream_prefix, "c").read() is None


def test_a_job_delivered_too_many_times_goes_to_the_dead_letter_stream(redis_client, stream_prefix):
    job_id = job()
    publish(redis_client, f"{stream_prefix}.jobs", job_id)
    for name in ("a", "b", "c"):  # three deliveries, none acked
        assert consumer(redis_client, stream_prefix, name).read() is not None
        time.sleep(PAST_FAST)
    assert consumer(redis_client, stream_prefix, "d").read() is None
    dead = redis_client.xrange(f"{stream_prefix}.dlq")
    assert len(dead) == 1
    fields = dead[0][1]
    assert fields["job_id"] == job_id
    assert fields["stream"] == f"{stream_prefix}.jobs"
    assert "delivered 3 times" in fields["reason"]
    assert redis_client.xpending(f"{stream_prefix}.jobs", "workers")["pending"] == 0


def test_an_unreadable_message_is_dead_lettered_not_delivered(redis_client, stream_prefix):
    redis_client.xadd(f"{stream_prefix}.jobs", {"data": "not json"})
    assert consumer(redis_client, stream_prefix, "a").read() is None
    dead = redis_client.xrange(f"{stream_prefix}.dlq")
    assert len(dead) == 1
    assert "unreadable" in dead[0][1]["reason"]


@pytest.mark.parametrize(
    "payload",
    [f'{{"v": 2, "job_id": "{uuid.uuid4()}"}}', '{"v": 1}', '{"v": 1, "job_id": "../../etc"}', "[]"],
)
def test_messages_with_an_unknown_version_or_a_bad_job_id_are_dead_lettered(redis_client, stream_prefix, payload):
    redis_client.xadd(f"{stream_prefix}.jobs", {"data": payload})
    assert consumer(redis_client, stream_prefix, "a").read() is None
    assert redis_client.xlen(f"{stream_prefix}.dlq") == 1


def test_a_good_message_after_a_bad_one_is_still_delivered(redis_client, stream_prefix):
    redis_client.xadd(f"{stream_prefix}.jobs", {"data": "garbage"})
    good = job()
    publish(redis_client, f"{stream_prefix}.jobs", good)
    delivery = consumer(redis_client, stream_prefix, "a").read()
    assert delivery is not None
    assert delivery.job_id == good


def test_streams_are_trimmed(redis_client, stream_prefix):
    for _ in range(600):
        publish(redis_client, f"{stream_prefix}.jobs", job(), maxlen=100)
    length = redis_client.xlen(f"{stream_prefix}.jobs")
    assert 100 <= length < 600


def test_the_dead_letter_stream_is_trimmed_too(redis_client, stream_prefix):
    for _ in range(600):
        redis_client.xadd(f"{stream_prefix}.jobs", {"data": "garbage"})
    c = consumer(redis_client, stream_prefix, "a", dlq_maxlen=100)
    assert c.read() is None  # one call dead-letters every unreadable message it meets
    assert 100 <= redis_client.xlen(f"{stream_prefix}.dlq") < 600


def test_creating_the_group_twice_is_harmless(redis_client, stream_prefix):
    c = consumer(redis_client, stream_prefix, "a")
    c.ensure_group()
    c.ensure_group()


def test_a_job_published_before_any_group_exists_is_not_lost(redis_client, stream_prefix):
    job_id = job()
    publish(redis_client, f"{stream_prefix}.jobs", job_id)  # nobody has created the group yet
    delivery = consumer(redis_client, stream_prefix, "late").read()
    assert delivery is not None
    assert delivery.job_id == job_id


def test_a_connection_made_for_blocking_reads_waits_longer_than_the_block():
    """redis-py's own socket timeout is about 5 s: an idle worker blocking for 5 s would fail every time."""
    from common.streams import connect

    client = connect("redis://localhost:6379/0", block_ms=5000)
    assert client.connection_pool.connection_kwargs["socket_timeout"] > 5
    assert client.connection_pool.connection_kwargs["decode_responses"] is True
