package com.terrabyte.backend.measurement;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.verify;

import com.terrabyte.backend.api.ApiException;
import com.terrabyte.backend.score.CropScoreProfileRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.bean.override.mockito.MockitoBean;

/**
 * Exercises {@link MeasurementService#ingest} directly.
 *
 * <p>Over MQTT, the gateway id comes from the topic, not the payload, so a
 * mismatch between the two can only be triggered by calling the service
 * layer with a transport id that disagrees with the envelope — the HTTP
 * debug endpoint always passes {@code envelope.gatewayId()} as the
 * transport id, so it can never observe this path.
 */
@SpringBootTest
@ActiveProfiles("test")
class MeasurementServiceIngestTests {

    private static final String HARDWARE_ID = "orangepi-pro-01";
    private static final String NODE_ID = "pot-01";

    @Autowired
    private MeasurementService measurementService;

    @Autowired
    @Qualifier("postgresJdbcTemplate")
    private JdbcTemplate jdbcTemplate;

    @MockitoBean
    private MeasurementStore measurementStore;

    @MockitoBean
    private CropScoreProfileRepository profileRepository;

    @BeforeEach
    void resetData() {
        jdbcTemplate.update("""
                UPDATE device
                SET user_id = NULL, space_id = NULL, claimed_at = NULL,
                    status = 'OFFLINE', last_seen_at = NULL
                """);
        jdbcTemplate.update("UPDATE pot SET node_id=NULL,crop_code=NULL,crop_selected_at=NULL,status='OFFLINE',last_seen_at=NULL");
        jdbcTemplate.update("DELETE FROM telemetry_event");
    }

    @Test
    void rejectsAPayloadWhoseGatewayIdDisagreesWithTheTransportGatewayId() {
        TelemetryEnvelope envelope = envelope(HARDWARE_ID, "node-x", UUID.randomUUID().toString());

        assertThatThrownBy(() -> measurementService.ingest("some-other-gateway", envelope))
                .isInstanceOf(ApiException.class)
                .satisfies(e -> {
                    ApiException apiException = (ApiException) e;
                    assertThat(apiException.status()).isEqualTo(HttpStatus.FORBIDDEN);
                    assertThat(apiException.code()).isEqualTo("GATEWAY_ID_MISMATCH");
                });
    }

    @Test
    void bindsTheNodeUsingTheNodeIdFromTheEnvelopeNode() {
        TelemetryEnvelope envelope = envelope(HARDWARE_ID, "node-from-envelope", UUID.randomUUID().toString());

        measurementService.ingest(HARDWARE_ID, envelope);

        String nodeId = jdbcTemplate.queryForObject(
                "SELECT node_id FROM pot WHERE device_id = "
                        + "(SELECT id FROM device WHERE hardware_id = ?) ORDER BY id LIMIT 1",
                String.class,
                HARDWARE_ID);
        assertThat(nodeId).isEqualTo("node-from-envelope");
        verify(measurementStore).write(any(TelemetrySample.class));
    }

    private TelemetryEnvelope envelope(String gatewayId, String nodeId, String eventId) {
        return new TelemetryEnvelope(
                2,
                "telemetry.sample",
                gatewayId,
                eventId,
                Instant.now().minusSeconds(5),
                List.of(new TelemetryEnvelope.Node(
                        nodeId,
                        1,
                        new TelemetryEnvelope.Measurements(27.0, 58.0, 230.0, null, null, null),
                        new TelemetryEnvelope.Quality(true, true, null),
                        null)));
    }
}
