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
                .addTag("site_id", sample.siteId())
                .addTag("zone_id", sample.zoneId())
                .addTag("soil_type", sample.soilType())
                .addTag("crop_type", sample.cropType())
                .addTag("calibration_version", sample.calibrationVersion())
                .addField("sequence", sample.sequence())
                .addField("soil_moisture_pct", sample.soilMoisturePct())
                .addField("soil_moisture_raw_adc", sample.soilMoistureRawAdc())
                .addField("air_temperature_c", sample.airTemperatureC())
                .addField("air_humidity_pct", sample.airHumidityPct())
                .addField("soil_sensor_valid", sample.soilSensorValid())
                .addField("air_sensor_valid", sample.airSensorValid())
                .addField("light_sensor_valid", sample.lightSensorValid())
                .time(sample.observedAt(), WritePrecision.NS);
        if (sample.plantLightPpfdUmolM2S() != null) {
            point.addField("plant_light_ppfd_umol_m2_s", sample.plantLightPpfdUmolM2S());
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
                null,
                record.getTime(),
                number(values.get("sequence")).longValue(),
                string(values, "site_id"),
                string(values, "zone_id"),
                string(values, "soil_type"),
                string(values, "crop_type"),
                string(values, "calibration_version"),
                number(values.get("soil_moisture_pct")).doubleValue(),
                number(values.get("soil_moisture_raw_adc")).longValue(),
                number(values.get("air_temperature_c")).doubleValue(),
                number(values.get("air_humidity_pct")).doubleValue(),
                nullableNumber(values.get("plant_light_ppfd_umol_m2_s")),
                Boolean.TRUE.equals(values.get("soil_sensor_valid")),
                Boolean.TRUE.equals(values.get("air_sensor_valid")),
                Boolean.TRUE.equals(values.get("light_sensor_valid")));
    }

    private Number number(Object value) {
        if (value instanceof Number number) {
            return number;
        }
        throw new IllegalStateException("InfluxDB numeric field is missing or invalid");
    }

    private Double nullableNumber(Object value) {
        return value == null ? null : number(value).doubleValue();
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
