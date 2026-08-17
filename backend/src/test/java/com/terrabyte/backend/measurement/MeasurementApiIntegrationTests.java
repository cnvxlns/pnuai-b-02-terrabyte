package com.terrabyte.backend.measurement;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.List;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.springframework.http.MediaType.APPLICATION_JSON;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.terrabyte.backend.auth.SignupRequest;
import com.terrabyte.backend.device.RegisterDeviceRequest;
import com.terrabyte.backend.score.CropScoreProfile;
import com.terrabyte.backend.score.CropScoreProfileRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

@SpringBootTest(properties = "app.telemetry.http-ingest.enabled=true")
@AutoConfigureMockMvc
@ActiveProfiles("test")
class MeasurementApiIntegrationTests {

    private static final String HARDWARE_ID = "orangepi-pro-01";
    private static final String SERIAL_CODE = "483920";
    private static final String NODE_ID = "pot-01";

    private final MockMvc mockMvc;
    private final ObjectMapper objectMapper;
    private final JdbcTemplate jdbcTemplate;

    @MockitoBean
    private MeasurementStore measurementStore;

    @MockitoBean
    private CropScoreProfileRepository profileRepository;

    @Autowired
    MeasurementApiIntegrationTests(
            MockMvc mockMvc,
            ObjectMapper objectMapper,
            @Qualifier("postgresJdbcTemplate") JdbcTemplate jdbcTemplate) {
        this.mockMvc = mockMvc;
        this.objectMapper = objectMapper;
        this.jdbcTemplate = jdbcTemplate;
    }

    @BeforeEach
    void resetData() {
        jdbcTemplate.update("""
                UPDATE device
                SET user_id = NULL, space_id = NULL, claimed_at = NULL,
                    status = 'OFFLINE', last_seen_at = NULL
                """);
        jdbcTemplate.update("UPDATE pot SET node_id=NULL,crop_code=NULL,crop_selected_at=NULL,status='OFFLINE',last_seen_at=NULL");
        jdbcTemplate.update("DELETE FROM app_user");
        jdbcTemplate.update("DELETE FROM telemetry_event");
    }

    @Test
    void acceptsHardwareTelemetryAndMarksDeviceOnline() throws Exception {
        Instant observedAt = Instant.now().minusSeconds(5);

        mockMvc.perform(post("/api/telemetry")
                        .contentType(APPLICATION_JSON)
                        .content(telemetryBody(HARDWARE_ID, UUID.randomUUID().toString(), observedAt, NODE_ID, 1042, 58.0)))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.accepted").value(true))
                .andExpect(jsonPath("$.hardwareDeviceId").value(HARDWARE_ID))
                .andExpect(jsonPath("$.sequence").value(1042));

        verify(measurementStore).write(any(TelemetrySample.class));
        String statusValue = jdbcTemplate.queryForObject(
                "SELECT status FROM device WHERE hardware_id = ?",
                String.class,
                HARDWARE_ID);
        assertThat(statusValue).isEqualTo("ONLINE");
    }

    @Test
    void rejectsSchemaVersionOneAndInvalidMeasurement() throws Exception {
        Instant observedAt = Instant.now().minusSeconds(5);

        mockMvc.perform(post("/api/telemetry")
                        .contentType(APPLICATION_JSON)
                        .content(telemetryBody(
                                1, HARDWARE_ID, UUID.randomUUID().toString(), observedAt, NODE_ID, 1042, 58.0)))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("VALIDATION_FAILED"));

        mockMvc.perform(post("/api/telemetry")
                        .contentType(APPLICATION_JSON)
                        .content(telemetryBody(HARDWARE_ID, UUID.randomUUID().toString(), observedAt, NODE_ID, 1042, 101.0)))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("VALIDATION_FAILED"));
    }

    @Test
    void rejectsUnknownHardwareDevice() throws Exception {
        mockMvc.perform(post("/api/telemetry")
                        .contentType(APPLICATION_JSON)
                        .content(telemetryBody(
                                "unknown-device", UUID.randomUUID().toString(),
                                Instant.now().minusSeconds(5), NODE_ID, 1, 58.0)))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value("DEVICE_NOT_FOUND"));
    }

    @Test
    void ignoresDuplicateEventIdWithoutWritingASecondSample() throws Exception {
        Instant observedAt = Instant.now().minusSeconds(5);
        String eventId = UUID.randomUUID().toString();
        String body = telemetryBody(HARDWARE_ID, eventId, observedAt, NODE_ID, 1042, 58.0);

        mockMvc.perform(post("/api/telemetry").contentType(APPLICATION_JSON).content(body))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.accepted").value(true));
        mockMvc.perform(post("/api/telemetry").contentType(APPLICATION_JSON).content(body))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.accepted").value(true));

        verify(measurementStore, times(1)).write(any(TelemetrySample.class));
    }

    @Test
    void acceptsEnvelopeWithoutSoilMoistureRawAdc() throws Exception {
        Instant observedAt = Instant.now().minusSeconds(5);
        String body = objectMapper.writeValueAsString(new TelemetryEnvelope(
                2,
                "telemetry.sample",
                HARDWARE_ID,
                UUID.randomUUID().toString(),
                observedAt,
                List.of(new TelemetryEnvelope.Node(
                        NODE_ID,
                        1,
                        new TelemetryEnvelope.Measurements(
                                27.1, 58.0, 230.5, null, null, null),
                        new TelemetryEnvelope.Quality(true, true, null),
                        null))));

        mockMvc.perform(post("/api/telemetry").contentType(APPLICATION_JSON).content(body))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.accepted").value(true));

        verify(measurementStore).write(any(TelemetrySample.class));
    }

    @Test
    void bindsOnePotPerNodeForAMultiNodeEnvelope() throws Exception {
        String token = signupAndGetToken();
        long deviceId = registerAndGetDeviceId(token);
        Instant observedAt = Instant.now().minusSeconds(5);

        String body = objectMapper.writeValueAsString(new TelemetryEnvelope(
                2,
                "telemetry.sample",
                HARDWARE_ID,
                UUID.randomUUID().toString(),
                observedAt,
                List.of(
                        node("node-a", 1),
                        node("node-b", 2))));

        mockMvc.perform(post("/api/telemetry").contentType(APPLICATION_JSON).content(body))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.sequence").value(2));

        List<String> nodeIds = jdbcTemplate.queryForList(
                "SELECT node_id FROM pot WHERE device_id = ? ORDER BY node_id", String.class, deviceId);
        assertThat(nodeIds).containsExactly("node-a", "node-b");
        verify(measurementStore, times(2)).write(any(TelemetrySample.class));
    }

    @Test
    void carriesSoilTemperatureFromEnvelopeThroughToTheStoredSample() throws Exception {
        Instant observedAt = Instant.now().minusSeconds(5);
        String body = objectMapper.writeValueAsString(new TelemetryEnvelope(
                2,
                "telemetry.sample",
                HARDWARE_ID,
                UUID.randomUUID().toString(),
                observedAt,
                List.of(new TelemetryEnvelope.Node(
                        NODE_ID,
                        1,
                        new TelemetryEnvelope.Measurements(
                                27.1, 58.0, 230.5, 19.4, 45.0, 1847L),
                        new TelemetryEnvelope.Quality(true, true, true),
                        null))));

        mockMvc.perform(post("/api/telemetry").contentType(APPLICATION_JSON).content(body))
                .andExpect(status().isAccepted());

        org.mockito.ArgumentCaptor<TelemetrySample> captor =
                org.mockito.ArgumentCaptor.forClass(TelemetrySample.class);
        verify(measurementStore).write(captor.capture());
        assertThat(captor.getValue().soilTemperatureC()).isEqualTo(19.4);
    }

    @Test
    void treatsAnAbsentSoilTemperatureAsNullRatherThanZero() throws Exception {
        Instant observedAt = Instant.now().minusSeconds(5);
        String body = objectMapper.writeValueAsString(new TelemetryEnvelope(
                2,
                "telemetry.sample",
                HARDWARE_ID,
                UUID.randomUUID().toString(),
                observedAt,
                List.of(new TelemetryEnvelope.Node(
                        NODE_ID,
                        1,
                        new TelemetryEnvelope.Measurements(
                                27.1, 58.0, 230.5, null, null, null),
                        new TelemetryEnvelope.Quality(true, true, null),
                        null))));

        mockMvc.perform(post("/api/telemetry").contentType(APPLICATION_JSON).content(body))
                .andExpect(status().isAccepted());

        org.mockito.ArgumentCaptor<TelemetrySample> captor =
                org.mockito.ArgumentCaptor.forClass(TelemetrySample.class);
        verify(measurementStore).write(captor.capture());
        // Must be null, not 0.0 — a confident 0°C is indistinguishable from a
        // real cold reading, whereas null correctly says "no probe wired in".
        assertThat(captor.getValue().soilTemperatureC()).isNull();
    }

    @Test
    void carriesTheEdgeIrrigationSuggestionThroughToTheStoredSample() throws Exception {
        // Raw JSON rather than a serialised record: this pins the wire names the
        // edge actually sends, which serialising our own record cannot do.
        String body = telemetryBodyWithSuggestion("""
                ,"irrigation_suggestion":{"volume_ml":118,\
                "model_version":"water-balance-v1",\
                "assumed_crop_code":"lettuce",\
                "assumed_substrate_volume_ml":3000}""");

        mockMvc.perform(post("/api/telemetry").contentType(APPLICATION_JSON).content(body))
                .andExpect(status().isAccepted());

        org.mockito.ArgumentCaptor<TelemetrySample> captor =
                org.mockito.ArgumentCaptor.forClass(TelemetrySample.class);
        verify(measurementStore).write(captor.capture());
        assertThat(captor.getValue().irrigationSuggestion())
                .isEqualTo(new IrrigationSuggestion(118, "water-balance-v1", "lettuce", 3000));
    }

    @Test
    void acceptsAnEnvelopeWithNoIrrigationSuggestionAtAll() throws Exception {
        // The edge omits the block whenever it cannot compute a dose, which is
        // an ordinary reading and must not be refused.
        mockMvc.perform(post("/api/telemetry")
                        .contentType(APPLICATION_JSON)
                        .content(telemetryBodyWithSuggestion("")))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.accepted").value(true));

        org.mockito.ArgumentCaptor<TelemetrySample> captor =
                org.mockito.ArgumentCaptor.forClass(TelemetrySample.class);
        verify(measurementStore).write(captor.capture());
        assertThat(captor.getValue().irrigationSuggestion()).isNull();
    }

    @Test
    void rejectsAnIrrigationSuggestionOutsideTheContractedRange() throws Exception {
        mockMvc.perform(post("/api/telemetry")
                        .contentType(APPLICATION_JSON)
                        .content(telemetryBodyWithSuggestion("""
                                ,"irrigation_suggestion":{"volume_ml":501,\
                                "model_version":"water-balance-v1"}""")))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("VALIDATION_FAILED"));
    }

    @Test
    void returnsLatestAndTimeSeriesForDeviceOwner() throws Exception {
        String token = signupAndGetToken();
        long deviceId = registerAndGetDeviceId(token);
        long potId = jdbcTemplate.queryForObject("SELECT MIN(id) FROM pot WHERE device_id=?", Long.class, deviceId);
        selectCrop(token, deviceId, "lettuce");
        TelemetrySample sample = sample(potId, deviceId, Instant.now().minusSeconds(5));
        when(measurementStore.findLatest(potId)).thenReturn(java.util.Optional.of(sample));
        when(measurementStore.findPoints(
                eq(potId),
                eq(MeasurementMetric.AIR_TEMPERATURE_C),
                any(Instant.class)))
                .thenReturn(List.of(new MeasurementPoint(sample.observedAt(), 27.1)));
        when(profileRepository.findActiveByCropCode("lettuce"))
                .thenReturn(java.util.Optional.of(profile("lettuce", "상추")));

        mockMvc.perform(get("/api/devices/{deviceId}/measurements/latest", deviceId)
                        .header("Authorization", bearer(token)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.hardwareDeviceId").value(HARDWARE_ID))
                .andExpect(jsonPath("$.measurements.airTemperatureC").value(27.1))
                .andExpect(jsonPath("$.measurements.plantLightPpfdUmolM2S").value(230.5))
                .andExpect(jsonPath("$.measurements.soilTemperatureC").value(21.0))
                .andExpect(jsonPath("$.quality.airSensorValid").value(true));

        mockMvc.perform(get("/api/devices/{deviceId}/measurements", deviceId)
                        .header("Authorization", bearer(token))
                        .queryParam("metric", "air_temperature_c")
                        .queryParam("range", "24h"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.metric").value("air_temperature_c"))
                .andExpect(jsonPath("$.unit").value("℃"))
                .andExpect(jsonPath("$.points[0].value").value(27.1));

        when(measurementStore.findPoints(
                eq(potId),
                eq(MeasurementMetric.SOIL_TEMPERATURE_C),
                any(Instant.class)))
                .thenReturn(List.of(new MeasurementPoint(sample.observedAt(), 21.0)));

        mockMvc.perform(get("/api/devices/{deviceId}/measurements", deviceId)
                        .header("Authorization", bearer(token))
                        .queryParam("metric", "soil_temperature_c")
                        .queryParam("range", "24h"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.metric").value("soil_temperature_c"))
                .andExpect(jsonPath("$.unit").value("℃"))
                .andExpect(jsonPath("$.points[0].value").value(21.0));

        mockMvc.perform(get("/api/devices/{deviceId}/score", deviceId)
                        .header("Authorization", bearer(token)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.cropCode").value("lettuce"))
                .andExpect(jsonPath("$.cropName").value("상추"))
                .andExpect(jsonPath("$.total").value(96.1))
                .andExpect(jsonPath("$.grade").value("GOOD"))
                .andExpect(jsonPath("$.factors[0].key").value("temperature"))
                .andExpect(jsonPath("$.factors[2].key").value("plantLight"))
                .andExpect(jsonPath("$.factors[2].score").value(88.7));
    }

    @Test
    void rejectsUnsupportedSeriesParameters() throws Exception {
        String token = signupAndGetToken();
        long deviceId = registerAndGetDeviceId(token);

        mockMvc.perform(get("/api/devices/{deviceId}/measurements", deviceId)
                        .header("Authorization", bearer(token))
                        .queryParam("metric", "co2")
                        .queryParam("range", "24h"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("UNSUPPORTED_METRIC"));
    }

    private TelemetryEnvelope.Node node(String nodeId, long sequence) {
        return new TelemetryEnvelope.Node(
                nodeId,
                sequence,
                new TelemetryEnvelope.Measurements(27.0, 58.0, 230.0, 21.0, 40.0, 1000L),
                new TelemetryEnvelope.Quality(true, true, true),
                null);
    }

    /** One node, with {@code suggestionJson} spliced in verbatim (may be empty). */
    private String telemetryBodyWithSuggestion(String suggestionJson) {
        return """
                {"schema_version":2,"event_type":"telemetry.sample",\
                "gateway_id":"%s","event_id":"%s","observed_at":"%s",\
                "nodes":[{"node_id":"%s","sequence":7,\
                "measurements":{"air_temperature_c":27.1,"air_humidity_pct":58.0,\
                "plant_light_ppfd_umol_m2_s":230.5},\
                "quality":{"air_sensor_valid":true,"light_sensor_valid":true,\
                "soil_sensor_valid":false}%s}]}"""
                .formatted(
                        HARDWARE_ID,
                        UUID.randomUUID(),
                        Instant.now().minusSeconds(5),
                        NODE_ID,
                        suggestionJson);
    }

    private String telemetryBody(
            String hardwareId,
            String eventId,
            Instant observedAt,
            String nodeId,
            long sequence,
            double humidity) throws Exception {
        return telemetryBody(2, hardwareId, eventId, observedAt, nodeId, sequence, humidity);
    }

    private String telemetryBody(
            int schemaVersion,
            String hardwareId,
            String eventId,
            Instant observedAt,
            String nodeId,
            long sequence,
            double humidity) throws Exception {
        TelemetryEnvelope envelope = new TelemetryEnvelope(
                schemaVersion,
                "telemetry.sample",
                hardwareId,
                eventId,
                observedAt,
                List.of(new TelemetryEnvelope.Node(
                        nodeId,
                        sequence,
                        new TelemetryEnvelope.Measurements(
                                27.1, humidity, 230.5, 31.2, 45.0, 1847L),
                        new TelemetryEnvelope.Quality(true, true, true),
                        null)));
        return objectMapper.writeValueAsString(envelope);
    }

    private TelemetrySample sample(long potId, long deviceId, Instant observedAt) {
        return new TelemetrySample(
                potId,
                deviceId,
                NODE_ID,
                "lettuce",
                HARDWARE_ID,
                UUID.randomUUID().toString(),
                observedAt,
                1042,
                58.0,
                1847,
                27.1,
                58.0,
                230.5,
                21.0,
                true,
                true,
                true,
                null);
    }

    private CropScoreProfile profile(String cropCode, String cropName) {
        return new CropScoreProfile(
                cropCode, cropName,
                15, 24, 30, 36,
                30, 50, 70, 90,
                0, 260, 500, 750);
    }

    private String signupAndGetToken() throws Exception {
        String response = mockMvc.perform(post("/api/auth/signup")
                        .contentType(APPLICATION_JSON)
                        .content(objectMapper.writeValueAsString(
                                new SignupRequest("sensor-owner@example.com", "password1", "센서소유자"))))
                .andExpect(status().isCreated())
                .andReturn()
                .getResponse()
                .getContentAsString();
        return objectMapper.readTree(response).get("accessToken").asText();
    }

    private long registerAndGetDeviceId(String token) throws Exception {
        RegisterDeviceRequest request = new RegisterDeviceRequest(
                SERIAL_CODE,
                "부산 도심 옥상 A",
                "건물 옥상",
                new BigDecimal("42"));
        String response = mockMvc.perform(post("/api/devices")
                        .header("Authorization", bearer(token))
                        .contentType(APPLICATION_JSON)
                        .content(objectMapper.writeValueAsString(request)))
                .andExpect(status().isCreated())
                .andReturn()
                .getResponse()
                .getContentAsString();
        JsonNode json = objectMapper.readTree(response);
        return json.get("id").asLong();
    }

    private String bearer(String token) {
        return "Bearer " + token;
    }

    private void selectCrop(String token, long deviceId, String cropCode) throws Exception {
        mockMvc.perform(patch("/api/devices/{deviceId}/crop", deviceId)
                        .header("Authorization", bearer(token))
                        .contentType(APPLICATION_JSON)
                        .content("{\"cropCode\":\"" + cropCode + "\"}"))
                .andExpect(status().isOk());
    }
}
