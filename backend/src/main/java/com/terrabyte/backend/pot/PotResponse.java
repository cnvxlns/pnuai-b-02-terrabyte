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
        @JsonInclude(JsonInclude.Include.NON_NULL) Instant cropSelectedAt,
        DeviceStatus status,
        @JsonInclude(JsonInclude.Include.NON_NULL) Instant lastSeenAt) {

    public static PotResponse from(Pot pot) {
        return new PotResponse(
                pot.id(), pot.deviceId(), pot.nodeId(), pot.label(), pot.cropCode(),
                pot.cropSelectedAt(), pot.status(), pot.lastSeenAt());
    }
}
