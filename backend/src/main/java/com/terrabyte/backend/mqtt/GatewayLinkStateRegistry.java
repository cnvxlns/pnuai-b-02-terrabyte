package com.terrabyte.backend.mqtt;

import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

import org.springframework.stereotype.Component;

/**
 * What each gateway last said about its own link, and whether it may be commanded.
 *
 * <p>In memory rather than in a table, because {@code up/status} is retained:
 * the broker hands the current value of every gateway back on subscribe, so a
 * backend restart rebuilds this within one round trip. Persisting it would only
 * add a way for the database and the broker to disagree.
 *
 * <p>The state that matters is {@code RESYNC}. A gateway reports it while it is
 * still holding irrigation records this server has not received, and commanding
 * it then is the failure the whole edge-autonomy design exists to prevent: the
 * command was authorised against a daily budget that does not yet include water
 * already in the soil. {@code SAFE_HOLD} is refused for the blunter reason that
 * the gateway has said nothing may run at all.
 */
@Component
public class GatewayLinkStateRegistry {

    /**
     * States in which a command must not be published.
     *
     * <p>Deliberately a deny-list. A gateway running a build older than this
     * contract sends no state at all, and an allow-list would lock every one of
     * them out of commands — breaking the ordinary path to guard the rare one.
     * {@code EDGE_AUTONOMOUS} is absent on purpose: a gateway that believes we
     * are gone is best corrected by a command arriving.
     */
    private static final Set<String> REFUSING = Set.of("RESYNC", "SAFE_HOLD");

    private final Map<String, String> states = new ConcurrentHashMap<>();

    /** @param state the gateway's reported link state, or null when it sends none */
    public void record(String gatewayId, String state) {
        if (gatewayId == null || gatewayId.isBlank()) {
            return;
        }
        if (state == null || state.isBlank()) {
            states.remove(gatewayId);
            return;
        }
        states.put(gatewayId, state);
    }

    /**
     * Drops what a gateway said, for when it goes offline.
     *
     * <p>The Last Will carries no state, so without this a gateway that
     * disappeared mid-RESYNC would stay locked out of commands for the lifetime
     * of the process — long after it came back and drained its queue.
     */
    public void forget(String gatewayId) {
        states.remove(gatewayId);
    }

    /** Unknown gateways are commandable; only a reported refusal blocks. */
    public boolean acceptsCommands(String gatewayId) {
        String state = states.get(gatewayId);
        // Checked before the set, because Set.of is null-hostile: contains(null)
        // throws rather than answering false, and null is the ordinary case for
        // a gateway that has not reported yet.
        return state == null || !REFUSING.contains(state);
    }

    /** The last reported state, or null. For diagnostics and the status page. */
    public String stateOf(String gatewayId) {
        return states.get(gatewayId);
    }
}
