"""Transport-agnostic delivery contract between the outbox and a publisher.

``service.py`` only ever calls ``publisher.send(event) -> DeliveryResult`` and
``publisher.close()``. Keeping that seam in its own module means the HTTP and
MQTT implementations (``backend.py`` / ``mqtt_publisher.py``) can be swapped
without either one importing the other, and without ``service.py`` knowing
which transport it is driving.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol, runtime_checkable

from .protocol import CommandAck, Event


class Delivery(str, Enum):
    DELIVERED = "delivered"
    RETRY = "retry"
    DEAD = "dead"


@dataclass(frozen=True)
class DeliveryResult:
    outcome: Delivery
    reason: str
    retry_after_seconds: float | None = None


class Publisher(Protocol):
    def send(self, event: Event) -> DeliveryResult: ...

    def close(self) -> None: ...


# ``(payload, retained)``. The retain flag travels with the bytes because the
# command contract forbids retained commands outright — a retained command
# re-executes stale irrigation on every reconnect — and only the transport can
# see that flag. Dropping it here would leave the relay unable to tell a live
# command from a fossil.
CommandHandler = Callable[[bytes, bool], None]


@runtime_checkable
class CommandTransport(Protocol):
    """The downlink half, which only MQTT has.

    Split from :class:`Publisher` rather than folded into it because HTTP has no
    command downlink in the contract at all: there is no endpoint to poll and no
    ack endpoint to post to. A single fat protocol would force ``HttpPublisher``
    to grow two methods that can only ever raise, and the service would then
    have to guess at runtime whether calling them was legitimate. Instead the
    relay is simply not built when the transport cannot carry commands, and this
    protocol is the check — see ``BridgeService._build_command_relay``.
    """

    def subscribe_commands(self, handler: CommandHandler) -> None: ...

    def send_ack(self, ack: CommandAck) -> DeliveryResult: ...
