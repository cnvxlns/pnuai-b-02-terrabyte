package com.terrabyte.backend.pot;

import jakarta.validation.constraints.NotNull;

/**
 * @param enabled boxed and required on purpose: a primitive would read a missing
 *                field as false and silently take the pot out of automatic
 *                control, which is the direction that stops watering.
 */
public record AutoControlRequest(@NotNull(message = "enabled 는 필수입니다.") Boolean enabled) {
}
