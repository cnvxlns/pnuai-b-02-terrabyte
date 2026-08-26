package com.terrabyte.backend.notification;

import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.terrabyte.backend.irrigation.CommandAck;
import com.terrabyte.backend.irrigation.CommandAckService;
import com.terrabyte.backend.irrigation.CommandIdGenerator;
import com.terrabyte.backend.irrigation.CommandOrigin;
import com.terrabyte.backend.irrigation.CommandState;
import com.terrabyte.backend.irrigation.DeviceCommand;
import com.terrabyte.backend.irrigation.DeviceCommandRepository;
import com.terrabyte.backend.measurement.MeasurementStore;
import com.terrabyte.backend.measurement.MeasurementService;
import com.terrabyte.backend.measurement.TelemetryEnvelope;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.bean.override.mockito.MockitoBean;

@SpringBootTest
@ActiveProfiles("test")
class NotificationTriggerIntegrationTests {

    private static final String HARDWARE_ID = "orangepi-pro-01";

    @Autowired
    private MeasurementService measurementService;

    @Autowired
    private NotificationService notificationService;

    @Autowired
    private NotificationDeliveryWorker deliveryWorker;

    @Autowired
    private CommandAckService ackService;

    @Autowired
    private DeviceCommandRepository commands;

    @Autowired
    private CommandIdGenerator commandIdGenerator;

    @Autowired
    @Qualifier("postgresJdbcTemplate")
    private JdbcTemplate jdbcTemplate;

    @MockitoBean
    private MeasurementStore measurementStore;

    @MockitoBean
    private PushSender pushSender;

    private long userId;

    @BeforeEach
    void prepareClaimedDevice() {
        jdbcTemplate.update("DELETE FROM notification_delivery");
        jdbcTemplate.update("DELETE FROM notification_event");
        jdbcTemplate.update("DELETE FROM notification_condition_state");
        jdbcTemplate.update("DELETE FROM push_registration");
        jdbcTemplate.update("DELETE FROM telemetry_event");
        jdbcTemplate.update("DELETE FROM device_command");
        jdbcTemplate.update("DELETE FROM app_user WHERE email = ?", "trigger-owner@example.com");
        jdbcTemplate.update(
                "INSERT INTO app_user (email, password_hash, nickname) VALUES (?, ?, ?)",
                "trigger-owner@example.com", "unused", "트리거테스터");
        userId = jdbcTemplate.queryForObject(
                "SELECT id FROM app_user WHERE email = ?",
                Long.class,
                "trigger-owner@example.com");
        jdbcTemplate.update(
                "UPDATE device SET user_id = ?, status = 'OFFLINE', last_seen_at = NULL"
                        + " WHERE hardware_id = ?",
                userId,
                HARDWARE_ID);
        jdbcTemplate.update(
                "UPDATE pot SET node_id = NULL, status = 'OFFLINE'"
                        + " WHERE device_id = (SELECT id FROM device WHERE hardware_id = ?)",
                HARDWARE_ID);
        jdbcTemplate.update(
                "UPDATE pot SET node_id = 'pot-alert' WHERE id = ("
                        + "SELECT MIN(id) FROM pot WHERE device_id = ("
                        + "SELECT id FROM device WHERE hardware_id = ?))",
                HARDWARE_ID);
        notificationService.register(userId,
                new RegisterPushTokenRequest("trigger-token", PushPlatform.ANDROID));
        when(pushSender.send(anyString(), any(PushMessage.class)))
                .thenReturn(PushSendResult.sent("test"));
    }

    @Test
    void mqttPresenceCreatesOneOfflineAlertUntilTheGatewayRecovers() {
        measurementService.updateGatewayPresence(HARDWARE_ID, false);
        measurementService.updateGatewayPresence(HARDWARE_ID, false);

        assertThat(countEvents(NotificationType.DEVICE_OFFLINE)).isEqualTo(1);

        measurementService.updateGatewayPresence(HARDWARE_ID, true);
        measurementService.updateGatewayPresence(HARDWARE_ID, false);

        assertThat(countEvents(NotificationType.DEVICE_OFFLINE)).isEqualTo(2);
        assertThat(deliveryWorker.drainOnce()).isEqualTo(2);
        verify(pushSender, times(2)).send(eq("trigger-token"), any(PushMessage.class));
    }

    @Test
    void invalidSensorQualityCreatesAnAlertAndValidQualityResolvesIt() {
        measurementService.ingest(HARDWARE_ID, envelope(false));
        measurementService.ingest(HARDWARE_ID, envelope(false));
        assertThat(countEvents(NotificationType.SENSOR_ANOMALY)).isEqualTo(1);

        measurementService.ingest(HARDWARE_ID, envelope(true));
        measurementService.ingest(HARDWARE_ID, envelope(false));
        assertThat(countEvents(NotificationType.SENSOR_ANOMALY)).isEqualTo(2);
    }

    /**
     * The whole chain, from what the gateway reported to what the phone shows.
     *
     * <p>Driven through {@link CommandAckService} rather than by publishing the
     * event by hand: the event contract and its listener were both in place long
     * before anything published one, so a test that publishes it itself passes
     * whether or not the ack path is wired at all.
     */
    @Test
    void aCompletedIrrigationAckReachesThePhoneOncePerCommand() {
        long deviceId = jdbcTemplate.queryForObject(
                "SELECT id FROM device WHERE hardware_id = ?", Long.class, HARDWARE_ID);
        long potId = jdbcTemplate.queryForObject(
                "SELECT id FROM pot WHERE device_id = ? AND node_id = 'pot-alert'",
                Long.class,
                deviceId);
        String commandId = issuePumpCommand(potId);

        ackService.apply(HARDWARE_ID, completedAck(commandId, potId, 250));
        // The gateway's redelivery, which must not become a second alert.
        ackService.apply(HARDWARE_ID, completedAck(commandId, potId, 250));

        assertThat(countEvents(NotificationType.IRRIGATION_COMPLETED)).isEqualTo(1);
        assertThat(deliveryWorker.drainOnce()).isEqualTo(1);
        verify(pushSender, times(1)).send(eq("trigger-token"), any(PushMessage.class));
    }

    private String issuePumpCommand(long potId) {
        Instant issuedAt = Instant.now();
        String commandId = commandIdGenerator.next(issuedAt);
        commands.save(new DeviceCommand(
                commandId, potId, "evt-" + commandId,
                DeviceCommand.ACTUATOR_PUMP, DeviceCommand.ACTION_DOSE,
                250, 20_000, CommandState.ISSUED,
                issuedAt, issuedAt.plus(Duration.ofMinutes(2)),
                null, null, null, null, null, CommandOrigin.CLOUD));
        return commandId;
    }

    private CommandAck completedAck(String commandId, long potId, int actualMl) {
        return new CommandAck(
                commandId, "completed", Instant.now(), "OK", potId, actualMl, 12_000,
                "volume_reached");
    }

    private int countEvents(NotificationType type) {
        Integer count = jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM notification_event WHERE user_id = ? AND type = ?",
                Integer.class,
                userId,
                type.name());
        return count == null ? 0 : count;
    }

    private TelemetryEnvelope envelope(boolean airSensorValid) {
        return new TelemetryEnvelope(
                2,
                "telemetry.sample",
                HARDWARE_ID,
                UUID.randomUUID().toString(),
                Instant.now().minusSeconds(2),
                List.of(new TelemetryEnvelope.Node(
                        "pot-alert",
                        1,
                        new TelemetryEnvelope.Measurements(
                                27.0, 58.0, null, 230.0, null, null, null),
                        new TelemetryEnvelope.Quality(airSensorValid, true, null),
                        null)));
    }
}
