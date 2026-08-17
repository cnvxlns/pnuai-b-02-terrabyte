package com.terrabyte.backend.irrigation;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;

import java.time.Instant;
import java.util.List;
import java.util.Optional;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.terrabyte.backend.measurement.MeasurementPoint;
import com.terrabyte.backend.measurement.MeasurementStore;
import com.terrabyte.backend.measurement.TelemetrySample;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import org.springframework.web.context.WebApplicationContext;

/**
 * The manual irrigation endpoint, end to end through the Governor.
 *
 * <p>Security is bypassed with a standalone MockMvc setup because what is under
 * test here is the safety envelope, not authentication — the other integration
 * tests already cover that.
 */
@SpringBootTest
@ActiveProfiles("test")
class IrrigationApiIntegrationTests {

    private static final long POT_ID = 1L;

    @Autowired private WebApplicationContext context;
    @Autowired private ObjectMapper objectMapper;
    @Autowired private JdbcTemplate jdbcTemplate;
    @Autowired private IrrigationController controller;
    @Autowired private IrrigationService irrigationService;
    @Autowired private IrrigationDecisionRepository decisions;

    @MockitoBean private MeasurementStore measurementStore;

    private MockMvc mockMvc;

    @BeforeEach
    void setUp() {
        mockMvc = MockMvcBuilders.standaloneSetup(controller).build();

        jdbcTemplate.update("DELETE FROM device_command");
        jdbcTemplate.update("DELETE FROM irrigation_decision");

        org.mockito.Mockito.when(measurementStore.findLatest(POT_ID))
                .thenReturn(Optional.of(freshSample()));
        org.mockito.Mockito.when(
                        measurementStore.findPoints(
                                org.mockito.ArgumentMatchers.anyLong(),
                                org.mockito.ArgumentMatchers.any(),
                                org.mockito.ArgumentMatchers.any()))
                .thenReturn(List.<MeasurementPoint>of());
    }

    private static TelemetrySample freshSample() {
        return new TelemetrySample(
                POT_ID, 1L, "node-1", "lettuce", "orangepi-pro-01", "evt-1",
                Instant.now().minusSeconds(30), 1L, 22.0, 0L, 24.0, 55.0, 300.0, 21.0,
                true, true, true, null);
    }

    private static TelemetrySample sampleSuggesting(int volumeMl) {
        return new TelemetrySample(
                POT_ID, 1L, "node-1", "lettuce", "orangepi-pro-01", "evt-1",
                Instant.now().minusSeconds(30), 1L, 22.0, 0L, 24.0, 55.0, 300.0, 21.0,
                true, true, true,
                new com.terrabyte.backend.measurement.IrrigationSuggestion(
                        volumeMl, "water-balance-v1", "lettuce", 3000));
    }

    private MvcResult water(int volumeMl) throws Exception {
        String body = objectMapper.writeValueAsString(
                new IrrigationController.ManualIrrigationRequest(volumeMl, false, null));
        return mockMvc.perform(post("/api/pots/{potId}/irrigation", POT_ID)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body))
                .andReturn();
    }

    @Test
    void aValidRequestIsGrantedAndRecorded() throws Exception {
        MvcResult result = water(100);

        assertThat(result.getResponse().getStatus()).isEqualTo(201);
        IrrigationOutcome outcome = objectMapper.readValue(
                result.getResponse().getContentAsString(), IrrigationOutcome.class);
        assertThat(outcome.granted()).isTrue();
        assertThat(outcome.grantedMl()).isEqualTo(100);
        assertThat(outcome.commandId()).isNotBlank();

        assertThat(decisions.findRecentByPotId(POT_ID, 10)).hasSize(1);
        assertThat(jdbcTemplate.queryForObject(
                        "SELECT COUNT(*) FROM device_command WHERE pot_id = ?",
                        Integer.class, POT_ID))
                .isEqualTo(1);
    }

    @Test
    void aSecondRequestIsRefusedWhileTheFirstIsOutstanding() throws Exception {
        assertThat(water(100).getResponse().getStatus()).isEqualTo(201);

        MvcResult second = water(100);

        // 409, not 400: the request was fine, the state was not.
        assertThat(second.getResponse().getStatus()).isEqualTo(409);
        IrrigationOutcome outcome = objectMapper.readValue(
                second.getResponse().getContentAsString(), IrrigationOutcome.class);
        assertThat(outcome.denyReason()).isEqualTo(DenyReason.IN_FLIGHT);
    }

    @Test
    void anAbsurdVolumeIsClampedNotHonoured() throws Exception {
        MvcResult result = water(99_999);

        IrrigationOutcome outcome = objectMapper.readValue(
                result.getResponse().getContentAsString(), IrrigationOutcome.class);
        assertThat(outcome.grantedMl()).isEqualTo(200);
        assertThat(outcome.clampReason()).isEqualTo(ClampReason.MAX_DOSE);
    }

    @Test
    void aNonPositiveVolumeIsRejectedByValidation() throws Exception {
        assertThat(water(0).getResponse().getStatus()).isEqualTo(400);
    }

    // -- the automatic path, now sized by the edge -------------------------

    @Test
    void anEdgeSuggestionSizesTheDoseAndIsAttributedToTheEdge() {
        org.mockito.Mockito.when(measurementStore.findLatest(POT_ID))
                .thenReturn(Optional.of(sampleSuggesting(118)));

        IrrigationOutcome outcome = irrigationService.requestAutomatic(POT_ID, "evt-auto-1");

        assertThat(outcome.granted()).isTrue();
        assertThat(outcome.grantedMl()).isEqualTo(118);
        assertThat(outcome.volumeSource()).isEqualTo(VolumeSource.EDGE_SUGGESTION);
        assertThat(outcome.aiModelVersion()).isEqualTo("water-balance-v1");

        IrrigationDecision decision = decisions.findRecentByPotId(POT_ID, 1).get(0);
        assertThat(decision.source()).isEqualTo(CommandSource.RULE_AI);
        // The audit columns keep their names: they still answer "what proposed
        // this volume", and the answer is now the edge instead of a model server.
        assertThat(decision.aiModelVersion()).isEqualTo("water-balance-v1");
        assertThat(decision.aiRequestedMl()).isEqualTo(118);
    }

    @Test
    void withoutASuggestionTheTableDecidesAndNothingIsAttributed() {
        org.mockito.Mockito.when(measurementStore.findLatest(POT_ID))
                .thenReturn(Optional.of(freshSample()));

        IrrigationOutcome outcome = irrigationService.requestAutomatic(POT_ID, "evt-auto-2");

        // Pot 1 has no recorded substrate volume, so the smallest band applies.
        assertThat(outcome.grantedMl()).isEqualTo(40);
        assertThat(outcome.volumeSource()).isEqualTo(VolumeSource.POT_SIZE_FALLBACK);
        assertThat(outcome.aiModelVersion()).isNull();

        IrrigationDecision decision = decisions.findRecentByPotId(POT_ID, 1).get(0);
        assertThat(decision.source()).isEqualTo(CommandSource.RULE);
        assertThat(decision.aiRequestedMl()).isNull();
    }

    @Test
    void refusalsAppearInTheTimeline() throws Exception {
        water(100);
        water(100); // refused, IN_FLIGHT

        MvcResult result = mockMvc.perform(
                        get("/api/pots/{potId}/irrigation/timeline", POT_ID))
                .andReturn();

        assertThat(result.getResponse().getStatus()).isEqualTo(200);
        // Both outcomes are visible: a pot that is never watered has to be
        // diagnosable from this list alone.
        assertThat(result.getResponse().getContentAsString()).contains("IN_FLIGHT");
    }
}
