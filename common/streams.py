"""Redis Streams hand-offs between services (TECH.md: Job handling and recovery).

A message carries only a job id and a schema version. Each service reads through a consumer group, so a
message goes to one worker. A worker that stays silent past the visibility timeout loses its message to
another worker (XPENDING + XCLAIM). A message delivered `max_deliveries` times without an ack, and any
message that cannot be read, goes to the dead-letter stream. Postgres is the source of truth: the
reconciler re-publishes whatever the queue loses.
"""

from __future__ import annotations

import json
import socket
import uuid
from dataclasses import dataclass
from typing import Any

import redis

SCHEMA_VERSION = 1
DEFAULT_MAXLEN = 10_000
DEFAULT_DLQ_MAXLEN = 1_000


class BadMessage(ValueError):
    """A stream entry this code version cannot read."""


@dataclass(frozen=True)
class Delivery:
    message_id: str
    job_id: str
    deliveries: int  # 1 on the first delivery, 2 after one reclaim, and so on


def connect(url: str, *, block_ms: int = 5000) -> redis.Redis:
    """A client whose socket waits longer than a blocking read, or an idle worker would time out every time."""
    return redis.Redis.from_url(url, decode_responses=True, socket_timeout=block_ms / 1000 + 25)


def consumer_name(worker_no: int = 0) -> str:
    """Unique per worker, and per host, so two machines never share a consumer."""
    return f"{socket.gethostname()}-{worker_no}"


def _encode(job_id: str) -> dict[str, str]:
    return {"data": json.dumps({"v": SCHEMA_VERSION, "job_id": job_id})}


def _decode(fields: dict[str, str] | None) -> str:
    try:
        payload = json.loads((fields or {})["data"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BadMessage("unreadable message: not JSON with a data field") from exc
    if not isinstance(payload, dict) or payload.get("v") != SCHEMA_VERSION:
        raise BadMessage("unreadable message: unknown schema version")
    try:
        return str(uuid.UUID(str(payload.get("job_id"))))
    except ValueError as exc:
        raise BadMessage("unreadable message: job_id is not a UUID") from exc


def publish(r: redis.Redis, stream: str, job_id: str, *, maxlen: int = DEFAULT_MAXLEN) -> str:
    """Add a job id to a stream (approximately trimmed to `maxlen`) and return the message id."""
    uuid.UUID(job_id)  # a bad id is a bug in the caller: fail here, not in every consumer
    return str(r.xadd(stream, _encode(job_id), maxlen=maxlen, approximate=True))  # type: ignore[arg-type]


def send_to_dlq(
    r: redis.Redis,
    dlq: str,
    *,
    stream: str,
    group: str,
    message_id: str,
    job_id: str,
    reason: str,
    deliveries: int,
    maxlen: int = DEFAULT_DLQ_MAXLEN,
) -> None:
    """Record a failed hand-off in the dead-letter stream (trimmed). The job row in Postgres is the record."""
    r.xadd(
        dlq,
        {
            "job_id": job_id,
            "stream": stream,
            "group": group,
            "message_id": message_id,
            "reason": reason,
            "deliveries": str(deliveries),
        },
        maxlen=maxlen,
        approximate=True,
    )


class Consumer:
    def __init__(
        self,
        r: redis.Redis,
        stream: str,
        group: str,
        name: str,
        *,
        visibility_timeout_s: float,
        max_deliveries: int = 3,
        dlq: str = "dlq",
        dlq_maxlen: int = DEFAULT_DLQ_MAXLEN,
    ) -> None:
        self.r, self.stream, self.group, self.name = r, stream, group, name
        self.visibility_ms = max(1, int(visibility_timeout_s * 1000))
        self.max_deliveries = max_deliveries
        self.dlq, self.dlq_maxlen = dlq, dlq_maxlen

    def ensure_group(self) -> None:
        """Create the group (and the stream) if missing. It starts at the beginning, so nothing published
        before the first worker came up is lost."""
        try:
            self.r.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def read(self, block_ms: int | None = None) -> Delivery | None:
        """The next job for this worker: a message from a silent worker first, else a new one.

        Unreadable messages are dead-lettered on the way. Returns None when there is nothing to do.
        """
        self.ensure_group()
        while True:
            entry = self._next_entry(block_ms)
            if entry is None:
                return None
            message_id, fields, deliveries = entry
            try:
                return Delivery(message_id, _decode(fields), deliveries)
            except BadMessage as exc:
                self._dead_letter(message_id, "", str(exc), deliveries)

    def ack(self, delivery: Delivery) -> None:
        self.r.xack(self.stream, self.group, delivery.message_id)

    def _next_entry(self, block_ms: int | None) -> tuple[str, dict[str, str] | None, int] | None:
        stale = self._claim_stale()
        if stale is not None:
            return stale
        response: Any = self.r.xreadgroup(self.group, self.name, {self.stream: ">"}, count=1, block=block_ms)
        if not response:
            return None
        message_id, fields = response[0][1][0]
        return message_id, fields, 1

    def _claim_stale(self) -> tuple[str, dict[str, str] | None, int] | None:
        while True:
            pending: Any = self.r.xpending_range(
                self.stream, self.group, min="-", max="+", count=10, idle=self.visibility_ms
            )
            if not pending:
                return None
            for item in pending:
                message_id, delivered = item["message_id"], item["times_delivered"]
                if delivered >= self.max_deliveries:
                    self._dead_letter(
                        message_id,
                        self._job_id_of(message_id),
                        f"delivered {delivered} times without being acknowledged",
                        delivered,
                    )
                    continue
                claimed: Any = self.r.xclaim(
                    self.stream, self.group, self.name, min_idle_time=self.visibility_ms, message_ids=[message_id]
                )
                if claimed:  # empty when another worker claimed it first
                    claimed_id, fields = claimed[0]
                    return claimed_id, fields, delivered + 1
            if len(pending) < 10:
                return None

    def _job_id_of(self, message_id: str) -> str:
        entries: Any = self.r.xrange(self.stream, message_id, message_id)
        try:
            return _decode(entries[0][1]) if entries else ""
        except BadMessage:
            return ""

    def dead_letter(self, delivery: Delivery, reason: str) -> None:
        """Give up on a delivered job: record it in the dead-letter stream and acknowledge it."""
        self._dead_letter(delivery.message_id, delivery.job_id, reason, delivery.deliveries)

    def _dead_letter(self, message_id: str, job_id: str, reason: str, deliveries: int) -> None:
        send_to_dlq(
            self.r,
            self.dlq,
            stream=self.stream,
            group=self.group,
            message_id=message_id,
            job_id=job_id,
            reason=reason,
            deliveries=deliveries,
            maxlen=self.dlq_maxlen,
        )
        self.r.xack(self.stream, self.group, message_id)
