package com.terrabyte.backend.crop;

import java.math.BigDecimal;

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
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;

@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class CropApiIntegrationTests {

    private static final String SERIAL_CODE = "483920";

    private final MockMvc mockMvc;
    private final ObjectMapper objectMapper;
    private final JdbcTemplate jdbcTemplate;

    @Autowired
    CropApiIntegrationTests(
            MockMvc mockMvc,
            ObjectMapper objectMapper,
            @Qualifier("postgresJdbcTemplate") JdbcTemplate jdbcTemplate) {
        this.mockMvc = mockMvc;
        this.objectMapper = objectMapper;
        this.jdbcTemplate = jdbcTemplate;
    }

    @BeforeEach
    void resetData() {
        jdbcTemplate.update("DELETE FROM cultivation_space");
        jdbcTemplate.update("""
                UPDATE device
                SET user_id = NULL, space_id = NULL, claimed_at = NULL,
                    status = 'OFFLINE', last_seen_at = NULL
                """);
        jdbcTemplate.update("UPDATE pot SET node_id=NULL,crop_code=NULL,crop_selected_at=NULL,status='OFFLINE',last_seen_at=NULL");
        jdbcTemplate.update("DELETE FROM app_user");
    }

    @Test
    void returnsActiveCropsInDisplayOrderAndSearchesByNameOrCode() throws Exception {
        String token = signupAndGetToken("crop-list@example.com");

        mockMvc.perform(get("/api/crops").header("Authorization", bearer(token)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.length()").value(8))
                .andExpect(jsonPath("$[0].code").value("cherry_tomato"))
                .andExpect(jsonPath("$[0].name").value("방울토마토"))
                .andExpect(jsonPath("$[7].code").value("coriander"));

        mockMvc.perform(get("/api/crops")
                        .header("Authorization", bearer(token))
                        .queryParam("q", "바질"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.length()").value(1))
                .andExpect(jsonPath("$[0].code").value("basil"));

        mockMvc.perform(get("/api/crops")
                        .header("Authorization", bearer(token))
                        .queryParam("q", "TOMATO"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.length()").value(1))
                .andExpect(jsonPath("$[0].name").value("방울토마토"));
    }

    @Test
    void selectsAndChangesCropForAnOwnedDevice() throws Exception {
        String token = signupAndGetToken("crop-owner@example.com");
        long deviceId = registerAndGetDeviceId(token);

        select(token, deviceId, "basil")
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.deviceId").value(deviceId))
                .andExpect(jsonPath("$.crop.code").value("basil"))
                .andExpect(jsonPath("$.crop.name").value("바질"))
                .andExpect(jsonPath("$.selectedAt").isNotEmpty());

        mockMvc.perform(get("/api/me").header("Authorization", bearer(token)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.hasCrop").value(true))
                .andExpect(jsonPath("$.device.cropCode").value("basil"));

        select(token, deviceId, "lettuce")
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.crop.code").value("lettuce"));

        String selected = jdbcTemplate.queryForObject(
                "SELECT crop_code FROM pot WHERE device_id = ? ORDER BY id LIMIT 1", String.class, deviceId);
        org.assertj.core.api.Assertions.assertThat(selected).isEqualTo("lettuce");
    }

    @Test
    void potResponseExposesCropSelectionTimestampAfterSelection() throws Exception {
        String token = signupAndGetToken("pot-crop-owner@example.com");
        long deviceId = registerAndGetDeviceId(token);
        long potId = jdbcTemplate.queryForObject(
                "SELECT id FROM pot WHERE device_id = ? ORDER BY id LIMIT 1", Long.class, deviceId);

        mockMvc.perform(get("/api/pots/{potId}", potId)
                        .header("Authorization", bearer(token)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.cropCode").doesNotExist())
                .andExpect(jsonPath("$.cropSelectedAt").doesNotExist());

        selectPot(token, potId, "basil")
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.selectedAt").isNotEmpty());

        mockMvc.perform(get("/api/pots/{potId}", potId)
                        .header("Authorization", bearer(token)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.cropCode").value("basil"))
                .andExpect(jsonPath("$.cropSelectedAt").isNotEmpty());
    }

    @Test
    void rejectsUnknownCropAndADeviceOwnedByAnotherUser() throws Exception {
        String ownerToken = signupAndGetToken("crop-owner@example.com");
        long deviceId = registerAndGetDeviceId(ownerToken);
        String otherToken = signupAndGetToken("other@example.com");

        select(ownerToken, deviceId, "unknown")
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value("CROP_NOT_FOUND"));

        select(otherToken, deviceId, "basil")
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value("DEVICE_NOT_FOUND"));
    }

    @Test
    void requiresAuthenticationAndValidatesRequests() throws Exception {
        mockMvc.perform(get("/api/crops"))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.code").value("AUTHENTICATION_REQUIRED"));

        String token = signupAndGetToken("validation@example.com");
        long deviceId = registerAndGetDeviceId(token);
        select(token, deviceId, " ")
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("VALIDATION_FAILED"));
    }

    private org.springframework.test.web.servlet.ResultActions select(
            String token, long deviceId, String cropCode) throws Exception {
        return mockMvc.perform(patch("/api/devices/{deviceId}/crop", deviceId)
                .header("Authorization", bearer(token))
                .contentType(APPLICATION_JSON)
                .content(objectMapper.writeValueAsString(new CropSelectionRequest(cropCode))));
    }

    private org.springframework.test.web.servlet.ResultActions selectPot(
            String token, long potId, String cropCode) throws Exception {
        return mockMvc.perform(patch("/api/pots/{potId}/crop", potId)
                .header("Authorization", bearer(token))
                .contentType(APPLICATION_JSON)
                .content(objectMapper.writeValueAsString(new CropSelectionRequest(cropCode))));
    }

    private String signupAndGetToken(String email) throws Exception {
        String response = mockMvc.perform(post("/api/auth/signup")
                        .contentType(APPLICATION_JSON)
                        .content(objectMapper.writeValueAsString(
                                new SignupRequest(email, "password1", "작물사용자"))))
                .andExpect(status().isCreated())
                .andReturn()
                .getResponse()
                .getContentAsString();
        return objectMapper.readTree(response).get("accessToken").asText();
    }

    private long registerAndGetDeviceId(String token) throws Exception {
        String response = mockMvc.perform(post("/api/devices")
                        .header("Authorization", bearer(token))
                        .contentType(APPLICATION_JSON)
                        .content(objectMapper.writeValueAsString(new RegisterDeviceRequest(
                                SERIAL_CODE,
                                "부산 도심 옥상 A",
                                "건물 옥상",
                                new BigDecimal("42")))))
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
}
