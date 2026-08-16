#pragma once

#include <Arduino.h>

enum SensorValidity : uint8_t {
  kAirTemperatureValid = 1U << 0,
  kRelativeHumidityValid = 1U << 1,
  kPpfdValid = 1U << 2,
  kSoilTemperatureValid = 1U << 3,
  kSoilMoistureValid = 1U << 4,
  kIlluminanceValid = 1U << 5,
};

constexpr uint8_t kCoreTelemetryFieldsValid =
    kAirTemperatureValid | kRelativeHumidityValid;
constexpr uint8_t kAllSensorFieldsValid =
    kCoreTelemetryFieldsValid | kPpfdValid | kSoilTemperatureValid |
    kSoilMoistureValid | kIlluminanceValid;

struct SensorSample {
  float airTemperatureC = NAN;
  float relativeHumidityPct = NAN;
  float ppfdUmolM2S = NAN;
  float illuminanceLux = NAN;
  float soilTemperatureC = NAN;
  float soilMoisturePct = NAN;
  uint8_t validity = 0;
};

void beginSensorAdapter();
SensorSample readSensorSample();
