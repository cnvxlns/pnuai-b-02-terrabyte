package com.terrabyte.backend.score;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.Optional;
import java.util.UUID;

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
import com.terrabyte.backend.measurement.MeasurementStore;
import com.terrabyte.backend.measurement.TelemetrySample;
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

@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class EnvironmentScoreApiIntegrationTests {

    private static final String DEMO_SERIAL_CODE = "483920";

    private final MockMvc mockMvc;
    private final ObjectMapper objectMapper;
    private final JdbcTemplate jdbcTemplate;

    @MockitoBean
    private MeasurementStore measurementStore;

    @MockitoBean
    private CropScoreProfileRepository profileRepository;

    @Autowired
    EnvironmentScoreApiIntegrationTests(
            MockMvc mockMvc,
            ObjectMapper objectMapper,
            @Qualifier("postgresJdbcTemplate") JdbcTemplate jdbcTemplate) {
        this.mockMvc = mockMvc;
        this.objectMapper = objectMapper;
        this.jdbcTemplate = jdbcTemplate;
    }

    @BeforeEach
    void resetData() {
        jdbcTemplate.update("DELETE FROM device WHERE serial_code <> ?", DEMO_SERIAL_CODE);
        jdbcTemplate.update(
                """
                UPDATE device
                SET user_id = NULL, space_id = NULL, claimed_at = NULL,
                    status = 'OFFLINE', last_seen_at = NULL
                """);
        jdbcTemplate.update("UPDATE pot SET node_id=NULL,crop_code=NULL,crop_selected_at=NULL,status='OFFLINE',last_seen_at=NULL");
        jdbcTemplate.update("DELETE FROM app_user");
    }

    @Test
    void returnsPotentialScoreForOwnedPot() throws Exception {
        String token = signupAndGetToken("score-owner@example.com", "점수소유자");
        JsonNode device = registerAndGetDevice(token);
        long deviceId = device.get("id").asLong();
        long potId = device.get("pots").get(0).get("id").asLong();
        selectCrop(token, potId, "lettuce");
        when(measurementStore.findLatest(potId)).thenReturn(Optional.of(sample(potId, deviceId)));
        when(profileRepository.findActiveByCropCode("lettuce"))
                .thenReturn(Optional.of(profile()));

        mockMvc.perform(get("/api/pots/{potId}/score/potential", potId)
                        .header("Authorization", bearer(token)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.potId").value(potId))
                .andExpect(jsonPath("$.current").value(63.0))
                .andExpect(jsonPath("$.potential").value(100.0))
                .andExpect(jsonPath("$.delta").value(37.0))
                .andExpect(jsonPath("$.improvedFactors.length()").value(2))
                .andExpect(jsonPath("$.improvedFactors[0].key").value("humidity"))
                .andExpect(jsonPath("$.improvedFactors[0].label").value("습도"))
                .andExpect(jsonPath("$.improvedFactors[0].from").value(40.0))
                .andExpect(jsonPath("$.improvedFactors[0].to").value(50.0))
                .andExpect(jsonPath("$.improvedFactors[1].key").value("plantLight"))
                .andExpect(jsonPath("$.improvedFactors[1].label").value("광량"))
                .andExpect(jsonPath("$.improvedFactors[1].from").value(130.0))
                .andExpect(jsonPath("$.improvedFactors[1].to").value(260.0));
    }

    private String signupAndGetToken(String email, String nickname) throws Exception {
        String response = mockMvc.perform(post("/api/auth/signup")
                        .contentType(APPLICATION_JSON)
                        .content(objectMapper.writeValueAsString(
                                new SignupRequest(email, "password1", nickname))))
                .andExpect(status().isCreated())
                .andReturn()
                .getResponse()
                .getContentAsString();
        return objectMapper.readTree(response).get("accessToken").asText();
    }

    private JsonNode registerAndGetDevice(String token) throws Exception {
        RegisterDeviceRequest request = new RegisterDeviceRequest(
                DEMO_SERIAL_CODE,
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
        return objectMapper.readTree(response);
    }

    private void selectCrop(String token, long potId, String cropCode) throws Exception {
        mockMvc.perform(patch("/api/pots/{potId}", potId)
                        .header("Authorization", bearer(token))
                        .contentType(APPLICATION_JSON)
                        .content("{\"label\":\"상추 화분\",\"cropCode\":\"" + cropCode + "\"}"))
                .andExpect(status().isOk());
    }

    private TelemetrySample sample(long potId, long deviceId) {
        return new TelemetrySample(
                potId,
                deviceId,
                "pot-01",
                "lettuce",
                "orangepi-pro-01",
                UUID.randomUUID().toString(),
                Instant.now().minusSeconds(5),
                1,
                0,
                0,
                27,
                40,
                130,
                null,
                false,
                true,
                true);
    }

    private CropScoreProfile profile() {
        return new CropScoreProfile(
                "lettuce", "상추",
                15, 24, 30, 36,
                30, 50, 70, 90,
                0, 260, 500, 750);
    }

    private String bearer(String token) {
        return "Bearer " + token;
    }
}
