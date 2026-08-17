#pragma once

// A machine-local header can override any TB_* setting below. The local file
// is intentionally ignored by Git; copy TelemetryConfig.local.h.example to
// TelemetryConfig.local.h before provisioning a device.
#if defined(__has_include)
#if __has_include("TelemetryConfig.local.h")
#include "TelemetryConfig.local.h"
#endif
#endif

#ifndef TB_NODE_ID
#define TB_NODE_ID "UNCONFIGURED"
#endif

#ifndef TB_FIRMWARE_VERSION
#define TB_FIRMWARE_VERSION "0.3.0"
#endif

#ifndef TB_SERIAL_BAUD
#define TB_SERIAL_BAUD 115200UL
#endif

// Native-USB boards get a short window for the host to open the port, which
// makes the startup hello less likely to be lost. Sampling still starts if no
// host is connected.
#ifndef TB_SERIAL_READY_TIMEOUT_MS
#define TB_SERIAL_READY_TIMEOUT_MS 2000UL
#endif

#ifndef TB_MOCK_SENSOR_ENABLED
#define TB_MOCK_SENSOR_ENABLED 0
#endif

// DHT22 should not be sampled faster than once every two seconds.
#ifndef TB_TELEMETRY_INTERVAL_MS
#define TB_TELEMETRY_INTERVAL_MS 5000UL
#endif

#ifndef TB_DHT22_ENABLED
#define TB_DHT22_ENABLED 1
#endif

#ifndef TB_DHT22_PIN
#define TB_DHT22_PIN 2
#endif

#if TB_TELEMETRY_INTERVAL_MS == 0UL
#error "TB_TELEMETRY_INTERVAL_MS must be greater than zero"
#endif

#if !TB_MOCK_SENSOR_ENABLED && TB_DHT22_ENABLED && \
    TB_TELEMETRY_INTERVAL_MS < 2000UL
#error "TB_TELEMETRY_INTERVAL_MS must be at least 2000 for DHT22"
#endif

// GY-30 modules normally use a BH1750 digital illuminance sensor over I2C.
// ADD=GND selects 0x23; ADD=VCC selects 0x5C.
#ifndef TB_GY30_ENABLED
#define TB_GY30_ENABLED 1
#endif

#ifndef TB_GY30_I2C_ADDRESS
#define TB_GY30_I2C_ADDRESS 0x23
#endif

#if TB_GY30_I2C_ADDRESS != 0x23 && TB_GY30_I2C_ADDRESS != 0x5C
#error "TB_GY30_I2C_ADDRESS must be 0x23 or 0x5C"
#endif

// BH1750 measures illuminance in lux, not PPFD. A custom calibration against a
// PAR/PPFD reference is preferred. Named profiles are only rough spectral
// estimates and must not be treated as measured calibration.
#define TB_PPFD_LIGHT_SOURCE_CUSTOM 0
#define TB_PPFD_LIGHT_SOURCE_SUNLIGHT_APPROX 1
#define TB_PPFD_LIGHT_SOURCE_COOL_WHITE_FLUORESCENT_APPROX 2
#define TB_PPFD_LIGHT_SOURCE_HPS_APPROX 3
#define TB_PPFD_LIGHT_SOURCE_METAL_HALIDE_APPROX 4
#define TB_PPFD_LIGHT_SOURCE_WHITE_LED_4000K_APPROX 5

#ifndef TB_PPFD_LIGHT_SOURCE
#define TB_PPFD_LIGHT_SOURCE TB_PPFD_LIGHT_SOURCE_CUSTOM
#endif

#if TB_PPFD_LIGHT_SOURCE != TB_PPFD_LIGHT_SOURCE_CUSTOM &&                 \
    TB_PPFD_LIGHT_SOURCE != TB_PPFD_LIGHT_SOURCE_SUNLIGHT_APPROX &&        \
    TB_PPFD_LIGHT_SOURCE !=                                                \
        TB_PPFD_LIGHT_SOURCE_COOL_WHITE_FLUORESCENT_APPROX &&              \
    TB_PPFD_LIGHT_SOURCE != TB_PPFD_LIGHT_SOURCE_HPS_APPROX &&             \
    TB_PPFD_LIGHT_SOURCE != TB_PPFD_LIGHT_SOURCE_METAL_HALIDE_APPROX &&     \
    TB_PPFD_LIGHT_SOURCE != TB_PPFD_LIGHT_SOURCE_WHITE_LED_4000K_APPROX
#error "TB_PPFD_LIGHT_SOURCE is not a supported light-source profile"
#endif

#ifndef TB_GY30_PPFD_CALIBRATION_ENABLED
#define TB_GY30_PPFD_CALIBRATION_ENABLED 0
#endif

#if TB_GY30_PPFD_CALIBRATION_ENABLED && !TB_GY30_ENABLED
#error "GY-30 must be enabled when GY-30 PPFD conversion is enabled"
#endif

#if !TB_MOCK_SENSOR_ENABLED && TB_GY30_PPFD_CALIBRATION_ENABLED
#if TB_PPFD_LIGHT_SOURCE != TB_PPFD_LIGHT_SOURCE_CUSTOM
#if defined(TB_PPFD_PER_LUX) || defined(TB_PPFD_OFFSET)
#error "Named PPFD profiles cannot be combined with custom PPFD coefficients"
#endif
#if TB_PPFD_LIGHT_SOURCE == TB_PPFD_LIGHT_SOURCE_SUNLIGHT_APPROX
#define TB_PPFD_PER_LUX 0.0185f
#elif TB_PPFD_LIGHT_SOURCE == \
    TB_PPFD_LIGHT_SOURCE_COOL_WHITE_FLUORESCENT_APPROX
#define TB_PPFD_PER_LUX 0.0135f
#elif TB_PPFD_LIGHT_SOURCE == TB_PPFD_LIGHT_SOURCE_HPS_APPROX
#define TB_PPFD_PER_LUX 0.0122f
#elif TB_PPFD_LIGHT_SOURCE == TB_PPFD_LIGHT_SOURCE_METAL_HALIDE_APPROX
#define TB_PPFD_PER_LUX 0.0141f
#elif TB_PPFD_LIGHT_SOURCE == TB_PPFD_LIGHT_SOURCE_WHITE_LED_4000K_APPROX
#define TB_PPFD_PER_LUX 0.0143f
#endif
#define TB_PPFD_OFFSET 0.0f
#ifndef TB_PPFD_CALIBRATED_MIN_LUX
#define TB_PPFD_CALIBRATED_MIN_LUX 0.0f
#endif
#ifndef TB_PPFD_CALIBRATED_MAX_LUX
#define TB_PPFD_CALIBRATED_MAX_LUX 54612.5f
#endif
#endif

#ifndef TB_PPFD_PER_LUX
#error "Define calibrated TB_PPFD_PER_LUX when GY-30 PPFD conversion is enabled"
#endif
#ifndef TB_PPFD_OFFSET
#error "Define calibrated TB_PPFD_OFFSET when GY-30 PPFD conversion is enabled"
#endif
#ifndef TB_PPFD_CALIBRATED_MIN_LUX
#error "Define TB_PPFD_CALIBRATED_MIN_LUX when GY-30 PPFD conversion is enabled"
#endif
#ifndef TB_PPFD_CALIBRATED_MAX_LUX
#error "Define TB_PPFD_CALIBRATED_MAX_LUX when GY-30 PPFD conversion is enabled"
#endif
#endif

// Optional waterproof DS18B20 soil-temperature probe on a OneWire bus.
#ifndef TB_SOIL_TEMPERATURE_ENABLED
#define TB_SOIL_TEMPERATURE_ENABLED 0
#endif

#ifndef TB_SOIL_TEMPERATURE_PIN
#define TB_SOIL_TEMPERATURE_PIN 3
#endif

// Optional analog capacitive soil-moisture sensor. Calibration endpoints are
// deliberately required because ADC direction and values vary by sensor,
// supply voltage, soil, and board.
#ifndef TB_SOIL_MOISTURE_ENABLED
#define TB_SOIL_MOISTURE_ENABLED 0
#endif

#ifndef TB_SOIL_MOISTURE_ADC_PIN
#define TB_SOIL_MOISTURE_ADC_PIN A1
#endif

#if !TB_MOCK_SENSOR_ENABLED && TB_SOIL_MOISTURE_ENABLED
#ifndef TB_SOIL_MOISTURE_DRY_ADC
#error "Define calibrated TB_SOIL_MOISTURE_DRY_ADC when soil moisture is enabled"
#endif
#ifndef TB_SOIL_MOISTURE_WET_ADC
#error "Define calibrated TB_SOIL_MOISTURE_WET_ADC when soil moisture is enabled"
#endif
#if TB_SOIL_MOISTURE_DRY_ADC == TB_SOIL_MOISTURE_WET_ADC
#error "Soil-moisture dry and wet ADC calibration values must differ"
#endif
#endif

// Validation limits. The temperature limits match the rated DHT22 range.
#ifndef TB_MIN_AIR_TEMPERATURE_C
#define TB_MIN_AIR_TEMPERATURE_C (-40.0f)
#endif

#ifndef TB_MAX_AIR_TEMPERATURE_C
#define TB_MAX_AIR_TEMPERATURE_C 80.0f
#endif

#ifndef TB_MIN_RELATIVE_HUMIDITY_PCT
#define TB_MIN_RELATIVE_HUMIDITY_PCT 0.0f
#endif

#ifndef TB_MAX_RELATIVE_HUMIDITY_PCT
#define TB_MAX_RELATIVE_HUMIDITY_PCT 100.0f
#endif

#ifndef TB_MIN_PPFD_UMOL_M2_S
#define TB_MIN_PPFD_UMOL_M2_S 0.0f
#endif

#ifndef TB_MAX_PPFD_UMOL_M2_S
#define TB_MAX_PPFD_UMOL_M2_S 5000.0f
#endif

#ifndef TB_MIN_ILLUMINANCE_LUX
#define TB_MIN_ILLUMINANCE_LUX 0.0f
#endif

// BH1750 high-resolution output is a 16-bit count divided by 1.2.
#ifndef TB_MAX_ILLUMINANCE_LUX
#define TB_MAX_ILLUMINANCE_LUX 54612.5f
#endif

#ifndef TB_MIN_SOIL_TEMPERATURE_C
#define TB_MIN_SOIL_TEMPERATURE_C (-20.0f)
#endif

#ifndef TB_MAX_SOIL_TEMPERATURE_C
#define TB_MAX_SOIL_TEMPERATURE_C 80.0f
#endif

#ifndef TB_MIN_SOIL_MOISTURE_PCT
#define TB_MIN_SOIL_MOISTURE_PCT 0.0f
#endif

#ifndef TB_MAX_SOIL_MOISTURE_PCT
#define TB_MAX_SOIL_MOISTURE_PCT 100.0f
#endif
