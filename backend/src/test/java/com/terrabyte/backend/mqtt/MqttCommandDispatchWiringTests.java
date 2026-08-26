package com.terrabyte.backend.mqtt;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;

import java.time.Clock;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.terrabyte.backend.device.DeviceRepository;
import com.terrabyte.backend.irrigation.CommandDispatcher;
import com.terrabyte.backend.irrigation.CommandTargetResolver;
import com.terrabyte.backend.irrigation.IrrigationConfig;
import com.terrabyte.backend.irrigation.LoggingCommandDispatcher;

import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * Which {@link CommandDispatcher} wins when the real transport is switched on.
 *
 * <p>A context slice rather than a {@code @SpringBootTest}, because turning the
 * flag on in a full context would start {@link MqttUplinkRouter}, which connects
 * to a broker that no test has.
 *
 * <p>The interesting case is the ambiguous one. {@code IrrigationConfig} is
 * registered <em>first</em> here on purpose: that is the order the real component
 * scan uses — {@code irrigation} sorts before {@code mqtt} — and in that order its
 * {@code @ConditionalOnMissingBean} cannot see a bean that has not been parsed
 * yet, so it registers the fallback and both dispatchers exist. That is a
 * NoUniqueBeanDefinitionException waiting to happen, and {@code @Primary} on the
 * MQTT bean is what resolves it independently of parse order.
 */
class MqttCommandDispatchWiringTests {

    private final ApplicationContextRunner runner = new ApplicationContextRunner()
            .withUserConfiguration(
                    IrrigationConfig.class, MqttConfig.class, StubsConfig.class,
                    // Component-scanned in the running application; this slice
                    // builds its context by hand, so it has to be named.
                    GatewayLinkStateRegistry.class)
            .withPropertyValues(
                    "app.mqtt.enabled=true",
                    "app.mqtt.url=tcp://localhost:1883",
                    "app.mqtt.client-id=terrabyte-backend-test",
                    "app.mqtt.username=u",
                    "app.mqtt.password=p");

    @Test
    void theMqttDispatcherTakesPrecedenceWhenCommandDispatchIsOn() {
        runner.withPropertyValues("app.mqtt.command-dispatch.enabled=true").run(context -> {
            assertThat(context).hasNotFailed();
            // Two candidates really are registered — this is the assertion that
            // shows @Primary is doing work rather than decorating a case that
            // could not arise — and resolution is still unambiguous.
            assertThat(context.getBeanNamesForType(CommandDispatcher.class)).hasSize(2);
            assertThat(context.getBean(CommandDispatcher.class))
                    .isInstanceOf(MqttCommandDispatcher.class);
        });
    }

    @Test
    void theFallbackStaysInChargeWhileCommandDispatchIsOff() {
        // Off is the default, and merging in this state is the point: the code is
        // reviewable and reachable without any environment moving water.
        runner.run(context -> {
            assertThat(context).hasNotFailed();
            assertThat(context.getBean(CommandDispatcher.class))
                    .isInstanceOf(LoggingCommandDispatcher.class);
            assertThat(context.getBeanNamesForType(CommandDispatcher.class)).hasSize(1);
        });
    }

    @Test
    void aBrokerlessContextRegistersNoDispatcherOfItsOwn() {
        // Asking for command dispatch without the transport is a configuration
        // mistake worth catching: with app.mqtt.enabled false there is no
        // MqttClient, so the whole MqttConfig class is skipped and the honest
        // logging fallback remains.
        new ApplicationContextRunner()
                .withUserConfiguration(
                    IrrigationConfig.class, MqttConfig.class, StubsConfig.class,
                    // Component-scanned in the running application; this slice
                    // builds its context by hand, so it has to be named.
                    GatewayLinkStateRegistry.class)
                .withPropertyValues(
                        "app.mqtt.enabled=false",
                        "app.mqtt.command-dispatch.enabled=true")
                .run(context -> {
                    assertThat(context).hasNotFailed();
                    assertThat(context.getBean(CommandDispatcher.class))
                            .isInstanceOf(LoggingCommandDispatcher.class);
                });
    }

    /** The collaborators MqttConfig's bean methods need, and nothing more. */
    @Configuration(proxyBeanMethods = false)
    static class StubsConfig {

        // Not for the dispatcher: the heartbeat publisher in the same
        // configuration class needs it, and an unsatisfiable dependency there
        // fails the whole context rather than just its own bean.
        @Bean
        DeviceRepository deviceRepository() {
            return mock(DeviceRepository.class);
        }

        @Bean
        ObjectMapper objectMapper() {
            return new ObjectMapper();
        }

        @Bean
        CommandTargetResolver commandTargetResolver() {
            return mock(CommandTargetResolver.class);
        }

        @Bean
        Clock clock() {
            return Clock.systemUTC();
        }
    }
}
