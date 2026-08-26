package com.terrabyte.backend.irrigation;

import java.util.List;

import jakarta.validation.Valid;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.Size;

import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * Manual irrigation and the decision timeline.
 *
 * <p>The POST goes through {@link IrrigationService} and therefore through the
 * Governor. There is deliberately no endpoint that issues a command directly.
 *
 * <p>A refusal answers 409 rather than 400: the request was well-formed and the
 * server understood it, the current state just does not allow it. The client
 * shows the reason to the user.
 *
 * <p>Both endpoints are scoped to the caller's own pots. Being authenticated is
 * not enough here: this controller moves water in someone's home, and the
 * timeline says when their plants were dry. A pot belonging to another user
 * answers 404, not 403, so pot ids stay unenumerable.
 */
@RestController
@RequestMapping("/api/pots/{potId}/irrigation")
public class IrrigationController {

    private final IrrigationService irrigationService;

    public IrrigationController(IrrigationService irrigationService) {
        this.irrigationService = irrigationService;
    }

    public record ManualIrrigationRequest(
            @Positive(message = "관수량은 0보다 커야 합니다.") int volumeMl,
            boolean cooldownOverride,
            @Size(max = 200, message = "사유는 200자 이하여야 합니다.") String overrideReason) {}

    @PostMapping
    public ResponseEntity<IrrigationOutcome> water(
            @AuthenticationPrincipal Jwt jwt,
            @PathVariable long potId,
            @Valid @RequestBody ManualIrrigationRequest request) {

        IrrigationOutcome outcome = irrigationService.requestManual(
                potId, Long.parseLong(jwt.getSubject()), request.volumeMl(),
                request.cooldownOverride(), request.overrideReason());

        return outcome.granted()
                ? ResponseEntity.status(HttpStatus.CREATED).body(outcome)
                : ResponseEntity.status(HttpStatus.CONFLICT).body(outcome);
    }

    /**
     * Every decision for this pot, refusals included.
     *
     * <p>Refusals are the point: a pot that is never watered fails silently
     * unless someone can see the reasons stacking up.
     */
    @GetMapping("/timeline")
    public List<IrrigationTimelineEntry> timeline(
            @AuthenticationPrincipal Jwt jwt,
            @PathVariable long potId,
            @RequestParam(defaultValue = "20") int limit) {
        return irrigationService.timeline(potId, Long.parseLong(jwt.getSubject()), limit);
    }
}
