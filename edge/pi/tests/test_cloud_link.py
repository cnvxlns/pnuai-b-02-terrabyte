import unittest

from terrabyte_edge.cloud_link import CloudLink, CloudLinkState


# The cadence the backend actually publishes at. Every test below beats at this
# rate, because a state machine that only works on an idealised cadence is the
# bug this module was written to avoid.
BEAT = 30.0


class FakeClock:
    """Monotonic seconds the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CloudLinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.link = CloudLink(clock=self.clock)

    # -- boot --------------------------------------------------------------

    def test_starts_in_boot_before_any_heartbeat(self) -> None:
        self.assertEqual(self.link.evaluate(), CloudLinkState.BOOT)

    def test_a_first_heartbeat_only_reaches_link_up(self) -> None:
        self.link.record_heartbeat()

        # The cloud has spoken once. A minute of it speaking is what
        # CLOUD_ONLINE means, so a single packet cannot buy that claim.
        self.assertEqual(self.link.evaluate(), CloudLinkState.LINK_UP)

    def test_a_minute_of_heartbeats_reaches_cloud_online(self) -> None:
        self.beat(3)

        self.assertEqual(self.link.evaluate(), CloudLinkState.CLOUD_ONLINE)

    def test_the_healthy_cadence_never_breaks_the_streak(self) -> None:
        # The regression this module was rewritten for: with the degrade window
        # set to one nominal interval, every ordinary heartbeat restarted the
        # recovery streak and CLOUD_ONLINE was unreachable.
        self.beat(20)

        self.assertEqual(self.link.evaluate(), CloudLinkState.CLOUD_ONLINE)

    # -- losing the cloud --------------------------------------------------

    def test_three_missed_intervals_degrade(self) -> None:
        self.beat(3)

        self.clock.advance(3 * BEAT)

        self.assertEqual(self.link.evaluate(), CloudLinkState.CLOUD_DEGRADED)

    def test_one_missed_heartbeat_stays_online(self) -> None:
        self.beat(3)

        self.clock.advance(2 * BEAT)

        # A dropped beat is normal traffic on a marginal link, not an outage.
        self.assertEqual(self.link.evaluate(), CloudLinkState.CLOUD_ONLINE)

    def test_fifteen_minutes_of_silence_goes_autonomous(self) -> None:
        self.beat(3)

        self.clock.advance(900.0)

        self.assertEqual(self.link.evaluate(), CloudLinkState.EDGE_AUTONOMOUS)

    def test_a_gateway_that_never_hears_the_cloud_goes_autonomous(self) -> None:
        # No heartbeat has ever arrived: a broker that accepts a connection but
        # sits in front of a dead Spring looks exactly like this.
        self.clock.advance(900.0)

        self.assertEqual(self.link.evaluate(), CloudLinkState.EDGE_AUTONOMOUS)

    # -- coming back -------------------------------------------------------

    def test_recovery_needs_a_full_minute_of_heartbeats(self) -> None:
        self.goAutonomous()

        self.link.record_heartbeat()
        self.clock.advance(BEAT)
        self.link.record_heartbeat()

        # Thirty seconds of heartbeats is not proof the cloud is back.
        # Declaring ONLINE here is what makes an unstable link oscillate.
        self.assertEqual(self.link.evaluate(), CloudLinkState.LINK_UP)

        self.clock.advance(BEAT)
        self.link.record_heartbeat()
        self.assertEqual(self.link.evaluate(), CloudLinkState.CLOUD_ONLINE)

    def test_a_gap_restarts_the_recovery_streak(self) -> None:
        self.goAutonomous()

        self.link.record_heartbeat()
        self.clock.advance(4 * BEAT)
        # Past the degrade window, so this heartbeat starts a fresh streak
        # rather than extending the one that was already half spent.
        self.link.record_heartbeat()
        self.clock.advance(BEAT)
        self.link.record_heartbeat()

        self.assertEqual(self.link.evaluate(), CloudLinkState.LINK_UP)

    # -- the backlog gate --------------------------------------------------

    def test_pending_control_records_hold_the_link_in_resync(self) -> None:
        self.goAutonomous()
        self.link.set_control_backlog(1)

        self.beat(3)

        # Heartbeats are flowing and the streak is long enough, but the server
        # has not been told about water this gateway already delivered.
        self.assertEqual(self.link.evaluate(), CloudLinkState.RESYNC)

    def test_draining_the_backlog_releases_cloud_online(self) -> None:
        self.goAutonomous()
        self.link.set_control_backlog(1)
        self.beat(3)

        self.link.set_control_backlog(0)

        self.assertEqual(self.link.evaluate(), CloudLinkState.CLOUD_ONLINE)

    def test_resync_refuses_cloud_commands(self) -> None:
        self.goAutonomous()
        self.link.set_control_backlog(2)
        self.beat(3)

        # The budget behind an inbound command predates records the server has
        # not seen, so executing it is how the pot gets watered twice.
        self.assertFalse(self.link.accepts_cloud_commands)

    # -- the hold ----------------------------------------------------------

    def test_a_hold_outranks_everything_including_autonomy(self) -> None:
        self.goAutonomous()

        self.link.hold("clock behind TB_CLOCK_MINIMUM_UTC")

        self.assertEqual(self.link.evaluate(), CloudLinkState.SAFE_HOLD)
        self.assertFalse(self.link.may_irrigate_autonomously)
        self.assertFalse(self.link.accepts_cloud_commands)

    def test_releasing_a_hold_returns_to_what_the_clock_says(self) -> None:
        self.goAutonomous()
        self.link.hold("clock unsynced")
        self.link.evaluate()

        self.link.release()

        self.assertEqual(self.link.evaluate(), CloudLinkState.EDGE_AUTONOMOUS)

    # -- fixtures ----------------------------------------------------------

    def beat(self, count: int) -> None:
        """Deliver ``count`` heartbeats at the backend's real cadence.

        Ends on a heartbeat rather than on a gap, so a test that then advances
        the clock is measuring exactly the silence it asked for.
        """

        for index in range(count):
            if index:
                self.clock.advance(BEAT)
            self.link.record_heartbeat()

    def goAutonomous(self) -> None:
        self.beat(3)
        self.clock.advance(900.0)
        assert self.link.evaluate() is CloudLinkState.EDGE_AUTONOMOUS
