package com.terrabyte.backend.pot;

import java.time.Instant;
import java.util.List;
import java.util.Locale;

import com.terrabyte.backend.api.ApiException;
import com.terrabyte.backend.crop.Crop;
import com.terrabyte.backend.crop.CropRepository;
import com.terrabyte.backend.device.DeviceRepository;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class PotService {

    private final PotRepository potRepository;
    private final DeviceRepository deviceRepository;
    private final CropRepository cropRepository;

    public PotService(
            PotRepository potRepository,
            DeviceRepository deviceRepository,
            CropRepository cropRepository) {
        this.potRepository = potRepository;
        this.deviceRepository = deviceRepository;
        this.cropRepository = cropRepository;
    }

    public List<PotResponse> findAll(long userId) {
        return potRepository.findAllOwned(userId).stream().map(PotResponse::from).toList();
    }

    public PotResponse findOne(long userId, long potId) {
        return PotResponse.from(potRepository.findOwned(potId, userId)
                .orElseThrow(() -> new ApiException(
                        HttpStatus.NOT_FOUND, "POT_NOT_FOUND", "화분을 찾을 수 없습니다.")));
    }

    /**
     * Hands this pot to the rule engine, or takes it back.
     *
     * <p>Off means "I will decide when to water", not "nothing may run": manual
     * irrigation and light commands ignore this entirely. Only the periodic
     * evaluation in {@code RuleEngine} reads it.
     */
    @Transactional
    public PotResponse setAutoControl(long userId, long potId, boolean enabled) {
        Pot pot = potRepository.findOwned(potId, userId)
                .orElseThrow(() -> new ApiException(
                        HttpStatus.NOT_FOUND, "POT_NOT_FOUND", "화분을 찾을 수 없습니다."));
        potRepository.setAutoControl(pot.id(), enabled);
        return potRepository.findById(pot.id())
                .map(PotResponse::from)
                .orElseThrow(() -> new ApiException(
                        HttpStatus.NOT_FOUND, "POT_NOT_FOUND", "화분을 찾을 수 없습니다."));
    }

    @Transactional
    public PotResponse update(long userId, long potId, UpdatePotRequest request) {
        Pot pot = potRepository.findOwned(potId, userId)
                .orElseThrow(() -> new ApiException(
                        HttpStatus.NOT_FOUND, "POT_NOT_FOUND", "화분을 찾을 수 없습니다."));

        potRepository.updateLabel(pot.id(), request.label().trim());

        if (request.cropCode() != null && !request.cropCode().isBlank()) {
            String cropCode = request.cropCode().trim().toLowerCase(Locale.ROOT);
            Crop crop = cropRepository.findActiveByCode(cropCode)
                    .orElseThrow(() -> new ApiException(
                            HttpStatus.NOT_FOUND, "CROP_NOT_FOUND", "선택할 수 있는 작물을 찾을 수 없습니다."));
            potRepository.selectCrop(pot.id(), crop.code(), Instant.now());
        }

        return findOne(userId, pot.id());
    }

    @Transactional
    public PotResponse create(long userId, long deviceId, CreatePotRequest request) {
        deviceRepository.findByIdAndUserId(deviceId, userId)
                .orElseThrow(() -> new ApiException(
                        HttpStatus.NOT_FOUND, "DEVICE_NOT_FOUND", "기기를 찾을 수 없습니다."));

        String label = request.label().trim();
        String nodeId = request.nodeId() == null || request.nodeId().isBlank()
                ? null
                : request.nodeId().trim();
        Crop crop = request.cropCode() == null || request.cropCode().isBlank()
                ? null
                : cropRepository.findActiveByCode(request.cropCode().trim().toLowerCase(Locale.ROOT))
                        .orElseThrow(() -> new ApiException(
                                HttpStatus.NOT_FOUND, "CROP_NOT_FOUND", "선택할 수 있는 작물을 찾을 수 없습니다."));

        if (nodeId != null && potRepository.findByDeviceAndNode(deviceId, nodeId).isPresent()) {
            throw new ApiException(
                    HttpStatus.CONFLICT, "POT_NODE_ALREADY_ASSIGNED", "이미 연결된 화분 노드입니다.");
        }

        Pot created = potRepository.save(deviceId, nodeId, label);
        if (crop != null) {
            potRepository.selectCrop(created.id(), crop.code(), Instant.now());
            created = potRepository.findById(created.id())
                    .orElseThrow(() -> new IllegalStateException("Created pot could not be loaded"));
        }
        return PotResponse.from(created);
    }
}
