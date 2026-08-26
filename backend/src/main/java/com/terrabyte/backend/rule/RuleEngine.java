package com.terrabyte.backend.rule;

import java.time.Clock;
import java.time.LocalTime;
import java.util.Optional;
import java.util.UUID;

import com.terrabyte.backend.irrigation.IrrigationService;
import com.terrabyte.backend.irrigation.LightService;
import com.terrabyte.backend.measurement.MeasurementStore;
import com.terrabyte.backend.measurement.TelemetrySample;
import com.terrabyte.backend.pot.Pot;
import com.terrabyte.backend.pot.PotRepository;
import com.terrabyte.backend.score.CropScoreProfile;
import com.terrabyte.backend.score.CropScoreProfileRepository;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * Turns readings into requests, once a minute.
 *
 * <p>Deliberately thin, and that is the whole design. Every safety question —
 * minimum interval, daily budget, dose size, a command already in flight, a
 * reading too old to trust — already belongs to {@code IrrigationGovernor}, and
 * a second copy here would drift from it. What this class owns is
 * <em>noticing</em>: it decides that a pot looks dry and hands the question on.
 *
 * <p>So a refusal is the normal outcome, not an error. The engine asks on every
 * pass while soil stays below the gate, and the Governor answers "not yet" until
 * the cooldown expires. Each of those refusals is written to
 * {@code irrigation_decision} with its reason, which is what makes "why was this
 * pot never watered" answerable at all.
 *
 * <p>No heat-pad rule. There is no heat pad — {@code edge/arduino/src/main.cpp}
 * says so where it decides which actuators to report — and a rule for hardware
 * that does not exist would produce commands the firmware rejects.
 *
 * <p>No sensor-anomaly rule either, because there already is one:
 * {@code MeasurementService} publishes {@code SensorQualityObservedEvent} and
 * {@code DevicePresenceObservedEvent} as telemetry arrives, which is both
 * earlier and more accurate than anything a once-a-minute sweep could conclude.
 */
@Component
public class RuleEngine {

    private static final Logger LOGGER = LoggerFactory.getLogger(RuleEngine.class);

    private final PotRepository potRepository;
    private final MeasurementStore measurementStore;
    private final CropScoreProfileRepository profileRepository;
    private final IrrigationService irrigationService;
    private final LightService lightService;
    private final RuleProperties properties;
    private final Clock clock;

    public RuleEngine(
            PotRepository potRepository,
            MeasurementStore measurementStore,
            CropScoreProfileRepository profileRepository,
            IrrigationService irrigationService,
            LightService lightService,
            RuleProperties properties,
            Clock clock) {
        this.potRepository = potRepository;
        this.measurementStore = measurementStore;
        this.profileRepository = profileRepository;
        this.irrigationService = irrigationService;
        this.lightService = lightService;
        this.properties = properties;
        this.clock = clock;
    }

    /**
     * Scheduled entry point.
     *
     * <p>{@code fixedDelay}, like {@code ExpiredCommandSweeper}: a slow pass must
     * not queue another behind it, and two passes overlapping would ask the
     * Governor twice for the same pot in the same moment.
     */
    @Scheduled(
            initialDelayString = "${app.rule.initial-delay-ms:20000}",
            fixedDelayString = "${app.rule.interval-ms:60000}")
    public void evaluate() {
        try {
            evaluateOnce();
        } catch (Exception e) {
            LOGGER.error("rule pass failed; the next tick will retry", e);
        }
    }

    /** One sweep over every pot under automatic control. */
    public void evaluateOnce() {
        for (Pot pot : potRepository.findAllUnderAutomaticControl()) {
            try {
                evaluatePot(pot);
            } catch (Exception e) {
                // Per pot, not per pass. One pot with a broken node must not
                // stop the pot next to it from being watered.
                LOGGER.error("rule evaluation failed pot_id={}", pot.id(), e);
            }
        }
    }

    private void evaluatePot(Pot pot) {
        Optional<TelemetrySample> latest = measurementStore.findLatest(pot.id());
        if (latest.isEmpty()) {
            // Nothing to be right about. Asking the Governor anyway would write
            // a decision row for a pot nobody has measured.
            LOGGER.debug("no reading for pot_id={}, skipping", pot.id());
            return;
        }
        TelemetrySample sample = latest.get();
        applyIrrigationRule(pot, sample);
        applyLightRule(pot, sample);
    }

    /**
     * Soil below the gate becomes a request; everything else is the Governor's.
     *
     * <p>Sample freshness is checked there too, and only there. Duplicating the
     * check would mean two definitions of "too old" that could disagree.
     */
    private void applyIrrigationRule(Pot pot, TelemetrySample sample) {
        if (!sample.soilSensorValid()) {
            // A probe reading 20 % while reporting itself broken is evidence of
            // a broken probe, not of dry soil.
            LOGGER.debug("soil probe invalid pot_id={}, no irrigation rule", pot.id());
            return;
        }
        if (sample.soilMoisturePct() >= properties.soilDryGatePct()) {
            return;
        }

        String correlationId = correlationId("rule-water");
        LOGGER.info(
                "rule: soil {}% below {}% pot_id={} correlation_id={}",
                sample.soilMoisturePct(), properties.soilDryGatePct(), pot.id(), correlationId);
        irrigationService.requestAutomatic(pot.id(), correlationId);
    }

    /**
     * Keeps the lamp inside the crop's own PPFD band, and only in daylight hours.
     *
     * <p>The band comes from {@code crop_score_profile}, so a crop the score
     * model has never heard of gets no lamp rather than an invented threshold.
     * The band is also what gives the rule hysteresis: a single threshold would
     * flip the lamp on every pass while a reading hovered on it.
     */
    private void applyLightRule(Pot pot, TelemetrySample sample) {
        LocalTime now = LocalTime.now(clock);
        if (!properties.isDaytime(now)) {
            // Plants need the dark as much as the light. A rule that only chased
            // PPFD would run the lamp all night.
            request(pot, false, "rule-dark");
            return;
        }
        if (!sample.lightSensorValid() || sample.plantLightPpfdUmolM2S() == null) {
            LOGGER.debug("no usable light reading pot_id={}, no light rule", pot.id());
            return;
        }
        Optional<CropScoreProfile> profile =
                profileRepository.findActiveByCropCode(pot.cropCode());
        if (profile.isEmpty()) {
            LOGGER.debug("no score profile for crop={}, no light rule", pot.cropCode());
            return;
        }

        double ppfd = sample.plantLightPpfdUmolM2S();
        double margin = properties.lightBandMargin();
        double low = profile.get().ppfdOptimalLow() * margin;
        double high = profile.get().ppfdOptimalHigh() * margin;

        if (ppfd < low) {
            request(pot, true, "rule-dim");
        } else if (ppfd > high) {
            request(pot, false, "rule-bright");
        }
        // Inside the band: nothing to say. The lamp keeps whatever state it has.
    }

    private void request(Pot pot, boolean on, String prefix) {
        String correlationId = correlationId(prefix);
        LOGGER.info(
                "rule: light {} pot_id={} correlation_id={}",
                on ? "on" : "off", pot.id(), correlationId);
        lightService.requestAutomatic(pot.id(), on, correlationId);
    }

    /**
     * The number that ties a reading to a decision to a command to a result.
     *
     * <p>Prefixed with what triggered it, so the reason survives even when the
     * decision row is read on its own.
     */
    private String correlationId(String prefix) {
        return prefix + "-" + UUID.randomUUID();
    }
}
