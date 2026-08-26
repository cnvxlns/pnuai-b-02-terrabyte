package com.terrabyte.backend.irrigation;

import java.util.List;

import com.terrabyte.backend.api.ApiException;
import com.terrabyte.backend.pot.PotRepository;

import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;

/**
 * Reads the command log back for the app.
 *
 * <p>Separate from {@link IrrigationService} because it issues nothing: keeping
 * the read path out of the class that authorises water means a change to how
 * history is displayed cannot reach the Governor by accident.
 */
@Service
public class CommandHistoryService {

    private static final int MAX_LIMIT = 100;

    private final DeviceCommandRepository commandRepository;
    private final PotRepository potRepository;

    public CommandHistoryService(
            DeviceCommandRepository commandRepository, PotRepository potRepository) {
        this.commandRepository = commandRepository;
        this.potRepository = potRepository;
    }

    public List<CommandHistoryEntry> recent(long potId, long userId, int limit) {
        requireOwnedPot(potId, userId);
        return commandRepository.findRecentByPot(potId, Math.clamp(limit, 1, MAX_LIMIT)).stream()
                .map(CommandHistoryEntry::from)
                .toList();
    }

    public ActuatorStatusResponse actuatorStatus(long potId, long userId) {
        requireOwnedPot(potId, userId);
        return new ActuatorStatusResponse(
                latest(potId, DeviceCommand.ACTUATOR_PUMP),
                latest(potId, DeviceCommand.ACTUATOR_LIGHT));
    }

    private CommandHistoryEntry latest(long potId, String actuator) {
        return commandRepository.findLatestByActuator(potId, actuator)
                .map(CommandHistoryEntry::from)
                .orElse(null);
    }

    /**
     * 404 rather than 403, matching every other pot lookup: a 403 would confirm
     * that a pot id exists, which turns the id space into something worth
     * enumerating.
     */
    private void requireOwnedPot(long potId, long userId) {
        potRepository
                .findOwned(potId, userId)
                .orElseThrow(() -> new ApiException(
                        HttpStatus.NOT_FOUND, "POT_NOT_FOUND", "화분을 찾을 수 없습니다."));
    }
}
