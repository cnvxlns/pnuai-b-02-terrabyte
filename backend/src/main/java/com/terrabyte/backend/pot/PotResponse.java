package com.terrabyte.backend.pot;

import java.time.Instant;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.terrabyte.backend.device.DeviceStatus;

public record PotResponse(
        long id,
        long deviceId,
        @JsonInclude(JsonInclude.Include.NON_NULL) String nodeId,
        String label,
        @JsonInclude(JsonInclude.Include.NON_NULL) String cropCode,
        DeviceStatus status,
        @JsonInclude(JsonInclude.Include.NON_NULL) Instant lastSeenAt,
        boolean autoControlEnabled) {

    public static PotResponse from(Pot pot) {
        return new PotResponse(
                pot.id(), pot.deviceId(), pot.nodeId(), pot.label(), pot.cropCode(),
                pot.status(), pot.lastSeenAt(), pot.autoControlEnabled());
    }
}
