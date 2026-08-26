package com.terrabyte.backend.irrigation;

import static org.assertj.core.api.Assertions.assertThat;

import java.math.BigDecimal;
import java.time.Duration;
import java.time.Instant;
import java.util.List;

import com.terrabyte.backend.irrigation.CommandAckService.AckResult;
import com.terrabyte.backend.measurement.MeasurementStore;
import com.terrabyte.backend.notification.IrrigationCompletedEvent;
import com.terrabyte.backend.notification.PushSender;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.context.event.ApplicationEvents;
import org.springframework.test.context.event.RecordApplicationEvents;

/**
 * The command state machine, from a device report to the budget it changes.
 */
@SpringBootTest
@ActiveProfiles("test")
@RecordApplicationEvents
class CommandAckServiceTests {

    private static final long POT_ID = 1L;
    private static final String NODE_ID = "terrabyte-node-01";
    private static final String OWNER_EMAIL = "ack-owner@example.com";

    @Autowired private CommandAckService ackService;
    @Autowired private DeviceCommandRepository commands;
    @Autowired private CommandIdGenerator commandIdGenerator;
    @Autowired private ApplicationEvents events;

    @Autowired
    @Qualifier("postgresJdbcTemplate")
    private JdbcTemplate jdbcTemplate;

    @MockitoBean private MeasurementStore measurementStore;

    /** Kept off the wire: an announcement here must not try to reach Firebase. */
    @MockitoBean private PushSender pushSender;

    private String gatewayId;

    @BeforeEach
    void setUp() {
        jdbcTemplate.update("DELETE FROM device_command");
        jdbcTemplate.update("UPDATE pot SET node_id = ? WHERE id = ?", NODE_ID, POT_ID);
        // Ownership is per-test, because whether anyone is listening is exactly
        // what two of these tests are about. Another class may have left an owner
        // on this device.
        clearOwner();
        gatewayId = jdbcTemplate.queryForObject(
                "SELECT d.hardware_id FROM device d JOIN pot p ON p.device_id = d.id"
                        + " WHERE p.id = ?",
                String.class, POT_ID);
    }

    @AfterEach
    void tearDown() {
        jdbcTemplate.update("DELETE FROM device_command");
        jdbcTemplate.update("UPDATE pot SET node_id = NULL WHERE id = ?", POT_ID);
        clearOwner();
    }

    private void clearOwner() {
        jdbcTemplate.update(
                "UPDATE device SET user_id = NULL"
                        + " WHERE id = (SELECT device_id FROM pot WHERE id = ?)",
                POT_ID);
        jdbcTemplate.update(
                "DELETE FROM notification_delivery WHERE notification_id IN ("
                        + "SELECT id FROM notification_event WHERE user_id IN ("
                        + "SELECT id FROM app_user WHERE email = ?))",
                OWNER_EMAIL);
        jdbcTemplate.update(
                "DELETE FROM notification_event WHERE user_id IN ("
                        + "SELECT id FROM app_user WHERE email = ?)",
                OWNER_EMAIL);
        jdbcTemplate.update(
                "DELETE FROM notification_condition_state WHERE user_id IN ("
                        + "SELECT id FROM app_user WHERE email = ?)",
                OWNER_EMAIL);
        jdbcTemplate.update("DELETE FROM app_user WHERE email = ?", OWNER_EMAIL);
    }

    // -- the happy path ----------------------------------------------------

    @Test
    void anAcceptedPumpAckMovesIssuedToAcceptedAndCanExpire() {
        String commandId = issue(100);

        assertThat(ackService.apply(gatewayId, ack(commandId, "accepted", null, null, null)))
                .isEqualTo(AckResult.APPLIED);

        DeviceCommand command = commands.findById(commandId).orElseThrow();
        assertThat(command.state()).isEqualTo(CommandState.ACCEPTED);
        assertThat(command.ackedAt()).isNotNull();
        assertThat(commands.expirableCommandIds(Instant.now().plus(Duration.ofMinutes(5))))
                .contains(commandId);
    }

    @Test
    void anAcceptedLightAckCompletesTheLatchAndCannotExpire() {
        String commandId = issueLight(true);

        assertThat(ackService.apply(gatewayId, ack(commandId, "accepted", null, null, null)))
                .isEqualTo(AckResult.APPLIED);

        DeviceCommand command = commands.findById(commandId).orElseThrow();
        assertThat(command.state()).isEqualTo(CommandState.COMPLETED);
        assertThat(command.ackedAt()).isNotNull();
        assertThat(command.completedAt()).isNotNull();
        assertThat(command.actualMl()).isNull();
        assertThat(command.actualRuntimeMs()).isNull();
        assertThat(command.stopCause()).isEqualTo("OK");
        assertThat(commands.expirableCommandIds(Instant.now().plus(Duration.ofMinutes(5))))
                .doesNotContain(commandId);
    }

    @Test
    void aCompletedAckMakesTheReportedVolumeAuthoritative() {
        String commandId = issue(100);
        ackService.apply(gatewayId, ack(commandId, "accepted", null, null, null));

        assertThat(ackService.apply(
                        gatewayId, ack(commandId, "completed", 96, 12_000, "volume_reached")))
                .isEqualTo(AckResult.APPLIED);

        DeviceCommand command = commands.findById(commandId).orElseThrow();
        assertThat(command.state()).isEqualTo(CommandState.COMPLETED);
        assertThat(command.actualMl()).isEqualTo(96);
        assertThat(command.actualRuntimeMs()).isEqualTo(12_000);
        assertThat(command.stopCause()).isEqualTo("volume_reached");
        // 96, not the 100 authorised: the budget now runs on measurement.
        assertThat(consumed()).isEqualTo(96);
    }

    @Test
    void aRejectedAckTakesTheVolumeBackOffTheBudget() {
        String commandId = issue(150);

        assertThat(ackService.apply(
                        gatewayId, ack(commandId, "rejected", null, null, null, "INTERLOCK_COOLDOWN")))
                .isEqualTo(AckResult.APPLIED);

        DeviceCommand command = commands.findById(commandId).orElseThrow();
        assertThat(command.state()).isEqualTo(CommandState.REJECTED);
        assertThat(command.stopCause()).isEqualTo("INTERLOCK_COOLDOWN");
        // The only report that provably moved no water.
        assertThat(consumed()).isZero();
    }

    @Test
    void anAbortedAckStillCountsWhatRan() {
        String commandId = issue(150);

        assertThat(ackService.apply(gatewayId, ack(commandId, "aborted", 40, 3_020, "watchdog")))
                .isEqualTo(AckResult.APPLIED);

        assertThat(commands.findById(commandId).orElseThrow().state())
                .isEqualTo(CommandState.ABORTED);
        assertThat(consumed()).isEqualTo(40);
    }

    // -- idempotency, which the guarded UPDATE gives for free --------------

    @Test
    void aRedeliveredCompletedAckChangesNothingASecondTime() {
        String commandId = issue(100);
        ackService.apply(gatewayId, ack(commandId, "completed", 96, 12_000, "volume_reached"));

        // QoS 1 promises duplicates on both hops. Without the allowed-from guard
        // this second delivery would re-apply the volume.
        assertThat(ackService.apply(
                        gatewayId, ack(commandId, "completed", 96, 12_000, "volume_reached")))
                .isEqualTo(AckResult.IGNORED);
        assertThat(consumed()).isEqualTo(96);
    }

    @Test
    void anOutOfOrderAcceptedAckCannotResurrectAFinishedCommand() {
        String commandId = issue(100);
        ackService.apply(gatewayId, ack(commandId, "completed", 96, 12_000, "volume_reached"));

        assertThat(ackService.apply(gatewayId, ack(commandId, "accepted", null, null, null)))
                .isEqualTo(AckResult.IGNORED);
        assertThat(commands.findById(commandId).orElseThrow().state())
                .isEqualTo(CommandState.COMPLETED);
    }

    // -- EXPIRED is not quite terminal -------------------------------------

    @Test
    void aLateRejectedAckDemotesAnExpiredCommand() {
        String commandId = issue(150);
        commands.markExpired(commandId, Instant.now().plus(Duration.ofMinutes(5)));
        assertThat(consumed()).isEqualTo(150);

        assertThat(ackService.apply(gatewayId, ack(commandId, "rejected", null, null, null, "EXPIRED")))
                .isEqualTo(AckResult.APPLIED);

        // Worth overwriting a terminal state: it is the only ack that can say
        // "no water moved", and it removes a phantom 150 mL from the budget.
        assertThat(commands.findById(commandId).orElseThrow().state())
                .isEqualTo(CommandState.REJECTED);
        assertThat(consumed()).isZero();
    }

    @Test
    void aLateCompletedAckCorrectsAnExpiredCommandWithoutDoubleCounting() {
        String commandId = issue(150);
        commands.markExpired(commandId, Instant.now().plus(Duration.ofMinutes(5)));

        assertThat(ackService.apply(
                        gatewayId, ack(commandId, "completed", 118, 15_000, "volume_reached")))
                .isEqualTo(AckResult.APPLIED);

        assertThat(commands.findById(commandId).orElseThrow().state())
                .isEqualTo(CommandState.COMPLETED);
        // 118, not 150 + 118: the row is updated, never added to.
        assertThat(consumed()).isEqualTo(118);
    }

    @Test
    void aLateAbortedAckLeavesAnExpiredCommandAlone() {
        String commandId = issue(150);
        commands.markExpired(commandId, Instant.now().plus(Duration.ofMinutes(5)));

        assertThat(ackService.apply(gatewayId, ack(commandId, "aborted", 40, 3_000, "watchdog")))
                .isEqualTo(AckResult.IGNORED);

        // Both states already mean "water may have moved" and both count at
        // granted_ml, so the conservative 150 stays.
        assertThat(commands.findById(commandId).orElseThrow().state())
                .isEqualTo(CommandState.EXPIRED);
        assertThat(consumed()).isEqualTo(150);
    }

    // -- authentication ----------------------------------------------------

    @Test
    void anAckFromTheWrongGatewayCannotSpendAnotherGatewaysBudget() {
        String commandId = issue(150);

        AckResult result = ackService.apply(
                "orangepi-impostor", ack(commandId, "rejected", null, null, null, "OK"));

        // The attack this blocks: completing or rejecting someone else's command
        // lowers their consumed volume for the day and buys extra doses out of
        // their budget.
        assertThat(result).isEqualTo(AckResult.DROPPED);
        assertThat(commands.findById(commandId).orElseThrow().state())
                .isEqualTo(CommandState.ISSUED);
        assertThat(consumed()).isEqualTo(150);
    }

    @Test
    void anAckForAnUnknownCommandIsDropped() {
        assertThat(ackService.apply(gatewayId, ack("01UNKNOWNCOMMANDID", "completed", 10, 1_000, "x")))
                .isEqualTo(AckResult.DROPPED);
    }

    // -- robustness against the three-way reason vocabulary ----------------

    @Test
    void anUnrecognisedPhaseIsDroppedRatherThanThrown() {
        String commandId = issue(100);

        // "expired" is a legitimate *reason* on the wire but not a phase. The
        // state machine must not treat the two vocabularies as one.
        assertThat(ackService.apply(gatewayId, ack(commandId, "expired", null, null, null)))
                .isEqualTo(AckResult.DROPPED);
        assertThat(commands.findById(commandId).orElseThrow().state())
                .isEqualTo(CommandState.ISSUED);
    }

    @Test
    void anUnknownReasonStillAppliesTheTransition() {
        String commandId = issue(100);

        // The firmware, the MQTT contract and DenyReason spell this vocabulary
        // three different ways. A value none of them knows must not cost us the
        // state change — it is stored as text and the phase does the deciding.
        assertThat(ackService.apply(
                        gatewayId,
                        ack(commandId, "rejected", null, null, null, "something_nobody_defined")))
                .isEqualTo(AckResult.APPLIED);
        assertThat(commands.findById(commandId).orElseThrow().stopCause())
                .isEqualTo("something_nobody_defined");
    }

    @Test
    void anOverlongReasonIsTruncatedToFitTheColumn() {
        String commandId = issue(100);
        String tooLong = "REASON_LONGER_THAN_THIRTY_CHARACTERS_BY_FAR";

        assertThat(ackService.apply(gatewayId, ack(commandId, "rejected", null, null, null, tooLong)))
                .isEqualTo(AckResult.APPLIED);

        // Losing the tail of a diagnostic string must never cost the transition.
        assertThat(commands.findById(commandId).orElseThrow().stopCause())
                .isEqualTo(tooLong.substring(0, 30));
    }

    @Test
    void theExecutionStopCauseWinsOverTheGenericOkReason() {
        String commandId = issue(100);

        ackService.apply(gatewayId, new CommandAck(
                commandId, "completed", null, "OK", POT_ID, 96, 12_000, "volume_reached"));

        assertThat(commands.findById(commandId).orElseThrow().stopCause())
                .isEqualTo("volume_reached");
    }

    // -- the device's clock is not trusted outright ------------------------

    @Test
    void anAckTimestampFromTheFutureIsClampedToNow() {
        String commandId = issue(100);
        Instant now = Instant.now();

        ackService.apply(gatewayId, new CommandAck(
                commandId, "completed", now.plus(Duration.ofDays(30)), "OK", POT_ID,
                96, 12_000, "volume_reached"));

        // A completed_at thirty days out would block this pot on the cooldown
        // gate for thirty days.
        assertThat(commands.findById(commandId).orElseThrow().completedAt())
                .isBeforeOrEqualTo(Instant.now());
    }

    @Test
    void anAckTimestampBeforeTheCommandIsClampedToIssueTime() {
        String commandId = issue(100);
        DeviceCommand issued = commands.findById(commandId).orElseThrow();

        ackService.apply(gatewayId, new CommandAck(
                commandId, "completed", issued.issuedAt().minus(Duration.ofDays(1)), "OK",
                POT_ID, 96, 12_000, "volume_reached"));

        // Backdating a completion retires the six-hour cooldown early, which is
        // the direction that over-waters.
        assertThat(commands.findById(commandId).orElseThrow().completedAt())
                .isEqualTo(issued.issuedAt());
    }

    // -- the announcement a completion earns -------------------------------

    @Test
    void aCompletedPumpAckAnnouncesTheIrrigationOnce() {
        long userId = claimOwner();
        String commandId = issue(100);

        ackService.apply(gatewayId, ack(commandId, "completed", 96, 12_000, "volume_reached"));
        // QoS 1 promises duplicates on both hops. The announcement rides on the
        // guarded UPDATE rather than on a suppression window of its own: no rows
        // changed means nothing happened means nobody is told.
        ackService.apply(gatewayId, ack(commandId, "completed", 96, 12_000, "volume_reached"));

        List<IrrigationCompletedEvent> announced =
                events.stream(IrrigationCompletedEvent.class).toList();
        assertThat(announced).hasSize(1);
        assertThat(announced.getFirst().userId()).isEqualTo(userId);
        assertThat(announced.getFirst().potId()).isEqualTo(POT_ID);
        assertThat(announced.getFirst().commandId()).isEqualTo(commandId);
        assertThat(announced.getFirst().actualMilliliters())
                .isEqualByComparingTo(BigDecimal.valueOf(96));
    }

    @Test
    void aCompletedLightAckAnnouncesNoIrrigation() {
        claimOwner();
        String commandId = issueLight(true);

        // The one path where COMPLETED is not water: a light latch completes on
        // its accepted ack. Announcing "관수가 완료되었습니다" here would be a lie.
        assertThat(ackService.apply(gatewayId, ack(commandId, "accepted", null, null, null)))
                .isEqualTo(AckResult.APPLIED);

        assertThat(events.stream(IrrigationCompletedEvent.class)).isEmpty();
    }

    @Test
    void anUnclaimedDeviceHasNobodyToAnnounceTo() {
        String commandId = issue(100);

        assertThat(ackService.apply(
                        gatewayId, ack(commandId, "completed", 96, 12_000, "volume_reached")))
                .isEqualTo(AckResult.APPLIED);

        // The water still counts against the budget; there is simply no owner to
        // address the notification to.
        assertThat(consumed()).isEqualTo(96);
        assertThat(events.stream(IrrigationCompletedEvent.class)).isEmpty();
    }

    // -- fixtures ----------------------------------------------------------

    /** Gives this pot's gateway an owner, which is who an announcement is for. */
    private long claimOwner() {
        jdbcTemplate.update(
                "INSERT INTO app_user (email, password_hash, nickname) VALUES (?, ?, ?)",
                OWNER_EMAIL, "unused", "관수알림테스터");
        long userId = jdbcTemplate.queryForObject(
                "SELECT id FROM app_user WHERE email = ?", Long.class, OWNER_EMAIL);
        jdbcTemplate.update(
                "UPDATE device SET user_id = ? WHERE id = (SELECT device_id FROM pot WHERE id = ?)",
                userId, POT_ID);
        return userId;
    }

    private String issue(int grantedMl) {
        Instant issuedAt = Instant.now();
        String commandId = commandIdGenerator.next(issuedAt);
        commands.save(new DeviceCommand(
                commandId, POT_ID, "evt-" + commandId,
                DeviceCommand.ACTUATOR_PUMP, DeviceCommand.ACTION_DOSE,
                grantedMl, 20_000, CommandState.ISSUED,
                issuedAt, issuedAt.plus(Duration.ofMinutes(2)),
                null, null, null, null, null, CommandOrigin.CLOUD));
        return commandId;
    }

    private String issueLight(boolean on) {
        Instant issuedAt = Instant.now();
        String commandId = commandIdGenerator.next(issuedAt);
        commands.save(DeviceCommand.issuedLight(
                commandId, POT_ID, "evt-" + commandId, on, issuedAt,
                issuedAt.plus(Duration.ofMinutes(2))));
        return commandId;
    }

    private CommandAck ack(
            String commandId, String phase, Integer ml, Integer runtimeMs, String stopCause) {
        return ack(commandId, phase, ml, runtimeMs, stopCause, "OK");
    }

    private CommandAck ack(
            String commandId,
            String phase,
            Integer ml,
            Integer runtimeMs,
            String stopCause,
            String reason) {
        return new CommandAck(
                commandId, phase, Instant.now(), reason, POT_ID, ml, runtimeMs, stopCause);
    }

    private int consumed() {
        return commands.consumedMlSince(POT_ID, Instant.now().minus(Duration.ofHours(24)));
    }
}
