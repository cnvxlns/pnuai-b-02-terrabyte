"""How alive the cloud is, decided from the backend's own heartbeat.

**Not the broker connection.** ``mqtt_publisher`` knows whether a TCP session to
Mosquitto is up, and that is the wrong question: the broker is a separate
container from Spring, so it happily accepts publishes while the application
behind it is dead, restarting, or wedged on a database lock. A gateway that
equates "broker reachable" with "cloud alive" waits forever for commands nobody
is issuing. ``BackendHeartbeatPublisher`` exists precisely so this module has
something that only a running application can produce (``dn/heartbeat``, QoS 0,
never retained — a retained one would be handed to us on connect and would lie).

**Hysteresis, because the alternative oscillates.** On a marginal link the raw
"is a heartbeat overdue" answer flips every few seconds, and every flip would
start or stop autonomous irrigation. The three windows are deliberately
asymmetric: leaving CLOUD_ONLINE is cheap and fast (30 s), entering
EDGE_AUTONOMOUS is slow and expensive (15 min, because it is the state that can
move water with nobody supervising), and returning needs a full minute of
uninterrupted heartbeats before we believe it.

**The backlog is a hard gate on CLOUD_ONLINE.** Any irrigation this gateway
performed on its own is owed to the server, which counts it against the daily
budget. Reaching CLOUD_ONLINE with that debt unpaid is the exact failure the
design calls out: the server would authorise more water without knowing what
already went in. So while records are pending, the best state available is
RESYNC — no matter how the gateway got here.
"""

from __future__ import annotations

from enum import Enum
import time
from typing import Callable


# The backend's publish cadence, from `app.mqtt.heartbeat.interval-ms:30000`.
# Every window below is a multiple of it, and that coupling is the point: a
# threshold shorter than the cadence fires on healthy traffic.
BACKEND_HEARTBEAT_INTERVAL_SECONDS = 30.0

# Seconds of silence before the cloud is presumed unwell — three intervals, so
# two beats can go missing before anyone worries.
#
# **Deviation from docs/design/edge_ai_hardening.md, which says 30 s.** That
# number predates the heartbeat being pinned to a 30 s cadence, and taken
# literally it means "one nominal interval, exactly", so ordinary jitter reads
# as an outage and the recovery streak below can never accumulate. If the 30 s
# demo behaviour is wanted, lower the *backend* interval to 10 s as well; the
# two numbers only make sense together.
DEFAULT_DEGRADE_AFTER_SECONDS = 3 * BACKEND_HEARTBEAT_INTERVAL_SECONDS

# Seconds of silence before this gateway will water on its own. Long: the cost
# of being wrong is unsupervised irrigation.
DEFAULT_AUTONOMY_AFTER_SECONDS = 900.0

# Seconds of uninterrupted heartbeats before the cloud is believed again.
DEFAULT_RECOVER_AFTER_SECONDS = 60.0


class CloudLinkState(str, Enum):
    """Where this gateway thinks it stands with the cloud.

    ``str`` mixin so the value drops straight into the ``up/status`` payload and
    into log lines without a conversion at every call site.
    """

    #: Nothing has ever confirmed a live backend. Not an error — every gateway
    #: passes through here — but it is not a licence to water either.
    BOOT = "BOOT"

    #: Heartbeats are arriving but have not yet run long enough to trust.
    LINK_UP = "LINK_UP"

    #: The backend is answering and owes us nothing.
    CLOUD_ONLINE = "CLOUD_ONLINE"

    #: Heartbeats stopped. Still short of the autonomy window, so nothing local
    #: may start; this state exists to be visible before it matters.
    CLOUD_DEGRADED = "CLOUD_DEGRADED"

    #: The only state in which the emergency rule may dispense water.
    EDGE_AUTONOMOUS = "EDGE_AUTONOMOUS"

    #: The backend is back but has not yet been told what we did without it.
    #: Cloud commands are refused here — see ``command_relay`` — because the
    #: budget they were authorised against is stale by definition.
    RESYNC = "RESYNC"

    #: Something makes any irrigation unsafe regardless of the cloud, and the
    #: clock is the case that matters: every gate in the safety envelope
    #: (cooldown, daily budget, sample freshness) is a comparison of timestamps.
    SAFE_HOLD = "SAFE_HOLD"


class CloudLink:
    """Tracks heartbeat arrivals and reports the state they imply.

    Deliberately passive: it holds no thread and no socket, and never asks the
    time except through ``clock``. Everything it knows arrives through
    :meth:`record_heartbeat`, :meth:`set_control_backlog` and :meth:`hold`, which
    keeps the whole transition table testable without a broker.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        degrade_after_seconds: float = DEFAULT_DEGRADE_AFTER_SECONDS,
        autonomy_after_seconds: float = DEFAULT_AUTONOMY_AFTER_SECONDS,
        recover_after_seconds: float = DEFAULT_RECOVER_AFTER_SECONDS,
    ) -> None:
        if not 0.0 < degrade_after_seconds <= autonomy_after_seconds:
            raise ValueError(
                "degrade_after_seconds must be positive and no later than autonomy"
            )
        if recover_after_seconds < 0.0:
            raise ValueError("recover_after_seconds must not be negative")

        self._clock = clock
        self._degrade_after = degrade_after_seconds
        self._autonomy_after = autonomy_after_seconds
        self._recover_after = recover_after_seconds

        # Where silence is measured from before anything has been heard. Boot is
        # the honest origin: a gateway that has been up for twenty minutes
        # without one heartbeat is as unsupervised as one that lost the cloud.
        self._started_at = clock()
        self._last_heartbeat_at: float | None = None
        self._streak_started_at: float | None = None
        self._control_backlog = 0
        self._hold_reason: str | None = None
        self._state = CloudLinkState.BOOT

    # -- inputs ------------------------------------------------------------

    def record_heartbeat(self) -> None:
        """A ``dn/heartbeat`` arrived from the backend."""

        now = self._clock()
        if (
            self._last_heartbeat_at is None
            or now - self._last_heartbeat_at >= self._degrade_after
        ):
            # The run was broken, so recovery starts counting from here rather
            # than from whenever the link was last healthy.
            self._streak_started_at = now
        self._last_heartbeat_at = now

    def set_control_backlog(self, pending: int) -> None:
        """How many control records still owe the server an upload."""

        if pending < 0:
            raise ValueError("pending must not be negative")
        self._control_backlog = pending

    def hold(self, reason: str) -> None:
        """Force SAFE_HOLD until :meth:`release` — e.g. an unsynced clock."""

        self._hold_reason = reason

    def release(self) -> None:
        self._hold_reason = None

    # -- output ------------------------------------------------------------

    @property
    def state(self) -> CloudLinkState:
        """The last evaluated state, without consulting the clock again."""

        return self._state

    @property
    def hold_reason(self) -> str | None:
        return self._hold_reason

    def evaluate(self) -> CloudLinkState:
        """Recompute from the clock and return the current state.

        Ordered by severity, so the most restrictive answer that applies wins.
        """

        now = self._clock()
        silence = now - (self._last_heartbeat_at or self._started_at)

        if self._hold_reason is not None:
            state = CloudLinkState.SAFE_HOLD
        elif silence >= self._autonomy_after:
            state = CloudLinkState.EDGE_AUTONOMOUS
        elif silence >= self._degrade_after:
            state = CloudLinkState.CLOUD_DEGRADED
        elif self._last_heartbeat_at is None:
            state = CloudLinkState.BOOT
        elif self._control_backlog > 0:
            state = CloudLinkState.RESYNC
        elif now - (self._streak_started_at or now) >= self._recover_after:
            state = CloudLinkState.CLOUD_ONLINE
        else:
            state = CloudLinkState.LINK_UP

        self._state = state
        return state

    # Both gates below re-evaluate rather than reading :attr:`state`. A stale
    # answer here is not merely out of date, it is permissive in the wrong
    # direction: a caller that forgot to tick would keep executing commands
    # through a hold, or keep watering after the cloud came back. `evaluate` is
    # idempotent, so paying for it on every question costs nothing.

    @property
    def may_irrigate_autonomously(self) -> bool:
        """The single question ``autonomy`` asks of this module."""

        return self.evaluate() is CloudLinkState.EDGE_AUTONOMOUS

    @property
    def accepts_cloud_commands(self) -> bool:
        """Whether a command arriving from the backend may be executed.

        Refused in RESYNC because the budget behind it predates records the
        server has not seen, and in SAFE_HOLD because nothing may run at all.
        """

        return self.evaluate() not in (CloudLinkState.RESYNC, CloudLinkState.SAFE_HOLD)
