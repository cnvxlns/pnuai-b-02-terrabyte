package com.terrabyte.backend.irrigation;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.time.Duration;
import java.time.Instant;
import java.util.List;

import com.terrabyte.backend.api.ApiException;
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
 * What the app shows for "did my tap do anything".
 *
 * <p>Issue #53's criterion is that a user pressing the button repeatedly can
 * still tell each command's state apart, which needs the commands themselves —
 * the irrigation timeline covers decisions and stops at the pump.
 */
@SpringBootTest
@ActiveProfiles("test")
class CommandHistoryTests {

    private static final long POT_ID = 1L;
    private static final String OWNER_EMAIL = "history-owner@example.com";

    @Autowired private CommandHistoryService historyService;
    @Autowired private DeviceCommandRepository commands;
    @Autowired private CommandIdGenerator commandIdGenerator;

    @Autowired
    @Qualifier("postgresJdbcTemplate")
    private JdbcTemplate jdbcTemplate;

    @MockitoBean private MeasurementStore measurementStore;

    private long userId;

    @BeforeEach
    void setUp() {
        jdbcTemplate.update("DELETE FROM device_command");
        jdbcTemplate.update("DELETE FROM app_user WHERE email = ?", OWNER_EMAIL);
        jdbcTemplate.update(
                "INSERT INTO app_user (email, password_hash, nickname) VALUES (?, ?, ?)",
                OWNER_EMAIL, "unused", "이력");
        userId = jdbcTemplate.queryForObject(
                "SELECT id FROM app_user WHERE email = ?", Long.class, OWNER_EMAIL);
        jdbcTemplate.update(
                "UPDATE device SET user_id = ? WHERE id = (SELECT device_id FROM pot WHERE id = ?)",
                userId, POT_ID);
    }

    @AfterEach
    void tearDown() {
        jdbcTemplate.update("DELETE FROM device_command");
        jdbcTemplate.update(
                "UPDATE device SET user_id = NULL"
                        + " WHERE id = (SELECT device_id FROM pot WHERE id = ?)",
                POT_ID);
        jdbcTemplate.update("DELETE FROM app_user WHERE email = ?", OWNER_EMAIL);
    }

    @Test
    void everyCommandForThePotIsListedNewestFirst() {
        Instant now = Instant.now();
        issue(now.minus(Duration.ofMinutes(5)), CommandState.COMPLETED);
        issue(now.minus(Duration.ofMinutes(1)), CommandState.ISSUED);

        List<CommandHistoryEntry> history = historyService.recent(POT_ID, userId, 20);

        assertThat(history).hasSize(2);
        // The one still in flight is the one the user just pressed, and it is
        // the one they are looking for.
        assertThat(history.getFirst().state()).isEqualTo(CommandState.ISSUED);
    }

    @Test
    void repeatedTapsStayTellableApart() {
        Instant now = Instant.now();
        String first = issue(now.minus(Duration.ofSeconds(20)), CommandState.COMPLETED);
        String second = issue(now.minus(Duration.ofSeconds(10)), CommandState.REJECTED);

        List<CommandHistoryEntry> history = historyService.recent(POT_ID, userId, 20);

        assertThat(history).extracting(CommandHistoryEntry::commandId)
                .containsExactly(second, first);
        assertThat(history).extracting(CommandHistoryEntry::state)
                .containsExactly(CommandState.REJECTED, CommandState.COMPLETED);
    }

    @Test
    void anotherUsersPotIsIndistinguishableFromAMissingOne() {
        assertThatThrownBy(() -> historyService.recent(POT_ID, userId + 999, 20))
                .isInstanceOf(ApiException.class);
    }

    @Test
    void theLatestStateOfEachActuatorIsAvailableSeparately() {
        Instant now = Instant.now();
        issue(now.minus(Duration.ofMinutes(9)), CommandState.COMPLETED);
        issueLight(now.minus(Duration.ofMinutes(2)), true);

        ActuatorStatusResponse status = historyService.actuatorStatus(POT_ID, userId);

        // The server does not track live actuator state — StatusUplinkHandler
        // discards the firmware's actuators block on purpose — so the last
        // command and how it ended is the honest answer to "is the lamp on".
        assertThat(status.pump()).isNotNull();
        assertThat(status.pump().state()).isEqualTo(CommandState.COMPLETED);
        assertThat(status.light()).isNotNull();
        assertThat(status.light().action()).isEqualTo(DeviceCommand.ACTION_ON);
    }

    @Test
    void anActuatorThatHasNeverRunReportsNothingRatherThanOff() {
        ActuatorStatusResponse status = historyService.actuatorStatus(POT_ID, userId);

        // Null, not "off". Nobody has ever commanded this lamp, and claiming it
        // is off would be a claim about hardware nothing has looked at.
        assertThat(status.pump()).isNull();
        assertThat(status.light()).isNull();
    }

    private String issue(Instant at, CommandState state) {
        String commandId = commandIdGenerator.next(at);
        commands.save(new DeviceCommand(
                commandId, POT_ID, "corr-" + commandId, DeviceCommand.ACTUATOR_PUMP,
                DeviceCommand.ACTION_DOSE, 100, 20_000, state, at,
                at.plus(Duration.ofMinutes(2)), null, null, null, null, null,
                CommandOrigin.CLOUD));
        return commandId;
    }

    private String issueLight(Instant at, boolean on) {
        String commandId = commandIdGenerator.next(at);
        commands.save(DeviceCommand.issuedLight(
                commandId, POT_ID, "corr-" + commandId, on, at,
                at.plus(Duration.ofMinutes(2))));
        return commandId;
    }
}
