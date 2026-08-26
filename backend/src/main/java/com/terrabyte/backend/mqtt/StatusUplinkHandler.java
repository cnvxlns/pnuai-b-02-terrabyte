package com.terrabyte.backend.mqtt;

import java.nio.charset.StandardCharsets;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.terrabyte.backend.measurement.MeasurementService;
import org.eclipse.paho.client.mqttv3.MqttMessage;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

/** Records a gateway going online or offline from {@code up/status}. */
@Component
public class StatusUplinkHandler implements MqttUplinkHandler {

    private static final Logger LOGGER = LoggerFactory.getLogger(StatusUplinkHandler.class);

    private final MeasurementService measurementService;
    private final GatewayLinkStateRegistry linkStates;
    private final ObjectMapper objectMapper;

    public StatusUplinkHandler(
            MeasurementService measurementService,
            GatewayLinkStateRegistry linkStates,
            ObjectMapper objectMapper) {
        this.measurementService = measurementService;
        this.linkStates = linkStates;
        this.objectMapper = objectMapper;
    }

    @Override
    public String topicSuffix() {
        return "status";
    }

    @Override
    public boolean handle(String gatewayId, MqttMessage message) {
        try {
            String payload = new String(message.getPayload(), StandardCharsets.UTF_8);
            StatusPayload status = objectMapper.readValue(payload, StatusPayload.class);
            measurementService.updateGatewayPresence(gatewayId, status.online());
            if (status.online()) {
                linkStates.record(gatewayId, status.state());
            } else {
                // The Last Will carries no state, and it is the message that
                // arrives when a gateway vanishes mid-RESYNC. Keeping the old
                // value would lock it out of commands for the life of the
                // process, long after it recovered and drained its queue.
                linkStates.forget(gatewayId);
            }
            return true;
        } catch (Exception e) {
            // Presence is republished retained on every reconnect, so a lost
            // status message self-heals. No point holding up the session for it.
            LOGGER.error("failed to process status gateway_id={}", gatewayId, e);
            return true;
        }
    }

    /**
     * {@code {"online": true|false, "state": "CLOUD_ONLINE"}} — retained, and its
     * offline half is also this gateway's MQTT Last Will.
     *
     * <p>{@code state} is absent from a gateway running a build that predates
     * edge autonomy, and the null that produces is meaningful: no claim made.
     * Unknown properties are ignored so a gateway ahead of this server can add
     * fields without every status message becoming a parse error.
     */
    @JsonIgnoreProperties(ignoreUnknown = true)
    record StatusPayload(boolean online, String state) {
    }
}
