package com.terrabyte.backend.mqtt;

import static org.assertj.core.api.Assertions.assertThat;

import java.nio.charset.StandardCharsets;

import org.eclipse.paho.client.mqttv3.MqttMessage;
import org.junit.jupiter.api.Test;

/**
 * What the gateway says about itself in {@code up/status}, and what it costs it.
 *
 * <p>RESYNC means the gateway is holding irrigation records this server has not
 * seen. Publishing a command to it then is how a pot gets watered twice: the
 * command was authorised against a budget that does not yet include water
 * already in the soil.
 */
class GatewayLinkStateTests {

    private static final String GATEWAY = "orangepi-pro-01";

    private final GatewayLinkStateRegistry registry = new GatewayLinkStateRegistry();

    @Test
    void anUnknownGatewayIsAssumedReady() {
        // Optimistic on purpose. Every gateway is unknown until its first status
        // arrives, and refusing to command one that has simply not spoken yet
        // would break the ordinary path to protect against the rare one.
        assertThat(registry.acceptsCommands(GATEWAY)).isTrue();
    }

    @Test
    void aResyncingGatewayIsNotCommanded() {
        registry.record(GATEWAY, "RESYNC");

        assertThat(registry.acceptsCommands(GATEWAY)).isFalse();
    }

    @Test
    void aHeldGatewayIsNotCommanded() {
        registry.record(GATEWAY, "SAFE_HOLD");

        assertThat(registry.acceptsCommands(GATEWAY)).isFalse();
    }

    @Test
    void anOnlineGatewayIsCommandedAgain() {
        registry.record(GATEWAY, "RESYNC");

        registry.record(GATEWAY, "CLOUD_ONLINE");

        assertThat(registry.acceptsCommands(GATEWAY)).isTrue();
    }

    @Test
    void anAutonomousGatewayStillAcceptsCommands() {
        registry.record(GATEWAY, "EDGE_AUTONOMOUS");

        // It believes we are gone; if it is wrong, the command is exactly what
        // proves it. Only an unpaid record debt justifies refusing.
        assertThat(registry.acceptsCommands(GATEWAY)).isTrue();
    }

    @Test
    void aGatewayThatWentOfflineForgetsItsState() {
        registry.record(GATEWAY, "RESYNC");

        registry.forget(GATEWAY);

        // The Last Will carries no state, and a stale RESYNC would lock the
        // gateway out of commands for as long as the process lives.
        assertThat(registry.acceptsCommands(GATEWAY)).isTrue();
    }

    @Test
    void aStatusWithNoStateLeavesTheGatewayCommandable() {
        StatusUplinkHandler.StatusPayload payload =
                new StatusUplinkHandler.StatusPayload(true, null);

        assertThat(payload.state()).isNull();
    }

    @Test
    void theStatusPayloadCarriesTheState() throws Exception {
        var mapper = new com.fasterxml.jackson.databind.ObjectMapper();
        MqttMessage message = new MqttMessage(
                "{\"online\":true,\"state\":\"RESYNC\"}".getBytes(StandardCharsets.UTF_8));

        StatusUplinkHandler.StatusPayload payload = mapper.readValue(
                message.getPayload(), StatusUplinkHandler.StatusPayload.class);

        assertThat(payload.online()).isTrue();
        assertThat(payload.state()).isEqualTo("RESYNC");
    }
}
