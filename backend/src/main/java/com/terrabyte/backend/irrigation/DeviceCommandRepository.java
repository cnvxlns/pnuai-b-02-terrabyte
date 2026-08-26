package com.terrabyte.backend.irrigation;

import java.sql.PreparedStatement;

import org.springframework.dao.DuplicateKeyException;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Timestamp;
import java.sql.Types;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.List;
import java.util.Optional;
import java.util.stream.Collectors;

import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

@Repository
public class DeviceCommandRepository {

    private final JdbcTemplate jdbcTemplate;

    public DeviceCommandRepository(@Qualifier("postgresJdbcTemplate") JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    /** Inserts the command. Void to match IrrigationDecisionRepository#save. */
    public void save(DeviceCommand command) {
        jdbcTemplate.update(connection -> {
            PreparedStatement statement = connection.prepareStatement("""
                    INSERT INTO device_command (
                        command_id, pot_id, correlation_id, actuator, action, granted_ml,
                        max_runtime_ms, state, issued_at, expires_at, acked_at, completed_at,
                        actual_ml, actual_runtime_ms, stop_cause, origin)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """);
            bind(statement, command);
            return statement;
        });
    }

    /** The column order both inserts above share, in one place so they cannot drift. */
    private static void bind(PreparedStatement statement, DeviceCommand command)
            throws java.sql.SQLException {
        statement.setString(1, command.commandId());
        statement.setLong(2, command.potId());
        statement.setString(3, command.correlationId());
        statement.setString(4, command.actuator());
        statement.setString(5, command.action());
        setNullableInt(statement, 6, command.grantedMl());
        statement.setInt(7, command.maxRuntimeMs());
        statement.setString(8, command.state().name());
        statement.setTimestamp(9, Timestamp.from(command.issuedAt()));
        statement.setTimestamp(10, Timestamp.from(command.expiresAt()));
        setNullableTimestamp(statement, 11, command.ackedAt());
        setNullableTimestamp(statement, 12, command.completedAt());
        setNullableInt(statement, 13, command.actualMl());
        setNullableInt(statement, 14, command.actualRuntimeMs());
        statement.setString(15, command.stopCause());
        statement.setString(16, command.origin().name());
    }

    /**
     * Inserts a command that may already be here, and says which it was.
     *
     * <p>For rows this server did not originate: irrigation a gateway performed
     * on its own arrives over QoS 1 from a queue that retries until the publish
     * is acknowledged, so the same delivery reaching us twice is ordinary
     * traffic rather than an error. {@link #save} is the right shape for a
     * command <em>we</em> minted, where a duplicate id would be a bug worth
     * throwing over.
     *
     * <p>The conflict target is the primary key, so the second insert is a
     * no-op rather than an update: the first arrival is the authoritative one
     * and a later copy carries nothing new.
     *
     * @return true when this call is what created the row
     */
    public boolean saveIfAbsent(DeviceCommand command) {
        try {
            save(command);
            return true;
        } catch (DuplicateKeyException alreadyHere) {
            // Caught rather than avoided with ON CONFLICT: that syntax is
            // PostgreSQL's, and H2 in PostgreSQL compatibility mode — which is
            // what the tests run against — rejects it outright. Letting the
            // primary key do the work is portable and needs no dialect branch.
            return false;
        }
    }

    public Optional<DeviceCommand> findById(String commandId) {
        return jdbcTemplate.query(
                        "SELECT * FROM device_command WHERE command_id = ?",
                        this::mapCommand,
                        commandId)
                .stream()
                .findFirst();
    }

    /**
     * Gate 5: is there already a command in flight for this pot?
     *
     * <p>Outstanding is determined by the command's authorised runtime, not its
     * delivery TTL. The time bound matters: without it, one command whose
     * terminal report never arrived would block the pot forever.
     */
    public boolean hasOutstanding(long potId, String actuator, Instant now) {
        return occupancyCandidates(potId, actuator).stream()
                .anyMatch(command -> command.isOutstandingAt(now));
    }

    /**
     * When the outstanding command stops blocking, for the refusal message.
     *
     * <p>A refusal that only says "no" leaves the user tapping the button
     * again; telling them when it will work is the difference between a
     * safeguard and a fault.
     */
    public Optional<Instant> outstandingUntil(long potId, String actuator, Instant now) {
        return occupancyCandidates(potId, actuator).stream()
                .filter(command -> command.isOutstandingAt(now))
                .map(DeviceCommand::occupancyEndsAt)
                .max(Instant::compareTo);
    }

    private List<DeviceCommand> occupancyCandidates(long potId, String actuator) {
        // EXPIRED only says the delivery TTL elapsed without a terminal ack.
        // It may still have started before expiry and remain inside its runtime.
        return jdbcTemplate.query("""
                SELECT * FROM device_command
                WHERE pot_id = ? AND actuator = ?
                  AND state IN ('ISSUED', 'ACCEPTED', 'EXPIRED')
                """, this::mapCommand, potId, actuator);
    }

    /**
     * The issue time of the oldest command still counted against the budget.
     *
     * <p>Budget is a rolling 24-hour window, so this is the moment the window
     * starts to release volume: {@code oldest + 24h} is the earliest the pot
     * can be watered again on budget grounds.
     */
    public Optional<Instant> oldestCountedIssuedAt(long potId, Instant since) {
        List<Timestamp> rows = jdbcTemplate.queryForList("""
                SELECT issued_at FROM device_command
                WHERE pot_id = ?
                  AND actuator = 'pump'
                  AND issued_at >= ?
                  AND state <> 'REJECTED'
                ORDER BY issued_at ASC
                """, Timestamp.class, potId, Timestamp.from(since));
        return rows.isEmpty() ? Optional.empty() : Optional.of(rows.get(0).toInstant());
    }

    /** Gate 4: when this pot was last actually watered, or empty if it never was. */
    public Optional<Instant> lastCompletedAt(long potId) {
        Timestamp lastCompletedAt = jdbcTemplate.queryForObject("""
                SELECT MAX(completed_at) FROM device_command
                WHERE pot_id = ? AND actuator = 'pump' AND state = 'COMPLETED'
                """, Timestamp.class, potId);
        return Optional.ofNullable(lastCompletedAt).map(Timestamp::toInstant);
    }

    /**
     * Gate 6: millilitres this pot has consumed since {@code since}.
     *
     * <p>This is the safety-critical query. It deliberately over-counts, because
     * the cost of the two errors is not symmetric: over-counting delays a dose,
     * under-counting floods the pot.
     *
     * <p>Two rules encode that:
     * <ul>
     *   <li>{@code COALESCE(actual_ml, granted_ml)} — a command with no report
     *       counts at the volume it was authorised for. ISSUED, ACCEPTED and
     *       EXPIRED commands may all have run; an EXPIRED one in particular is
     *       "we stopped waiting", not "it did not happen", so it must not drop
     *       out of the sum.</li>
     *   <li>{@code state <> 'REJECTED'} — the sole exclusion. A rejection is the
     *       device telling us it never touched the pump, so it is the only case
     *       where zero is a fact rather than an assumption.</li>
     * </ul>
     *
     * <p>The actuator filter is explicit rather than relying on non-pump
     * commands happening to leave granted_ml NULL — that would quietly break
     * the day a light command starts carrying a number in that column.
     */
    public int consumedMlSince(long potId, Instant since) {
        Integer total = jdbcTemplate.queryForObject("""
                SELECT COALESCE(SUM(COALESCE(actual_ml, granted_ml)), 0)
                FROM device_command
                WHERE pot_id = ?
                  AND actuator = 'pump'
                  AND issued_at >= ?
                  AND state <> 'REJECTED'
                """, Integer.class, potId, Timestamp.from(since));
        return total == null ? 0 : total;
    }

    // -- state transitions -------------------------------------------------
    //
    // Every one of these is a single UPDATE whose WHERE clause names the states
    // it is allowed to leave, taken from CommandAckPhase. Never a read followed
    // by a write, and never a blind SET: the guard is what makes a redelivered
    // ack — which QoS 1 promises, on both hops — a zero-row no-op instead of a
    // second helping of the same volume. The returned row count is therefore the
    // caller's answer to "did this ack change anything".

    /** The device has the command and is running, or is about to. */
    public int markAccepted(String commandId, Instant ackedAt) {
        return jdbcTemplate.update("""
                UPDATE device_command
                SET state = 'ACCEPTED', acked_at = ?
                WHERE command_id = ? AND state IN (%s)
                """.formatted(allowedFrom(CommandAckPhase.ACCEPTED)),
                Timestamp.from(ackedAt),
                commandId);
    }

    /**
     * The device refused before touching the pump — the one report that provably
     * moved no water, and so the only one that takes volume back off the budget.
     */
    public int markRejected(String commandId, String stopCause, Instant ackedAt) {
        // completed_at stays null on purpose. Gate 4 reads MAX(completed_at) for
        // COMPLETED rows only, but a rejection has no completion time and
        // inventing one would be a lie stored in a column other code may read.
        return jdbcTemplate.update("""
                UPDATE device_command
                SET state = 'REJECTED', acked_at = COALESCE(acked_at, ?), stop_cause = ?
                WHERE command_id = ? AND state IN (%s)
                """.formatted(allowedFrom(CommandAckPhase.REJECTED)),
                Timestamp.from(ackedAt),
                stopCause,
                commandId);
    }

    /** Records the device's execution report, making {@code actual_ml} authoritative. */
    public int markCompleted(
            String commandId,
            Integer actualMl,
            Integer actualRuntimeMs,
            String stopCause,
            Instant completedAt) {
        return applyExecutionReport(
                CommandAckPhase.COMPLETED, commandId, actualMl, actualRuntimeMs, stopCause,
                completedAt);
    }

    /** Stopped part-way, by a watchdog or an operator. Water may have moved. */
    public int markAborted(
            String commandId,
            Integer actualMl,
            Integer actualRuntimeMs,
            String stopCause,
            Instant completedAt) {
        return applyExecutionReport(
                CommandAckPhase.ABORTED, commandId, actualMl, actualRuntimeMs, stopCause,
                completedAt);
    }

    private int applyExecutionReport(
            CommandAckPhase phase,
            String commandId,
            Integer actualMl,
            Integer actualRuntimeMs,
            String stopCause,
            Instant completedAt) {
        Instant storedAt = completedAt.truncatedTo(ChronoUnit.MICROS);
        // acked_at is COALESCEd rather than set: it means "when the device first
        // answered", and a command that went ISSUED → ACCEPTED → COMPLETED
        // already has the earlier, truer value.
        return jdbcTemplate.update("""
                UPDATE device_command
                SET state = ?, acked_at = COALESCE(acked_at, ?), completed_at = ?,
                    actual_ml = ?, actual_runtime_ms = ?, stop_cause = ?
                WHERE command_id = ? AND state IN (%s)
                """.formatted(allowedFrom(phase)),
                phase.target().name(),
                Timestamp.from(storedAt),
                Timestamp.from(storedAt),
                actualMl,
                actualRuntimeMs,
                stopCause,
                commandId);
    }

    /**
     * Commands whose TTL has passed with no terminal report, oldest first.
     *
     * <p>Read separately from the update so the sweep can log each transition by
     * id. A command that receives its ack between this read and that update just
     * loses the race and the update reports zero rows, which is the correct
     * outcome — the real report beats the assumption.
     */
    public List<String> expirableCommandIds(Instant now) {
        return jdbcTemplate.queryForList("""
                SELECT command_id FROM device_command
                WHERE state IN ('ISSUED', 'ACCEPTED') AND expires_at <= ?
                ORDER BY issued_at ASC
                """, String.class, Timestamp.from(now));
    }

    /**
     * Gives up waiting for a report.
     *
     * <p>Not a statement that nothing happened: EXPIRED keeps counting against
     * the budget at {@code granted_ml}, because the device may well have watered
     * and then lost connectivity. {@code expires_at} is re-checked in the WHERE
     * clause so this cannot expire a command that was still live when the update
     * ran.
     */
    public int markExpired(String commandId, Instant now) {
        return jdbcTemplate.update("""
                UPDATE device_command
                SET state = 'EXPIRED'
                WHERE command_id = ? AND state IN ('ISSUED', 'ACCEPTED') AND expires_at <= ?
                """, commandId, Timestamp.from(now));
    }

    /**
     * The {@code IN (...)} list for a phase, as SQL literals.
     *
     * <p>Inlined rather than bound to placeholders because the number of them
     * varies per phase, and inlining is safe here for a reason worth stating: the
     * values are {@link CommandState} enum constant names, so they are decided at
     * compile time and no caller can influence them.
     */
    private static String allowedFrom(CommandAckPhase phase) {
        return phase.allowedFrom().stream()
                .map(state -> "'" + state.name() + "'")
                .collect(Collectors.joining(", "));
    }
    private static void setNullableInt(PreparedStatement statement, int index, Integer value)
            throws SQLException {
        if (value == null) {
            statement.setNull(index, Types.INTEGER);
        } else {
            statement.setInt(index, value);
        }
    }

    private static void setNullableTimestamp(PreparedStatement statement, int index, Instant value)
            throws SQLException {
        if (value == null) {
            statement.setNull(index, Types.TIMESTAMP);
        } else {
            statement.setTimestamp(index, Timestamp.from(value));
        }
    }

    private DeviceCommand mapCommand(ResultSet resultSet, int rowNumber) throws SQLException {
        return new DeviceCommand(
                resultSet.getString("command_id"),
                resultSet.getLong("pot_id"),
                resultSet.getString("correlation_id"),
                resultSet.getString("actuator"),
                resultSet.getString("action"),
                nullableInt(resultSet, "granted_ml"),
                resultSet.getInt("max_runtime_ms"),
                CommandState.valueOf(resultSet.getString("state")),
                resultSet.getTimestamp("issued_at").toInstant(),
                resultSet.getTimestamp("expires_at").toInstant(),
                nullableInstant(resultSet, "acked_at"),
                nullableInstant(resultSet, "completed_at"),
                nullableInt(resultSet, "actual_ml"),
                nullableInt(resultSet, "actual_runtime_ms"),
                resultSet.getString("stop_cause"),
                CommandOrigin.valueOf(resultSet.getString("origin")));
    }

    private static Integer nullableInt(ResultSet resultSet, String column) throws SQLException {
        int value = resultSet.getInt(column);
        return resultSet.wasNull() ? null : value;
    }

    private static Instant nullableInstant(ResultSet resultSet, String column) throws SQLException {
        Timestamp value = resultSet.getTimestamp(column);
        return value == null ? null : value.toInstant();
    }
}
