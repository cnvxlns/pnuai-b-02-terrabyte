package com.terrabyte.backend.mqtt;

import static org.assertj.core.api.Assertions.assertThat;

import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;

import com.terrabyte.backend.irrigation.CommandOrigin;
import com.terrabyte.backend.irrigation.CommandState;
import com.terrabyte.backend.irrigation.DeviceCommand;
import com.terrabyte.backend.irrigation.DeviceCommandRepository;
import com.terrabyte.backend.measurement.MeasurementStore;

import org.eclipse.paho.client.mqttv3.MqttMessage;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.bean.override.mockito.MockitoBean;

/**
 * Water the gateway delivered on its own, arriving after the cloud came back.
 *
 * <p>The failure this path exists to prevent is not losing a record — it is the
 * server authorising a second dose on top of one it never heard about.
 */
@SpringBootTest
@ActiveProfiles("test")
class EdgeIrrigationHandlerTests {

    private static final long POT_ID = 1L;
    private static final String NODE_ID = "terrabyte-node-01";

    @Autowired private EdgeIrrigationHandler handler;
    @Autowired private DeviceCommandRepository commands;

    @Autowired
    @Qualifier("postgresJdbcTemplate")
    private JdbcTemplate jdbcTemplate;

    @MockitoBean private MeasurementStore measurementStore;

    private String gatewayId;

    @BeforeEach
    void setUp() {
        jdbcTemplate.update("DELETE FROM device_command");
        jdbcTemplate.update("UPDATE pot SET node_id = ? WHERE id = ?", NODE_ID, POT_ID);
        gatewayId = jdbcTemplate.queryForObject(
                "SELECT d.hardware_id FROM device d JOIN pot p ON p.device_id = d.id"
                        + " WHERE p.id = ?",
                String.class, POT_ID);
    }

    @AfterEach
    void tearDown() {
        jdbcTemplate.update("DELETE FROM device_command");
        jdbcTemplate.update("UPDATE pot SET node_id = NULL WHERE id = ?", POT_ID);
    }

    @Test
    void aRecordBecomesACompletedEdgeFallbackCommand() {
        assertThat(handler.handle(gatewayId, message("rec-1", 60.0))).isTrue();

        DeviceCommand command = commands.findById("rec-1").orElseThrow();
        assertThat(command.potId()).isEqualTo(POT_ID);
        assertThat(command.origin()).isEqualTo(CommandOrigin.EDGE_FALLBACK);
        // Terminal on arrival: it already happened, and nothing is coming later
        // to change it.
        assertThat(command.state()).isEqualTo(CommandState.COMPLETED);
        assertThat(command.actualMl()).isEqualTo(60);
    }

    @Test
    void theWaterCountsAgainstTheDailyBudgetWithNoExtraCode() {
        handler.handle(gatewayId, message("rec-1", 60.0));

        // consumedMlSince already sums COALESCE(actual_ml, granted_ml) over every
        // non-rejected pump row, so the Governor sees this without knowing the
        // edge exists.
        assertThat(commands.consumedMlSince(POT_ID, Instant.now().minus(Duration.ofHours(24))))
                .isEqualTo(60);
    }

    @Test
    void aRedeliveredRecordIsCountedOnce() {
        handler.handle(gatewayId, message("rec-1", 60.0));
        handler.handle(gatewayId, message("rec-1", 60.0));

        // QoS 1 on the gateway's hop and a control queue that retries until the
        // publish is acknowledged: duplicates are ordinary traffic here.
        assertThat(commands.consumedMlSince(POT_ID, Instant.now().minus(Duration.ofHours(24))))
                .isEqualTo(60);
    }

    @Test
    void aRecordForAnotherGatewaysNodeIsRefused() {
        assertThat(handler.handle("orangepi-pro-99", message("rec-1", 60.0))).isTrue();

        // The topic's gateway segment is the only authenticated identity on this
        // path. Trusting a node id alone would let one gateway spend another
        // pot's budget.
        assertThat(commands.findById("rec-1")).isEmpty();
    }

    @Test
    void anUnparsableRecordIsDroppedRatherThanRedelivered() {
        MqttMessage garbage = new MqttMessage("not json".getBytes(StandardCharsets.UTF_8));

        assertThat(handler.handle(gatewayId, garbage)).isTrue();
        assertThat(commands.findById("rec-1")).isEmpty();
    }

    @Test
    void aRecordThatDeliveredNothingIsNotAnIrrigation() {
        assertThat(handler.handle(gatewayId, message("rec-1", 0.0))).isTrue();

        // Zero millilitres is a bookkeeping event, not water. Recording it would
        // charge the budget nothing but would still occupy a command id.
        assertThat(commands.findById("rec-1")).isEmpty();
    }

    private MqttMessage message(String recordId, double volumeMl) {
        String body = """
                {"schema_version":2,"message_type":"edge_irrigation","gateway_id":"%s",
                 "record_id":"%s","node_id":"%s","volume_ml":%s,
                 "dispensed_at":"2026-08-27T01:02:03Z","origin":"EDGE_FALLBACK"}
                """.formatted(gatewayId, recordId, NODE_ID, volumeMl);
        return new MqttMessage(body.getBytes(StandardCharsets.UTF_8));
    }
}
