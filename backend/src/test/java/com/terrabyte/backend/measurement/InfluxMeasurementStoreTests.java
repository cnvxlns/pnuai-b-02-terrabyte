package com.terrabyte.backend.measurement;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.Optional;

import com.influxdb.client.InfluxDBClient;
import com.influxdb.client.QueryApi;
import com.influxdb.client.WriteApiBlocking;
import com.influxdb.client.write.Point;
import com.influxdb.query.FluxRecord;
import com.influxdb.query.FluxTable;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

/**
 * What the edge's suggestion looks like once it is in InfluxDB, and what comes
 * back out.
 *
 * <p>The client is mocked rather than run against a broker: the assertion that
 * matters is the shape of the {@link Point} — specifically that the suggestion
 * lands in fields and not tags — and that is fully visible without a server.
 */
class InfluxMeasurementStoreTests {

    private static final long POT_ID = 42L;
    private static final Instant OBSERVED_AT = Instant.parse("2026-08-17T10:00:00Z");

    private InfluxDBClient client;
    private WriteApiBlocking writeApi;
    private QueryApi queryApi;
    private InfluxMeasurementStore store;

    @BeforeEach
    void setUp() {
        client = mock(InfluxDBClient.class);
        writeApi = mock(WriteApiBlocking.class);
        queryApi = mock(QueryApi.class);
        when(client.getWriteApiBlocking()).thenReturn(writeApi);
        when(client.getQueryApi()).thenReturn(queryApi);
        store = new InfluxMeasurementStore(
                client, new InfluxProperties("http://localhost:8086", "t", "terrabyte", "telemetry"));
    }

    @Test
    void theSuggestionIsWrittenAsFieldsNeverAsTags() {
        store.write(sample(new IrrigationSuggestion(118, "water-balance-v1", "lettuce", 3000)));

        Point point = writtenPoint();
        assertThat(point.getFields())
                .containsEntry("irrigation_suggestion_volume_ml", 118)
                .containsEntry("irrigation_suggestion_model_version", "water-balance-v1")
                .containsEntry("irrigation_suggestion_crop_code", "lettuce")
                .containsEntry("irrigation_suggestion_substrate_volume_ml", 3000);
        // A per-sample volume as a tag would mint one series per dose. The same
        // rule already keeps event_id out of the tag set.
        assertThat(point.getTags().keySet())
                .containsExactlyInAnyOrder("pot_id", "device_id", "node_id", "crop_code");
    }

    @Test
    void aSampleWithoutASuggestionWritesNoSuggestionFields() {
        store.write(sample(null));

        assertThat(writtenPoint().getFields().keySet())
                .noneMatch(field -> field.startsWith("irrigation_suggestion"));
    }

    @Test
    void theSuggestionRoundTripsThroughAQuery() {
        store.write(sample(new IrrigationSuggestion(118, "water-balance-v1", "lettuce", 3000)));
        stubQueryWith(writtenPoint().getFields());

        Optional<TelemetrySample> latest = store.findLatest(POT_ID);

        assertThat(latest).isPresent();
        assertThat(latest.get().irrigationSuggestion())
                .isEqualTo(new IrrigationSuggestion(118, "water-balance-v1", "lettuce", 3000));
    }

    @Test
    void anAbsentSuggestionReadsBackAsNullRatherThanAnEmptyOne() {
        store.write(sample(null));
        stubQueryWith(writtenPoint().getFields());

        assertThat(store.findLatest(POT_ID)).get()
                .extracting(TelemetrySample::irrigationSuggestion)
                .isNull();
    }

    @Test
    void aSuggestionStoredWithoutItsAssumptionsStillReadsBack() {
        // The edge may know its dose without knowing which crop the cloud thinks
        // is planted. Losing the volume because of that would be the wrong trade.
        store.write(sample(new IrrigationSuggestion(118, "water-balance-v1", null, null)));
        stubQueryWith(writtenPoint().getFields());

        assertThat(store.findLatest(POT_ID)).get()
                .extracting(TelemetrySample::irrigationSuggestion)
                .isEqualTo(new IrrigationSuggestion(118, "water-balance-v1", null, null));
    }

    private Point writtenPoint() {
        ArgumentCaptor<Point> captor = ArgumentCaptor.forClass(Point.class);
        org.mockito.Mockito.verify(writeApi, org.mockito.Mockito.atLeastOnce())
                .writePoint(captor.capture());
        return captor.getValue();
    }

    /** Feeds the written fields back as the pivoted row a Flux query returns. */
    private void stubQueryWith(Map<String, Object> fields) {
        FluxRecord record = new FluxRecord(0);
        record.getValues().put("_time", OBSERVED_AT);
        record.getValues().put("pot_id", Long.toString(POT_ID));
        record.getValues().put("device_id", "1");
        record.getValues().put("node_id", "node-1");
        record.getValues().put("crop_code", "lettuce");
        record.getValues().putAll(fields);

        FluxTable table = new FluxTable();
        table.getRecords().add(record);
        when(queryApi.query(anyString())).thenReturn(List.of(table));
    }

    private static TelemetrySample sample(IrrigationSuggestion suggestion) {
        return new TelemetrySample(
                POT_ID, 1L, "node-1", "lettuce", "orangepi-pro-01", "evt-1",
                OBSERVED_AT, 1L, 22.0, 1847L, 24.0, 55.0, 300.0, 21.0,
                true, true, true, suggestion);
    }
}
