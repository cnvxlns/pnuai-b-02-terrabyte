package com.terrabyte.backend.irrigation;

/**
 * Why a request was refused. One per gate, in gate order.
 *
 * <p>These are stored, not just logged: a pot that never gets watered is a
 * silent failure unless the refusals are visible somewhere.
 */
public enum DenyReason {
    /** Gate 1: the reading behind the decision is older than the allowed age. */
    INPUT_STALE,
    /** Gate 2: the soil probe reported itself invalid, or the value is impossible. */
    SENSOR_INVALID,
    /** Gate 3: soil moisture moved further in a minute than physics allows. */
    IMPLAUSIBLE_JUMP,
    /** Gate 4: watered too recently. Manual requests may override this one. */
    COOLDOWN,
    /** Gate 5: a command for this pot is already outstanding. */
    IN_FLIGHT,
    /** Gate 6: the 24-hour budget is exhausted. Nothing overrides this. */
    DAILY_BUDGET,
    /**
     * The requested volume was not a usable number.
     *
     * <p>The {@code AI_} prefix is historical — it predates the removal of the
     * model server, and the name is persisted in {@code irrigation_decision}, so
     * renaming it would rewrite stored history for no gain. It now covers any
     * source proposing a nonsensical volume.
     *
     * <p>Not a gate: it is how an out-of-range AI suggestion is refused rather
     * than clamped. Clamping 99999 to 200 would ship a plausible-looking value
     * from a broken model.
     */
    AI_OUT_OF_RANGE
}
