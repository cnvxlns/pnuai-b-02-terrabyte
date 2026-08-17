package com.terrabyte.backend.irrigation;

import com.terrabyte.backend.measurement.IrrigationSuggestion;
import com.terrabyte.backend.measurement.TelemetrySample;
import com.terrabyte.backend.pot.Pot;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

/**
 * Decides how much water to <em>request</em> for one pot.
 *
 * <p>The edge computes a dose from a water-balance formula and ships it with the
 * reading it was derived from. This class either accepts that number or reaches
 * for a fixed table — it never invents a third one.
 *
 * <p>The value returned here is still a <em>request</em>. {@link IrrigationGovernor}
 * re-checks it against the per-dose ceiling and the daily budget before any
 * command is issued, so this is the first of two independent limits.
 */
@Component
public class VolumeResolver {

    private static final Logger log = LoggerFactory.getLogger(VolumeResolver.class);

    /**
     * The range a suggestion must fall in to be usable. Anything else is a broken edge.
     *
     * <p>The floor is 1, not 0. A 0 mL suggestion means the edge formula disagrees with
     * the rule engine that already decided this pot needs water — but overturning that
     * decision is not this class's job, and passing 0 on would have the Governor refuse
     * it as {@code AI_OUT_OF_RANGE}, filing a coherent answer in the audit trail under
     * a reason that misdescribes it. Falling back to the conservative table dose
     * resolves the disagreement in the correctable direction.
     */
    private static final int MIN_SUGGESTION_ML = 1;
    private static final int MAX_SUGGESTION_ML = 500;

    /**
     * @param volumeMl       what the caller should request
     * @param source         what decided it
     * @param modelVersion   the edge formula behind it, or null when the table decided.
     *                       Written to {@code irrigation_decision.ai_model_version}
     * @param edgeProposedMl what the edge actually proposed, **kept even when it was
     *                       rejected as out of range**. Written to
     *                       {@code irrigation_decision.ai_requested_ml}. A 99999 that
     *                       survives only in a log line cannot be used to blame the
     *                       formula afterwards; a stored one can.
     */
    public record ResolvedVolume(
            int volumeMl, VolumeSource source, String modelVersion, Integer edgeProposedMl) {
    }

    /**
     * @param pot    the pot being watered; also the reference the edge's own
     *               assumptions are checked against
     * @param sample the latest reading, or null when the pot has never reported
     */
    public ResolvedVolume resolve(Pot pot, TelemetrySample sample) {
        IrrigationSuggestion suggestion = sample == null ? null : sample.irrigationSuggestion();
        if (suggestion == null || suggestion.volumeMl() == null) {
            return fallback(pot, null, null);
        }

        int volumeMl = suggestion.volumeMl();
        if (volumeMl < MIN_SUGGESTION_ML || volumeMl > MAX_SUGGESTION_ML) {
            // Fall back, do NOT clamp. Clamping 99999 down to 500 would ship a
            // plausible-looking dose from an edge that is demonstrably broken and
            // hide the fault: the pot gets watered, the number looks ordinary,
            // and nobody learns the formula failed. Same reasoning as the
            // Governor's refusal of a non-positive request.
            log.warn(
                    "edge irrigation suggestion {} mL outside [{}, {}] for pot_id={} — "
                            + "falling back to the pot-size table (model {})",
                    volumeMl, MIN_SUGGESTION_ML, MAX_SUGGESTION_ML,
                    pot.id(), suggestion.modelVersion());
            return fallback(pot, suggestion.modelVersion(), volumeMl);
        }

        warnOnDrift(pot, suggestion);
        return new ResolvedVolume(
                volumeMl, VolumeSource.EDGE_SUGGESTION, suggestion.modelVersion(), volumeMl);
    }

    /**
     * The edge sizes its dose from assumptions it holds locally. When those have
     * drifted from the pot record — a crop changed in the app, a pot re-potted —
     * the suggestion is sized for a pot that no longer exists. The suggestion is
     * still used: the edge is physically at the plant and its substrate figure is
     * arguably the more trustworthy of the two. The warning is the deliverable
     * here; a silent divergence is the failure this whole pair of wire fields
     * exists to prevent.
     *
     * <p>Only compared when both sides have a value. A pot with no crop selected
     * yet is not disagreeing with the edge, it is saying nothing, and warning on
     * every sample for it would bury the real mismatches.
     */
    private void warnOnDrift(Pot pot, IrrigationSuggestion suggestion) {
        String assumedCrop = suggestion.assumedCropCode();
        if (assumedCrop != null && pot.cropCode() != null && !assumedCrop.equals(pot.cropCode())) {
            log.warn(
                    "irrigation suggestion drift pot_id={} crop: edge assumed {} but pot is {}",
                    pot.id(), assumedCrop, pot.cropCode());
        }

        Integer assumedVolume = suggestion.assumedSubstrateVolumeMl();
        if (assumedVolume != null
                && pot.substrateVolumeMl() != null
                && !assumedVolume.equals(pot.substrateVolumeMl())) {
            log.warn(
                    "irrigation suggestion drift pot_id={} substrate volume: "
                            + "edge assumed {} mL but pot is {} mL",
                    pot.id(), assumedVolume, pot.substrateVolumeMl());
        }
    }

    /**
     * @param rejectedModelVersion which formula produced the unusable value, or null
     *                             when there was no suggestion at all
     * @param rejectedMl           the unusable value itself, preserved for the audit
     *                             trail so a misbehaving edge can be identified later
     */
    private ResolvedVolume fallback(
            Pot pot, String rejectedModelVersion, Integer rejectedMl) {
        return new ResolvedVolume(
                fallbackVolumeMl(pot.substrateVolumeMl()),
                VolumeSource.POT_SIZE_FALLBACK,
                rejectedModelVersion,
                rejectedMl);
    }

    /**
     * Fixed doses by pot substrate volume. Deliberately a small step function
     * rather than a formula: it has to stay auditable by eye and identical across
     * restarts and edge firmware versions.
     *
     * @param substrateVolumeMl substrate volume in mL, or null when unknown
     */
    public static int fallbackVolumeMl(Integer substrateVolumeMl) {
        if (substrateVolumeMl == null || substrateVolumeMl <= 0) {
            // Unknown pot gets the smallest dose. Guessing high is the mistake
            // that floods a pot, and a too-small dose is corrected on the next
            // cycle whereas standing water is not.
            return 40;
        }
        if (substrateVolumeMl <= 1000) {
            return 40;
        }
        if (substrateVolumeMl <= 3000) {
            return 80;
        }
        if (substrateVolumeMl <= 6000) {
            return 120;
        }
        return 160;
    }
}
