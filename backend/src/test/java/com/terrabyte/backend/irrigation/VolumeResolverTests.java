package com.terrabyte.backend.irrigation;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.Instant;
import java.util.List;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.terrabyte.backend.device.DeviceStatus;
import com.terrabyte.backend.measurement.IrrigationSuggestion;
import com.terrabyte.backend.measurement.TelemetrySample;
import com.terrabyte.backend.pot.Pot;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;

/**
 * The volume that reaches the Governor, and the reasons it is what it is.
 *
 * <p>Two behaviours here are safety properties rather than conveniences, and
 * both have a test that exists only to hold them: an out-of-range suggestion is
 * discarded rather than clamped, and an unknown pot size takes the smallest dose.
 *
 * <p>The drift warnings are asserted through a Logback appender because the
 * warning <em>is</em> the behaviour — the suggestion is used either way, so
 * nothing else observable distinguishes a mismatch from a match.
 */
class VolumeResolverTests {

    private static final long POT_ID = 42L;
    private static final String MODEL = "water-balance-v1";

    private VolumeResolver resolver;
    private ListAppender<ILoggingEvent> logs;
    private ch.qos.logback.classic.Logger logger;

    @BeforeEach
    void setUp() {
        resolver = new VolumeResolver();
        logger = (ch.qos.logback.classic.Logger) LoggerFactory.getLogger(VolumeResolver.class);
        logs = new ListAppender<>();
        logs.start();
        logger.addAppender(logs);
    }

    @AfterEach
    void tearDown() {
        logger.detachAppender(logs);
    }

    // -- the suggestion is used -------------------------------------------

    @Test
    void aSaneSuggestionIsUsedAsIs() {
        VolumeResolver.ResolvedVolume resolved =
                resolver.resolve(pot("lettuce", 3000), sampleWith(suggestion(118, "lettuce", 3000)));

        assertThat(resolved.volumeMl()).isEqualTo(118);
        assertThat(resolved.source()).isEqualTo(VolumeSource.EDGE_SUGGESTION);
        assertThat(resolved.modelVersion()).isEqualTo(MODEL);
    }

    @Test
    void bothRangeEndpointsAreAccepted() {
        assertThat(resolver.resolve(pot("lettuce", 3000), sampleWith(suggestion(1, "lettuce", 3000))))
                .isEqualTo(new VolumeResolver.ResolvedVolume(1, VolumeSource.EDGE_SUGGESTION, MODEL, 1));
        assertThat(resolver.resolve(pot("lettuce", 3000), sampleWith(suggestion(500, "lettuce", 3000))))
                .isEqualTo(
                        new VolumeResolver.ResolvedVolume(500, VolumeSource.EDGE_SUGGESTION, MODEL, 500));
    }

    @Test
    void aZeroSuggestionFallsBackRatherThanBecomingARefusal() {
        // 0 means the formula disagrees with the rule engine that already decided
        // this pot needs water. Passing it on would have the Governor file that
        // coherent answer under AI_OUT_OF_RANGE, a reason that misdescribes it.
        VolumeResolver.ResolvedVolume resolved =
                resolver.resolve(pot("lettuce", 3000), sampleWith(suggestion(0, "lettuce", 3000)));

        assertThat(resolved.source()).isEqualTo(VolumeSource.POT_SIZE_FALLBACK);
        assertThat(resolved.volumeMl()).isEqualTo(80);
        // The rejected value still reaches the audit trail.
        assertThat(resolved.edgeProposedMl()).isEqualTo(0);
    }

    @Test
    void aRejectedSuggestionIsStillRecorded() {
        // Without this the only trace of a broken edge is a log line, and a log
        // line cannot be joined against the decision it produced.
        VolumeResolver.ResolvedVolume resolved = resolver.resolve(
                pot("lettuce", 3000), sampleWith(suggestion(99_999, "lettuce", 3000)));

        assertThat(resolved.source()).isEqualTo(VolumeSource.POT_SIZE_FALLBACK);
        assertThat(resolved.edgeProposedMl()).isEqualTo(99_999);
        assertThat(resolved.modelVersion()).isEqualTo(MODEL);
    }

    // -- the fallback table ------------------------------------------------

    @Test
    void everyPotSizeBandGetsItsFixedDose() {
        assertThat(fallbackFor(1)).isEqualTo(40);
        assertThat(fallbackFor(1000)).isEqualTo(40);
        assertThat(fallbackFor(1001)).isEqualTo(80);
        assertThat(fallbackFor(3000)).isEqualTo(80);
        assertThat(fallbackFor(3001)).isEqualTo(120);
        assertThat(fallbackFor(6000)).isEqualTo(120);
        assertThat(fallbackFor(6001)).isEqualTo(160);
        assertThat(fallbackFor(50_000)).isEqualTo(160);
    }

    @Test
    void anUnknownPotSizeTakesTheSmallestDoseRatherThanAMiddleGuess() {
        // Guessing high is the mistake that floods a pot; a short dose is
        // corrected on the next cycle, standing water is not.
        assertThat(fallbackFor(null)).isEqualTo(40);
        assertThat(fallbackFor(0)).isEqualTo(40);
        assertThat(fallbackFor(-100)).isEqualTo(40);
    }

    @Test
    void aSampleWithoutASuggestionFallsBack() {
        VolumeResolver.ResolvedVolume resolved =
                resolver.resolve(pot("lettuce", 5000), sampleWith(null));

        assertThat(resolved.volumeMl()).isEqualTo(120);
        assertThat(resolved.source()).isEqualTo(VolumeSource.POT_SIZE_FALLBACK);
        assertThat(resolved.modelVersion()).isNull();
    }

    @Test
    void aPotThatHasNeverReportedFallsBack() {
        // The Governor refuses on gate 1 regardless, so this only has to be a
        // well-formed number — the refusal stays decided in exactly one place.
        VolumeResolver.ResolvedVolume resolved = resolver.resolve(pot("lettuce", 800), null);

        assertThat(resolved.volumeMl()).isEqualTo(40);
        assertThat(resolved.source()).isEqualTo(VolumeSource.POT_SIZE_FALLBACK);
    }

    // -- out of range ------------------------------------------------------

    @Test
    void anOverlargeSuggestionFallsBackAndIsNotClampedToTheCeiling() {
        VolumeResolver.ResolvedVolume resolved =
                resolver.resolve(pot("lettuce", 3000), sampleWith(suggestion(99_999, "lettuce", 3000)));

        // 80, the band's dose — not 500. Clamping would ship a plausible number
        // from a demonstrably broken edge and hide the fault.
        assertThat(resolved.volumeMl()).isEqualTo(80);
        assertThat(resolved.volumeMl()).isNotEqualTo(500);
        assertThat(resolved.source()).isEqualTo(VolumeSource.POT_SIZE_FALLBACK);
        // The rejected formula is named even though its answer was thrown away —
        // "which version misbehaved" is the question this column has to answer.
        assertThat(resolved.modelVersion()).isEqualTo(MODEL);
        assertThat(resolved.edgeProposedMl()).isEqualTo(99_999);
        assertThat(warnings()).anyMatch(message -> message.contains("outside"));
    }

    @Test
    void aNegativeSuggestionFallsBackAndIsNotRaisedToZero() {
        VolumeResolver.ResolvedVolume resolved =
                resolver.resolve(pot("lettuce", 7000), sampleWith(suggestion(-30, "lettuce", 7000)));

        assertThat(resolved.volumeMl()).isEqualTo(160);
        assertThat(resolved.source()).isEqualTo(VolumeSource.POT_SIZE_FALLBACK);
        assertThat(warnings()).isNotEmpty();
    }

    // -- drift -------------------------------------------------------------

    @Test
    void aCropMismatchWarnsAndStillUsesTheSuggestion() {
        VolumeResolver.ResolvedVolume resolved =
                resolver.resolve(pot("basil", 3000), sampleWith(suggestion(118, "lettuce", 3000)));

        assertThat(resolved.volumeMl()).isEqualTo(118);
        assertThat(resolved.source()).isEqualTo(VolumeSource.EDGE_SUGGESTION);
        assertThat(warnings()).anySatisfy(message ->
                assertThat(message).contains("lettuce").contains("basil"));
    }

    @Test
    void aSubstrateVolumeMismatchWarnsAndStillUsesTheSuggestion() {
        VolumeResolver.ResolvedVolume resolved =
                resolver.resolve(pot("lettuce", 6000), sampleWith(suggestion(118, "lettuce", 3000)));

        assertThat(resolved.volumeMl()).isEqualTo(118);
        assertThat(resolved.source()).isEqualTo(VolumeSource.EDGE_SUGGESTION);
        assertThat(warnings()).anySatisfy(message ->
                assertThat(message).contains("3000").contains("6000"));
    }

    @Test
    void matchingAssumptionsAreSilent() {
        resolver.resolve(pot("lettuce", 3000), sampleWith(suggestion(118, "lettuce", 3000)));
        assertThat(warnings()).isEmpty();
    }

    @Test
    void aPotWithNothingToCompareAgainstIsNotADrift() {
        // No crop selected and no recorded volume is silence, not disagreement.
        // Warning here would bury the mismatches that matter.
        resolver.resolve(pot(null, null), sampleWith(suggestion(118, "lettuce", 3000)));
        assertThat(warnings()).isEmpty();
    }

    // -- helpers -----------------------------------------------------------

    private int fallbackFor(Integer substrateVolumeMl) {
        return resolver.resolve(pot("lettuce", substrateVolumeMl), sampleWith(null)).volumeMl();
    }

    private List<String> warnings() {
        return logs.list.stream()
                .filter(event -> event.getLevel() == Level.WARN)
                .map(ILoggingEvent::getFormattedMessage)
                .toList();
    }

    private static IrrigationSuggestion suggestion(
            int volumeMl, String assumedCropCode, Integer assumedSubstrateVolumeMl) {
        return new IrrigationSuggestion(
                volumeMl, MODEL, assumedCropCode, assumedSubstrateVolumeMl);
    }

    private static Pot pot(String cropCode, Integer substrateVolumeMl) {
        return new Pot(
                POT_ID, 1L, "node-1", "화분 1", cropCode, Instant.parse("2026-08-01T00:00:00Z"),
                DeviceStatus.ONLINE, Instant.parse("2026-08-17T09:59:00Z"),
                Instant.parse("2026-07-01T00:00:00Z"), substrateVolumeMl);
    }

    private static TelemetrySample sampleWith(IrrigationSuggestion suggestion) {
        return new TelemetrySample(
                POT_ID, 1L, "node-1", "lettuce", "orangepi-pro-01", "evt-1",
                Instant.parse("2026-08-17T10:00:00Z"), 1L, 22.0, 0L, 24.0, 55.0, 300.0, 21.0,
                true, true, true, suggestion);
    }
}
