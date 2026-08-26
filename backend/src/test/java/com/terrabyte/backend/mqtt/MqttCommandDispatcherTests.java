package com.terrabyte.backend.mqtt;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

import java.nio.charset.StandardCharsets;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.Optional;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.terrabyte.backend.irrigation.ClampReason;
import com.terrabyte.backend.irrigation.CommandOrigin;
import com.terrabyte.backend.irrigation.CommandSource;
import com.terrabyte.backend.irrigation.CommandTargetResolver;
import com.terrabyte.backend.irrigation.CommandTargetResolver.CommandTarget;
import com.terrabyte.backend.irrigation.DeviceCommand;
import com.terrabyte.backend.irrigation.IrrigationGrant;

import org.eclipse.paho.client.mqttv3.MqttClient;
import org.eclipse.paho.client.mqttv3.MqttDeliveryToken;
import org.eclipse.paho.client.mqttv3.MqttException;
import org.eclipse.paho.client.mqttv3.MqttMessage;
import org.eclipse.paho.client.mqttv3.MqttTopic;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.http.converter.json.Jackson2ObjectMapperBuilder;

/** Publishing behaviour, without a broker. */
class MqttCommandDispatcherTests {

    private static final Instant NOW = Instant.parse("2026-08-04T10:00:30Z");
    private static final long POT_ID = 42L;

    private static final MqttProperties PROPERTIES = new MqttProperties(
            true, "tcp://localhost:1883", "terrabyte-backend", "u", "p", "tb/v2",
            Duration.ofSeconds(10), Duration.ofSeconds(30), false, Duration.ofSeconds(5));

    private final ObjectMapper objectMapper = Jackson2ObjectMapperBuilder.json().build();

    private GatewayLinkStateRegistry linkStates;
    private MqttClient mqttClient;
    private MqttTopic mqttTopic;
    private CommandTargetResolver targetResolver;
    private MqttCommandDispatcher dispatcher;

    @BeforeEach
    void setUp() throws Exception {
        mqttClient = mock(MqttClient.class);
        mqttTopic = mock(MqttTopic.class);
        targetResolver = mock(CommandTargetResolver.class);

        when(mqttClient.getTopic(any())).thenReturn(mqttTopic);
        MqttDeliveryToken token = mock(MqttDeliveryToken.class);
        when(mqttTopic.publish(any(MqttMessage.class))).thenReturn(token);
        when(targetResolver.resolve(POT_ID))
                .thenReturn(Optional.of(
                        new CommandTarget(POT_ID, "orangepi-pro-01", "terrabyte-node-01")));

        linkStates = new GatewayLinkStateRegistry();
        dispatcher = new MqttCommandDispatcher(
                mqttClient, PROPERTIES, objectMapper, targetResolver, linkStates,
                Duration.ofSeconds(5), Clock.fixed(NOW, ZoneOffset.UTC));
    }

    @Test
    void aResyncingGatewayIsNotSentCommands() {
        linkStates.record("orangepi-pro-01", "RESYNC");

        assertThat(dispatcher.dispatch(grant(NOW.plusSeconds(90)))).isFalse();

        // The gateway is still holding irrigation records this server has not
        // seen, so the budget this command was authorised against is stale.
        // Publishing anyway is how the pot gets watered twice.
        verifyNoInteractions(mqttClient);
    }

    @Test
    void aGatewayThatFinishedResyncingIsSentCommandsAgain() {
        linkStates.record("orangepi-pro-01", "RESYNC");
        linkStates.record("orangepi-pro-01", "CLOUD_ONLINE");

        assertThat(dispatcher.dispatch(grant(NOW.plusSeconds(90)))).isTrue();
    }

    @Test
    void anAutonomousGatewayIsStillSentCommands() {
        linkStates.record("orangepi-pro-01", "EDGE_AUTONOMOUS");

        // It believes we are gone. A command arriving is what corrects it, and
        // its own envelope is far narrower than anything we would authorise.
        assertThat(dispatcher.dispatch(grant(NOW.plusSeconds(90)))).isTrue();
    }

    @Test
    void publishesToTheGatewaysOwnDownlinkTopic() {
        assertThat(dispatcher.dispatch(grant(NOW.plusSeconds(90)))).isTrue();

        verify(mqttClient).getTopic("tb/v2/orangepi-pro-01/dn/command");
    }

    @Test
    void publishesAtLeastOnceAndNeverRetained() throws Exception {
        dispatcher.dispatch(grant(NOW.plusSeconds(90)));

        ArgumentCaptor<MqttMessage> published = ArgumentCaptor.forClass(MqttMessage.class);
        verify(mqttTopic).publish(published.capture());

        assertThat(published.getValue().getQos()).isEqualTo(1);
        // A retained command replays on every gateway reconnect, TTL and all.
        // This is the assertion that stops that from ever being introduced.
        assertThat(published.getValue().isRetained()).isFalse();
    }

    @Test
    void publishesThePayloadAddressedToTheResolvedNode() throws Exception {
        dispatcher.dispatch(grant(NOW.plusSeconds(90)));

        ArgumentCaptor<MqttMessage> published = ArgumentCaptor.forClass(MqttMessage.class);
        verify(mqttTopic).publish(published.capture());
        JsonNode json = objectMapper.readTree(
                new String(published.getValue().getPayload(), StandardCharsets.UTF_8));

        assertThat(json.get("gateway_id").asText()).isEqualTo("orangepi-pro-01");
        assertThat(json.get("node_id").asText()).isEqualTo("terrabyte-node-01");
        assertThat(json.get("pot_id").asLong()).isEqualTo(POT_ID);
        assertThat(json.get("params").get("volume_ml").asInt()).isEqualTo(120);
    }

    @Test
    void refusesToPublishACommandThatHasAlreadyExpired() {
        // The Governor stamps expires_at and stops. Before this check, nothing
        // owned the sentence "if it has expired, do not send it".
        assertThat(dispatcher.dispatch(grant(NOW.minusSeconds(1)))).isFalse();

        verifyNoInteractions(mqttClient);
    }

    @Test
    void treatsExpiryAsInclusiveOfTheDeadline() {
        assertThat(dispatcher.dispatch(grant(NOW))).isFalse();

        verifyNoInteractions(mqttClient);
    }

    @Test
    void refusesWhenThePotHasNoBoundNodeToAddress() {
        when(targetResolver.resolve(POT_ID))
                .thenReturn(Optional.of(new CommandTarget(POT_ID, "orangepi-pro-01", null)));

        assertThat(dispatcher.dispatch(grant(NOW.plusSeconds(90)))).isFalse();

        verifyNoInteractions(mqttClient);
    }

    @Test
    void refusesWhenTheGatewayCannotBeResolved() {
        when(targetResolver.resolve(anyLong())).thenReturn(Optional.empty());

        assertThat(dispatcher.dispatch(grant(NOW.plusSeconds(90)))).isFalse();

        verifyNoInteractions(mqttClient);
    }

    @Test
    void reportsFailureRatherThanThrowingWhenTheBrokerIsUnreachable() throws Exception {
        when(mqttTopic.publish(any(MqttMessage.class)))
                .thenThrow(new MqttException(MqttException.REASON_CODE_CLIENT_NOT_CONNECTED));

        // An unreachable broker is the ordinary case, not an exceptional one:
        // the command stays ISSUED, the caller is told dispatched=false, and the
        // expiry sweep retires the row.
        assertThat(dispatcher.dispatch(grant(NOW.plusSeconds(90)))).isFalse();
    }

    @Test
    void doesNotWaitForeverOnADeliveryThatNeverCompletes() throws Exception {
        MqttDeliveryToken token = mock(MqttDeliveryToken.class);
        when(mqttTopic.publish(any(MqttMessage.class))).thenReturn(token);

        dispatcher.dispatch(grant(NOW.plusSeconds(90)));

        // Bounded, because this runs on the thread serving a user's tap and
        // Paho's own default is to wait indefinitely.
        verify(token).waitForCompletion(5_000L);
        verify(token, never()).waitForCompletion();
    }

    @Test
    void publishesALightCommandThroughTheSameDownlink() throws Exception {
        DeviceCommand command = DeviceCommand.issuedLight(
                "01J8LIGHT",
                POT_ID,
                "manual-light-1",
                true,
                NOW.minusSeconds(1),
                NOW.plusSeconds(90));
        CommandTarget target = new CommandTarget(
                POT_ID, "orangepi-pro-01", "terrabyte-node-01");

        assertThat(dispatcher.dispatchLight(command, target)).isTrue();

        ArgumentCaptor<MqttMessage> published = ArgumentCaptor.forClass(MqttMessage.class);
        verify(mqttTopic).publish(published.capture());
        JsonNode json = objectMapper.readTree(published.getValue().getPayload());
        assertThat(json.get("actuator").asText()).isEqualTo("light");
        assertThat(json.get("params").get("on").asBoolean()).isTrue();
    }

    private static IrrigationGrant grant(Instant expiresAt) {
        return new IrrigationGrant(
                "01J8F3QK2M7X9ZB4CDEFGH", POT_ID, 120, 18_000,
                NOW.minusSeconds(30), expiresAt, "corr-1",
                CommandSource.RULE_AI, CommandOrigin.CLOUD,
                300, ClampReason.DAILY_BUDGET, "irrigation_rf_v3");
    }
}
