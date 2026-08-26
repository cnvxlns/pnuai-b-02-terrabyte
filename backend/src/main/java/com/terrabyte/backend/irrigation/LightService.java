package com.terrabyte.backend.irrigation;

import java.time.Clock;
import java.time.Instant;
import java.util.Optional;

import com.terrabyte.backend.api.ApiException;
import com.terrabyte.backend.irrigation.CommandTargetResolver.CommandTarget;
import com.terrabyte.backend.pot.PotRepository;

import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;

/** Issues manual grow-light latch commands without importing irrigation policy. */
@Service
public class LightService {

    private final PotRepository potRepository;
    private final CommandTargetResolver targetResolver;
    private final DeviceCommandRepository commandRepository;
    private final CommandIdGenerator commandIdGenerator;
    private final IrrigationProperties properties;
    private final CommandDispatcher dispatcher;
    private final Clock clock;

    public LightService(
            PotRepository potRepository,
            CommandTargetResolver targetResolver,
            DeviceCommandRepository commandRepository,
            CommandIdGenerator commandIdGenerator,
            IrrigationProperties properties,
            CommandDispatcher dispatcher,
            Clock clock) {
        this.potRepository = potRepository;
        this.targetResolver = targetResolver;
        this.commandRepository = commandRepository;
        this.commandIdGenerator = commandIdGenerator;
        this.properties = properties;
        this.dispatcher = dispatcher;
        this.clock = clock;
    }

    public LightOutcome requestManual(long potId, long userId, boolean on) {
        requireOwnedPot(potId, userId);
        return request(potId, on, "manual-light-" + clock.millis());
    }

    /**
     * The rule engine decided the lamp should change state.
     *
     * <p>No user id, exactly like {@code IrrigationService#requestAutomatic}:
     * nobody tapped anything, so there is no ownership to prove. The caller
     * supplies the correlation id instead, which is what joins this command to
     * the reading that caused it.
     */
    public LightOutcome requestAutomatic(long potId, boolean on, String correlationId) {
        return request(potId, on, correlationId);
    }

    private LightOutcome request(long potId, boolean on, String correlationId) {
        Optional<CommandTarget> resolved = targetResolver.resolve(potId);
        if (resolved.isEmpty() || !resolved.get().isAddressable()) {
            // A transport returning false after authorisation means delivery
            // failed. A missing addressee is different: the request cannot be
            // issued at all, and recording it as sent would be misleading.
            return LightOutcome.denied(
                    on,
                    LightDenyReason.NO_ADDRESSABLE_NODE,
                    "화분에 연결된 조명 노드를 찾을 수 없습니다.",
                    null);
        }

        Instant now = clock.instant();
        Optional<Instant> outstandingUntil = commandRepository.outstandingUntil(
                potId, DeviceCommand.ACTUATOR_LIGHT, now);
        if (outstandingUntil.isPresent()) {
            return LightOutcome.denied(
                    on,
                    LightDenyReason.IN_FLIGHT,
                    "이전 조명 명령의 응답을 기다리고 있습니다.",
                    outstandingUntil.get());
        }

        DeviceCommand command = DeviceCommand.issuedLight(
                commandIdGenerator.next(now),
                potId,
                correlationId,
                on,
                now,
                now.plus(properties.commandTtl()));

        // Persist first so an acknowledgement can always join on command_id,
        // including when the broker answers faster than the HTTP request does.
        commandRepository.save(command);
        boolean dispatched = dispatcher.dispatchLight(command, resolved.get());
        return LightOutcome.issued(command, on, dispatched);
    }

    /** Keeps another user's pot indistinguishable from a nonexistent id. */
    private void requireOwnedPot(long potId, long userId) {
        potRepository
                .findOwned(potId, userId)
                .orElseThrow(() -> new ApiException(
                        HttpStatus.NOT_FOUND, "POT_NOT_FOUND", "화분을 찾을 수 없습니다."));
    }
}
