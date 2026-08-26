package com.terrabyte.backend.irrigation;

import java.time.Instant;

import com.fasterxml.jackson.annotation.JsonInclude;

/**
 * One irrigation, end to end: the reading, the judgement, and what happened.
 *
 * <p>Issue #51 asks for exactly this in one place — "관수를 했거나 거절한 한
 * 건을 선택하면 사용한 센서값, 판단 이유, 명령 결과를 모두 확인할 수 있다".
 * {@link IrrigationDecision} on its own answers the first two and stops at the
 * command id, so a caller had to know to go and look up the third.
 *
 * <p>The command half is null for a refusal, and that is the honest shape: a
 * decision that granted nothing never produced a command, so there is no state
 * to report rather than an unknown one.
 */
public record IrrigationTimelineEntry(
        Long id,
        long potId,
        String correlationId,
        CommandSource source,
        @JsonInclude(JsonInclude.Include.NON_NULL) Instant sampleObservedAt,
        @JsonInclude(JsonInclude.Include.NON_NULL) Double soilMoisturePct,
        String ruleVerdict,
        @JsonInclude(JsonInclude.Include.NON_NULL) String aiModelVersion,
        @JsonInclude(JsonInclude.Include.NON_NULL) Integer aiRequestedMl,
        @JsonInclude(JsonInclude.Include.NON_NULL) Integer grantedMl,
        @JsonInclude(JsonInclude.Include.NON_NULL) DenyReason denyReason,
        @JsonInclude(JsonInclude.Include.NON_NULL) ClampReason clampReason,
        @JsonInclude(JsonInclude.Include.NON_NULL) String commandId,
        Instant createdAt,
        /** How the command ended, or null when the decision produced none. */
        @JsonInclude(JsonInclude.Include.NON_NULL) CommandState commandState,
        /** What the device reported actually came out. */
        @JsonInclude(JsonInclude.Include.NON_NULL) Integer actualMl,
        /** The firmware's own word for why the run stopped, verbatim. */
        @JsonInclude(JsonInclude.Include.NON_NULL) String stopCause,
        @JsonInclude(JsonInclude.Include.NON_NULL) Instant completedAt) {

    public static IrrigationTimelineEntry of(IrrigationDecision decision, DeviceCommand command) {
        return new IrrigationTimelineEntry(
                decision.id(),
                decision.potId(),
                decision.correlationId(),
                decision.source(),
                decision.sampleObservedAt(),
                decision.soilMoisturePct(),
                decision.ruleVerdict(),
                decision.aiModelVersion(),
                decision.aiRequestedMl(),
                decision.grantedMl(),
                decision.denyReason(),
                decision.clampReason(),
                decision.commandId(),
                decision.createdAt(),
                command == null ? null : command.state(),
                command == null ? null : command.actualMl(),
                command == null ? null : command.stopCause(),
                command == null ? null : command.completedAt());
    }
}
