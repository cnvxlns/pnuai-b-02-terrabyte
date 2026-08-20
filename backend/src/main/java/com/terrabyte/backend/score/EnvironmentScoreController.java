package com.terrabyte.backend.score;

import com.terrabyte.backend.api.ApiException;
import com.terrabyte.backend.pot.Pot;
import com.terrabyte.backend.pot.PotRepository;
import org.springframework.http.HttpStatus;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api")
public class EnvironmentScoreController {

    private final EnvironmentScoreService service;
    private final PotRepository potRepository;

    public EnvironmentScoreController(
            EnvironmentScoreService service,
            PotRepository potRepository) {
        this.service = service;
        this.potRepository = potRepository;
    }

    @GetMapping("/pots/{potId}/score")
    public EnvironmentScoreResponse latest(
            @AuthenticationPrincipal Jwt jwt,
            @PathVariable long potId) {
        return service.latest(Long.parseLong(jwt.getSubject()), potId);
    }

    @GetMapping("/pots/{potId}/score/potential")
    public EnvironmentScorePotentialResponse potential(
            @AuthenticationPrincipal Jwt jwt,
            @PathVariable long potId) {
        return service.potential(Long.parseLong(jwt.getSubject()), potId);
    }

    @GetMapping("/pots/{potId}/crop-recommendations")
    public java.util.List<CropRecommendationResponse> cropRecommendations(
            @AuthenticationPrincipal Jwt jwt,
            @PathVariable long potId) {
        return service.cropRecommendations(Long.parseLong(jwt.getSubject()), potId);
    }

    @GetMapping("/pots/{potId}/diagnostic-history")
    public java.util.List<DiagnosticHistoryRecord> diagnosticHistory(
            @AuthenticationPrincipal Jwt jwt,
            @PathVariable long potId) {
        return service.diagnosticHistory(Long.parseLong(jwt.getSubject()), potId);
    }

    @Deprecated
    @GetMapping("/devices/{deviceId}/score")
    public EnvironmentScoreResponse latestForDevice(
            @AuthenticationPrincipal Jwt jwt,
            @PathVariable long deviceId) {
        long userId = Long.parseLong(jwt.getSubject());
        Pot pot = potRepository.representative(deviceId, userId)
                .orElseThrow(() -> new ApiException(
                        HttpStatus.NOT_FOUND,
                        "DEVICE_NOT_FOUND",
                        "기기를 찾을 수 없습니다."));
        return service.latest(userId, pot.id());
    }
}
