package com.terrabyte.backend.rule;

import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Configuration;

/**
 * Registers the rule thresholds, mirroring {@code IrrigationConfig}.
 *
 * <p>Its own class rather than an annotation on the engine, for the same reason
 * the irrigation limits have one: these numbers decide when a pump runs, and one
 * obvious place where they enter the context makes them easier to audit.
 */
@Configuration
@EnableConfigurationProperties(RuleProperties.class)
public class RuleConfig {
}
