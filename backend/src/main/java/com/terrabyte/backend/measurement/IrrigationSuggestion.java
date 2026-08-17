package com.terrabyte.backend.measurement;

import com.fasterxml.jackson.databind.PropertyNamingStrategies;
import com.fasterxml.jackson.databind.annotation.JsonNaming;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.Size;

/**
 * The dose the edge computed for itself, carried on telemetry.
 *
 * <p>The edge runs a water-balance formula locally and ships the answer with the
 * reading it was derived from. It replaced a scikit-learn model behind an HTTP
 * call: the model only ever recovered the formula it was trained on, and a
 * network round-trip buys nothing for a computation that needs no network.
 *
 * <p>Absent is normal and must never fail ingestion — the edge omits the whole
 * block whenever it cannot compute one (no soil probe, no crop selected yet).
 *
 * <p>{@code assumedCropCode} and {@code assumedSubstrateVolumeMl} are not inputs
 * the backend needs; they are the edge's own assumptions, sent so the backend can
 * notice they have drifted from the pot record. Without them a pot re-planted in
 * the cloud would keep receiving doses sized for the crop it no longer holds,
 * and nothing would say so.
 *
 * <p>One record rather than four loose fields on {@link TelemetrySample}: the
 * drift check needs all of it together, and a single nullable component says
 * "no suggestion" once instead of four correlated nulls saying it four times.
 */
@JsonNaming(PropertyNamingStrategies.SnakeCaseStrategy.class)
public record IrrigationSuggestion(
        // Boxed and @NotNull rather than a primitive: a block that arrives
        // without a volume is malformed, and a primitive would silently read it
        // as a confident 0 mL.
        @NotNull @Min(0) @Max(500) Integer volumeMl,
        @Size(max = 50) String modelVersion,
        @Size(max = 50) String assumedCropCode,
        @Positive Integer assumedSubstrateVolumeMl) {
}
