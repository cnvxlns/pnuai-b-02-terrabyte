package com.terrabyte.backend.measurement;

import java.time.Clock;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;

import com.terrabyte.backend.api.ApiException;
import com.terrabyte.backend.device.Device;
import com.terrabyte.backend.device.DeviceRepository;
import com.terrabyte.backend.device.DeviceStatus;
import com.terrabyte.backend.pot.Pot;
import com.terrabyte.backend.pot.PotRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class MeasurementService {

    private static final Logger LOGGER = LoggerFactory.getLogger(MeasurementService.class);

    private final DeviceRepository deviceRepository;
    private final PotRepository potRepository;
    private final MeasurementStore measurementStore;
    private final TelemetryEventRepository telemetryEventRepository;
    private final Clock clock;

    public MeasurementService(
            DeviceRepository deviceRepository,
            PotRepository potRepository,
            MeasurementStore measurementStore,
            TelemetryEventRepository telemetryEventRepository,
            Clock clock) {
        this.deviceRepository = deviceRepository;
        this.potRepository = potRepository;
        this.measurementStore = measurementStore;
        this.telemetryEventRepository = telemetryEventRepository;
        this.clock = clock;
    }

    /**
     * Ingest one telemetry v2 envelope.
     *
     * <p>{@code gatewayId} comes from the transport, not the payload. Over MQTT
     * it is parsed out of the topic, which the broker ACL already restricts to
     * the authenticated gateway — that is what replaced the shared
     * {@code X-Device-Key}. A payload that disagrees with the transport is
     * rejected rather than trusted.
     */
    @Transactional
    public TelemetryAcceptedResponse ingest(String gatewayId, TelemetryEnvelope envelope) {
        if (!envelope.gatewayId().equals(gatewayId)) {
            throw new ApiException(
                    HttpStatus.FORBIDDEN,
                    "GATEWAY_ID_MISMATCH",
                    "전송 경로의 게이트웨이와 페이로드의 게이트웨이가 다릅니다.");
        }
        validateObservedAt(envelope.observedAt());

        Device device = deviceRepository.findByHardwareId(gatewayId)
                .orElseThrow(() -> new ApiException(
                        HttpStatus.NOT_FOUND,
                        "DEVICE_NOT_FOUND",
                        "등록되지 않은 하드웨어 기기입니다."));

        Instant now = clock.instant();
        if (!telemetryEventRepository.claim(
                envelope.eventId(), gatewayId, envelope.observedAt(), now)) {
            // QoS 1 redelivery, or the edge retrying an event it never saw
            // acknowledged. Report success so the outbox stops retrying.
            LOGGER.debug("duplicate telemetry event_id={} ignored", envelope.eventId());
            return new TelemetryAcceptedResponse(
                    true, device.id(), gatewayId, envelope.observedAt(), 0L);
        }

        long sequence = 0L;
        for (TelemetryEnvelope.Node node : envelope.nodes()) {
            Pot pot = potRepository.findByDeviceAndNode(device.id(), node.nodeId())
                    .orElseGet(() -> bindNode(device, node.nodeId(), envelope.observedAt()));
            potRepository.markOnline(pot.id(), envelope.observedAt());
            measurementStore.write(TelemetrySample.from(
                    envelope, node, pot.id(), device.id(), pot.cropCode()));
            sequence = node.sequence();
        }
        deviceRepository.markOnline(device.id(), envelope.observedAt());

        return new TelemetryAcceptedResponse(
                true, device.id(), gatewayId, envelope.observedAt(), sequence);
    }

    /**
     * Record a gateway going online or offline.
     *
     * <p>Driven by the MQTT last will, so a gateway that loses power is marked
     * offline by the broker without the backend polling for it.
     */
    @Transactional
    public void updateGatewayPresence(String gatewayId, boolean online) {
        deviceRepository.findByHardwareId(gatewayId).ifPresentOrElse(
                device -> {
                    if (online) {
                        deviceRepository.markOnline(device.id(), clock.instant());
                    } else {
                        deviceRepository.markOffline(device.id(), clock.instant());
                        potRepository.markOfflineByDevice(device.id());
                    }
                    LOGGER.info("gateway presence gateway_id={} online={}", gatewayId, online);
                },
                () -> LOGGER.warn("presence for unknown gateway_id={}", gatewayId));
    }

    public LatestMeasurementsResponse latest(long userId, long potId) {
        Pot pot = ownedPot(userId, potId);
        TelemetrySample sample = measurementStore.findLatest(pot.id())
                .orElseThrow(() -> new ApiException(
                        HttpStatus.NOT_FOUND,
                        "MEASUREMENT_NOT_FOUND",
                        "아직 수신된 측정 데이터가 없습니다."));
        return LatestMeasurementsResponse.from(pot.id(), sample);
    }

    public MeasurementSeriesResponse series(
            long userId,
            long potId,
            String metricValue,
            String rangeValue) {
        Pot pot = ownedPot(userId, potId);
        MeasurementMetric metric = MeasurementMetric.from(metricValue);
        MeasurementRange range = MeasurementRange.from(rangeValue);
        return new MeasurementSeriesResponse(
                pot.id(),
                metric.field(),
                metric.unit(),
                range.value(),
                        measurementStore.findPoints(
                        pot.id(), metric, clock.instant().minus(range.duration())));
    }

    public DeviceSensorStatusResponse sensorStatus(long userId, long deviceId) {
        Device device = deviceRepository.findByIdAndUserId(deviceId, userId)
                .orElseThrow(() -> new ApiException(
                        HttpStatus.NOT_FOUND, "DEVICE_NOT_FOUND", "기기를 찾을 수 없습니다."));
        List<DeviceSensorStatusResponse.SensorStatus> sensors = new ArrayList<>();
        for (Pot pot : potRepository.findAllByDevice(device.id())) {
            TelemetrySample sample = measurementStore.findLatest(pot.id()).orElse(null);
            sensors.add(sensor(pot, "air", "온·습도 센서", "air_temperature_c,air_humidity_pct",
                    device, sample, sample != null && sample.airSensorValid()));
            sensors.add(sensor(pot, "light", "조도 센서", "plant_light_ppfd_umol_m2_s",
                    device, sample, sample != null && sample.lightSensorValid()));
            sensors.add(sensor(pot, "soil-moisture", "토양 수분 센서", "soil_moisture_pct",
                    device, sample, sample != null && sample.soilSensorValid()));
            sensors.add(sensor(pot, "soil-temperature", "토양 온도 센서", "soil_temperature_c",
                    device, sample, sample != null && sample.soilSensorValid() && sample.soilTemperatureC() != null));
        }
        return new DeviceSensorStatusResponse(device.id(), sensors);
    }

    @Deprecated
    public LatestMeasurementsResponse latestForDevice(long userId, long deviceId) {
        return latest(userId, representativePot(userId, deviceId).id());
    }

    @Deprecated
    public MeasurementSeriesResponse seriesForDevice(
            long userId,
            long deviceId,
            String metric,
            String range) {
        return series(userId, representativePot(userId, deviceId).id(), metric, range);
    }

    private Pot bindNode(Device device, String nodeId, Instant observedAt) {
        var unbound = potRepository.oldestUnbound(device.id());
        if (unbound.isPresent()) {
            Pot pot = unbound.get();
            if (potRepository.bind(pot.id(), nodeId, observedAt) != 1) {
                throw new ApiException(
                        HttpStatus.CONFLICT, "POT_BINDING_CONFLICT", "화분 노드 연결이 충돌했습니다.");
            }
            return potRepository.findById(pot.id())
                    .orElseThrow(() -> new IllegalStateException("Bound pot could not be loaded"));
        }
        int nextNumber = potRepository.findAllByDevice(device.id()).size() + 1;
        return potRepository.save(device.id(), nodeId, "화분 " + nextNumber);
    }

    private Pot representativePot(long userId, long deviceId) {
        return potRepository.representative(deviceId, userId)
                .orElseThrow(() -> new ApiException(
                        HttpStatus.NOT_FOUND, "DEVICE_NOT_FOUND", "기기를 찾을 수 없습니다."));
    }

    private DeviceSensorStatusResponse.SensorStatus sensor(
            Pot pot,
            String sensorKey,
            String label,
            String metric,
            Device device,
            TelemetrySample sample,
            boolean valid) {
        DeviceSensorStatusResponse.Status status = device.status() != DeviceStatus.ONLINE
                ? DeviceSensorStatusResponse.Status.OFFLINE
                : sample == null
                        ? DeviceSensorStatusResponse.Status.UNKNOWN
                        : valid
                                ? DeviceSensorStatusResponse.Status.ONLINE
                                : DeviceSensorStatusResponse.Status.UNAVAILABLE;
        return new DeviceSensorStatusResponse.SensorStatus(
                pot.id() + ":" + sensorKey,
                pot.id(),
                pot.label(),
                label,
                metric,
                status);
    }

    private Pot ownedPot(long userId, long potId) {
        return potRepository.findOwned(potId, userId)
                .orElseThrow(() -> new ApiException(
                        HttpStatus.NOT_FOUND, "POT_NOT_FOUND", "화분을 찾을 수 없습니다."));
    }

    private void validateObservedAt(Instant observedAt) {
        if (observedAt.isAfter(clock.instant().plusSeconds(300))) {
            throw new ApiException(
                    HttpStatus.BAD_REQUEST,
                    "OBSERVED_AT_IN_FUTURE",
                    "측정 시각이 현재 시각보다 너무 앞서 있습니다.");
        }
    }
}
