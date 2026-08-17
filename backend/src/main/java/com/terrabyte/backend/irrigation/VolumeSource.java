package com.terrabyte.backend.irrigation;

/**
 * Where the requested volume came from.
 *
 * <p>Recorded on the outcome so an operator can answer "did anything actually
 * size this dose, or did we just reach for the table?" without re-deriving it
 * from the model version being null.
 */
public enum VolumeSource {
    /** The edge's own water-balance estimate, sent with the reading it was derived from. */
    EDGE_SUGGESTION,
    /** No usable suggestion, so the fixed pot-size table decided. */
    POT_SIZE_FALLBACK
}
