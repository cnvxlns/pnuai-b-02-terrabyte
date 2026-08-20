package com.terrabyte.backend.score;

import java.util.List;

public record EnvironmentScorePotentialResponse(
        long potId,
        double current,
        double potential,
        double delta,
        List<ImprovedFactor> improvedFactors) {

    public record ImprovedFactor(
            String key,
            String label,
            double from,
            double to) {
    }
}
