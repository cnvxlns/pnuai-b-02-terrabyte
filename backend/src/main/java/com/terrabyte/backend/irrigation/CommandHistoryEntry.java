package com.terrabyte.backend.irrigation;

import java.time.Instant;

import com.fasterxml.jackson.annotation.JsonInclude;

/**
 * One command as the app shows it.
 *
 * <p>A narrower view than {@link DeviceCommand}: the runtime, the TTL and the
 * correlation id are operator concerns, and a history screen that shows
 * everything shows nothing.
 */
public record CommandHistoryEntry(
        String commandId,
        String actuator,
        String action,
        CommandState state,
        CommandOrigin origin,
        @JsonInclude(JsonInclude.Include.NON_NULL) Integer grantedMl,
        @JsonInclude(JsonInclude.Include.NON_NULL) Integer actualMl,
        /** The firmware's own word for why the run stopped, verbatim. */
        @JsonInclude(JsonInclude.Include.NON_NULL) String stopCause,
        Instant issuedAt,
        @JsonInclude(JsonInclude.Include.NON_NULL) Instant completedAt) {

    public static CommandHistoryEntry from(DeviceCommand command) {
        return new CommandHistoryEntry(
                command.commandId(),
                command.actuator(),
                command.action(),
                command.state(),
                command.origin(),
                command.grantedMl(),
                command.actualMl(),
                command.stopCause(),
                command.issuedAt(),
                command.completedAt());
    }
}
