package com.terrabyte.backend.irrigation;

import java.util.List;

import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/** What the app reads to show "did my tap do anything". */
@RestController
@RequestMapping("/api/pots/{potId}")
public class CommandHistoryController {

    private final CommandHistoryService historyService;

    public CommandHistoryController(CommandHistoryService historyService) {
        this.historyService = historyService;
    }

    /**
     * Every command for this pot, newest first, refusals included.
     *
     * <p>The refusals are the point. A user who taps four times needs four rows;
     * showing only what succeeded makes a rejection indistinguishable from an
     * app that dropped the tap.
     */
    @GetMapping("/commands")
    public List<CommandHistoryEntry> commands(
            @AuthenticationPrincipal Jwt jwt,
            @PathVariable long potId,
            @RequestParam(defaultValue = "20") int limit) {
        return historyService.recent(potId, Long.parseLong(jwt.getSubject()), limit);
    }

    /** The last thing each actuator was told to do, and how it ended. */
    @GetMapping("/actuators")
    public ActuatorStatusResponse actuators(
            @AuthenticationPrincipal Jwt jwt, @PathVariable long potId) {
        return historyService.actuatorStatus(potId, Long.parseLong(jwt.getSubject()));
    }
}
