package com.terrabyte.backend.irrigation;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.Duration;
import java.time.Instant;
import java.util.List;

import com.terrabyte.backend.measurement.MeasurementStore;

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
 * Issue #51's completion criterion, as a test.
 *
 * <p>"관수를 했거나 거절한 한 건을 선택하면 사용한 센서값, 판단 이유, 명령
 * 결과를 모두 확인할 수 있다" — one entry, all three.
 */
@SpringBootTest
@ActiveProfiles("test")
class IrrigationTimelineTests {

    private static final long POT_ID = 1L;
    private static final String OWNER_EMAIL = "timeline-owner@example.com";

    @Autowired private IrrigationService irrigationService;
    @Autowired private IrrigationDecisionRepository decisions;
    @Autowired private DeviceCommandRepository commands;
    @Autowired private CommandIdGenerator commandIdGenerator;

    @Autowired
    @Qualifier("postgresJdbcTemplate")
    private JdbcTemplate jdbcTemplate;

    @MockitoBean private MeasurementStore measurementStore;

    private long userId;

    @BeforeEach
    void setUp() {
        jdbcTemplate.update("DELETE FROM irrigation_decision");
        jdbcTemplate.update("DELETE FROM device_command");
        jdbcTemplate.update("DELETE FROM app_user WHERE email = ?", OWNER_EMAIL);
        jdbcTemplate.update(
                "INSERT INTO app_user (email, password_hash, nickname) VALUES (?, ?, ?)",
                OWNER_EMAIL, "unused", "타임라인");
        userId = jdbcTemplate.queryForObject(
                "SELECT id FROM app_user WHERE email = ?", Long.class, OWNER_EMAIL);
        jdbcTemplate.update(
                "UPDATE device SET user_id = ? WHERE id = (SELECT device_id FROM pot WHERE id = ?)",
                userId, POT_ID);
    }

    @AfterEach
    void tearDown() {
        jdbcTemplate.update("DELETE FROM irrigation_decision");
        jdbcTemplate.update("DELETE FROM device_command");
        jdbcTemplate.update(
                "UPDATE device SET user_id = NULL"
                        + " WHERE id = (SELECT device_id FROM pot WHERE id = ?)",
                POT_ID);
        jdbcTemplate.update("DELETE FROM app_user WHERE email = ?", OWNER_EMAIL);
    }

    @Test
    void aGrantedEntryCarriesTheReadingTheReasonAndTheResult() {
        Instant now = Instant.now();
        String commandId = commandIdGenerator.next(now);
        commands.save(new DeviceCommand(
                commandId, POT_ID, "corr-1", DeviceCommand.ACTUATOR_PUMP,
                DeviceCommand.ACTION_DOSE, 100, 20_000, CommandState.COMPLETED,
                now, now.plus(Duration.ofMinutes(2)), now, now, 96, 12_000,
                "volume_reached", CommandOrigin.CLOUD));
        decisions.save(new IrrigationDecision(
                null, POT_ID, "corr-1", CommandSource.RULE_AI, now, 21.5,
                IrrigationDecision.VERDICT_NEEDED, "irrigation-reg-v1", 110, 100,
                null, ClampReason.MAX_DOSE, commandId, now));

        List<IrrigationTimelineEntry> timeline = irrigationService.timeline(POT_ID, userId, 20);

        assertThat(timeline).hasSize(1);
        IrrigationTimelineEntry entry = timeline.getFirst();
        // The reading it decided on.
        assertThat(entry.soilMoisturePct()).isEqualTo(21.5);
        // Why it decided that.
        assertThat(entry.ruleVerdict()).isEqualTo(IrrigationDecision.VERDICT_NEEDED);
        assertThat(entry.aiRequestedMl()).isEqualTo(110);
        assertThat(entry.clampReason()).isEqualTo(ClampReason.MAX_DOSE);
        // What actually happened, which the decision row alone cannot say.
        assertThat(entry.commandState()).isEqualTo(CommandState.COMPLETED);
        assertThat(entry.actualMl()).isEqualTo(96);
        assertThat(entry.stopCause()).isEqualTo("volume_reached");
    }

    @Test
    void aRefusalHasNoCommandHalfAtAll() {
        Instant now = Instant.now();
        decisions.save(IrrigationDecision.denied(
                POT_ID, "corr-2", CommandSource.RULE, now, 48.0, DenyReason.COOLDOWN, now));

        IrrigationTimelineEntry entry = irrigationService.timeline(POT_ID, userId, 20).getFirst();

        assertThat(entry.denyReason()).isEqualTo(DenyReason.COOLDOWN);
        // Null rather than an "unknown" state: a decision that granted nothing
        // never produced a command, so there is nothing whose state is unknown.
        assertThat(entry.commandState()).isNull();
        assertThat(entry.actualMl()).isNull();
    }
}
