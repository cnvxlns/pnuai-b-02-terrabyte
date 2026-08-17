package com.terrabyte.backend.irrigation;

import java.time.Clock;
import java.time.Instant;
import java.util.List;
import java.util.Optional;

import com.terrabyte.backend.measurement.MeasurementMetric;
import com.terrabyte.backend.measurement.MeasurementPoint;
import com.terrabyte.backend.measurement.MeasurementStore;
import com.terrabyte.backend.measurement.TelemetrySample;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Isolation;
import org.springframework.transaction.annotation.Transactional;

/**
 * The single gate every irrigation command passes through.
 *
 * <p>Rules, AI suggestions, manual taps and the edge's own autonomous watering
 * all arrive here, and nothing downstream can issue a command without an
 * {@link IrrigationGrant} that only this class can mint. That is the design:
 * without one chokepoint, every other safeguard is advice that some future code
 * path forgets to take.
 *
 * <p>Gates run in order and stop at the first refusal. Every outcome — granted
 * or refused — is written to {@code irrigation_decision}, because a pot that
 * quietly never gets watered is as much a failure as one that floods, and only
 * the stored refusals make the first case visible.
 *
 * <p>Whatever sized the dose — the edge's water-balance formula, the pot-size
 * table, or a person tapping a button — has no say in gates 1–6. It only
 * proposes a number, which arrives as {@code requestedMl} and meets gate 7 like
 * any other.
 */
@Service
public class IrrigationGovernor {

    private static final Logger LOGGER = LoggerFactory.getLogger(IrrigationGovernor.class);

    private final IrrigationProperties properties;
    private final MeasurementStore measurementStore;
    private final DeviceCommandRepository commandRepository;
    private final IrrigationDecisionRepository decisionRepository;
    private final CommandIdGenerator commandIdGenerator;
    private final Clock clock;

    public IrrigationGovernor(
            IrrigationProperties properties,
            MeasurementStore measurementStore,
            DeviceCommandRepository commandRepository,
            IrrigationDecisionRepository decisionRepository,
            CommandIdGenerator commandIdGenerator,
            Clock clock) {
        this.properties = properties;
        this.measurementStore = measurementStore;
        this.commandRepository = commandRepository;
        this.decisionRepository = decisionRepository;
        this.commandIdGenerator = commandIdGenerator;
        this.clock = clock;
    }

    /**
     * Decide whether this pot may be watered, and how much.
     *
     * <p>SERIALIZABLE because gates 5 and 6 read state that a concurrent request
     * is about to change. Two simultaneous requests for the same pot must not
     * both observe "nothing outstanding" and both be granted — scenario 10.
     */
    @Transactional(isolation = Isolation.SERIALIZABLE)
    public AuthorizationResult authorize(IrrigationRequest request) {
        Instant now = clock.instant();

        // Not a gate: a volume that is not a volume. Refused rather than
        // coerced, because the only way to "fix" a negative request is to
        // invent a number the caller never asked for.
        if (request.requestedMl() <= 0) {
            return deny(request, now, DenyReason.AI_OUT_OF_RANGE,
                    "requested volume must be positive: " + request.requestedMl(), null, null);
        }
        if (request.cooldownOverride()
                && (request.overrideReason() == null || request.overrideReason().isBlank())) {
            // The override is the one place a human can weaken a safeguard, so
            // it does not happen anonymously.
            return deny(request, now, DenyReason.COOLDOWN,
                    "cooldown override requires a reason", null, null);
        }

        Optional<TelemetrySample> latest = measurementStore.findLatest(request.potId());

        // Gate 1 — freshness. Acting on a stale reading is acting on a guess
        // about the present.
        if (latest.isEmpty()) {
            return deny(request, now, DenyReason.INPUT_STALE,
                    "no telemetry has ever been recorded for this pot", null, null);
        }
        TelemetrySample sample = latest.get();
        if (sample.observedAt().isBefore(now.minus(properties.maxSampleAge()))) {
            return deny(request, now, DenyReason.INPUT_STALE,
                    "latest sample is older than " + properties.maxSampleAge(), sample, null);
        }

        // Gate 2 — sensor validity. A probe that reports itself broken, or a
        // value outside the physical range, cannot support a watering decision.
        if (!sample.soilSensorValid()) {
            return deny(request, now, DenyReason.SENSOR_INVALID,
                    "soil sensor reported itself invalid", sample, null);
        }
        double moisture = sample.soilMoisturePct();
        if (moisture < 0.0 || moisture > 100.0 || Double.isNaN(moisture)) {
            return deny(request, now, DenyReason.SENSOR_INVALID,
                    "soil moisture out of range: " + moisture, sample, null);
        }

        // Gate 3 — implausible jump. Soil moisture cannot move tens of points in
        // a minute. When it appears to, the probe is failing or was just
        // yanked out of the pot, and neither is a state to irrigate from.
        Optional<String> jump = detectImplausibleJump(request.potId(), sample, now);
        if (jump.isPresent()) {
            return deny(request, now, DenyReason.IMPLAUSIBLE_JUMP, jump.get(), sample, null);
        }

        // Gate 4 — cooldown. Manual requests may skip this one, and only this
        // one, with a recorded reason.
        if (!request.cooldownOverride()) {
            Optional<Instant> lastCompleted = commandRepository.lastCompletedAt(request.potId());
            if (lastCompleted.isPresent()
                    && lastCompleted.get().isAfter(now.minus(properties.minInterval()))) {
                Instant availableAt = lastCompleted.get().plus(properties.minInterval());
                return deny(request, now, DenyReason.COOLDOWN,
                        "last completed irrigation was at " + lastCompleted.get(),
                        sample, availableAt);
            }
        }

        // Gate 5 — in flight. An outstanding command may still be running; a
        // second one would stack on top of it.
        Optional<Instant> outstandingUntil =
                commandRepository.outstandingExpiresAt(request.potId(), now);
        if (outstandingUntil.isPresent()) {
            return deny(request, now, DenyReason.IN_FLIGHT,
                    "a command for this pot is still outstanding", sample,
                    outstandingUntil.get());
        }

        // Gate 6 — daily budget, measured from what was actually reported as
        // delivered, not from what was authorised. Unfinished and expired
        // commands count at their granted volume because they may well have run.
        int consumed = commandRepository.consumedMlSince(
                request.potId(), now.minus(properties.budgetWindow()));
        int remaining = properties.dailyBudgetMl() - consumed;
        if (remaining <= 0) {
            return deny(request, now, DenyReason.DAILY_BUDGET,
                    "24h budget exhausted: " + consumed + "/" + properties.dailyBudgetMl() + "mL",
                    sample, budgetFreesAt(request.potId(), now));
        }

        // Gate 7 — clamp. Not a refusal: the request is honoured, smaller.
        int granted = request.requestedMl();
        ClampReason clampReason = null;
        if (granted > properties.doseMaxMl()) {
            granted = properties.doseMaxMl();
            clampReason = ClampReason.MAX_DOSE;
        }
        if (granted > remaining) {
            granted = remaining;
            clampReason = ClampReason.DAILY_BUDGET;
        }
        if (granted < properties.doseMinMl()) {
            // What is left of the budget cannot buy a dose big enough to wet
            // anything. Raising it to the floor would spend budget the gate
            // above just said was not there.
            return deny(request, now, DenyReason.DAILY_BUDGET,
                    "remaining budget " + remaining + "mL is below the minimum dose",
                    sample, budgetFreesAt(request.potId(), now));
        }

        return grant(request, now, sample, granted, clampReason);
    }

    private Optional<String> detectImplausibleJump(
            long potId, TelemetrySample sample, Instant now) {
        Instant since = now.minus(properties.implausibleJumpWindow());
        List<MeasurementPoint> recent =
                measurementStore.findPoints(potId, MeasurementMetric.SOIL_MOISTURE_PCT, since);
        for (MeasurementPoint point : recent) {
            double delta = Math.abs(point.value() - sample.soilMoisturePct());
            if (delta >= properties.implausibleJumpPct()) {
                return Optional.of(
                        "soil moisture moved %.1f%%p within %s"
                                .formatted(delta, properties.implausibleJumpWindow()));
            }
        }
        return Optional.empty();
    }

    private AuthorizationResult grant(
            IrrigationRequest request,
            Instant now,
            TelemetrySample sample,
            int grantedMl,
            ClampReason clampReason) {

        String commandId = commandIdGenerator.next();
        IrrigationGrant issued = new IrrigationGrant(
                commandId,
                request.potId(),
                grantedMl,
                properties.runtimeMsFor(grantedMl),
                now,
                now.plus(properties.commandTtl()),
                request.correlationId(),
                request.source(),
                CommandOrigin.CLOUD);

        decisionRepository.save(IrrigationDecision.granted(
                request.potId(),
                request.correlationId(),
                request.source(),
                sample.observedAt(),
                sample.soilMoisturePct(),
                request.aiModelVersion(),
                request.aiRequestedMl(),
                grantedMl,
                clampReason,
                commandId,
                now));

        LOGGER.info(
                "irrigation granted pot_id={} command_id={} granted_ml={} clamp={} source={}",
                request.potId(), commandId, grantedMl, clampReason, request.source());
        return new AuthorizationResult.Granted(issued, clampReason);
    }

    /** When the budget window releases enough volume for another dose. */
    private Instant budgetFreesAt(long potId, Instant now) {
        return commandRepository
                .oldestCountedIssuedAt(potId, now.minus(properties.budgetWindow()))
                .map(oldest -> oldest.plus(properties.budgetWindow()))
                .orElse(null);
    }

    private AuthorizationResult deny(
            IrrigationRequest request,
            Instant now,
            DenyReason reason,
            String detail,
            TelemetrySample sample,
            Instant nextAvailableAt) {

        // The AI attribution is recorded on refusals too: "the model asked for
        // 260 mL and the budget refused" is exactly the pairing you want when
        // deciding whether the model or the envelope needs adjusting.
        decisionRepository.save(IrrigationDecision.denied(
                request.potId(),
                request.correlationId(),
                request.source(),
                sample == null ? now : sample.observedAt(),
                sample == null ? null : sample.soilMoisturePct(),
                IrrigationDecision.VERDICT_NEEDED,
                request.aiModelVersion(),
                request.aiRequestedMl(),
                reason,
                now));

        LOGGER.info(
                "irrigation denied pot_id={} reason={} detail={} next_available_at={} source={}",
                request.potId(), reason, detail, nextAvailableAt, request.source());
        return new AuthorizationResult.Denied(reason, detail, nextAvailableAt);
    }
}
