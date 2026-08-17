package com.terrabyte.backend.irrigation;

import java.time.Instant;

/**
 * What happened to one irrigation request, in the shape the API returns.
 *
 * <p>{@code dispatched} is separate from {@code granted} on purpose. Until #50
 * gives the backend a downlink, a command can be authorised and recorded while
 * nothing delivers it, and a caller that cannot tell those apart would report a
 * watering that never happened.
 */
public record IrrigationOutcome(
        boolean granted,
        String commandId,
        Integer grantedMl,
        ClampReason clampReason,
        Instant expiresAt,
        boolean dispatched,
        DenyReason denyReason,
        String detail,
        /** When the refusal lifts, or null when it clears on new data instead. */
        Instant nextAvailableAt,
        VolumeSource volumeSource,
        /** Kept under its old name: it still answers "what proposed this volume". */
        String aiModelVersion) {

    public static IrrigationOutcome granted(
            IrrigationGrant grant,
            ClampReason clampReason,
            boolean dispatched,
            VolumeSource volumeSource,
            String aiModelVersion) {
        return new IrrigationOutcome(
                true,
                grant.commandId(),
                grant.grantedMl(),
                clampReason,
                grant.expiresAt(),
                dispatched,
                null,
                null,
                null,
                volumeSource,
                aiModelVersion);
    }

    public static IrrigationOutcome denied(
            DenyReason reason, String detail, Instant nextAvailableAt) {
        return new IrrigationOutcome(
                false, null, null, null, null, false,
                reason, detail, nextAvailableAt, null, null);
    }
}
