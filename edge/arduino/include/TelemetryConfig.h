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

// 0.4.0 was the first version with an inbound command path; 0.5.0 adds the
// grow-light latch (act:"led"). The `hello` record is how the Orange Pi tells
// which commands it may send at all, so this has to move whenever the inbound
// vocabulary does. protocol_version stays 1: adding keys to telemetry is
// additive, and the MQTT schema_version is a different namespace that the
// Orange Pi translates into.
#ifndef TB_FIRMWARE_VERSION
#define TB_FIRMWARE_VERSION "0.5.0"
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

// TSL2591 digital illuminance sensor over I2C at its fixed address, 0x29.
#ifndef TB_TSL2591_ENABLED
#define TB_TSL2591_ENABLED 1
#endif

#ifndef TB_TSL2591_I2C_ADDRESS
#define TB_TSL2591_I2C_ADDRESS 0x29
#endif

#if TB_TSL2591_I2C_ADDRESS != 0x29
#error "TB_TSL2591_I2C_ADDRESS must be 0x29"
#endif

#ifndef TB_TSL2591_GAIN
#define TB_TSL2591_GAIN 25
#endif

#if TB_TSL2591_GAIN != 1 && TB_TSL2591_GAIN != 25 && \
    TB_TSL2591_GAIN != 428 && TB_TSL2591_GAIN != 9876
#error "TB_TSL2591_GAIN must be 1, 25, 428, or 9876"
#endif

#ifndef TB_TSL2591_INTEGRATION_MS
#define TB_TSL2591_INTEGRATION_MS 300
#endif

#if TB_TSL2591_INTEGRATION_MS != 100 && \
    TB_TSL2591_INTEGRATION_MS != 200 && \
    TB_TSL2591_INTEGRATION_MS != 300 && \
    TB_TSL2591_INTEGRATION_MS != 400 && \
    TB_TSL2591_INTEGRATION_MS != 500 && \
    TB_TSL2591_INTEGRATION_MS != 600
#error "TB_TSL2591_INTEGRATION_MS must be 100, 200, 300, 400, 500, or 600"
#endif

#ifndef TB_TSL2591_AUTO_GAIN_ENABLED
#define TB_TSL2591_AUTO_GAIN_ENABLED 1
#endif

// Illuminance is not PPFD. Enable this conversion only after calibration
// against a PAR/PPFD reference using the final light source.
#ifndef TB_PPFD_CALIBRATION_ENABLED
#define TB_PPFD_CALIBRATION_ENABLED 0
#endif

#if TB_PPFD_CALIBRATION_ENABLED && !TB_TSL2591_ENABLED
#error "The light sensor must be enabled when PPFD conversion is enabled"
#endif

#if !TB_MOCK_SENSOR_ENABLED && TB_PPFD_CALIBRATION_ENABLED
#ifndef TB_PPFD_PER_LUX
#error "Define calibrated TB_PPFD_PER_LUX when PPFD conversion is enabled"
#endif
#ifndef TB_PPFD_OFFSET
#error "Define calibrated TB_PPFD_OFFSET when PPFD conversion is enabled"
#endif
#ifndef TB_PPFD_CALIBRATED_MIN_LUX
#error "Define TB_PPFD_CALIBRATED_MIN_LUX when PPFD conversion is enabled"
#endif
#ifndef TB_PPFD_CALIBRATED_MAX_LUX
#error "Define TB_PPFD_CALIBRATED_MAX_LUX when PPFD conversion is enabled"
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
#define TB_SOIL_MOISTURE_ADC_PIN A0
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

// The endpoints must be measurements, not the rails.
//
// "Defined and different" is not enough, and 1023/0 is the proof: it satisfies
// both checks above and is what a board reports before anyone has calibrated
// it. The percentage that comes out is then a linear stretch of the entire ADC
// range — plausible-looking, wrong by a wide margin, and completely silent.
// node-001 shipped that way and reported 53% for soil sitting at 40%, which is
// the difference between "the rule engine waters this pot" and "it never does".
//
// A capacitive probe cannot reach either rail. Dry air lands somewhere around
// 450-700 and full immersion around 180-350, depending on the supply rail, so
// the span is a few hundred counts rather than a thousand. The bounds below are
// deliberately far looser than any real calibration: they are here to catch a
// placeholder, not to grade a measurement.
#if TB_SOIL_MOISTURE_DRY_ADC > 950 || TB_SOIL_MOISTURE_WET_ADC > 950
#error "Soil-moisture calibration looks like an ADC rail (>950), not a measurement. Calibrate the probe in air and in water, or copy the config for this board."
#endif
#if TB_SOIL_MOISTURE_DRY_ADC < 60 || TB_SOIL_MOISTURE_WET_ADC < 60
#error "Soil-moisture calibration looks like an ADC rail (<60), not a measurement. Calibrate the probe in air and in water, or copy the config for this board."
#endif
#if (TB_SOIL_MOISTURE_DRY_ADC - TB_SOIL_MOISTURE_WET_ADC > 600) || (TB_SOIL_MOISTURE_WET_ADC - TB_SOIL_MOISTURE_DRY_ADC > 600)
#error "Soil-moisture calibration spans more than 600 ADC counts. A capacitive probe spans a few hundred; this looks like the full range was used as a placeholder."
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

// TSL2591 rated maximum illuminance.
#ifndef TB_MAX_ILLUMINANCE_LUX
#define TB_MAX_ILLUMINANCE_LUX 88000.0f
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

// ---------------------------------------------------------------------------
// Actuator hard interlocks (G1-G3). See docs/design/edge_ai_hardening.md.
//
// These bounds exist so the pump stops on its own when the Orange Pi, the
// broker, and the cloud are all dead. No inbound command can widen them: a
// command asks for a duration and the firmware answers with a duration.
// ---------------------------------------------------------------------------

// G1 absolute maximum single run. A command asking for more is clamped, not
// rejected, so a mis-scaled request still delivers water instead of nothing.
//
// 210 s comes from the measured flow rate: a 500 mL bottle emptied in 8 min
// 30 s (510 s), i.e. 0.980392 mL/s, so 210 s is 205.9 mL. That is the smallest
// value that lets the server's 200 mL dose-max-ml (204 000 ms) run to
// completion; at the previous 30 s the ceiling was 29.4 mL and EVERY maximum
// dose would have been truncated and reported as stop:"max_runtime".
//
// This weakens the last-resort bound, and the number is worth stating plainly.
// Against G2 below, the worst sustained duty a stream of unique command ids can
// hold rises from 30/630 = 4.8% to 210/810 = 25.9%, i.e. from about 4 L to
// about 22 L per day if the gateway, the broker and the server are all wrong at
// once. That residue is carried by the server's 600 mL/24h budget and the
// gateway's own budget, not by this constant.
// TODO(G5): a rolling per-hour duty cap would bound it in firmware regardless
// of how the interval is tuned. Deliberately not bundled here - three safety
// numbers changing in one review is how safety reviews stop working.
#ifndef TB_PUMP_ABS_MAX_MS
#define TB_PUMP_ABS_MAX_MS 210000UL
#endif

// G2 minimum interval between runs, measured from the last stop.
#ifndef TB_PUMP_MIN_INTERVAL_MS
#define TB_PUMP_MIN_INTERVAL_MS 600000UL
#endif

// G3 dead-man watchdog. While the pump runs, any inbound serial byte counts as
// proof the host is alive; silence for this long stops the pump.
#ifndef TB_HOST_TIMEOUT_MS
#define TB_HOST_TIMEOUT_MS 3000UL
#endif

#if TB_PUMP_ABS_MAX_MS == 0UL
#error "TB_PUMP_ABS_MAX_MS must be greater than zero"
#endif

#if TB_HOST_TIMEOUT_MS == 0UL
#error "TB_HOST_TIMEOUT_MS must be greater than zero"
#endif

// The dead-man window has to outlast one host tick plus jitter, otherwise a
// perfectly healthy link aborts every run.
#if TB_HOST_TIMEOUT_MS < 1000UL
#error "TB_HOST_TIMEOUT_MS must exceed the 1s host dead-man tick period"
#endif

// The firmware cooldown and the server cooldown (6h) are managed separately, so
// they can drift apart. The firmware side must always be the shorter of the two
// or the firmware rejects commands the server already approved, which reaches
// the user as an unexplained failure. 6h is the ceiling, not a target.
#if TB_PUMP_MIN_INTERVAL_MS >= 21600000UL
#error "TB_PUMP_MIN_INTERVAL_MS must stay below the 6h server cooldown"
#endif

// A run must fit inside the cooldown window; otherwise the guard would be
// asked to start a run that its own interval rule already forbids.
#if TB_PUMP_ABS_MAX_MS >= TB_PUMP_MIN_INTERVAL_MS
#error "TB_PUMP_ABS_MAX_MS must be shorter than TB_PUMP_MIN_INTERVAL_MS"
#endif

// Pump output wiring. D4 drives the gate of the pump MOSFET.
#ifndef TB_PUMP_PIN
#define TB_PUMP_PIN 4
#endif

// Output polarity. A MOSFET gate is active HIGH, so the defaults below are
// correct for the fitted hardware: gate HIGH conducts, gate LOW does not.
//
// The levels stay named rather than hard-coded because the design doc specifies
// "OUTPUT + LOW" for G4, which is only safe on an active-HIGH input; many
// low-cost relay modules are active LOW, and on those, driving LOW at boot
// turns the pump ON. Anyone swapping the MOSFET for such a module must set both.
//
// Neither level protects the window between reset and setup(): the pin is a
// high-impedance input there and the gate floats. Only a hardware pull-down
// (~10k) on the gate covers that, and it belongs on both D4 and D5.
#ifndef TB_PUMP_ON_LEVEL
#define TB_PUMP_ON_LEVEL HIGH
#endif

#ifndef TB_PUMP_OFF_LEVEL
#define TB_PUMP_OFF_LEVEL LOW
#endif

// Guarded on HIGH/LOW being defined, not on ARDUINO. Both symbols come from
// <Arduino.h>, and the guard translation units deliberately do not include it
// so that they stay host-testable. ARDUINO is defined for every file in an
// Arduino build regardless, so keying off it made this check fire in exactly
// the units where HIGH and LOW are absent: an undefined identifier is 0 in a
// preprocessor expression, so `HIGH == LOW` became `0 == 0`. The result was
// that the whole firmware failed to compile for the board.
#if defined(HIGH) && defined(LOW) && (TB_PUMP_ON_LEVEL == TB_PUMP_OFF_LEVEL)
#error "TB_PUMP_ON_LEVEL and TB_PUMP_OFF_LEVEL must differ"
#endif

// ---------------------------------------------------------------------------
// Grow-light output (D5). A latch, not a dose.
//
// The pump interlocks do not transfer. G1 bounds a run because water
// accumulates in the pot; light does not accumulate, so there is no equivalent
// ceiling. G2 bounds re-dosing because the substrate needs time to take it up;
// a lamp has no analogue. What remains is the dead-man, and it needs a window
// two orders of magnitude longer: the light is meant to stay on for a whole
// photoperiod, and a lamp stuck on for five minutes costs electricity while a
// pump stuck on for five minutes floods a room.
//
// The daily on-time ceiling is a horticultural policy, not a physical
// interlock, so it lives on the gateway where it can be weighed against the
// day's accumulated DLI. Expressing it here would need a wall clock this board
// does not have.
// ---------------------------------------------------------------------------

#ifndef TB_LED_ENABLED
#define TB_LED_ENABLED 1
#endif

// D5 drives the gate of the grow-light MOSFET, active HIGH.
#ifndef TB_LED_PIN
#define TB_LED_PIN 5
#endif

#ifndef TB_LED_ON_LEVEL
#define TB_LED_ON_LEVEL HIGH
#endif

#ifndef TB_LED_OFF_LEVEL
#define TB_LED_OFF_LEVEL LOW
#endif

// LED dead-man. While the light is on, any inbound serial byte proves the host
// is alive; silence for this long turns it off. Five minutes leaves room for
// four consecutive missed ticks at the gateway's 60 s LED keep-alive cadence.
#ifndef TB_LED_HOST_TIMEOUT_MS
#define TB_LED_HOST_TIMEOUT_MS 300000UL
#endif

// Keyed on HIGH/LOW being defined, not on ARDUINO - see the pump equivalent
// above for why the difference is what makes the firmware compile at all.
#if defined(HIGH) && defined(LOW) && TB_LED_ENABLED && (TB_LED_ON_LEVEL == TB_LED_OFF_LEVEL)
#error "TB_LED_ON_LEVEL and TB_LED_OFF_LEVEL must differ"
#endif

#if TB_LED_ENABLED && (TB_LED_PIN == TB_PUMP_PIN)
#error "TB_LED_PIN and TB_PUMP_PIN must not be the same pin"
#endif

// A shared pin would make a sensor read toggle an actuator, or an actuator
// write corrupt a sensor bus. Cheap to check, expensive to discover in a pot.
#if TB_LED_ENABLED && (TB_LED_PIN == TB_DHT22_PIN)
#error "TB_LED_PIN collides with TB_DHT22_PIN"
#endif
#if TB_PUMP_PIN == TB_DHT22_PIN
#error "TB_PUMP_PIN collides with TB_DHT22_PIN"
#endif
#if TB_SOIL_TEMPERATURE_ENABLED && TB_LED_ENABLED && \
    (TB_LED_PIN == TB_SOIL_TEMPERATURE_PIN)
#error "TB_LED_PIN collides with TB_SOIL_TEMPERATURE_PIN"
#endif
#if TB_SOIL_TEMPERATURE_ENABLED && (TB_PUMP_PIN == TB_SOIL_TEMPERATURE_PIN)
#error "TB_PUMP_PIN collides with TB_SOIL_TEMPERATURE_PIN"
#endif

// The two dead-man windows must not be equal, and the LED's must be the longer.
// The gateway ticks at a cadence chosen to sit between them: fast enough to
// hold the light on, slow enough that the silence which stops an orphaned pump
// run still happens. Collapse the two and that silence disappears, because G3
// counts bytes and does not care which actuator they were meant for.
#if TB_LED_ENABLED && (TB_LED_HOST_TIMEOUT_MS <= TB_HOST_TIMEOUT_MS)
#error "TB_LED_HOST_TIMEOUT_MS must exceed TB_HOST_TIMEOUT_MS"
#endif

// Below a minute the light would chatter on ordinary link jitter.
#if TB_LED_ENABLED && (TB_LED_HOST_TIMEOUT_MS < 60000UL)
#error "TB_LED_HOST_TIMEOUT_MS below 60s makes the light chatter on link jitter"
#endif

// Inbound serial line buffer. The longest line the contract defines is a pump
// command carrying a full 26-character ULID, around 80 bytes; the remainder is
// headroom for optional keys. This is sized independently of the dataset
// logger's 16-byte verb buffer, which cannot hold a JSON line at all.
#ifndef TB_SERIAL_RX_LINE_MAX
#define TB_SERIAL_RX_LINE_MAX 96
#endif

#if TB_SERIAL_RX_LINE_MAX < 80
#error "TB_SERIAL_RX_LINE_MAX must hold a full pump command line"
#endif
