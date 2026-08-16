package com.terrabyte.backend.measurement;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.hamcrest.Matchers.nullValue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
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
import org.mockito.ArgumentCaptor;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class MeasurementApiIntegrationTests {

    private static final String DEVICE_KEY = "terrabyte-local-device-key";
    private static final String HARDWARE_ID = "orangepi-pro-01";
    private static final String SERIAL_CODE = "483920";

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
    }

    @Test
    void acceptsHardwareTelemetryAndMarksDeviceOnline() throws Exception {
        Instant observedAt = Instant.now().minusSeconds(5);

        mockMvc.perform(post("/api/telemetry")
                        .header("X-Device-Key", DEVICE_KEY)
                        .contentType(APPLICATION_JSON)
                        .content(telemetryBody(HARDWARE_ID, observedAt, 1042, 58.0)))
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
    void acceptsTelemetryWithoutPpfdWhenLightSensorIsInvalid() throws Exception {
        Instant observedAt = Instant.now().minusSeconds(5);

        mockMvc.perform(post("/api/telemetry")
                        .header("X-Device-Key", DEVICE_KEY)
                        .contentType(APPLICATION_JSON)
                        .content(telemetryBody(HARDWARE_ID, observedAt, 1043, 58.0, null, false)))
                .andExpect(status().isAccepted());

        ArgumentCaptor<TelemetrySample> captor = ArgumentCaptor.forClass(TelemetrySample.class);
        verify(measurementStore).write(captor.capture());
        assertThat(captor.getValue().plantLightPpfdUmolM2S()).isNull();
        assertThat(captor.getValue().lightSensorValid()).isFalse();
    }

    @Test
    void rejectsInconsistentPpfdAndLightSensorValidity() throws Exception {
        Instant observedAt = Instant.now().minusSeconds(5);

        mockMvc.perform(post("/api/telemetry")
                        .header("X-Device-Key", DEVICE_KEY)
                        .contentType(APPLICATION_JSON)
                        .content(telemetryBody(HARDWARE_ID, observedAt, 1043, 58.0, null, true)))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("VALIDATION_FAILED"));

        mockMvc.perform(post("/api/telemetry")
                        .header("X-Device-Key", DEVICE_KEY)
                        .contentType(APPLICATION_JSON)
                        .content(telemetryBody(HARDWARE_ID, observedAt, 1044, 58.0, 230.5, false)))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("VALIDATION_FAILED"));
    }

    @Test
    void rejectsInvalidDeviceKeyAndInvalidMeasurement() throws Exception {
        Instant observedAt = Instant.now().minusSeconds(5);

        mockMvc.perform(post("/api/telemetry")
                        .header("X-Device-Key", "wrong-key")
                        .contentType(APPLICATION_JSON)
                        .content(telemetryBody(HARDWARE_ID, observedAt, 1042, 58.0)))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.code").value("INVALID_DEVICE_KEY"));

        mockMvc.perform(post("/api/telemetry")
                        .header("X-Device-Key", DEVICE_KEY)
                        .contentType(APPLICATION_JSON)
                        .content(telemetryBody(HARDWARE_ID, observedAt, 1042, 101.0)))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("VALIDATION_FAILED"));
    }

    @Test
    void rejectsUnknownHardwareDevice() throws Exception {
        mockMvc.perform(post("/api/telemetry")
                        .header("X-Device-Key", DEVICE_KEY)
                        .contentType(APPLICATION_JSON)
                        .content(telemetryBody("unknown-device", Instant.now().minusSeconds(5), 1, 58.0)))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value("DEVICE_NOT_FOUND"));
    }

    @Test
    void returnsLatestAndTimeSeriesForDeviceOwner() throws Exception {
        String token = signupAndGetToken();
        long deviceId = registerAndGetDeviceId(token);
        long potId = jdbcTemplate.queryForObject("SELECT MIN(id) FROM pot WHERE device_id=?", Long.class, deviceId);
        selectCrop(token, deviceId, "lettuce");
        TelemetrySample sample = sample(Instant.now().minusSeconds(5));
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
                .andExpect(jsonPath("$.quality.airSensorValid").value(true));

        mockMvc.perform(get("/api/devices/{deviceId}/measurements", deviceId)
                        .header("Authorization", bearer(token))
                        .queryParam("metric", "air_temperature_c")
                        .queryParam("range", "24h"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.metric").value("air_temperature_c"))
                .andExpect(jsonPath("$.unit").value("℃"))
                .andExpect(jsonPath("$.points[0].value").value(27.1));

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

        TelemetrySample sampleWithoutPpfd = sampleWithoutPpfd(Instant.now());
        when(measurementStore.findLatest(potId)).thenReturn(java.util.Optional.of(sampleWithoutPpfd));

        mockMvc.perform(get("/api/devices/{deviceId}/measurements/latest", deviceId)
                        .header("Authorization", bearer(token)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.measurements.plantLightPpfdUmolM2S").value(nullValue()))
                .andExpect(jsonPath("$.quality.lightSensorValid").value(false));

        mockMvc.perform(get("/api/devices/{deviceId}/score", deviceId)
                        .header("Authorization", bearer(token)))
                .andExpect(status().isUnprocessableEntity())
                .andExpect(jsonPath("$.code").value("SCORE_INPUT_INCOMPLETE"));
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

    private String telemetryBody(
            String hardwareId,
            Instant observedAt,
            long sequence,
            double humidity) throws Exception {
        return telemetryBody(hardwareId, observedAt, sequence, humidity, 230.5, true);
    }

    private String telemetryBody(
            String hardwareId,
            Instant observedAt,
            long sequence,
            double humidity,
            Double ppfd,
            boolean lightSensorValid) throws Exception {
        TelemetrySampleRequest request = new TelemetrySampleRequest(
                1,
                "telemetry.sample",
                hardwareId,
                observedAt,
                sequence,
                new TelemetrySampleRequest.Context(
                        "pnu-lab", "pot-01", "loam", "basil", "soil-v2"),
                new TelemetrySampleRequest.Measurements(31.2, 1847L, 27.1, humidity, ppfd),
                new TelemetrySampleRequest.Quality(true, true, lightSensorValid));
        return objectMapper.writeValueAsString(request);
    }

    private TelemetrySample sample(Instant observedAt) {
        return new TelemetrySample(
                HARDWARE_ID,
                observedAt,
                1042,
                "pnu-lab",
                "pot-01",
                "loam",
                "basil",
                "soil-v2",
                31.2,
                1847,
                27.1,
                58.0,
                230.5,
                true,
                true,
                true);
    }

    private TelemetrySample sampleWithoutPpfd(Instant observedAt) {
        return new TelemetrySample(
                HARDWARE_ID,
                observedAt,
                1043,
                "pnu-lab",
                "pot-01",
                "loam",
                "basil",
                "soil-v2",
                31.2,
                1847,
                27.1,
                58.0,
                null,
                true,
                true,
                false);
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
