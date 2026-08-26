package com.terrabyte.backend.rule;

import java.time.Duration;
import java.time.LocalTime;
import java.time.ZoneId;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * The thresholds the rule engine compares readings against.
 *
 * <p>In code and configuration rather than in a table, which is the open
 * question §8-3 of {@code docs/todolist.md} left unanswered. A table would let a
 * user tune the rules at runtime, at the cost of a migration, an API, its
 * authorisation, and a cache to invalidate — none of which anything asks for
 * yet. This mirrors {@code IrrigationProperties}, so both sets of numbers are
 * audited the same way and neither has a second home.
 *
 * <p>Crop-specific numbers are deliberately absent. The light band comes from
 * {@code crop_score_profile}, which already holds a per-crop PPFD optimum for
 * the suitability score; duplicating it here would create two answers to one
 * question.
 */
@ConfigurationProperties(prefix = "app.rule")
public record RuleProperties(
        Duration interval,
        Duration initialDelay,
        /**
         * Soil moisture below which the engine asks the Governor for water.
         *
         * <p>Well above the edge's autonomous 15 %: this path has a Governor
         * behind it that will refuse on interval and budget, so it can afford to
         * ask early. The emergency rule has nobody checking and cannot.
         */
        Double soilDryGatePct,
        /** Start of the window in which the lamp may be on, in the clock's zone. */
        LocalTime photoperiodStart,
        /** End of that window. Outside it the lamp is commanded off. */
        LocalTime photoperiodEnd,
        /**
         * Widens the crop's PPFD band before it is used as a lamp threshold.
         *
         * <p>1.0 uses the crop's optimum as-is. The band is what gives the rule
         * hysteresis: a single threshold would toggle the lamp on every pass
         * while a reading hovered on it.
         */
        Double lightBandMargin,
        /**
         * The zone the photoperiod window is written in.
         *
         * <p>Needed because the shared {@code Clock} bean is
         * {@code Clock.systemUTC()}, which is right for durations and
         * timestamps and wrong for a wall-clock window: read in UTC, a
         * 06:00-22:00 window in Seoul becomes 15:00-07:00 local, so the lamp
         * runs all night and sits dark all day. A photoperiod is a statement
         * about where the plant is, not about where the server keeps time.
         *
         * <p>Defaults to the JVM zone, which the deployment sets through
         * {@code TZ} in compose.
         */
        ZoneId photoperiodZone) {

    public RuleProperties {
        interval = interval == null ? Duration.ofMinutes(1) : interval;
        initialDelay = initialDelay == null ? Duration.ofSeconds(20) : initialDelay;
        soilDryGatePct = soilDryGatePct == null ? 35.0 : soilDryGatePct;
        photoperiodStart = photoperiodStart == null ? LocalTime.of(6, 0) : photoperiodStart;
        photoperiodEnd = photoperiodEnd == null ? LocalTime.of(22, 0) : photoperiodEnd;
        lightBandMargin = lightBandMargin == null ? 1.0 : lightBandMargin;
        photoperiodZone = photoperiodZone == null ? ZoneId.systemDefault() : photoperiodZone;

        if (soilDryGatePct <= 0.0 || soilDryGatePct > 100.0) {
            throw new IllegalArgumentException("app.rule.soil-dry-gate-pct must be within (0, 100]");
        }
        if (lightBandMargin <= 0.0) {
            throw new IllegalArgumentException("app.rule.light-band-margin must be positive");
        }
        if (!photoperiodStart.isBefore(photoperiodEnd)) {
            // A window that wraps midnight is a different rule, not a variation
            // of this one, and silently accepting it would mean a lamp that is
            // never on rather than one that is on all night.
            throw new IllegalArgumentException(
                    "app.rule.photoperiod-start must be before photoperiod-end");
        }
    }

    /** Whether the lamp is allowed on at this local time. */
    public boolean isDaytime(LocalTime localTime) {
        return !localTime.isBefore(photoperiodStart) && localTime.isBefore(photoperiodEnd);
    }
}
