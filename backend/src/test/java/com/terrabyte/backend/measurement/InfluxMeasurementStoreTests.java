package com.terrabyte.backend.measurement;

import java.time.Instant;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.influxdb.client.InfluxDBClient;
import com.influxdb.client.QueryApi;
import com.influxdb.client.WriteApiBlocking;
import com.influxdb.client.write.Point;
import com.influxdb.query.FluxRecord;
import com.influxdb.query.FluxTable;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

class InfluxMeasurementStoreTests {

    private final InfluxDBClient client = mock(InfluxDBClient.class);
    private final InfluxMeasurementStore store = new InfluxMeasurementStore(
            client,
            new InfluxProperties("http://localhost", "token", "org", "bucket", "key"));

    @Test
    void omitsPpfdFieldWhenMeasurementIsMissing() {
        WriteApiBlocking writeApi = mock(WriteApiBlocking.class);
        when(client.getWriteApiBlocking()).thenReturn(writeApi);

        store.write(sample(null, false));

        ArgumentCaptor<Point> captor = ArgumentCaptor.forClass(Point.class);
        verify(writeApi).writePoint(captor.capture());
        assertThat(captor.getValue().getFields())
                .doesNotContainKey("plant_light_ppfd_umol_m2_s")
                .containsEntry("light_sensor_valid", false);
    }

    @Test
    void readsLatestRecordWithoutPpfdField() {
        QueryApi queryApi = mock(QueryApi.class);
        FluxTable table = new FluxTable();
        FluxRecord record = new FluxRecord(0);
        record.getValues().put("_time", Instant.parse("2026-08-15T00:00:00Z"));
        record.getValues().put("pot_id", "11");
        record.getValues().put("device_id", "22");
        record.getValues().put("node_id", "pot-01");
        record.getValues().put("crop_code", "lettuce");
        record.getValues().put("sequence", 7L);
        record.getValues().put("site_id", "pnu-lab");
        record.getValues().put("zone_id", "pot-01");
        record.getValues().put("soil_type", "loam");
        record.getValues().put("crop_type", "lettuce");
        record.getValues().put("calibration_version", "v1");
        record.getValues().put("soil_moisture_pct", 31.2);
        record.getValues().put("soil_moisture_raw_adc", 1847L);
        record.getValues().put("air_temperature_c", 27.1);
        record.getValues().put("air_humidity_pct", 58.0);
        record.getValues().put("soil_sensor_valid", true);
        record.getValues().put("air_sensor_valid", true);
        record.getValues().put("light_sensor_valid", false);
        table.getRecords().add(record);
        when(client.getQueryApi()).thenReturn(queryApi);
        when(queryApi.query(anyString())).thenReturn(List.of(table));

        TelemetrySample result = store.findLatest(11).orElseThrow();

        assertThat(result.plantLightPpfdUmolM2S()).isNull();
        assertThat(result.lightSensorValid()).isFalse();
    }

    private TelemetrySample sample(Double ppfd, boolean lightSensorValid) {
        return new TelemetrySample(
                11,
                22,
                "pot-01",
                "lettuce",
                "orangepi-01",
                Instant.parse("2026-08-15T00:00:00Z"),
                7,
                "pnu-lab",
                "pot-01",
                "loam",
                "lettuce",
                "v1",
                31.2,
                1847,
                27.1,
                58.0,
                ppfd,
                true,
                true,
                lightSensorValid);
    }
}
