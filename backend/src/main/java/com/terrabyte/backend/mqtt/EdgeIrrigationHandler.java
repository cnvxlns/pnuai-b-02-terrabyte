package com.terrabyte.backend.mqtt;

import java.time.Clock;
import java.time.Instant;
import java.util.Optional;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.terrabyte.backend.device.Device;
import com.terrabyte.backend.device.DeviceRepository;
import com.terrabyte.backend.irrigation.CommandOrigin;
import com.terrabyte.backend.irrigation.CommandState;
import com.terrabyte.backend.irrigation.DeviceCommand;
import com.terrabyte.backend.irrigation.DeviceCommandRepository;
import com.terrabyte.backend.pot.Pot;
import com.terrabyte.backend.pot.PotRepository;

import org.eclipse.paho.client.mqttv3.MqttMessage;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

/**
 * Receives water a gateway delivered while this server was unreachable.
 *
 * <p>Recorded as {@code device_command(origin=EDGE_FALLBACK, state=COMPLETED)}
 * rather than as a new kind of row, because
 * {@code DeviceCommandRepository#consumedMlSince} already sums
 * {@code COALESCE(actual_ml, granted_ml)} across every non-rejected pump command
 * in the window. Reusing that shape is what makes the Governor count autonomous
 * irrigation without a line of budget code knowing the edge exists.
 *
 * <p>The failure this path prevents is not a missing record. It is the server
 * authorising a second dose on top of one already in the soil — which is why the
 * gateway refuses to leave RESYNC until every one of these has landed.
 */
@Component
public class EdgeIrrigationHandler implements MqttUplinkHandler {

    private static final Logger LOGGER = LoggerFactory.getLogger(EdgeIrrigationHandler.class);

    public static final String SUFFIX = "irrigation";

    /**
     * There was no command, so there was no authorised runtime either. Zero says
     * that honestly; the column is non-null and any other number would be a
     * fabricated authorisation.
     */
    private static final int NO_AUTHORISED_RUNTIME_MS = 0;

    private final DeviceCommandRepository commandRepository;
    private final DeviceRepository deviceRepository;
    private final PotRepository potRepository;
    private final ObjectMapper objectMapper;
    private final Clock clock;

    public EdgeIrrigationHandler(
            DeviceCommandRepository commandRepository,
            DeviceRepository deviceRepository,
            PotRepository potRepository,
            ObjectMapper objectMapper,
            Clock clock) {
        this.commandRepository = commandRepository;
        this.deviceRepository = deviceRepository;
        this.potRepository = potRepository;
        this.objectMapper = objectMapper;
        this.clock = clock;
    }

    @Override
    public String topicSuffix() {
        return SUFFIX;
    }

    @Override
    @Transactional
    public boolean handle(String gatewayId, MqttMessage message) {
        EdgeIrrigationMessage record;
        try {
            record = objectMapper.readValue(message.getPayload(), EdgeIrrigationMessage.class);
        } catch (Exception e) {
            // Redelivery cannot make bytes parseable. Acknowledged so the broker
            // stops offering it, and logged loudly because losing one of these
            // means the budget is short by whatever it carried.
            LOGGER.error("dropping unparsable edge irrigation gateway_id={}", gatewayId, e);
            return true;
        }

        if (record.recordId() == null || record.recordId().isBlank()) {
            LOGGER.error("dropping edge irrigation with no record id gateway_id={}", gatewayId);
            return true;
        }
        if (record.volumeMl() == null || record.volumeMl() <= 0.0) {
            // Zero millilitres is a bookkeeping event, not an irrigation. The
            // gateway should not send one; recording it would occupy a command
            // id and add a row that changes no budget.
            LOGGER.warn(
                    "ignoring edge irrigation that delivered nothing gateway_id={} record_id={}",
                    gatewayId, record.recordId());
            return true;
        }

        Optional<Pot> pot = resolvePot(gatewayId, record.nodeId());
        if (pot.isEmpty()) {
            // The topic's gateway segment is the only authenticated identity
            // here — the broker ACL confines a gateway to its own namespace — so
            // resolution runs forward from it. Trusting the node id on its own
            // would let one gateway spend another pot's budget.
            LOGGER.error(
                    "dropping edge irrigation for an unresolvable pot gateway_id={} "
                            + "node_id={} record_id={}",
                    gatewayId, record.nodeId(), record.recordId());
            return true;
        }

        Instant dispensedAt = parseDispensedAt(record, gatewayId);
        int actualMl = (int) Math.round(record.volumeMl());
        boolean stored = commandRepository.saveIfAbsent(new DeviceCommand(
                record.recordId(),
                pot.get().id(),
                // The record id doubles as the correlation id. Nothing on this
                // server started the chain, but there is still a chain: the same
                // value keys the gateway's irrigation_history row, the control
                // queue message and this command, which is exactly what a
                // correlation id is for. The column is NOT NULL, and inventing a
                // fresh uuid here would break that trail for no gain.
                record.recordId(),
                DeviceCommand.ACTUATOR_PUMP,
                DeviceCommand.ACTION_DOSE,
                // Granted is what the server authorised, and it authorised
                // nothing. actual_ml carries the whole truth, and
                // consumedMlSince prefers it.
                null,
                NO_AUTHORISED_RUNTIME_MS,
                CommandState.COMPLETED,
                dispensedAt,
                dispensedAt,
                dispensedAt,
                dispensedAt,
                actualMl,
                null,
                "edge_autonomous",
                CommandOrigin.EDGE_FALLBACK));

        if (stored) {
            LOGGER.warn(
                    "recorded autonomous irrigation gateway_id={} pot_id={} record_id={} ml={}",
                    gatewayId, pot.get().id(), record.recordId(), actualMl);
        } else {
            // The control queue retries until the publish is acknowledged and
            // the gateway's hop is QoS 1, so duplicates are ordinary traffic.
            // The record id is the primary key, which is what makes the second
            // one free.
            LOGGER.info(
                    "edge irrigation already recorded gateway_id={} record_id={}",
                    gatewayId, record.recordId());
        }
        return true;
    }

    private Optional<Pot> resolvePot(String gatewayId, String nodeId) {
        if (nodeId == null || nodeId.isBlank()) {
            return Optional.empty();
        }
        return deviceRepository.findByHardwareId(gatewayId)
                .map(Device::id)
                .flatMap(deviceId -> potRepository.findByDeviceAndNode(deviceId, nodeId));
    }

    /**
     * The gateway's timestamp, or ours if it is unusable.
     *
     * <p>Falling back rather than dropping: the volume is the fact that matters
     * to the budget, and a slightly wrong time costs at most a mis-sized
     * twenty-four hour window. Refusing the record would cost the whole dose.
     */
    private Instant parseDispensedAt(EdgeIrrigationMessage record, String gatewayId) {
        Instant now = clock.instant();
        if (record.dispensedAt() == null || record.dispensedAt().isBlank()) {
            return now;
        }
        try {
            Instant parsed = Instant.parse(record.dispensedAt());
            // A gateway whose clock runs ahead would otherwise push this row out
            // of every budget window that should contain it.
            return parsed.isAfter(now) ? now : parsed;
        } catch (Exception e) {
            LOGGER.warn(
                    "unparsable edge irrigation timestamp gateway_id={} record_id={} value={}",
                    gatewayId, record.recordId(), record.dispensedAt());
            return now;
        }
    }

    /** The {@code up/irrigation} payload. See the edge's EdgeIrrigationRecord. */
    @JsonIgnoreProperties(ignoreUnknown = true)
    record EdgeIrrigationMessage(
            @JsonProperty("record_id") String recordId,
            @JsonProperty("node_id") String nodeId,
            @JsonProperty("volume_ml") Double volumeMl,
            @JsonProperty("dispensed_at") String dispensedAt) {
    }
}
