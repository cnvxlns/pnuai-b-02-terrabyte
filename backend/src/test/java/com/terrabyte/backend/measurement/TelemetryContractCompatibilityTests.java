package com.terrabyte.backend.measurement;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.Instant;
import java.util.Set;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.json.JsonMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import jakarta.validation.ConstraintViolation;
import jakarta.validation.Validation;
import jakarta.validation.Validator;
import jakarta.validation.ValidatorFactory;
import org.junit.jupiter.api.Test;

/**
 * Pins the wire format against a payload copied verbatim from the edge bridge.
 *
 * <p>The v1 contract failed precisely here and nowhere else: both sides had
 * tests, both passed, and the two implementations still disagreed on six field
 * names, the URL, the auth header and the success code — so no telemetry could
 * ever arrive. Hand-written fixtures on the backend side cannot catch that,
 * because they encode the backend's own assumptions.
 *
 * <p>The JSON below is the literal output of
 * {@code Event.envelope_v2()} in {@code edge/pi/terrabyte_edge/protocol.py}. If
 * the edge changes its field names, this test must fail. Regenerate it with:
 *
 * <pre>
 * cd edge/pi &amp;&amp; python3 -c "from terrabyte_edge.protocol import Event; ..."
 * </pre>
 */
class TelemetryContractCompatibilityTests {

    /** Verbatim from the Orange Pi bridge — do not hand-edit to make a test pass. */
    private static final String EDGE_PAYLOAD = """
            {"schema_version":2,"event_type":"telemetry.sample",\
            "gateway_id":"orangepi-pro-01",\
            "event_id":"12345678-1234-5678-1234-567812345678",\
            "observed_at":"2026-07-21T04:05:06Z",\
            "nodes":[{"node_id":"terrabyte-node-01","sequence":42,\
            "measurements":{"air_temperature_c":24.5,"air_humidity_pct":61.2,\
            "plant_light_ppfd_umol_m2_s":382.0},\
            "quality":{"air_sensor_valid":true,"light_sensor_valid":true,\
            "soil_sensor_valid":false}}]}""";

    private final ObjectMapper objectMapper =
            JsonMapper.builder().addModule(new JavaTimeModule()).build();

    @Test
    void deserialisesThePayloadTheEdgeActuallyProduces() throws Exception {
        TelemetryEnvelope envelope =
                objectMapper.readValue(EDGE_PAYLOAD, TelemetryEnvelope.class);

        assertThat(envelope.schemaVersion()).isEqualTo(2);
        assertThat(envelope.eventType()).isEqualTo("telemetry.sample");
        assertThat(envelope.gatewayId()).isEqualTo("orangepi-pro-01");
        assertThat(envelope.eventId()).isEqualTo("12345678-1234-5678-1234-567812345678");
        assertThat(envelope.observedAt()).isEqualTo(Instant.parse("2026-07-21T04:05:06Z"));
        assertThat(envelope.nodes()).hasSize(1);

        TelemetryEnvelope.Node node = envelope.nodes().get(0);
        assertThat(node.nodeId()).isEqualTo("terrabyte-node-01");
        assertThat(node.sequence()).isEqualTo(42L);

        // The three renames that broke v1: the edge called these
        // relative_humidity_pct and ppfd_umol_m2_s, and sent captured_at_utc.
        assertThat(node.measurements().airTemperatureC()).isEqualTo(24.5);
        assertThat(node.measurements().airHumidityPct()).isEqualTo(61.2);
        assertThat(node.measurements().plantLightPpfdUmolM2S()).isEqualTo(382.0);
    }

    @Test
    void theEdgePayloadPassesBeanValidation() throws Exception {
        TelemetryEnvelope envelope =
                objectMapper.readValue(EDGE_PAYLOAD, TelemetryEnvelope.class);

        try (ValidatorFactory factory = Validation.buildDefaultValidatorFactory()) {
            Validator validator = factory.getValidator();
            Set<ConstraintViolation<TelemetryEnvelope>> violations = validator.validate(envelope);
            assertThat(violations).isEmpty();
        }
    }

    @Test
    void absentSoilMetricsSurviveAsNullRatherThanZero() throws Exception {
        TelemetryEnvelope envelope =
                objectMapper.readValue(EDGE_PAYLOAD, TelemetryEnvelope.class);
        TelemetryEnvelope.Measurements measurements = envelope.nodes().get(0).measurements();

        // No soil probe is wired into the current serial contract. The
        // distinction matters: a null soil reading must never reach the
        // irrigation path as a confident 0%, which would read as bone dry.
        assertThat(measurements.soilMoisturePct()).isNull();
        assertThat(measurements.soilTemperatureC()).isNull();
        assertThat(measurements.soilMoistureRawAdc()).isNull();
        assertThat(envelope.nodes().get(0).quality().soilSensorValidOrFalse()).isFalse();
    }

    /**
     * The soil probes are optional at the firmware level, so the edge emits a
     * second, wider shape once they are compiled in. Both must parse: pinning
     * only the three-metric payload would let the soil path rot unnoticed,
     * since nothing but this edge ever produces it.
     */
    private static final String EDGE_PAYLOAD_WITH_SOIL = """
            {"schema_version":2,"event_type":"telemetry.sample",\
            "gateway_id":"orangepi-pro-01",\
            "event_id":"12345678-1234-5678-1234-567812345678",\
            "observed_at":"2026-07-21T04:05:06Z",\
            "nodes":[{"node_id":"terrabyte-node-01","sequence":42,\
            "measurements":{"air_temperature_c":24.5,"air_humidity_pct":61.2,\
            "plant_light_ppfd_umol_m2_s":382.0,"soil_temperature_c":18.5,\
            "soil_moisture_pct":50.5},\
            "quality":{"air_sensor_valid":true,"light_sensor_valid":true,\
            "soil_sensor_valid":true}}]}""";

    @Test
    void deserialisesTheEdgePayloadThatCarriesSoilProbes() throws Exception {
        TelemetryEnvelope envelope =
                objectMapper.readValue(EDGE_PAYLOAD_WITH_SOIL, TelemetryEnvelope.class);
        TelemetryEnvelope.Node node = envelope.nodes().get(0);

        assertThat(node.measurements().soilTemperatureC()).isEqualTo(18.5);
        assertThat(node.measurements().soilMoisturePct()).isEqualTo(50.5);
        assertThat(node.quality().soilSensorValidOrFalse()).isTrue();

        try (ValidatorFactory factory = Validation.buildDefaultValidatorFactory()) {
            assertThat(factory.getValidator().validate(envelope)).isEmpty();
        }
    }

    /**
     * The edge computes its own irrigation volume from a water-balance formula
     * and ships it with the reading it came from. It is emitted only when the
     * edge can actually compute one, so both this shape and the two above have
     * to parse.
     */
    private static final String EDGE_PAYLOAD_WITH_SUGGESTION = """
            {"schema_version":2,"event_type":"telemetry.sample",\
            "gateway_id":"orangepi-pro-01",\
            "event_id":"12345678-1234-5678-1234-567812345678",\
            "observed_at":"2026-07-21T04:05:06Z",\
            "nodes":[{"node_id":"terrabyte-node-01","sequence":42,\
            "measurements":{"air_temperature_c":24.5,"air_humidity_pct":61.2,\
            "plant_light_ppfd_umol_m2_s":382.0,"soil_temperature_c":18.5,\
            "soil_moisture_pct":50.5},\
            "quality":{"air_sensor_valid":true,"light_sensor_valid":true,\
            "soil_sensor_valid":true},\
            "irrigation_suggestion":{"volume_ml":118,\
            "model_version":"water-balance-v1","assumed_crop_code":"lettuce",\
            "assumed_substrate_volume_ml":3000}}]}""";

    @Test
    void deserialisesTheEdgePayloadThatCarriesAnIrrigationSuggestion() throws Exception {
        TelemetryEnvelope envelope =
                objectMapper.readValue(EDGE_PAYLOAD_WITH_SUGGESTION, TelemetryEnvelope.class);
        IrrigationSuggestion suggestion = envelope.nodes().get(0).irrigationSuggestion();

        assertThat(suggestion).isNotNull();
        assertThat(suggestion.volumeMl()).isEqualTo(118);
        assertThat(suggestion.modelVersion()).isEqualTo("water-balance-v1");
        assertThat(suggestion.assumedCropCode()).isEqualTo("lettuce");
        assertThat(suggestion.assumedSubstrateVolumeMl()).isEqualTo(3000);

        try (ValidatorFactory factory = Validation.buildDefaultValidatorFactory()) {
            assertThat(factory.getValidator().validate(envelope)).isEmpty();
        }
    }

    @Test
    void aPayloadWithoutTheSuggestionBlockIsStillValid() throws Exception {
        // The block is optional on the wire. Requiring it would refuse every
        // reading from a node that cannot compute a dose — the exact mistake v1
        // made by requiring soil_moisture_raw_adc.
        TelemetryEnvelope envelope =
                objectMapper.readValue(EDGE_PAYLOAD, TelemetryEnvelope.class);

        assertThat(envelope.nodes().get(0).irrigationSuggestion()).isNull();
        try (ValidatorFactory factory = Validation.buildDefaultValidatorFactory()) {
            assertThat(factory.getValidator().validate(envelope)).isEmpty();
        }
    }

    @Test
    void aSuggestionAboveTheContractedCeilingFailsValidation() throws Exception {
        String tooLarge = EDGE_PAYLOAD_WITH_SUGGESTION.replace("\"volume_ml\":118", "\"volume_ml\":501");

        TelemetryEnvelope envelope = objectMapper.readValue(tooLarge, TelemetryEnvelope.class);

        try (ValidatorFactory factory = Validation.buildDefaultValidatorFactory()) {
            assertThat(factory.getValidator().validate(envelope)).isNotEmpty();
        }
    }

    @Test
    void aV1PayloadIsRejected() throws Exception {
        // v1 shape: schema_version 1, device_id, and the context block whose
        // zone_id used to smuggle the node id. No backward compatibility is
        // offered, so this must fail validation rather than silently ingest.
        String v1 = """
                {"schema_version":1,"event_type":"telemetry.sample",\
                "device_id":"orangepi-pro-01","observed_at":"2026-07-21T04:05:06Z",\
                "sequence":42,\
                "context":{"site_id":"s","zone_id":"terrabyte-node-01","soil_type":"loam",\
                "crop_type":"basil","calibration_version":"v1"},\
                "measurements":{"soil_moisture_pct":31.2,"soil_moisture_raw_adc":1847,\
                "air_temperature_c":24.5,"air_humidity_pct":61.2,\
                "plant_light_ppfd_umol_m2_s":382.0},\
                "quality":{"soil_sensor_valid":true,"air_sensor_valid":true,\
                "light_sensor_valid":true}}""";

        TelemetryEnvelope envelope = objectMapper
                .readerFor(TelemetryEnvelope.class)
                .without(com.fasterxml.jackson.databind.DeserializationFeature
                        .FAIL_ON_UNKNOWN_PROPERTIES)
                .readValue(v1);

        try (ValidatorFactory factory = Validation.buildDefaultValidatorFactory()) {
            Set<ConstraintViolation<TelemetryEnvelope>> violations =
                    factory.getValidator().validate(envelope);
            assertThat(violations).isNotEmpty();
        }
    }
}
