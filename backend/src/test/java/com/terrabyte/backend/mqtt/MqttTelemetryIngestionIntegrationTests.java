package com.terrabyte.backend.mqtt;

import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.awaitility.Awaitility.await;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.terrabyte.backend.measurement.MeasurementStore;
import com.terrabyte.backend.measurement.TelemetryEnvelope;
import com.terrabyte.backend.measurement.TelemetrySample;
import org.eclipse.paho.client.mqttv3.MqttClient;
import org.eclipse.paho.client.mqttv3.MqttConnectOptions;
import org.eclipse.paho.client.mqttv3.MqttMessage;
import org.eclipse.paho.client.mqttv3.persist.MemoryPersistence;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.testcontainers.containers.GenericContainer;
import org.testcontainers.containers.wait.strategy.Wait;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;
import org.testcontainers.utility.DockerImageName;
import org.testcontainers.utility.MountableFile;

/**
 * End-to-end proof that the MQTT transport (issue #45) ingests exactly the
 * way the HTTP debug endpoint does, using a real broker rather than a mock
 * of the Paho client.
 *
 * <p>Runs against a disposable {@code eclipse-mosquitto:2} Testcontainer
 * rather than an embedded broker, so the wire behaviour (manual acks, QoS 1,
 * persistent session, Last Will) is exercised for real instead of simulated.
 * The container config at {@code mqtt-test/mosquitto.conf} allows anonymous
 * access for this test only — it is not the production broker config under
 * {@code infra/mosquitto/}, which deliberately forbids that.
 *
 * <p>{@code disabledWithoutDocker = true} makes the whole class a skip, not
 * a failure, when Docker is not available — required so CI without Docker
 * stays green.
 */
@Testcontainers(disabledWithoutDocker = true)
@SpringBootTest
@ActiveProfiles("test")
class MqttTelemetryIngestionIntegrationTests {

    // orangepi-pro-01 (serial_code 483920) is the one seeded device that
    // survives the whole suite: DeviceApiIntegrationTests permanently deletes
    // every other seeded device row (including orangepi-pro-02) from the
    // shared in-memory H2 database, so a test relying on any other hardware
    // id would pass in isolation but fail depending on suite ordering.
    private static final String HARDWARE_ID = "orangepi-pro-01";
    // Deliberately not a real device: the mismatch below is rejected before
    // any device lookup happens, so it only needs to differ from HARDWARE_ID.
    private static final String OTHER_HARDWARE_ID = "some-other-gateway";
    private static final String NODE_ID = "pot-mqtt-01";
    private static final Duration AWAIT_TIMEOUT = Duration.ofSeconds(10);

    @Container
    static final GenericContainer<?> MOSQUITTO = new GenericContainer<>(
            DockerImageName.parse("eclipse-mosquitto:2"))
            .withExposedPorts(1883)
            .withCopyFileToContainer(
                    MountableFile.forClasspathResource("mqtt-test/mosquitto.conf"),
                    "/mosquitto/config/mosquitto.conf")
            .waitingFor(Wait.forListeningPort());

    @DynamicPropertySource
    static void mqttProperties(DynamicPropertyRegistry registry) {
        registry.add("app.mqtt.enabled", () -> "true");
        registry.add("app.mqtt.url", () -> "tcp://" + MOSQUITTO.getHost()
                + ":" + MOSQUITTO.getMappedPort(1883));
        registry.add("app.mqtt.client-id", () -> "terrabyte-backend-test-" + UUID.randomUUID());
        registry.add("app.mqtt.username", () -> "test-user");
        registry.add("app.mqtt.password", () -> "test-pass");
        registry.add("app.mqtt.clean-session", () -> "false");
    }

    @Autowired
    private ObjectMapper objectMapper;

    @Autowired
    @Qualifier("postgresJdbcTemplate")
    private JdbcTemplate jdbcTemplate;

    @MockitoBean
    private MeasurementStore measurementStore;

    private MqttClient publisher;

    @BeforeEach
    void resetDataAndConnectPublisher() throws Exception {
        jdbcTemplate.update("""
                UPDATE device
                SET user_id = NULL, space_id = NULL, claimed_at = NULL,
                    status = 'OFFLINE', last_seen_at = NULL
                """);
        jdbcTemplate.update(
                "UPDATE pot SET node_id=NULL,crop_code=NULL,crop_selected_at=NULL,"
                        + "status='OFFLINE',last_seen_at=NULL");
        jdbcTemplate.update("DELETE FROM telemetry_event");

        publisher = new MqttClient(
                "tcp://" + MOSQUITTO.getHost() + ":" + MOSQUITTO.getMappedPort(1883),
                "terrabyte-test-publisher-" + UUID.randomUUID(),
                new MemoryPersistence());
        MqttConnectOptions options = new MqttConnectOptions();
        options.setUserName("publisher");
        options.setPassword("publisher".toCharArray());
        options.setCleanSession(true);
        publisher.connect(options);
    }

    @AfterEach
    void disconnectPublisher() throws Exception {
        if (publisher != null && publisher.isConnected()) {
            publisher.disconnect();
        }
        publisher.close();
    }

    @Test
    void ingestsAValidEnvelopePublishedOverMqttJustLikeTheHttpPath() throws Exception {
        String eventId = UUID.randomUUID().toString();
        publish(telemetryTopic(HARDWARE_ID), envelopeJson(HARDWARE_ID, NODE_ID, eventId, 27.1, 58.0));

        awaitOneWrite();

        org.mockito.ArgumentCaptor<TelemetrySample> captor =
                org.mockito.ArgumentCaptor.forClass(TelemetrySample.class);
        verify(measurementStore, times(1)).write(captor.capture());
        TelemetrySample sample = captor.getValue();
        assertThat(sample.hardwareDeviceId()).isEqualTo(HARDWARE_ID);
        assertThat(sample.nodeId()).isEqualTo(NODE_ID);
        assertThat(sample.eventId()).isEqualTo(eventId);

        Long deviceId = jdbcTemplate.queryForObject(
                "SELECT id FROM device WHERE hardware_id = ?", Long.class, HARDWARE_ID);
        assertThat(sample.deviceId()).isEqualTo(deviceId);
        String potNode = jdbcTemplate.queryForObject(
                "SELECT node_id FROM pot WHERE id = ?", String.class, sample.potId());
        assertThat(potNode).isEqualTo(NODE_ID);
    }

    /**
     * Proves the gateway identity used for storage attribution comes from the
     * TOPIC segment, not the payload's {@code gateway_id} field.
     *
     * <p>First publishes under {@code HARDWARE_ID}'s topic with a payload that
     * claims to be a different, also-real gateway ({@code OTHER_HARDWARE_ID}).
     * {@link com.terrabyte.backend.measurement.MeasurementService#ingest}
     * rejects that as a transport/payload mismatch, so no write happens for
     * either device — a payload cannot borrow another gateway's topic identity
     * or vice versa. It then publishes a second, internally-consistent
     * envelope on the same connection and confirms it is stored under the
     * device the TOPIC named, proving the subscriber is unharmed by the
     * rejection and that topic identity is what governs attribution.
     */
    @Test
    void usesTheGatewayIdFromTheTopicRatherThanThePayload() throws Exception {
        String mismatchedEventId = UUID.randomUUID().toString();
        publish(telemetryTopic(HARDWARE_ID),
                envelopeJson(OTHER_HARDWARE_ID, NODE_ID, mismatchedEventId, 27.1, 58.0));

        // Give the mismatched publish a moment to be handled and confirm it
        // never produces a write, for either device.
        await().pollDelay(Duration.ofSeconds(2))
                .atMost(AWAIT_TIMEOUT)
                .untilAsserted(() -> verify(measurementStore, never()).write(any(TelemetrySample.class)));

        String matchedEventId = UUID.randomUUID().toString();
        publish(telemetryTopic(HARDWARE_ID),
                envelopeJson(HARDWARE_ID, NODE_ID, matchedEventId, 27.1, 58.0));

        awaitOneWrite();

        org.mockito.ArgumentCaptor<TelemetrySample> captor =
                org.mockito.ArgumentCaptor.forClass(TelemetrySample.class);
        verify(measurementStore, times(1)).write(captor.capture());
        assertThat(captor.getValue().hardwareDeviceId()).isEqualTo(HARDWARE_ID);
        assertThat(captor.getValue().eventId()).isEqualTo(matchedEventId);
    }

    @Test
    void deduplicatesARedeliveredEventIdWithoutASecondWrite() throws Exception {
        String eventId = UUID.randomUUID().toString();
        String payload = envelopeJson(HARDWARE_ID, NODE_ID, eventId, 27.1, 58.0);

        // Simulates QoS 1's at-least-once guarantee redelivering the same
        // publish, e.g. after the gateway never saw the PUBACK.
        publish(telemetryTopic(HARDWARE_ID), payload);
        awaitOneWrite();
        publish(telemetryTopic(HARDWARE_ID), payload);

        // Give the duplicate a moment to reach the subscriber, then assert
        // the write count never grows past one.
        await().pollDelay(Duration.ofSeconds(2))
                .atMost(AWAIT_TIMEOUT)
                .untilAsserted(() -> verify(measurementStore, times(1)).write(any(TelemetrySample.class)));
    }

    @Test
    void dropsAMalformedPayloadWithoutKillingTheSubscriber() throws Exception {
        // Not valid JSON at all — must be dropped (and acked) rather than
        // leaving the message in-flight and stalling the session.
        publish(telemetryTopic(HARDWARE_ID), "{ not json");

        // A schema-invalid payload: fails bean validation (schemaVersion must
        // be 2, humidity must be <= 100).
        String invalidEnvelope = objectMapper.writeValueAsString(new TelemetryEnvelope(
                1,
                "telemetry.sample",
                HARDWARE_ID,
                UUID.randomUUID().toString(),
                Instant.now().minusSeconds(5),
                List.of(new TelemetryEnvelope.Node(
                        NODE_ID,
                        1,
                        new TelemetryEnvelope.Measurements(27.0, 999.0, 230.0, null, null, null),
                        new TelemetryEnvelope.Quality(true, true, null)))));
        publish(telemetryTopic(HARDWARE_ID), invalidEnvelope);

        // Neither bad message may ever produce a write.
        await().pollDelay(Duration.ofSeconds(1))
                .atMost(AWAIT_TIMEOUT)
                .untilAsserted(() -> verify(measurementStore, never()).write(any(TelemetrySample.class)));

        // The important assertion: a subsequent VALID message on the same
        // connection must still be ingested. If an exception had escaped the
        // Paho callback thread instead of being caught, delivery would have
        // silently stopped and this would never arrive.
        String eventId = UUID.randomUUID().toString();
        publish(telemetryTopic(HARDWARE_ID), envelopeJson(HARDWARE_ID, NODE_ID, eventId, 27.1, 58.0));

        awaitOneWrite();
        verify(measurementStore, times(1)).write(any(TelemetrySample.class));
    }

    @Test
    void aStatusMessageMarksTheGatewayOffline() throws Exception {
        jdbcTemplate.update(
                "UPDATE device SET status = 'ONLINE' WHERE hardware_id = ?", HARDWARE_ID);

        publish(statusTopic(HARDWARE_ID), "{\"online\": false}");

        await().atMost(AWAIT_TIMEOUT).untilAsserted(() -> {
            String status = jdbcTemplate.queryForObject(
                    "SELECT status FROM device WHERE hardware_id = ?", String.class, HARDWARE_ID);
            assertThat(status).isEqualTo("OFFLINE");
        });
    }

    private void awaitOneWrite() {
        await().atMost(AWAIT_TIMEOUT).untilAsserted(
                () -> verify(measurementStore, times(1)).write(any(TelemetrySample.class)));
    }

    private void publish(String topic, String payload) throws Exception {
        MqttMessage message = new MqttMessage(payload.getBytes(StandardCharsets.UTF_8));
        message.setQos(1);
        publisher.publish(topic, message);
    }

    private String telemetryTopic(String gatewayId) {
        return "tb/v2/" + gatewayId + "/up/telemetry";
    }

    private String statusTopic(String gatewayId) {
        return "tb/v2/" + gatewayId + "/up/status";
    }

    private String envelopeJson(
            String gatewayId, String nodeId, String eventId, double airTemperatureC, double humidity)
            throws Exception {
        TelemetryEnvelope envelope = new TelemetryEnvelope(
                2,
                "telemetry.sample",
                gatewayId,
                eventId,
                Instant.now().minusSeconds(5),
                List.of(new TelemetryEnvelope.Node(
                        nodeId,
                        1,
                        new TelemetryEnvelope.Measurements(
                                airTemperatureC, humidity, 230.5, null, null, null),
                        new TelemetryEnvelope.Quality(true, true, null))));
        return objectMapper.writeValueAsString(envelope);
    }
}
