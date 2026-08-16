package com.terrabyte.backend.measurement;

import java.time.Instant;

public record TelemetrySample(
        long potId,
        long deviceId,
        String nodeId,
        String cropCode,
        String hardwareDeviceId,
        Instant observedAt,
        long sequence,
        String siteId,
        String zoneId,
        String soilType,
        String cropType,
        String calibrationVersion,
        double soilMoisturePct,
        long soilMoistureRawAdc,
        double airTemperatureC,
        double airHumidityPct,
        Double plantLightPpfdUmolM2S,
        boolean soilSensorValid,
        boolean airSensorValid,
        boolean lightSensorValid) {

    public static TelemetrySample from(
            TelemetrySampleRequest request,
            long potId,
            long deviceId,
            String nodeId,
            String cropCode) {
        return new TelemetrySample(
                potId,
                deviceId,
                nodeId,
                cropCode,
                request.deviceId(),
                request.observedAt(),
                request.sequence(),
                request.context().siteId(),
                request.context().zoneId(),
                request.context().soilType(),
                request.context().cropType(),
                request.context().calibrationVersion(),
                request.measurements().soilMoisturePct(),
                request.measurements().soilMoistureRawAdc(),
                request.measurements().airTemperatureC(),
                request.measurements().airHumidityPct(),
                request.measurements().plantLightPpfdUmolM2S(),
                request.quality().soilSensorValid(),
                request.quality().airSensorValid(),
                request.quality().lightSensorValid());
    }

    public TelemetrySample(
            String hardwareDeviceId,
            Instant observedAt,
            long sequence,
            String siteId,
            String zoneId,
            String soilType,
            String cropType,
            String calibrationVersion,
            double soilMoisturePct,
            long soilMoistureRawAdc,
            double airTemperatureC,
            double airHumidityPct,
            Double plantLightPpfdUmolM2S,
            boolean soilSensorValid,
            boolean airSensorValid,
            boolean lightSensorValid) {
        this(
                0, 0, zoneId, null, hardwareDeviceId, observedAt, sequence,
                siteId, zoneId, soilType, cropType, calibrationVersion,
                soilMoisturePct, soilMoistureRawAdc, airTemperatureC, airHumidityPct,
                plantLightPpfdUmolM2S, soilSensorValid, airSensorValid, lightSensorValid);
    }
}
