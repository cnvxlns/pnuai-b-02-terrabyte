package com.terrabyte.backend.pot;

import java.time.Instant;

import com.terrabyte.backend.device.DeviceStatus;

public record Pot(
        long id,
        long deviceId,
        String nodeId,
        String label,
        String cropCode,
        Instant cropSelectedAt,
        DeviceStatus status,
        Instant lastSeenAt,
        Instant createdAt,
        // Null means the volume was never recorded, which the irrigation
        // fallback table treats as the smallest pot rather than guessing.
        Integer substrateVolumeMl,
        // Whether the rule engine may act on this pot. Off means "I will do it
        // myself", not "nothing may run": manual irrigation and light commands
        // ignore this entirely.
        boolean autoControlEnabled) {
}
