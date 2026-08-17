package com.terrabyte.backend;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.List;
import java.util.Optional;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
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
import com.terrabyte.backend.measurement.MeasurementMetric;
import com.terrabyte.backend.measurement.MeasurementPoint;
import com.terrabyte.backend.measurement.MeasurementStore;
import com.terrabyte.backend.measurement.TelemetryEnvelope;
import com.terrabyte.backend.measurement.TelemetrySample;
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
class DeviceHierarchyIntegrationTests {

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private ObjectMapper objectMapper;

    @Autowired
    @Qualifier("postgresJdbcTemplate")
    private JdbcTemplate jdbcTemplate;

    @MockitoBean
    private MeasurementStore measurementStore;

    @MockitoBean
    private CropScoreProfileRepository profileRepository;

    @BeforeEach
    void resetData() {
        jdbcTemplate.update("DELETE FROM device WHERE serial_code NOT IN ('483920', '123456')");
        jdbcTemplate.update("""
                INSERT INTO device (serial_code, hardware_id, claim_code, mqtt_username)
                SELECT '123456', 'orangepi-pro-02', '123456', 'gw-orangepi-pro-02'
                WHERE NOT EXISTS (SELECT 1 FROM device WHERE serial_code = '123456')
                """);
        jdbcTemplate.update("""
                INSERT INTO pot (device_id, label)
                SELECT d.id, '화분 1' FROM device d
                WHERE d.serial_code IN ('483920', '123456')
                  AND NOT EXISTS (SELECT 1 FROM pot p WHERE p.device_id = d.id)
                """);
        jdbcTemplate.update("""
                UPDATE device SET user_id = NULL, space_id = NULL, claimed_at = NULL,
                    status = 'OFFLINE', last_seen_at = NULL
                """);
        jdbcTemplate.update("""
                DELETE FROM pot WHERE id NOT IN (
                    SELECT first_pot_id FROM (
                        SELECT MIN(id) AS first_pot_id FROM pot GROUP BY device_id
                    ) retained
                )
                """);
        jdbcTemplate.update("""
                UPDATE pot SET node_id = NULL, crop_code = NULL, crop_selected_at = NULL,
                    status = 'OFFLINE', last_seen_at = NULL
                """);
        jdbcTemplate.update("DELETE FROM app_user");
        jdbcTemplate.update("DELETE FROM telemetry_event");
    }

    @Test
    void registrationCreatesOrReturnsTheInitialPot() throws Exception {
        String token = signup("pot-owner@example.com");
        JsonNode registered = register(token, "483920", null);

        assertThat(registered.path("pots").size()).isEqualTo(1);
        assertThat(registered.path("pots").get(0).path("label").asText()).isEqualTo("화분 1");
        assertThat(jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM pot WHERE device_id = ?", Integer.class, registered.path("id").asLong()))
                .isEqualTo(1);
    }

    @Test
    void telemetryBindsTheOldestUnboundPotThenCreatesAPotForANewNode() throws Exception {
        String token = signup("binding-owner@example.com");
        long deviceId = register(token, "483920", null).path("id").asLong();

        ingest("node-a", 1);
        assertThat(jdbcTemplate.queryForObject(
                "SELECT node_id FROM pot WHERE device_id = ? ORDER BY id LIMIT 1", String.class, deviceId))
                .isEqualTo("node-a");

        ingest("node-b", 2);
        List<String> labels = jdbcTemplate.queryForList(
                "SELECT label FROM pot WHERE device_id = ? ORDER BY id", String.class, deviceId);
        List<String> nodes = jdbcTemplate.queryForList(
                "SELECT node_id FROM pot WHERE device_id = ? ORDER BY id", String.class, deviceId);
        assertThat(labels).containsExactly("화분 1", "화분 2");
        assertThat(nodes).containsExactly("node-a", "node-b");
    }

    @Test
    void oneUserCanOwnMultipleDevicesAndSpaces() throws Exception {
        String token = signup("multi-owner@example.com");
        register(token, "483920", null);
        JsonNode secondSpace = createSpace(token, "지하실");
        register(token, "123456", secondSpace.path("id").asLong());
        createSpace(token, "베란다");

        mockMvc.perform(get("/api/devices").header("Authorization", bearer(token)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.length()").value(2));
        mockMvc.perform(get("/api/spaces").header("Authorization", bearer(token)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.length()").value(3));
    }

    @Test
    void deprecatedDeviceAliasesDelegateToRepresentativePot() throws Exception {
        String token = signup("alias-owner@example.com");
        long deviceId = register(token, "483920", null).path("id").asLong();
        long potId = jdbcTemplate.queryForObject(
                "SELECT MIN(id) FROM pot WHERE device_id = ?", Long.class, deviceId);
        TelemetrySample sample = sample(potId, deviceId);
        when(measurementStore.findLatest(potId)).thenReturn(Optional.of(sample));
        when(measurementStore.findPoints(eq(potId), eq(MeasurementMetric.AIR_TEMPERATURE_C), any(Instant.class)))
                .thenReturn(List.of(new MeasurementPoint(sample.observedAt(), 27.1)));
        when(profileRepository.findActiveByCropCode("basil"))
                .thenReturn(Optional.of(new CropScoreProfile(
                        "basil", "바질", 15, 24, 30, 36, 30, 50, 70, 90, 0, 260, 500, 750)));

        mockMvc.perform(patch("/api/devices/{id}/crop", deviceId)
                        .header("Authorization", bearer(token))
                        .contentType(APPLICATION_JSON)
                        .content("{\"cropCode\":\"basil\"}"))
                .andExpect(status().isOk());
        mockMvc.perform(get("/api/devices/{id}/measurements/latest", deviceId)
                        .header("Authorization", bearer(token)))
                .andExpect(status().isOk());
        mockMvc.perform(get("/api/devices/{id}/measurements", deviceId)
                        .header("Authorization", bearer(token))
                        .queryParam("metric", "air_temperature_c"))
                .andExpect(status().isOk());
        mockMvc.perform(get("/api/devices/{id}/score", deviceId)
                        .header("Authorization", bearer(token)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.cropCode").value("basil"));
    }

    private String signup(String email) throws Exception {
        String body = objectMapper.writeValueAsString(new SignupRequest(email, "password1", "계층테스트"));
        String response = mockMvc.perform(post("/api/auth/signup").contentType(APPLICATION_JSON).content(body))
                .andExpect(status().isCreated()).andReturn().getResponse().getContentAsString();
        return objectMapper.readTree(response).path("accessToken").asText();
    }

    private JsonNode register(String token, String claimCode, Long spaceId) throws Exception {
        RegisterDeviceRequest request = spaceId == null
                ? new RegisterDeviceRequest(claimCode, "옥상", "건물 옥상", new BigDecimal("12"))
                : new RegisterDeviceRequest(claimCode, spaceId, null, null, null);
        String response = mockMvc.perform(post("/api/devices")
                        .header("Authorization", bearer(token))
                        .contentType(APPLICATION_JSON)
                        .content(objectMapper.writeValueAsString(request)))
                .andExpect(status().isCreated()).andReturn().getResponse().getContentAsString();
        return objectMapper.readTree(response);
    }

    private JsonNode createSpace(String token, String name) throws Exception {
        String response = mockMvc.perform(post("/api/spaces")
                        .header("Authorization", bearer(token))
                        .contentType(APPLICATION_JSON)
                        .content("{\"name\":\"" + name
                                + "\",\"spaceType\":\"실내\",\"areaSquareMeters\":8}"))
                .andExpect(status().isCreated()).andReturn().getResponse().getContentAsString();
        return objectMapper.readTree(response);
    }

    private void ingest(String nodeId, long sequence) throws Exception {
        TelemetryEnvelope envelope = new TelemetryEnvelope(
                2,
                "telemetry.sample",
                "orangepi-pro-01",
                UUID.randomUUID().toString(),
                Instant.now().minusSeconds(5),
                List.of(new TelemetryEnvelope.Node(
                        nodeId,
                        sequence,
                        new TelemetryEnvelope.Measurements(27.0, 58.0, 230.0, null, null, null),
                        new TelemetryEnvelope.Quality(true, true, null),
                        null)));
        mockMvc.perform(post("/api/telemetry")
                        .contentType(APPLICATION_JSON).content(objectMapper.writeValueAsString(envelope)))
                .andExpect(status().isAccepted());
    }

    private TelemetrySample sample(long potId, long deviceId) {
        return new TelemetrySample(
                potId, deviceId, "node-a", "basil", "orangepi-pro-01", UUID.randomUUID().toString(),
                Instant.now(), 1, 30, 1000, 27.1, 58, 230.5, null,
                true, true, true, null);
    }

    private String bearer(String token) {
        return "Bearer " + token;
    }
}
