package com.terrabyte.backend.measurement;

import java.time.Instant;

/**
 * One node's measurements, resolved against the pot and gateway that own them.
 *
 * <p>{@code eventId} is the Orange Pi outbox UUID. It travels all the way into
 * InfluxDB as a field (never a tag — a value unique per sample would explode
 * series cardinality) so a stored sample can be traced back to the edge event
 * that produced it.
 */
public record TelemetrySample(
        long potId,
        long deviceId,
        String nodeId,
        String cropCode,
        String hardwareDeviceId,
        String eventId,
        Instant observedAt,
        long sequence,
        double soilMoisturePct,
        long soilMoistureRawAdc,
        double airTemperatureC,
        double airHumidityPct,
        double plantLightPpfdUmolM2S,
        // Nullable, unlike soilMoisturePct above: the probe is optional, and a
        // missing reading collapsing to 0.0 would read as a confident "0°C"
        // rather than "unknown" — the same hazard soilMoisturePctOrZero()
        // already accepts for moisture must not be repeated for temperature.
        Double soilTemperatureC,
        boolean soilSensorValid,
        boolean airSensorValid,
        boolean lightSensorValid,
        // Null whenever the edge could not compute a dose for this reading. The
        // irrigation path falls back to the pot-size table rather than treating
        // the absence as an error.
        IrrigationSuggestion irrigationSuggestion) {

    public static TelemetrySample from(
            TelemetryEnvelope envelope,
            TelemetryEnvelope.Node node,
            long potId,
            long deviceId,
            String cropCode) {
        TelemetryEnvelope.Measurements measurements = node.measurements();
        TelemetryEnvelope.Quality quality = node.quality();
        return new TelemetrySample(
                potId,
                deviceId,
                node.nodeId(),
                cropCode,
                envelope.gatewayId(),
                envelope.eventId(),
                envelope.observedAt(),
                node.sequence(),
                measurements.soilMoisturePctOrZero(),
                measurements.soilMoistureRawAdcOrZero(),
                measurements.airTemperatureC(),
                measurements.airHumidityPct(),
                measurements.plantLightPpfdUmolM2S(),
                measurements.soilTemperatureC(),
                quality.soilSensorValidOrFalse(),
                quality.airSensorValid(),
                quality.lightSensorValid(),
                node.irrigationSuggestion());
    }
}
