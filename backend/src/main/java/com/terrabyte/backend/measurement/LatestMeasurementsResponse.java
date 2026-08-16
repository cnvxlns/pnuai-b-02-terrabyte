package com.terrabyte.backend.measurement;

import java.time.Instant;

public record LatestMeasurementsResponse(
        long deviceId,
        String hardwareDeviceId,
        Instant observedAt,
        long sequence,
        Context context,
        Measurements measurements,
        Quality quality) {

    public static LatestMeasurementsResponse from(long deviceId, TelemetrySample sample) {
        return new LatestMeasurementsResponse(
                deviceId,
                sample.hardwareDeviceId(),
                sample.observedAt(),
                sample.sequence(),
                new Context(
                        sample.siteId(),
                        sample.zoneId(),
                        sample.soilType(),
                        sample.cropType(),
                        sample.calibrationVersion()),
                new Measurements(
                        sample.soilMoisturePct(),
                        sample.soilMoistureRawAdc(),
                        sample.airTemperatureC(),
                        sample.airHumidityPct(),
                        sample.plantLightPpfdUmolM2S()),
                new Quality(
                        sample.soilSensorValid(),
                        sample.airSensorValid(),
                        sample.lightSensorValid()));
    }

    public record Context(
            String siteId,
            String zoneId,
            String soilType,
            String cropType,
            String calibrationVersion) {
    }

    public record Measurements(
            double soilMoisturePct,
            long soilMoistureRawAdc,
            double airTemperatureC,
            double airHumidityPct,
            Double plantLightPpfdUmolM2S) {
    }

    public record Quality(
            boolean soilSensorValid,
            boolean airSensorValid,
            boolean lightSensorValid) {
    }
}
