package com.terrabyte.backend.pot;

import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.List;
import java.util.Optional;

import com.terrabyte.backend.device.DeviceStatus;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.support.GeneratedKeyHolder;
import org.springframework.stereotype.Repository;

@Repository
public class PotRepository {

    private static final String SELECT_COLUMNS = """
            SELECT p.id, p.device_id, p.node_id, p.label, p.crop_code, p.crop_selected_at,
                   p.status, p.last_seen_at, p.created_at
            FROM pot p
            """;

    private final JdbcTemplate jdbcTemplate;

    public PotRepository(@Qualifier("postgresJdbcTemplate") JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    public Pot save(long deviceId, String nodeId, String label) {
        GeneratedKeyHolder keyHolder = new GeneratedKeyHolder();
        jdbcTemplate.update(connection -> {
            PreparedStatement statement = connection.prepareStatement(
                    "INSERT INTO pot (device_id, node_id, label) VALUES (?, ?, ?)",
                    new String[]{"id"});
            statement.setLong(1, deviceId);
            statement.setString(2, nodeId);
            statement.setString(3, label);
            return statement;
        }, keyHolder);
        Number key = keyHolder.getKey();
        if (key == null) {
            throw new IllegalStateException("Failed to read generated pot id");
        }
        return findById(key.longValue())
                .orElseThrow(() -> new IllegalStateException("Created pot could not be loaded"));
    }

    public Optional<Pot> findById(long potId) {
        return query(" WHERE p.id = ?", potId);
    }

    public Optional<Pot> findOwned(long potId, long userId) {
        return query(" JOIN device d ON d.id = p.device_id WHERE p.id = ? AND d.user_id = ?", potId, userId);
    }

    public Optional<Pot> representative(long deviceId, long userId) {
        return query(" JOIN device d ON d.id = p.device_id"
                + " WHERE p.device_id = ? AND d.user_id = ? ORDER BY p.id", deviceId, userId);
    }

    public Optional<Pot> findByDeviceAndNode(long deviceId, String nodeId) {
        return query(" WHERE p.device_id = ? AND p.node_id = ?", deviceId, nodeId);
    }

    public Optional<Pot> oldestUnbound(long deviceId) {
        return query(" WHERE p.device_id = ? AND p.node_id IS NULL ORDER BY p.created_at, p.id", deviceId);
    }

    public List<Pot> findAllOwned(long userId) {
        return jdbcTemplate.query(
                SELECT_COLUMNS + " JOIN device d ON d.id = p.device_id WHERE d.user_id = ? ORDER BY p.id",
                this::mapPot,
                userId);
    }

    public List<Pot> findAllByDevice(long deviceId) {
        return jdbcTemplate.query(
                SELECT_COLUMNS + " WHERE p.device_id = ? ORDER BY p.id", this::mapPot, deviceId);
    }

    public boolean existsCropByUser(long userId) {
        Integer count = jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM pot p JOIN device d ON d.id = p.device_id"
                        + " WHERE d.user_id = ? AND p.crop_code IS NOT NULL",
                Integer.class,
                userId);
        return count != null && count > 0;
    }

    public int bind(long potId, String nodeId, Instant lastSeenAt) {
        return jdbcTemplate.update(
                "UPDATE pot SET node_id = ?, status = 'ONLINE', last_seen_at = ?"
                        + " WHERE id = ? AND node_id IS NULL",
                nodeId,
                Timestamp.from(lastSeenAt),
                potId);
    }

    public void markOnline(long potId, Instant lastSeenAt) {
        jdbcTemplate.update(
                "UPDATE pot SET status = 'ONLINE', last_seen_at = ? WHERE id = ?",
                Timestamp.from(lastSeenAt),
                potId);
    }

    /**
     * Driven by the MQTT last will when a gateway's connection drops.
     *
     * <p>{@code last_seen_at} is deliberately left alone: it records when each
     * pot was last heard from, and overwriting it when the gateway disconnect
     * is detected would erase that.
     */
    public void markOfflineByDevice(long deviceId) {
        jdbcTemplate.update(
                "UPDATE pot SET status = 'OFFLINE' WHERE device_id = ?", deviceId);
    }

    public int selectCrop(long potId, String cropCode, Instant selectedAt) {
        return jdbcTemplate.update(
                "UPDATE pot SET crop_code = ?, crop_selected_at = ? WHERE id = ?",
                cropCode,
                Timestamp.from(selectedAt),
                potId);
    }

    public int updateLabel(long potId, String label) {
        return jdbcTemplate.update(
                "UPDATE pot SET label = ? WHERE id = ?",
                label,
                potId);
    }

    private Optional<Pot> query(String suffix, Object... arguments) {
        return jdbcTemplate.query(SELECT_COLUMNS + suffix, this::mapPot, arguments).stream().findFirst();
    }

    private Pot mapPot(ResultSet resultSet, int rowNumber) throws SQLException {
        Timestamp cropSelectedAt = resultSet.getTimestamp("crop_selected_at");
        Timestamp lastSeenAt = resultSet.getTimestamp("last_seen_at");
        return new Pot(
                resultSet.getLong("id"),
                resultSet.getLong("device_id"),
                resultSet.getString("node_id"),
                resultSet.getString("label"),
                resultSet.getString("crop_code"),
                cropSelectedAt == null ? null : cropSelectedAt.toInstant(),
                DeviceStatus.valueOf(resultSet.getString("status")),
                lastSeenAt == null ? null : lastSeenAt.toInstant(),
                resultSet.getTimestamp("created_at").toInstant());
    }
}
