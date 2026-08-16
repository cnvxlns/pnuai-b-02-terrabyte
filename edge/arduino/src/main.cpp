#include <Arduino.h>
#include <ctype.h>
#include <math.h>
#include <stdint.h>
#include <string.h>

#include "../include/SensorAdapter.h"
#include "../include/TelemetryConfig.h"

namespace {

constexpr uint8_t kProtocolVersion = 1;
constexpr size_t kMaxNodeIdLength = 64;

uint32_t nextSequence = 0;
uint32_t nextSampleAtMs = 0;
bool nodeIdIsValid = false;

bool isAllowedNodeIdCharacter(const char character) {
  return isalnum(static_cast<unsigned char>(character)) || character == '-' ||
         character == '_' || character == '.' || character == ':';
}

bool validateNodeId(const char* nodeId) {
  if (nodeId == nullptr) {
    return false;
  }

  const size_t length = strlen(nodeId);
  if (length == 0 || length > kMaxNodeIdLength ||
      strcmp(nodeId, "UNCONFIGURED") == 0) {
    return false;
  }

  for (size_t index = 0; index < length; ++index) {
    if (!isAllowedNodeIdCharacter(nodeId[index])) {
      return false;
    }
  }
  return true;
}

bool inRange(const float value, const float minimum, const float maximum) {
  return isfinite(value) && value >= minimum && value <= maximum;
}

uint8_t validatedFields(const SensorSample& sample) {
  uint8_t validity = sample.validity;

  if (!inRange(sample.airTemperatureC, TB_MIN_AIR_TEMPERATURE_C,
               TB_MAX_AIR_TEMPERATURE_C)) {
    validity &= ~kAirTemperatureValid;
  }
  if (!inRange(sample.relativeHumidityPct, TB_MIN_RELATIVE_HUMIDITY_PCT,
               TB_MAX_RELATIVE_HUMIDITY_PCT)) {
    validity &= ~kRelativeHumidityValid;
  }
  if (!inRange(sample.ppfdUmolM2S, TB_MIN_PPFD_UMOL_M2_S,
               TB_MAX_PPFD_UMOL_M2_S)) {
    validity &= ~kPpfdValid;
  }
#if TB_GY30_ENABLED
  if (!inRange(sample.illuminanceLux, TB_MIN_ILLUMINANCE_LUX,
               TB_MAX_ILLUMINANCE_LUX)) {
    validity &= ~kIlluminanceValid;
  }
#endif
#if TB_SOIL_TEMPERATURE_ENABLED
  if (!inRange(sample.soilTemperatureC, TB_MIN_SOIL_TEMPERATURE_C,
               TB_MAX_SOIL_TEMPERATURE_C)) {
    validity &= ~kSoilTemperatureValid;
  }
#endif
#if TB_SOIL_MOISTURE_ENABLED
  if (!inRange(sample.soilMoisturePct, TB_MIN_SOIL_MOISTURE_PCT,
               TB_MAX_SOIL_MOISTURE_PCT)) {
    validity &= ~kSoilMoistureValid;
  }
#endif

  return validity;
}

uint8_t requiredFields() {
  uint8_t required = kCoreTelemetryFieldsValid;
#if TB_GY30_ENABLED
  required |= kIlluminanceValid;
#endif
#if TB_SOIL_TEMPERATURE_ENABLED
  required |= kSoilTemperatureValid;
#endif
#if TB_SOIL_MOISTURE_ENABLED
  required |= kSoilMoistureValid;
#endif
  return required;
}

void printEnvelopeStart(const __FlashStringHelper* messageType) {
  Serial.print(F("{\"message_type\":\""));
  Serial.print(messageType);
  Serial.print(F("\",\"protocol_version\":"));
  Serial.print(kProtocolVersion);
  Serial.print(F(",\"node_id\":\""));
  Serial.print(TB_NODE_ID);
  Serial.print('"');
}

void emitHello() {
  printEnvelopeStart(F("hello"));
  Serial.print(F(",\"firmware_version\":\""));
  Serial.print(TB_FIRMWARE_VERSION);
  Serial.print(F("\",\"serial_baud\":"));
  Serial.print(TB_SERIAL_BAUD);
  Serial.print(F(",\"telemetry_interval_ms\":"));
  Serial.print(TB_TELEMETRY_INTERVAL_MS);
  Serial.print(F(",\"ready\":"));
  Serial.print(nodeIdIsValid ? F("true") : F("false"));
  Serial.println('}');
}

void emitConfigurationError() {
  printEnvelopeStart(F("configuration_error"));
  Serial.println(
      F(",\"reason\":\"node_id_missing_or_invalid\",\"ready\":false}"));
}

void emitSensorStatus(const uint32_t sequence, const uint32_t uptimeMs,
                      const uint8_t validity, const SensorSample& sample) {
  printEnvelopeStart(F("sensor_status"));
  Serial.print(F(",\"sequence\":"));
  Serial.print(sequence);
  Serial.print(F(",\"uptime_ms\":"));
  Serial.print(uptimeMs);
  Serial.print(F(",\"validity\":{\"air_temperature_c\":"));
  Serial.print((validity & kAirTemperatureValid) ? F("true") : F("false"));
  Serial.print(F(",\"relative_humidity_pct\":"));
  Serial.print((validity & kRelativeHumidityValid) ? F("true") : F("false"));
  Serial.print(F(",\"ppfd_umol_m2_s\":"));
  Serial.print((validity & kPpfdValid) ? F("true") : F("false"));
#if TB_GY30_ENABLED
  Serial.print(F(",\"illuminance_lux\":"));
  Serial.print((validity & kIlluminanceValid) ? F("true") : F("false"));
#endif
#if TB_SOIL_TEMPERATURE_ENABLED
  Serial.print(F(",\"soil_temperature_c\":"));
  Serial.print((validity & kSoilTemperatureValid) ? F("true") : F("false"));
#endif
#if TB_SOIL_MOISTURE_ENABLED
  Serial.print(F(",\"soil_moisture_pct\":"));
  Serial.print((validity & kSoilMoistureValid) ? F("true") : F("false"));
#endif
  Serial.print('}');
#if TB_GY30_ENABLED
  Serial.print(F(",\"illuminance_lux\":"));
  if (validity & kIlluminanceValid) {
    Serial.print(sample.illuminanceLux, 2);
  } else {
    Serial.print(F("null"));
  }
#endif
  Serial.println(F(",\"reason\":\"sensor_unavailable_or_out_of_range\"}"));
}

void emitTelemetry(const uint32_t sequence, const uint32_t uptimeMs,
                   const uint8_t validity, const SensorSample& sample) {
  printEnvelopeStart(F("telemetry"));
  Serial.print(F(",\"sequence\":"));
  Serial.print(sequence);
  Serial.print(F(",\"uptime_ms\":"));
  Serial.print(uptimeMs);
  Serial.print(F(",\"air_temperature_c\":"));
  Serial.print(sample.airTemperatureC, 2);
  Serial.print(F(",\"relative_humidity_pct\":"));
  Serial.print(sample.relativeHumidityPct, 2);
  Serial.print(F(",\"ppfd_umol_m2_s\":"));
  if (validity & kPpfdValid) {
    Serial.print(sample.ppfdUmolM2S, 2);
  } else {
    Serial.print(F("null"));
  }
#if TB_GY30_ENABLED
  Serial.print(F(",\"illuminance_lux\":"));
  Serial.print(sample.illuminanceLux, 2);
#endif
#if TB_SOIL_TEMPERATURE_ENABLED
  Serial.print(F(",\"soil_temperature_c\":"));
  Serial.print(sample.soilTemperatureC, 2);
#endif
#if TB_SOIL_MOISTURE_ENABLED
  Serial.print(F(",\"soil_moisture_pct\":"));
  Serial.print(sample.soilMoisturePct, 2);
#endif
  Serial.println('}');
}

void sampleAndPublish(const uint32_t uptimeMs) {
  const uint32_t sequence = nextSequence++;

  if (!nodeIdIsValid) {
    emitConfigurationError();
    return;
  }

  const SensorSample sample = readSensorSample();
  const uint8_t validity = validatedFields(sample);
  const uint8_t required = requiredFields();
  if ((validity & required) != required) {
    emitSensorStatus(sequence, uptimeMs, validity, sample);
    return;
  }

  emitTelemetry(sequence, uptimeMs, validity, sample);
}

}  // namespace

void setup() {
  Serial.begin(TB_SERIAL_BAUD);
  const uint32_t serialStartedAtMs = millis();
  while (!Serial &&
         (millis() - serialStartedAtMs) < TB_SERIAL_READY_TIMEOUT_MS) {
  }

  beginSensorAdapter();

  nodeIdIsValid = validateNodeId(TB_NODE_ID);
  emitHello();
  if (!nodeIdIsValid) {
    emitConfigurationError();
  }

  nextSampleAtMs = millis() + TB_TELEMETRY_INTERVAL_MS;
}

void loop() {
  const uint32_t now = millis();
  if (static_cast<int32_t>(now - nextSampleAtMs) < 0) {
    return;
  }

  sampleAndPublish(now);

  // Keep an anchored cadence, but skip missed slots instead of taking several
  // DHT22 readings back-to-back after a long blocking operation.
  nextSampleAtMs += TB_TELEMETRY_INTERVAL_MS;
  if (static_cast<int32_t>(now - nextSampleAtMs) >= 0) {
    nextSampleAtMs = now + TB_TELEMETRY_INTERVAL_MS;
  }
}
