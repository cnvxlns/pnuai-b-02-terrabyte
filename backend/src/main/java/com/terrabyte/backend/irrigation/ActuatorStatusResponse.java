package com.terrabyte.backend.irrigation;

import com.fasterxml.jackson.annotation.JsonInclude;

/**
 * What each actuator was last told to do, and how that ended.
 *
 * <p>Not live hardware state. The server deliberately does not track it —
 * {@code StatusUplinkHandler} discards the firmware's actuators block, and the
 * comment in {@code CommandAckService} says why — so the last command and its
 * outcome is the strongest honest claim available.
 *
 * <p>Null means never commanded, which is different from off. Claiming "off"
 * for a lamp nothing has ever looked at would be a statement about hardware
 * rather than about our records.
 */
public record ActuatorStatusResponse(
        @JsonInclude(JsonInclude.Include.NON_NULL) CommandHistoryEntry pump,
        @JsonInclude(JsonInclude.Include.NON_NULL) CommandHistoryEntry light) {
}
