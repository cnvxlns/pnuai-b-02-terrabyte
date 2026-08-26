package com.terrabyte.backend.irrigation;

import java.math.BigDecimal;
import java.time.Clock;
import java.time.Instant;
import java.util.Optional;

import com.terrabyte.backend.device.Device;
import com.terrabyte.backend.device.DeviceRepository;
import com.terrabyte.backend.irrigation.CommandTargetResolver.CommandTarget;
import com.terrabyte.backend.notification.IrrigationCompletedEvent;
import com.terrabyte.backend.pot.Pot;
import com.terrabyte.backend.pot.PotRepository;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * Applies a device report to the command it belongs to.
 *
 * <p>This closes the loop the safety envelope depends on. Without it the backend
 * publishes commands and never learns what happened, so the daily-budget gate
 * counts every command at its authorised volume forever and the cooldown gate
 * never sees a completion. That is why acknowledgement was built before
 * publishing: a system that moves water and records nothing is worse than one
 * that honestly reports {@code dispatched=false}.
 *
 * <p>The transition rules live in {@link CommandAckPhase} and are enforced in
 * SQL, not here. What this class owns is everything around them: who is allowed
 * to speak for a command, which clock to believe, and what to do with a report it
 * does not understand.
 */
@Service
public class CommandAckService {

    private static final Logger LOGGER = LoggerFactory.getLogger(CommandAckService.class);

    /** {@code device_command.stop_cause} is VARCHAR(30). */
    private static final int STOP_CAUSE_MAX = 30;

    private final DeviceCommandRepository commandRepository;
    private final CommandTargetResolver targetResolver;
    private final PotRepository potRepository;
    private final DeviceRepository deviceRepository;
    private final ApplicationEventPublisher eventPublisher;
    private final Clock clock;

    public CommandAckService(
            DeviceCommandRepository commandRepository,
            CommandTargetResolver targetResolver,
            PotRepository potRepository,
            DeviceRepository deviceRepository,
            ApplicationEventPublisher eventPublisher,
            Clock clock) {
        this.commandRepository = commandRepository;
        this.targetResolver = targetResolver;
        this.potRepository = potRepository;
        this.deviceRepository = deviceRepository;
        this.eventPublisher = eventPublisher;
        this.clock = clock;
    }

    /**
     * @param gatewayId the topic's gateway segment — the authenticated identity,
     *                  because the broker ACL restricts a gateway to publishing
     *                  beneath its own id
     */
    @Transactional
    public AckResult apply(String gatewayId, CommandAck ack) {
        if (ack.commandId() == null || ack.commandId().isBlank()) {
            LOGGER.warn("dropping ack with no command id gateway_id={}", gatewayId);
            return AckResult.DROPPED;
        }
        Optional<CommandAckPhase> resolvedPhase = CommandAckPhase.from(ack.phase());
        if (resolvedPhase.isEmpty()) {
            // Unknown phase, not unknown reason. There are exactly four phases
            // and a fifth means the contract moved; dropping is right because no
            // amount of redelivery will make it parseable.
            LOGGER.warn(
                    "dropping ack with unrecognised phase={} gateway_id={} command_id={}",
                    ack.phase(), gatewayId, ack.commandId());
            return AckResult.DROPPED;
        }
        CommandAckPhase phase = resolvedPhase.get();

        Optional<DeviceCommand> found = commandRepository.findById(ack.commandId());
        if (found.isEmpty()) {
            // Cannot become known later: the row is written before the command is
            // ever published, so an ack for an id we have never issued is either
            // a stale replay from a previous database or a fabrication.
            LOGGER.warn(
                    "dropping ack for unknown command_id={} gateway_id={} phase={}",
                    ack.commandId(), gatewayId, phase.wireValue());
            return AckResult.DROPPED;
        }
        DeviceCommand command = found.get();

        if (!ownedByGateway(gatewayId, command)) {
            return AckResult.DROPPED;
        }
        if (ack.potId() != null && ack.potId() != command.potId()) {
            // Not fatal — command_id is the join key and it already matched — but
            // a gateway that disagrees with us about which pot a command is for
            // is a gateway whose node bindings are worth looking at.
            LOGGER.warn(
                    "ack pot id disagrees with the command command_id={} claimed_pot_id={} "
                            + "actual_pot_id={}",
                    command.commandId(), ack.potId(), command.potId());
        }

        Instant at = effectiveAt(ack, command);
        String stopCause = stopCause(ack, command.commandId());
        CommandState recordedState = phase == CommandAckPhase.ACCEPTED
                        && DeviceCommand.ACTUATOR_LIGHT.equals(command.actuator())
                ? CommandState.COMPLETED
                : phase.target();
        int rows = applyTransition(phase, command, ack, stopCause, at);

        if (rows == 0) {
            // Either a QoS 1 duplicate or an out-of-order delivery. Both are
            // expected traffic, and both are already handled by definition —
            // the guard is what makes that true rather than hopeful.
            LOGGER.info(
                    "ack ignored, not allowed from current state command_id={} pot_id={} "
                            + "phase={} state={} allowed_from={}",
                    command.commandId(), command.potId(), phase.wireValue(),
                    command.state(), phase.allowedFrom());
            return AckResult.IGNORED;
        }

        LOGGER.info(
                "command state {} -> {} command_id={} pot_id={} gateway_id={} phase={} "
                        + "reason={} stop_cause={} actual_ml={} actual_runtime_ms={} at={}",
                command.state(), recordedState, command.commandId(), command.potId(), gatewayId,
                phase.wireValue(), ack.reason(), stopCause, ack.actualMl(), ack.actualRuntimeMs(),
                at);
        announceIfIrrigationCompleted(command, recordedState, ack, at);
        return AckResult.APPLIED;
    }

    /**
     * Tells the owner that water actually moved.
     *
     * <p>Deliberately downstream of the {@code rows == 0} return above, so the
     * guarded UPDATE is the only thing deciding whether this fires. A QoS 1
     * redelivery changes no rows, returns IGNORED, and never reaches here — which
     * is why the notification needs no suppression window of its own.
     *
     * <p>Restricted to the pump. A light latch is recorded COMPLETED on its
     * <em>accepted</em> ack (see {@link #applyTransition}), and "관수가
     * 완료되었습니다" for a light would be a lie about which actuator ran.
     *
     * <p>An unclaimed gateway simply has no one to address; the command is still
     * recorded and still counts against the budget.
     */
    private void announceIfIrrigationCompleted(
            DeviceCommand command, CommandState recordedState, CommandAck ack, Instant at) {

        if (recordedState != CommandState.COMPLETED
                || !DeviceCommand.ACTUATOR_PUMP.equals(command.actuator())) {
            return;
        }
        Optional<Pot> pot = potRepository.findById(command.potId());
        if (pot.isEmpty()) {
            return;
        }
        Long userId = deviceRepository.findById(pot.get().deviceId())
                .map(Device::userId)
                .orElse(null);
        if (userId == null) {
            LOGGER.debug(
                    "no owner to announce irrigation to command_id={} pot_id={}",
                    command.commandId(), command.potId());
            return;
        }
        eventPublisher.publishEvent(new IrrigationCompletedEvent(
                userId,
                pot.get().deviceId(),
                command.potId(),
                pot.get().label(),
                command.commandId(),
                ack.actualMl() == null ? null : BigDecimal.valueOf(ack.actualMl()),
                at));
    }

    private int applyTransition(
            CommandAckPhase phase,
            DeviceCommand command,
            CommandAck ack,
            String stopCause,
            Instant at) {
        return switch (phase) {
            case ACCEPTED -> {
                if (DeviceCommand.ACTUATOR_LIGHT.equals(command.actuator())) {
                    // A light is a latch: accepted says it was set, and the firmware
                    // sends no completed report after that success. Completing it here
                    // does mean a later dead-man aborted for this command cannot land:
                    // ABORTED is allowed only from ISSUED or ACCEPTED, so its guarded
                    // update matches no COMPLETED row. What we lose is notice that the
                    // light was later forced off. That is acceptable today because
                    // server-side light state tracking is deliberately out of scope;
                    // StatusUplinkHandler already discards the actuators block.
                    yield commandRepository.markCompleted(
                            command.commandId(), null, null, stopCause, at);
                }
                yield commandRepository.markAccepted(command.commandId(), at);
            }
            case REJECTED -> commandRepository.markRejected(command.commandId(), stopCause, at);
            case COMPLETED -> commandRepository.markCompleted(
                    command.commandId(), ack.actualMl(), ack.actualRuntimeMs(), stopCause, at);
            case ABORTED -> commandRepository.markAborted(
                    command.commandId(), ack.actualMl(), ack.actualRuntimeMs(), stopCause, at);
        };
    }

    /**
     * The one authorisation check on this path, and it runs in one direction only.
     *
     * <p>Resolve forwards — {@code command_id} to {@code pot_id} to the pot's
     * gateway — and compare with the topic. The reverse, deriving a pot from the
     * {@code (gateway, node)} pair the message carries, is not available: nothing
     * makes {@code node_id} unique across devices, so it would be a guess.
     *
     * <p>Without this check, gateway A can complete gateway B's commands. That is
     * not merely wrong bookkeeping: marking a command COMPLETED with a small
     * {@code actual_ml} lowers B's consumed volume for the day, which buys extra
     * doses out of B's budget. Budget theft, and the pot floods.
     */
    private boolean ownedByGateway(String gatewayId, DeviceCommand command) {
        Optional<CommandTarget> target = targetResolver.resolve(command.potId());
        if (target.isEmpty()) {
            LOGGER.warn(
                    "dropping ack for a command whose gateway cannot be resolved "
                            + "command_id={} pot_id={} gateway_id={}",
                    command.commandId(), command.potId(), gatewayId);
            return false;
        }
        if (!target.get().gatewayId().equals(gatewayId)) {
            LOGGER.error(
                    "rejecting ack from the wrong gateway command_id={} pot_id={} "
                            + "claimed_by={} belongs_to={}",
                    command.commandId(), command.potId(), gatewayId, target.get().gatewayId());
            return false;
        }
        return true;
    }

    /**
     * The timestamp to store, bounded by physics rather than trusted outright.
     *
     * <p>The gateway's clock is NTP-synced in theory and the value lands in
     * {@code completed_at}, which gate 4 reads as "when this pot was last
     * watered". A clock skewed into the past there would retire the six-hour
     * cooldown early and let a second dose through; skewed into the future it
     * would block the pot for as long as the skew lasts. Neither is a failure the
     * edge should be able to cause, so the report is clamped into the only window
     * it can honestly occupy: at or after the command was issued, at or before
     * now.
     */
    private Instant effectiveAt(CommandAck ack, DeviceCommand command) {
        Instant now = clock.instant();
        Instant reported = ack.at();
        if (reported == null) {
            return now;
        }
        if (reported.isBefore(command.issuedAt())) {
            LOGGER.warn(
                    "ack timestamp precedes the command, clamping command_id={} reported_at={} "
                            + "issued_at={}",
                    command.commandId(), reported, command.issuedAt());
            return command.issuedAt();
        }
        if (reported.isAfter(now)) {
            LOGGER.warn(
                    "ack timestamp is in the future, clamping command_id={} reported_at={} now={}",
                    command.commandId(), reported, now);
            return now;
        }
        return reported;
    }

    /**
     * What to record in {@code stop_cause}, which is diagnosis and nothing else.
     *
     * <p>{@code actual.stop_cause} when the device sent one, otherwise
     * {@code reason}: a completed report carries {@code reason: "OK"} alongside
     * the interesting {@code stop_cause: "volume_reached"}, so preferring the
     * latter keeps the useful half. Truncated rather than rejected, because a
     * vocabulary this system spells three different ways will eventually produce
     * a value longer than the column, and losing the tail of a diagnostic string
     * must never lose the state transition it came with.
     */
    private String stopCause(CommandAck ack, String commandId) {
        String raw = ack.stopCause() != null && !ack.stopCause().isBlank()
                ? ack.stopCause()
                : ack.reason();
        if (raw == null || raw.isBlank()) {
            return null;
        }
        String trimmed = raw.trim();
        if (trimmed.length() <= STOP_CAUSE_MAX) {
            return trimmed;
        }
        LOGGER.warn(
                "truncating stop cause to {} characters command_id={} value={}",
                STOP_CAUSE_MAX, commandId, trimmed);
        return trimmed.substring(0, STOP_CAUSE_MAX);
    }

    /** What became of one report. All three outcomes are final for that message. */
    public enum AckResult {
        /** The transition was applied; exactly one row changed. */
        APPLIED,
        /** Well-formed but not allowed from the current state: a duplicate or a replay. */
        IGNORED,
        /** Unusable — unknown phase, unknown command, or the wrong gateway. */
        DROPPED
    }
}
