package com.terrabyte.backend.mqtt;

import java.time.Clock;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.terrabyte.backend.device.DeviceRepository;
import com.terrabyte.backend.irrigation.CommandDispatcher;
import com.terrabyte.backend.irrigation.CommandTargetResolver;
import org.eclipse.paho.client.mqttv3.MqttClient;
import org.eclipse.paho.client.mqttv3.MqttConnectOptions;
import org.eclipse.paho.client.mqttv3.MqttException;
import org.eclipse.paho.client.mqttv3.persist.MemoryPersistence;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Primary;

/**
 * Wires the Paho client used for the MQTT operational transport.
 *
 * <p>Everything here is gated on {@code app.mqtt.enabled} so that the test
 * suite and a plain local {@code bootRun} — neither of which has a broker —
 * keep booting exactly as before this transport was added.
 */
@Configuration(proxyBeanMethods = false)
@EnableConfigurationProperties(MqttProperties.class)
@ConditionalOnProperty(prefix = "app.mqtt", name = "enabled", havingValue = "true")
public class MqttConfig {

    /**
     * {@link MemoryPersistence} is the correct choice, not a shortcut, and this
     * comment exists so nobody "fixes" it with a file store.
     *
     * <p>The backend does publish now — irrigation commands go out on
     * {@code dn/command} at QoS 1 — and a QoS 1 publish that a restart loses
     * would normally be an argument for durable client-side persistence. It is
     * the opposite here. Commands are never retained and live for two minutes,
     * so a command that was in flight across a restart is a command that must
     * <em>not</em> be redelivered afterwards: by the time the process is back the
     * moisture reading behind the decision is stale, and the expiry sweep is
     * already there to record the row as EXPIRED. Losing it is the intended
     * behaviour.
     *
     * <p>Inbound durability is a separate question and is answered on the broker
     * instead: see {@code cleanSession} below.
     */
    @Bean
    public MqttClient mqttClient(MqttProperties properties) throws MqttException {
        MqttClient client =
                new MqttClient(properties.url(), properties.clientId(), new MemoryPersistence());
        // Acknowledge a message only once it has actually been ingested, rather
        // than the moment it is handed to the callback. Without this the broker
        // considers delivery complete as soon as Paho receives the message, so
        // a sample that fails to reach InfluxDB is gone: the gateway's outbox
        // already dropped it when the *broker* acked the publish, and the
        // backend never gets it again.
        client.setManualAcks(true);
        return client;
    }

    @Bean
    public MqttConnectOptions mqttConnectOptions(MqttProperties properties) {
        MqttConnectOptions options = new MqttConnectOptions();
        options.setServerURIs(new String[] {properties.url()});
        options.setUserName(properties.username());
        options.setPassword(properties.password().toCharArray());
        // A persistent session (cleanSession=false) with a stable client id is
        // what makes the broker queue QoS 1 uplinks while the backend is
        // restarting or down. With a clean session the broker discards them and
        // the samples are lost, because the gateway's outbox entry was already
        // released by the broker's PUBACK long before the backend saw it.
        options.setCleanSession(properties.cleanSession());
        options.setConnectionTimeout((int) properties.connectionTimeout().toSeconds());
        options.setKeepAliveInterval((int) properties.keepAlive().toSeconds());
        // Paho reconnects the transport automatically, but it does not restore
        // subscriptions when cleanSession is true — the subscriber re-subscribes
        // itself from the connectComplete callback to cover that gap.
        options.setAutomaticReconnect(true);
        return options;
    }

    /**
     * The real downlink, off unless {@code app.mqtt.command-dispatch.enabled}.
     *
     * <p>Two annotations here are load-bearing and both record a specific trap.
     *
     * <p>The condition is on a {@code @Bean} method rather than on a
     * {@code @Component}. On a component it is evaluated during scanning and
     * simply never registers the bean — the failure {@code IrrigationConfig}
     * documents from the other side, which surfaced as fifty-one context load
     * failures and no dispatcher at all.
     *
     * <p>{@code @Primary} is what makes this win over the fallback, and it is not
     * interchangeable with letting {@code IrrigationConfig}'s
     * {@code @ConditionalOnMissingBean} yield. That annotation only sees beans
     * registered before its own configuration class is parsed, and the order two
     * user {@code @Configuration} classes are parsed in is not part of Spring's
     * contract. With {@code irrigation} sorting ahead of {@code mqtt} the fallback
     * is registered first, so the condition matches, and both dispatchers exist —
     * an ambiguous injection point and a context that will not start. Declaring
     * precedence explicitly means the outcome no longer depends on package names.
     *
     * <p>Off by default so this can be merged dark. Turning it on is what starts
     * moving water, and it needs the end-to-end scenarios first.
     */
    /**
     * The liveness heartbeat. On by default, unlike the command dispatcher: it
     * moves no water, and the failure it prevents is a gateway that keeps
     * publishing into a topic nobody reads because the broker outlived the
     * application. Needs {@code app.scheduling.enabled}, which is the switch for
     * every timed task in this process.
     */
    @Bean
    @ConditionalOnProperty(
            prefix = "app.mqtt.heartbeat",
            name = "enabled",
            havingValue = "true",
            matchIfMissing = true)
    public BackendHeartbeatPublisher backendHeartbeatPublisher(
            MqttClient mqttClient,
            MqttProperties properties,
            ObjectMapper objectMapper,
            DeviceRepository deviceRepository,
            Clock clock) {
        return new BackendHeartbeatPublisher(
                mqttClient, properties, objectMapper, deviceRepository, clock);
    }

    @Bean
    @Primary
    @ConditionalOnProperty(
            prefix = "app.mqtt.command-dispatch", name = "enabled", havingValue = "true")
    public CommandDispatcher mqttCommandDispatcher(
            MqttClient mqttClient,
            MqttProperties properties,
            ObjectMapper objectMapper,
            CommandTargetResolver targetResolver,
            GatewayLinkStateRegistry linkStates,
            Clock clock) {
        return new MqttCommandDispatcher(
                mqttClient, properties, objectMapper, targetResolver, linkStates,
                properties.publishTimeout(), clock);
    }
}
