package com.terrabyte.backend.irrigation;

import java.time.Clock;
import java.util.List;
import java.util.Optional;

import com.terrabyte.backend.ai.IrrigationPredictionRequest;
import com.terrabyte.backend.api.ApiException;
import com.terrabyte.backend.measurement.MeasurementStore;
import com.terrabyte.backend.measurement.TelemetrySample;
import com.terrabyte.backend.pot.Pot;
import com.terrabyte.backend.pot.PotRepository;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;

/**
 * Turns "this pot needs water" into an authorised, recorded command.
 *
 * <p>Order matters and is the whole point: the AI and the edge only ever propose
 * a number, and that number then meets {@link IrrigationGovernor} exactly like a
 * manual request would. There is no path from a model output to a pump that skips
 * the gates.
 */
@Service
public class IrrigationService {

    private static final Logger LOGGER = LoggerFactory.getLogger(IrrigationService.class);

    /**
     * Must match the AI server's {@code input_schema_version}; a mismatch makes it
     * refuse rather than answer for a contract it does not implement.
     */
    private static final int SCHEMA_VERSION = 1;

    private final IrrigationGovernor governor;
    private final VolumeResolver volumeResolver;
    private final DeviceCommandRepository commandRepository;
    private final IrrigationDecisionRepository decisionRepository;
    private final MeasurementStore measurementStore;
    private final PotRepository potRepository;
    private final IrrigationProperties properties;
    private final CommandDispatcher dispatcher;
    private final Clock clock;

    public IrrigationService(
            IrrigationGovernor governor,
            VolumeResolver volumeResolver,
            DeviceCommandRepository commandRepository,
            IrrigationDecisionRepository decisionRepository,
            MeasurementStore measurementStore,
            PotRepository potRepository,
            IrrigationProperties properties,
            CommandDispatcher dispatcher,
            Clock clock) {
        this.governor = governor;
        this.volumeResolver = volumeResolver;
        this.commandRepository = commandRepository;
        this.decisionRepository = decisionRepository;
        this.measurementStore = measurementStore;
        this.potRepository = potRepository;
        this.properties = properties;
        this.dispatcher = dispatcher;
        this.clock = clock;
    }

    /**
     * The rule engine decided water is needed; ask the AI how much, and take the
     * edge's dose when the AI has nothing usable to say.
     *
     * <p>Both answers are advisory. If the AI is unreachable, slow, disagrees on the
     * schema version or returns something out of range, the resolver drops to the
     * edge's water-balance dose and then to the pot-size table, and the request
     * continues — an AI outage must never stop a plant from being watered, and must
     * never water it more than the envelope allows either.
     */
    public IrrigationOutcome requestAutomatic(long potId, String correlationId) {
        Pot pot = requirePot(potId);
        TelemetrySample sample = measurementStore.findLatest(potId).orElse(null);

        VolumeResolver.ResolvedVolume resolved =
                volumeResolver.resolve(pot, sample, features(pot, sample));

        boolean sized = resolved.source() != VolumeSource.POT_SIZE_FALLBACK;
        CommandSource source = sized ? CommandSource.RULE_AI : CommandSource.RULE;

        AuthorizationResult result = governor.authorize(IrrigationRequest.fromModel(
                potId, resolved.volumeMl(), source, correlationId,
                resolved.modelVersion(),
                // 폴백이 이겼더라도 제안된 값을 남긴다. 거부된 99999 가
                // 로그에만 있으면 사후에 어느 쪽이 고장났는지 지목할 수 없다.
                resolved.proposedMl()));

        return complete(result, resolved);
    }

    /**
     * The feature vector for one prediction.
     *
     * <p>Built even when there is no reading: the Governor refuses on gate 1
     * regardless, so this only has to be well-formed — the refusal stays decided in
     * exactly one place.
     */
    private IrrigationPredictionRequest features(Pot pot, TelemetrySample sample) {
        if (sample == null) {
            return new IrrigationPredictionRequest(
                    SCHEMA_VERSION, pot.cropCode(), pot.substrateVolumeMl(),
                    null, null, null, null, null, null);
        }
        return new IrrigationPredictionRequest(
                SCHEMA_VERSION,
                pot.cropCode(),
                pot.substrateVolumeMl(),
                sample.soilMoisturePct(),
                sample.soilTemperatureC(),
                sample.airTemperatureC(),
                sample.airHumidityPct(),
                sample.plantLightPpfdUmolM2S(),
                hoursSinceLastIrrigation(pot.id()));
    }

    private Double hoursSinceLastIrrigation(long potId) {
        Optional<java.time.Instant> last = commandRepository.lastCompletedAt(potId);
        return last
                .map(instant ->
                        java.time.Duration.between(instant, clock.instant()).toMinutes() / 60.0)
                .orElse(null);
    }

    /**
     * Someone tapped the button in the app.
     *
     * <p>Takes the caller's user id, unlike {@link #requestAutomatic}: this is
     * the one entry point reachable from an HTTP request, so it is the one that
     * has to prove the pot belongs to whoever asked.
     */
    public IrrigationOutcome requestManual(
            long potId, long userId, int requestedMl,
            boolean cooldownOverride, String overrideReason) {

        requireOwnedPot(potId, userId);
        AuthorizationResult result = governor.authorize(new IrrigationRequest(
                potId, requestedMl, CommandSource.MANUAL,
                "manual-" + clock.instant().toEpochMilli(), cooldownOverride, overrideReason,
                null, null));

        return complete(result, null);
    }

    private IrrigationOutcome complete(
            AuthorizationResult result, VolumeResolver.ResolvedVolume resolved) {

        if (result instanceof AuthorizationResult.Denied denied) {
            return IrrigationOutcome.denied(
                    denied.reason(), denied.detail(), denied.nextAvailableAt());
        }

        AuthorizationResult.Granted granted = (AuthorizationResult.Granted) result;
        IrrigationGrant grant = granted.grant();

        // The command row is written before dispatch, not after. If the process
        // dies between the two, the budget still counts this volume — the
        // conservative direction. The reverse ordering could water twice.
        commandRepository.save(new DeviceCommand(
                grant.commandId(),
                grant.potId(),
                grant.correlationId(),
                DeviceCommand.ACTUATOR_PUMP,
                DeviceCommand.ACTION_DOSE,
                grant.grantedMl(),
                grant.maxRuntimeMs(),
                CommandState.ISSUED,
                grant.issuedAt(),
                grant.expiresAt(),
                null, null, null, null, null,
                grant.origin()));

        boolean dispatched = dispatcher.dispatch(grant);
        if (!dispatched) {
            LOGGER.warn(
                    "irrigation authorised but not delivered command_id={} pot_id={}",
                    grant.commandId(), grant.potId());
        }

        return IrrigationOutcome.granted(
                grant,
                granted.clampReason(),
                dispatched,
                resolved == null ? null : resolved.source(),
                resolved == null ? null : resolved.modelVersion());
    }

    /**
     * Every decision recorded for a pot the caller owns, refusals included.
     *
     * <p>Routed through the service rather than read straight out of the
     * repository by the controller, so the ownership check has exactly one home.
     * A watering history says when a plant was dry and when someone was home.
     */
    public List<IrrigationTimelineEntry> timeline(long potId, long userId, int limit) {
        requireOwnedPot(potId, userId);
        return decisionRepository.findRecentByPotId(potId, Math.clamp(limit, 1, 100)).stream()
                // Joined per decision rather than in one query, because a
                // refusal has no command id to join on and the list is capped at
                // a hundred rows. A join here would trade a readable answer for
                // a saving nobody can measure.
                .map(decision -> IrrigationTimelineEntry.of(
                        decision,
                        decision.commandId() == null
                                ? null
                                : commandRepository.findById(decision.commandId()).orElse(null)))
                .toList();
    }

    /**
     * The pot, if it belongs to this user.
     *
     * <p>404 rather than 403, matching {@code PotService}: a 403 would confirm
     * that a pot id exists, which turns the id space into something worth
     * enumerating.
     */
    private Pot requireOwnedPot(long potId, long userId) {
        return potRepository
                .findOwned(potId, userId)
                .orElseThrow(() -> new ApiException(
                        HttpStatus.NOT_FOUND, "POT_NOT_FOUND", "화분을 찾을 수 없습니다."));
    }

    /**
     * The pot, without an ownership check.
     *
     * <p>Only for {@link #requestAutomatic}, which no user invokes: the rule
     * engine acts on its own schedule and has no session to check against. Do
     * not reach for this from anything an HTTP request can call.
     */
    private Pot requirePot(long potId) {
        return potRepository
                .findById(potId)
                .orElseThrow(() -> new ApiException(
                        HttpStatus.NOT_FOUND, "POT_NOT_FOUND", "화분을 찾을 수 없습니다."));
    }
}
