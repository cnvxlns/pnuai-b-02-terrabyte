package com.terrabyte.backend.measurement;

import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.Optional;

import com.influxdb.client.InfluxDBClient;
import com.influxdb.client.domain.WritePrecision;
import com.influxdb.client.write.Point;
import com.influxdb.query.FluxRecord;
import org.springframework.stereotype.Repository;

@Repository
public class InfluxMeasurementStore implements MeasurementStore {

    private static final String MEASUREMENT = "telemetry_sample";

    private final InfluxDBClient client;
    private final InfluxProperties properties;

    public InfluxMeasurementStore(InfluxDBClient client, InfluxProperties properties) {
        this.client = client;
        this.properties = properties;
    }

    @Override
    public void write(TelemetrySample sample) {
        Point point = Point.measurement(MEASUREMENT)
                .addTag("pot_id", Long.toString(sample.potId()))
                .addTag("device_id", Long.toString(sample.deviceId()))
                .addTag("node_id", sample.nodeId())
                .addTag("crop_code", sample.cropCode() == null ? "" : sample.cropCode())
                // event_id is a field, not a tag: it is unique per sample, and
                // tagging it would create one series per measurement.
                .addField("event_id", sample.eventId())
                .addField("gateway_hardware_id", sample.hardwareDeviceId())
                .addField("sequence", sample.sequence())
                .addField("soil_moisture_pct", sample.soilMoisturePct())
                .addField("soil_moisture_raw_adc", sample.soilMoistureRawAdc())
                .addField("air_temperature_c", sample.airTemperatureC())
                .addField("air_humidity_pct", sample.airHumidityPct())
                .addField("plant_light_ppfd_umol_m2_s", sample.plantLightPpfdUmolM2S())
                .addField("soil_sensor_valid", sample.soilSensorValid())
                .addField("air_sensor_valid", sample.airSensorValid())
                .addField("light_sensor_valid", sample.lightSensorValid())
                .time(sample.observedAt(), WritePrecision.NS);
        // Point has no null-safe addField: writing a null as 0.0 would poison
        // the series with a confident "0°C" for nodes that have no soil probe.
        if (sample.soilTemperatureC() != null) {
            point.addField("soil_temperature_c", sample.soilTemperatureC());
        }
        // Fields, not tags, for the same reason as event_id above: the volume
        // varies per sample, so tagging it would mint a new series per dose.
        // The two "assumed" values are near-constant and would be safe as tags,
        // but splitting one logical block across tags and fields makes the
        // read-back path depend on which half survived.
        IrrigationSuggestion suggestion = sample.irrigationSuggestion();
        if (suggestion != null && suggestion.volumeMl() != null) {
            point.addField("irrigation_suggestion_volume_ml", suggestion.volumeMl());
            addOptional(point, "irrigation_suggestion_model_version", suggestion.modelVersion());
            addOptional(point, "irrigation_suggestion_crop_code", suggestion.assumedCropCode());
            if (suggestion.assumedSubstrateVolumeMl() != null) {
                point.addField(
                        "irrigation_suggestion_substrate_volume_ml",
                        suggestion.assumedSubstrateVolumeMl());
            }
        }
        client.getWriteApiBlocking().writePoint(point);
    }

    @Override
    public Optional<TelemetrySample> findLatest(long potId) {
        String flux = """
                from(bucket: "%s")
                  |> range(start: 1970-01-01T00:00:00Z)
                  |> filter(fn: (r) => r._measurement == "%s" and r.pot_id == "%s")
                  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
                  |> sort(columns: ["_time"], desc: true)
                  |> limit(n: 1)
                """.formatted(
                escape(properties.bucket()),
                MEASUREMENT,
                potId);

        return records(flux).stream().findFirst().map(this::toSample);
    }

    @Override
    public List<MeasurementPoint> findPoints(
            long potId,
            MeasurementMetric metric,
            Instant start) {
        String flux = """
                from(bucket: "%s")
                  |> range(start: time(v: "%s"))
                  |> filter(fn: (r) => r._measurement == "%s" and r.pot_id == "%s")
                  |> filter(fn: (r) => r._field == "%s")
                  |> sort(columns: ["_time"])
                """.formatted(
                escape(properties.bucket()),
                start,
                MEASUREMENT,
                potId,
                metric.field());

        return records(flux).stream()
                .map(record -> new MeasurementPoint(
                        record.getTime(),
                        number(record.getValue()).doubleValue()))
                .toList();
    }

    private List<FluxRecord> records(String flux) {
        return client.getQueryApi().query(flux).stream()
                .flatMap(table -> table.getRecords().stream())
                .toList();
    }

    private TelemetrySample toSample(FluxRecord record) {
        Map<String, Object> values = record.getValues();
        return new TelemetrySample(
                Long.parseLong(string(values, "pot_id")),
                Long.parseLong(string(values, "device_id")),
                string(values, "node_id"),
                string(values, "crop_code"),
                optional(values, "gateway_hardware_id"),
                optional(values, "event_id"),
                record.getTime(),
                number(values.get("sequence")).longValue(),
                number(values.get("soil_moisture_pct")).doubleValue(),
                number(values.get("soil_moisture_raw_adc")).longValue(),
                number(values.get("air_temperature_c")).doubleValue(),
                number(values.get("air_humidity_pct")).doubleValue(),
                number(values.get("plant_light_ppfd_umol_m2_s")).doubleValue(),
                optionalNumber(values.get("soil_temperature_c")),
                Boolean.TRUE.equals(values.get("soil_sensor_valid")),
                Boolean.TRUE.equals(values.get("air_sensor_valid")),
                Boolean.TRUE.equals(values.get("light_sensor_valid")),
                toSuggestion(values));
    }

    /**
     * The volume is what makes a suggestion a suggestion; without it the other
     * three columns describe an estimate that was never made, so the whole block
     * reads back as absent.
     */
    private IrrigationSuggestion toSuggestion(Map<String, Object> values) {
        Integer volumeMl = optionalInt(values.get("irrigation_suggestion_volume_ml"));
        if (volumeMl == null) {
            return null;
        }
        return new IrrigationSuggestion(
                volumeMl,
                optional(values, "irrigation_suggestion_model_version"),
                optional(values, "irrigation_suggestion_crop_code"),
                optionalInt(values.get("irrigation_suggestion_substrate_volume_ml")));
    }

    private Number number(Object value) {
        if (value instanceof Number number) {
            return number;
        }
        throw new IllegalStateException("InfluxDB numeric field is missing or invalid");
    }

    // Unlike number(), this tolerates absence: a node with no soil probe never
    // writes soil_temperature_c, and that must read back as null, not 0.0.
    private Double optionalNumber(Object value) {
        return value instanceof Number number ? number.doubleValue() : null;
    }

    private Integer optionalInt(Object value) {
        return value instanceof Number number ? number.intValue() : null;
    }

    private void addOptional(Point point, String field, String value) {
        if (value != null && !value.isBlank()) {
            point.addField(field, value);
        }
    }

    private String optional(Map<String, Object> values, String key) {
        Object value = values.get(key);
        return value == null ? null : value.toString();
    }

    private String string(Map<String, Object> values, String key) {
        Object value = values.get(key);
        if (value == null) {
            throw new IllegalStateException("InfluxDB tag is missing: " + key);
        }
        return value.toString();
    }

    private String escape(String value) {
        return value.replace("\\", "\\\\").replace("\"", "\\\"");
    }
}
