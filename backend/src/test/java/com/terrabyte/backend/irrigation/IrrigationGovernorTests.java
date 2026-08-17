package com.terrabyte.backend.irrigation;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

import com.terrabyte.backend.measurement.MeasurementMetric;
import com.terrabyte.backend.measurement.MeasurementPoint;
import com.terrabyte.backend.measurement.MeasurementStore;
import com.terrabyte.backend.measurement.TelemetrySample;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/**
 * The ten scenarios from docs/design/edge_ai_hardening.md §개선 1.
 *
 * <p>The failure condition stated there is absolute: if any combination of
 * inputs produces an approval above 200 mL in one dose or 600 mL in 24 hours,
 * the Governor is wrong. Several tests below exist only to hold that line.
 *
 * <p>Hand-rolled fakes rather than a mocking framework, matching the style of
 * the existing pure unit tests in this repo.
 */
class IrrigationGovernorTests {

    private static final long POT_ID = 42L;
    private static final Instant NOW = Instant.parse("2026-08-17T10:00:00Z");

    private FakeMeasurementStore measurements;
    private FakeCommandRepository commands;
    private RecordingDecisionRepository decisions;
    private IrrigationGovernor governor;

    @BeforeEach
    void setUp() {
        measurements = new FakeMeasurementStore();
        commands = new FakeCommandRepository();
        decisions = new RecordingDecisionRepository();
        measurements.sample = sample(NOW.minusSeconds(30), 22.0, true);
        governor = newGovernor(defaults());
    }

    private IrrigationGovernor newGovernor(IrrigationProperties properties) {
        return new IrrigationGovernor(
                properties,
                measurements,
                commands,
                decisions,
                new CommandIdGenerator(),
                Clock.fixed(NOW, ZoneOffset.UTC));
    }

    private static IrrigationProperties defaults() {
        return new IrrigationProperties(
                Duration.ofMinutes(10), Duration.ofHours(6), 600, 20, 200,
                8.0, Duration.ofMinutes(2), 30.0, Duration.ofSeconds(60));
    }

    private static TelemetrySample sample(Instant observedAt, double moisture, boolean valid) {
        return new TelemetrySample(
                POT_ID, 1L, "node-1", "lettuce", "orangepi-pro-01", "evt-1",
                observedAt, 1L, moisture, 0L, 24.0, 55.0, 300.0, 21.0,
                valid, true, true, null);
    }

    private static IrrigationRequest request(int ml) {
        return IrrigationRequest.automatic(POT_ID, ml, CommandSource.RULE_AI, "evt-1");
    }

    // -- scenario 1 ------------------------------------------------------

    @Test
    void staleSampleIsRefusedAndNoCommandIsIssued() {
        measurements.sample = sample(NOW.minus(Duration.ofMinutes(11)), 22.0, true);

        AuthorizationResult result = governor.authorize(request(100));

        assertThat(result).isInstanceOf(AuthorizationResult.Denied.class);
        assertThat(((AuthorizationResult.Denied) result).reason()).isEqualTo(DenyReason.INPUT_STALE);
        assertThat(commands.saved).isEmpty();
    }

    @Test
    void aPotThatHasNeverReportedIsRefused() {
        measurements.sample = null;
        assertThat(denyReason(governor.authorize(request(100)))).isEqualTo(DenyReason.INPUT_STALE);
    }

    // -- scenario 2 ------------------------------------------------------

    @Test
    void invalidSoilSensorIsRefused() {
        measurements.sample = sample(NOW.minusSeconds(30), 22.0, false);
        assertThat(denyReason(governor.authorize(request(100))))
                .isEqualTo(DenyReason.SENSOR_INVALID);
    }

    @Test
    void impossibleMoistureValueIsRefused() {
        measurements.sample = sample(NOW.minusSeconds(30), 140.0, true);
        assertThat(denyReason(governor.authorize(request(100))))
                .isEqualTo(DenyReason.SENSOR_INVALID);
    }

    // -- scenario 3 (gate 3) ---------------------------------------------

    @Test
    void aThirtyPointJumpWithinAMinuteIsRefused() {
        measurements.points = List.of(new MeasurementPoint(NOW.minusSeconds(40), 60.0));
        assertThat(denyReason(governor.authorize(request(100))))
                .isEqualTo(DenyReason.IMPLAUSIBLE_JUMP);
    }

    @Test
    void aGradualChangeIsNotAJump() {
        measurements.points = List.of(new MeasurementPoint(NOW.minusSeconds(40), 30.0));
        assertThat(governor.authorize(request(100))).isInstanceOf(AuthorizationResult.Granted.class);
    }

    // -- scenario 3 in the doc's numbering: cooldown ----------------------

    @Test
    void fiveHoursFiftyNineMinutesAfterTheLastIrrigationIsRefused() {
        commands.lastCompleted = NOW.minus(Duration.ofHours(5).plusMinutes(59));
        assertThat(denyReason(governor.authorize(request(100)))).isEqualTo(DenyReason.COOLDOWN);
    }

    @Test
    void sixHoursAndOneMinuteAfterTheLastIrrigationIsAllowed() {
        commands.lastCompleted = NOW.minus(Duration.ofHours(6).plusMinutes(1));
        assertThat(governor.authorize(request(100))).isInstanceOf(AuthorizationResult.Granted.class);
    }

    // -- scenario 4 ------------------------------------------------------

    @Test
    void anOutstandingCommandBlocksAnother() {
        commands.outstandingUntil = NOW.plusSeconds(60);
        assertThat(denyReason(governor.authorize(request(100)))).isEqualTo(DenyReason.IN_FLIGHT);
    }

    // -- scenario 5 ------------------------------------------------------

    @Test
    void aRequestBeyondTheRemainingBudgetIsTrimmedToWhatIsLeft() {
        commands.consumedMl = 550;

        AuthorizationResult result = governor.authorize(request(200));

        AuthorizationResult.Granted granted = asGranted(result);
        assertThat(granted.grant().grantedMl()).isEqualTo(50);
        assertThat(granted.clampReason()).isEqualTo(ClampReason.DAILY_BUDGET);
    }

    // -- scenario 6 ------------------------------------------------------

    @Test
    void anExhaustedBudgetRefuses() {
        commands.consumedMl = 600;
        assertThat(denyReason(governor.authorize(request(100))))
                .isEqualTo(DenyReason.DAILY_BUDGET);
    }

    @Test
    void aRemainderTooSmallToBeUsefulRefusesRatherThanRoundingUp() {
        // 595 consumed leaves 5 mL. Raising that to the 20 mL floor would spend
        // budget the gate just said was not there.
        commands.consumedMl = 595;
        assertThat(denyReason(governor.authorize(request(100))))
                .isEqualTo(DenyReason.DAILY_BUDGET);
    }

    // -- scenario 7 ------------------------------------------------------

    @Test
    void anAbsurdlyLargeRequestIsClampedToTheDoseCeiling() {
        AuthorizationResult.Granted granted = asGranted(governor.authorize(request(99_999)));
        assertThat(granted.grant().grantedMl()).isEqualTo(200);
        assertThat(granted.clampReason()).isEqualTo(ClampReason.MAX_DOSE);
    }

    // -- scenario 8 ------------------------------------------------------

    @Test
    void aNegativeRequestIsRefusedNotCoerced() {
        assertThat(denyReason(governor.authorize(request(-5))))
                .isEqualTo(DenyReason.AI_OUT_OF_RANGE);
    }

    @Test
    void aZeroRequestIsRefused() {
        assertThat(denyReason(governor.authorize(request(0))))
                .isEqualTo(DenyReason.AI_OUT_OF_RANGE);
    }

    // -- scenario 9 ------------------------------------------------------

    @Test
    void manualOverrideSkipsCooldownButNotTheBudget() {
        commands.lastCompleted = NOW.minusSeconds(60);
        commands.consumedMl = 600;

        IrrigationRequest manual = new IrrigationRequest(
                POT_ID, 100, CommandSource.MANUAL, "evt-1", true, "데모 시연", null, null);

        assertThat(denyReason(governor.authorize(manual))).isEqualTo(DenyReason.DAILY_BUDGET);
    }

    @Test
    void manualOverrideAloneClearsTheCooldown() {
        commands.lastCompleted = NOW.minusSeconds(60);

        IrrigationRequest manual = new IrrigationRequest(
                POT_ID, 100, CommandSource.MANUAL, "evt-1", true, "데모 시연", null, null);

        assertThat(governor.authorize(manual)).isInstanceOf(AuthorizationResult.Granted.class);
    }

    @Test
    void anOverrideWithoutAReasonIsRefused() {
        commands.lastCompleted = NOW.minusSeconds(60);

        IrrigationRequest manual = new IrrigationRequest(
                POT_ID, 100, CommandSource.MANUAL, "evt-1", true, "  ", null, null);

        assertThat(denyReason(governor.authorize(manual))).isEqualTo(DenyReason.COOLDOWN);
    }

    // -- "다시 가능한 시간" (issue #48 완료 기준) --------------------------

    @Test
    void aCooldownRefusalSaysWhenItLifts() {
        Instant lastWatered = NOW.minus(Duration.ofHours(2));
        commands.lastCompleted = lastWatered;

        AuthorizationResult.Denied denied = asDenied(governor.authorize(request(100)));

        assertThat(denied.reason()).isEqualTo(DenyReason.COOLDOWN);
        assertThat(denied.nextAvailableAt()).isEqualTo(lastWatered.plus(Duration.ofHours(6)));
    }

    @Test
    void anInFlightRefusalSaysWhenTheCommandExpires() {
        Instant expiry = NOW.plusSeconds(90);
        commands.outstandingUntil = expiry;

        AuthorizationResult.Denied denied = asDenied(governor.authorize(request(100)));

        assertThat(denied.reason()).isEqualTo(DenyReason.IN_FLIGHT);
        assertThat(denied.nextAvailableAt()).isEqualTo(expiry);
    }

    @Test
    void aBudgetRefusalSaysWhenTheWindowReleasesVolume() {
        Instant oldest = NOW.minus(Duration.ofHours(20));
        commands.consumedMl = 600;
        commands.oldestCounted = oldest;

        AuthorizationResult.Denied denied = asDenied(governor.authorize(request(100)));

        assertThat(denied.reason()).isEqualTo(DenyReason.DAILY_BUDGET);
        assertThat(denied.nextAvailableAt()).isEqualTo(oldest.plus(Duration.ofHours(24)));
    }

    @Test
    void aSensorRefusalHasNoTimeBecauseNoTimerFixesIt() {
        // Stale and broken readings clear when good data arrives, which no
        // clock can predict. Inventing a time would be a promise we cannot keep.
        measurements.sample = sample(NOW.minusSeconds(30), 22.0, false);

        AuthorizationResult.Denied denied = asDenied(governor.authorize(request(100)));

        assertThat(denied.reason()).isEqualTo(DenyReason.SENSOR_INVALID);
        assertThat(denied.nextAvailableAt()).isNull();
    }

    // -- AI 귀책 기록 (#55) ------------------------------------------------

    @Test
    void theModelAndItsNumberAreRecordedOnAGrant() {
        governor.authorize(IrrigationRequest.fromModel(
                POT_ID, 120, CommandSource.RULE_AI, "evt-1", "irrigation-reg-v1", 120));

        IrrigationDecision decision = decisions.saved.get(0);
        assertThat(decision.aiModelVersion()).isEqualTo("irrigation-reg-v1");
        assertThat(decision.aiRequestedMl()).isEqualTo(120);
        assertThat(decision.grantedMl()).isEqualTo(120);
    }

    @Test
    void aClampedGrantKeepsWhatTheModelOriginallyAsked() {
        // Without this pairing there is no way to tell afterwards whether the
        // model is oversized or the envelope is undersized.
        governor.authorize(IrrigationRequest.fromModel(
                POT_ID, 260, CommandSource.RULE_AI, "evt-1", "irrigation-reg-v1", 260));

        IrrigationDecision decision = decisions.saved.get(0);
        assertThat(decision.aiRequestedMl()).isEqualTo(260);
        assertThat(decision.grantedMl()).isEqualTo(200);
        assertThat(decision.clampReason()).isEqualTo(ClampReason.MAX_DOSE);
    }

    @Test
    void aRefusalAlsoRecordsWhatTheModelAsked() {
        commands.consumedMl = 600;

        governor.authorize(IrrigationRequest.fromModel(
                POT_ID, 150, CommandSource.RULE_AI, "evt-1", "irrigation-reg-v1", 150));

        IrrigationDecision decision = decisions.saved.get(0);
        assertThat(decision.denyReason()).isEqualTo(DenyReason.DAILY_BUDGET);
        assertThat(decision.aiModelVersion()).isEqualTo("irrigation-reg-v1");
        assertThat(decision.aiRequestedMl()).isEqualTo(150);
    }

    @Test
    void aManualRequestCarriesNoModelAttribution() {
        governor.authorize(new IrrigationRequest(
                POT_ID, 100, CommandSource.MANUAL, "evt-1", false, null, null, null));

        IrrigationDecision decision = decisions.saved.get(0);
        assertThat(decision.aiModelVersion()).isNull();
        assertThat(decision.aiRequestedMl()).isNull();
    }

    // -- the failure condition -------------------------------------------

    @Test
    void noInputCombinationEverExceedsTheDoseCeilingOrTheDailyBudget() {
        int[] requests = {1, 19, 20, 21, 199, 200, 201, 599, 600, 601, 99_999, Integer.MAX_VALUE};
        int[] consumed = {0, 1, 400, 550, 580, 599, 600, 700};

        for (int requested : requests) {
            for (int already : consumed) {
                commands.consumedMl = already;
                AuthorizationResult result = governor.authorize(request(requested));
                if (result instanceof AuthorizationResult.Granted granted) {
                    int ml = granted.grant().grantedMl();
                    assertThat(ml)
                            .as("dose ceiling, requested=%d consumed=%d", requested, already)
                            .isBetween(20, 200);
                    assertThat(already + ml)
                            .as("24h budget, requested=%d consumed=%d", requested, already)
                            .isLessThanOrEqualTo(600);
                }
            }
        }
    }

    // -- every outcome is recorded ---------------------------------------

    @Test
    void everyDecisionIsPersistedIncludingRefusals() {
        governor.authorize(request(100));
        commands.consumedMl = 600;
        governor.authorize(request(100));

        assertThat(decisions.saved).hasSize(2);
        assertThat(decisions.saved.get(0).grantedMl()).isNotNull();
        assertThat(decisions.saved.get(1).denyReason()).isEqualTo(DenyReason.DAILY_BUDGET);
    }

    @Test
    void theGrantCarriesARuntimeDerivedFromTheFlowRate() {
        // 100 mL at 8 mL/s is 12.5 s. The firmware interlock is the real
        // backstop; this only has to be a sane upper bound.
        AuthorizationResult.Granted granted = asGranted(governor.authorize(request(100)));
        assertThat(granted.grant().maxRuntimeMs()).isEqualTo(12_500);
        assertThat(granted.grant().expiresAt()).isEqualTo(NOW.plus(Duration.ofMinutes(2)));
    }

    // -- configuration guards --------------------------------------------

    @Test
    void aDoseCeilingAboveTheDailyBudgetIsRejectedAtStartup() {
        assertThat(
                        org.junit.jupiter.api.Assertions.assertThrows(
                                IllegalArgumentException.class,
                                () -> new IrrigationProperties(
                                        Duration.ofMinutes(10), Duration.ofHours(6),
                                        100, 20, 200, 8.0, Duration.ofMinutes(2),
                                        30.0, Duration.ofSeconds(60))))
                .hasMessageContaining("dose-max-ml");
    }

    // -- helpers ---------------------------------------------------------

    private static AuthorizationResult.Denied asDenied(AuthorizationResult result) {
        assertThat(result).isInstanceOf(AuthorizationResult.Denied.class);
        return (AuthorizationResult.Denied) result;
    }

    private static DenyReason denyReason(AuthorizationResult result) {
        assertThat(result).isInstanceOf(AuthorizationResult.Denied.class);
        return ((AuthorizationResult.Denied) result).reason();
    }

    private static AuthorizationResult.Granted asGranted(AuthorizationResult result) {
        assertThat(result).isInstanceOf(AuthorizationResult.Granted.class);
        return (AuthorizationResult.Granted) result;
    }

    private static final class FakeMeasurementStore implements MeasurementStore {
        private TelemetrySample sample;
        private List<MeasurementPoint> points = List.of();

        @Override
        public void write(TelemetrySample s) {
            throw new UnsupportedOperationException();
        }

        @Override
        public Optional<TelemetrySample> findLatest(long potId) {
            return Optional.ofNullable(sample);
        }

        @Override
        public List<MeasurementPoint> findPoints(
                long potId, MeasurementMetric metric, Instant start) {
            return points;
        }
    }

    private static final class FakeCommandRepository extends DeviceCommandRepository {
        private final List<DeviceCommand> saved = new ArrayList<>();
        private Instant outstandingUntil;
        private Instant lastCompleted;
        private Instant oldestCounted;
        private int consumedMl;

        private FakeCommandRepository() {
            super(null);
        }

        @Override
        public void save(DeviceCommand command) {
            saved.add(command);
        }

        @Override
        public Optional<Instant> outstandingExpiresAt(long potId, Instant now) {
            return Optional.ofNullable(outstandingUntil);
        }

        @Override
        public Optional<Instant> oldestCountedIssuedAt(long potId, Instant since) {
            return Optional.ofNullable(oldestCounted);
        }

        @Override
        public Optional<Instant> lastCompletedAt(long potId) {
            return Optional.ofNullable(lastCompleted);
        }

        @Override
        public int consumedMlSince(long potId, Instant since) {
            return consumedMl;
        }
    }

    private static final class RecordingDecisionRepository extends IrrigationDecisionRepository {
        private final List<IrrigationDecision> saved = new ArrayList<>();

        private RecordingDecisionRepository() {
            super(null);
        }

        @Override
        public void save(IrrigationDecision decision) {
            saved.add(decision);
        }
    }
}
