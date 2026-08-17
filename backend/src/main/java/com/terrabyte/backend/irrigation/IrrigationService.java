package com.terrabyte.backend.irrigation;

import java.time.Clock;

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
 * <p>Order matters and is the whole point: the edge only ever proposes a number,
 * and that number then meets {@link IrrigationGovernor} exactly like a manual
 * request would. There is no path from a suggested volume to a pump that skips
 * the gates.
 */
@Service
public class IrrigationService {

    private static final Logger LOGGER = LoggerFactory.getLogger(IrrigationService.class);

    private final IrrigationGovernor governor;
    private final VolumeResolver volumeResolver;
    private final DeviceCommandRepository commandRepository;
    private final MeasurementStore measurementStore;
    private final PotRepository potRepository;
    private final IrrigationProperties properties;
    private final CommandDispatcher dispatcher;
    private final Clock clock;

    public IrrigationService(
            IrrigationGovernor governor,
            VolumeResolver volumeResolver,
            DeviceCommandRepository commandRepository,
            MeasurementStore measurementStore,
            PotRepository potRepository,
            IrrigationProperties properties,
            CommandDispatcher dispatcher,
            Clock clock) {
        this.governor = governor;
        this.volumeResolver = volumeResolver;
        this.commandRepository = commandRepository;
        this.measurementStore = measurementStore;
        this.potRepository = potRepository;
        this.properties = properties;
        this.dispatcher = dispatcher;
        this.clock = clock;
    }

    /**
     * The rule engine decided water is needed; take the edge's dose and proceed.
     *
     * <p>The edge's answer is advisory. If it is missing or out of range the
     * resolver substitutes a volume from the pot-size table and the request
     * continues — a pot whose node cannot compute a dose still has to be watered,
     * and never more than the envelope allows.
     */
    public IrrigationOutcome requestAutomatic(long potId, String correlationId) {
        Pot pot = requirePot(potId);
        TelemetrySample sample = measurementStore.findLatest(potId).orElse(null);

        VolumeResolver.ResolvedVolume resolved = volumeResolver.resolve(pot, sample);

        boolean fromEdge = resolved.source() == VolumeSource.EDGE_SUGGESTION;
        CommandSource source = fromEdge ? CommandSource.RULE_AI : CommandSource.RULE;

        AuthorizationResult result = governor.authorize(IrrigationRequest.fromModel(
                potId, resolved.volumeMl(), source, correlationId,
                resolved.modelVersion(),
                // 폴백이 이겼더라도 엣지가 제안한 값을 남긴다. 거부된 99999 가
                // 로그에만 있으면 사후에 어느 쪽이 고장났는지 지목할 수 없다.
                resolved.edgeProposedMl()));

        return complete(result, resolved);
    }

    /** Someone tapped the button in the app. */
    public IrrigationOutcome requestManual(
            long potId, int requestedMl, boolean cooldownOverride, String overrideReason) {

        requirePot(potId);
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

    private Pot requirePot(long potId) {
        return potRepository
                .findById(potId)
                .orElseThrow(() -> new ApiException(
                        HttpStatus.NOT_FOUND, "POT_NOT_FOUND", "화분을 찾을 수 없습니다."));
    }
}
