package com.terrabyte.backend.pot;

import java.util.List;

import jakarta.validation.Valid;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/pots")
public class PotController {

    private final PotService potService;

    public PotController(PotService potService) {
        this.potService = potService;
    }

    @GetMapping
    public List<PotResponse> findAll(@AuthenticationPrincipal Jwt jwt) {
        return potService.findAll(Long.parseLong(jwt.getSubject()));
    }

    @GetMapping("/{potId}")
    public PotResponse findOne(@AuthenticationPrincipal Jwt jwt, @PathVariable long potId) {
        return potService.findOne(Long.parseLong(jwt.getSubject()), potId);
    }

    /** Turns the rule engine on or off for this pot. */
    @PatchMapping("/{potId}/auto-control")
    public PotResponse setAutoControl(
            @AuthenticationPrincipal Jwt jwt,
            @PathVariable long potId,
            @Valid @RequestBody AutoControlRequest request) {
        return potService.setAutoControl(
                Long.parseLong(jwt.getSubject()), potId, request.enabled());
    }

    @PatchMapping("/{potId}")
    public PotResponse update(
            @AuthenticationPrincipal Jwt jwt,
            @PathVariable long potId,
            @Valid @RequestBody UpdatePotRequest request) {
        return potService.update(Long.parseLong(jwt.getSubject()), potId, request);
    }
}
