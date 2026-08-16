package com.terrabyte.backend.measurement;

import java.time.Instant;

import com.fasterxml.jackson.databind.PropertyNamingStrategies;
import com.fasterxml.jackson.annotation.JsonIgnore;
import com.fasterxml.jackson.databind.annotation.JsonNaming;
import jakarta.validation.Valid;
import jakarta.validation.constraints.AssertTrue;
import jakarta.validation.constraints.DecimalMax;
import jakarta.validation.constraints.DecimalMin;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.PositiveOrZero;
import jakarta.validation.constraints.Size;

@JsonNaming(PropertyNamingStrategies.SnakeCaseStrategy.class)
public record TelemetrySampleRequest(
        @Min(1) @Max(1) int schemaVersion,
        @NotBlank @Pattern(regexp = "telemetry\\.sample") String eventType,
        @NotBlank @Size(max = 100) String deviceId,
        @NotNull Instant observedAt,
        @PositiveOrZero long sequence,
        @NotNull @Valid Context context,
        @NotNull @Valid Measurements measurements,
        @NotNull @Valid Quality quality) {

    @JsonIgnore
    @AssertTrue(message = "plant light PPFD and light sensor validity must be consistent")
    public boolean isLightMeasurementConsistent() {
        if (measurements == null || quality == null || quality.lightSensorValid() == null) {
            return true;
        }
        return (measurements.plantLightPpfdUmolM2S() != null)
                == quality.lightSensorValid();
    }

    @JsonNaming(PropertyNamingStrategies.SnakeCaseStrategy.class)
    public record Context(
            @NotBlank @Size(max = 100) String siteId,
            @NotBlank @Size(max = 100) String zoneId,
            @NotBlank @Size(max = 100) String soilType,
            @NotBlank @Size(max = 100) String cropType,
            @NotBlank @Size(max = 100) String calibrationVersion) {
    }

    @JsonNaming(PropertyNamingStrategies.SnakeCaseStrategy.class)
    public record Measurements(
            @NotNull @DecimalMin("0.0") @DecimalMax("100.0") Double soilMoisturePct,
            @NotNull @PositiveOrZero Long soilMoistureRawAdc,
            @NotNull @DecimalMin("-50.0") @DecimalMax("100.0") Double airTemperatureC,
            @NotNull @DecimalMin("0.0") @DecimalMax("100.0") Double airHumidityPct,
            @DecimalMin("0.0") @DecimalMax("5000.0") Double plantLightPpfdUmolM2S) {
    }

    @JsonNaming(PropertyNamingStrategies.SnakeCaseStrategy.class)
    public record Quality(
            @NotNull Boolean soilSensorValid,
            @NotNull Boolean airSensorValid,
            @NotNull Boolean lightSensorValid) {
    }
}
