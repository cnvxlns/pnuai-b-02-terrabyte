package com.terrabyte.backend.rule;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyBoolean;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

import java.time.Clock;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Optional;

import com.terrabyte.backend.device.DeviceStatus;
import com.terrabyte.backend.irrigation.DenyReason;
import com.terrabyte.backend.irrigation.IrrigationOutcome;
import com.terrabyte.backend.irrigation.IrrigationService;
import com.terrabyte.backend.irrigation.LightDenyReason;
import com.terrabyte.backend.irrigation.LightOutcome;
import com.terrabyte.backend.irrigation.LightService;
import com.terrabyte.backend.measurement.MeasurementStore;
import com.terrabyte.backend.measurement.TelemetrySample;
import com.terrabyte.backend.pot.Pot;
import com.terrabyte.backend.pot.PotRepository;
import com.terrabyte.backend.score.CropScoreProfile;
import com.terrabyte.backend.score.CropScoreProfileRepository;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.Mockito;

/**
 * The periodic evaluation that turns readings into requests.
 *
 * <p>Deliberately thin. Every safety question — minimum interval, daily budget,
 * dose size, in-flight commands — already belongs to the Governor, and asking it
 * again here would be a second copy that drifts. What the engine owns is
 * <em>noticing</em>.
 */
class RuleEngineTests {

    private static final long POT_ID = 7L;
    private static final String CROP = "lettuce";
    private static final Instant NOON =
            Instant.parse("2026-08-27T03:00:00Z"); // 12:00 KST

    private PotRepository pots;
    private MeasurementStore measurements;
    private CropScoreProfileRepository profiles;
    private IrrigationService irrigation;
    private LightService light;
    private RuleEngine engine;

    @BeforeEach
    void setUp() {
        pots = Mockito.mock(PotRepository.class);
        measurements = Mockito.mock(MeasurementStore.class);
        profiles = Mockito.mock(CropScoreProfileRepository.class);
        irrigation = Mockito.mock(IrrigationService.class);
        light = Mockito.mock(LightService.class);

        // Both outcomes are records, so they are constructed rather than mocked.
        when(irrigation.requestAutomatic(anyLong(), anyString()))
                .thenReturn(IrrigationOutcome.denied(DenyReason.COOLDOWN, "쿨다운", null));
        when(light.requestAutomatic(anyLong(), anyBoolean(), anyString()))
                .thenReturn(LightOutcome.denied(true, LightDenyReason.IN_FLIGHT, "대기", null));
        when(profiles.findActiveByCropCode(CROP)).thenReturn(Optional.of(profile()));

        engine = buildEngine(NOON);
    }

    private RuleEngine buildEngine(Instant now) {
        return new RuleEngine(
                pots, measurements, profiles, irrigation, light,
                new RuleProperties(null, null, null, null, null, null, null),
                Clock.fixed(now, ZoneId.of("Asia/Seoul")));
    }

    // -- what gets looked at -----------------------------------------------

    @Test
    void aPotWithNoReadingIsLeftAlone() {
        given(pot(CROP, DeviceStatus.ONLINE, true));
        when(measurements.findLatest(POT_ID)).thenReturn(Optional.empty());

        engine.evaluateOnce();

        // Nothing to be right about. The Governor would refuse anyway, but
        // asking it would write a decision row for a pot nobody measured.
        verifyNoInteractions(irrigation);
        verifyNoInteractions(light);
    }

    // -- the irrigation rule -----------------------------------------------

    @Test
    void drySoilAsksTheGovernorForWater() {
        given(pot(CROP, DeviceStatus.ONLINE, true));
        when(measurements.findLatest(POT_ID)).thenReturn(Optional.of(sample(20.0, 400.0)));

        engine.evaluateOnce();

        verify(irrigation).requestAutomatic(eqPot(), anyString());
    }

    @Test
    void wetSoilAsksForNothing() {
        given(pot(CROP, DeviceStatus.ONLINE, true));
        when(measurements.findLatest(POT_ID)).thenReturn(Optional.of(sample(60.0, 400.0)));

        engine.evaluateOnce();

        verify(irrigation, never()).requestAutomatic(anyLong(), anyString());
    }

    @Test
    void anInvalidSoilProbeAsksForNothing() {
        given(pot(CROP, DeviceStatus.ONLINE, true));
        when(measurements.findLatest(POT_ID))
                .thenReturn(Optional.of(sample(20.0, 400.0, false, true)));

        engine.evaluateOnce();

        // A probe reading 20 % while reporting itself broken is not evidence of
        // dry soil; it is evidence of a broken probe.
        verify(irrigation, never()).requestAutomatic(anyLong(), anyString());
    }

    @Test
    void everyRequestCarriesACorrelationId() {
        given(pot(CROP, DeviceStatus.ONLINE, true));
        when(measurements.findLatest(POT_ID)).thenReturn(Optional.of(sample(20.0, 400.0)));

        engine.evaluateOnce();

        var correlationId = org.mockito.ArgumentCaptor.forClass(String.class);
        verify(irrigation).requestAutomatic(anyLong(), correlationId.capture());
        assertThat(correlationId.getValue()).isNotBlank();
    }

    // -- the light rule ----------------------------------------------------

    @Test
    void dimLightInsideThePhotoperiodTurnsTheLampOn() {
        given(pot(CROP, DeviceStatus.ONLINE, true));
        when(measurements.findLatest(POT_ID)).thenReturn(Optional.of(sample(60.0, 50.0)));

        engine.evaluateOnce();

        verify(light).requestAutomatic(eqPot(), org.mockito.ArgumentMatchers.eq(true), anyString());
    }

    @Test
    void brightLightTurnsTheLampOff() {
        given(pot(CROP, DeviceStatus.ONLINE, true));
        when(measurements.findLatest(POT_ID)).thenReturn(Optional.of(sample(60.0, 700.0)));

        engine.evaluateOnce();

        verify(light).requestAutomatic(eqPot(), org.mockito.ArgumentMatchers.eq(false), anyString());
    }

    @Test
    void lightInsideTheOptimalBandChangesNothing() {
        given(pot(CROP, DeviceStatus.ONLINE, true));
        when(measurements.findLatest(POT_ID)).thenReturn(Optional.of(sample(60.0, 300.0)));

        engine.evaluateOnce();

        // Hysteresis. Without a band, a reading hovering on one threshold would
        // toggle the lamp on every pass.
        verify(light, never()).requestAutomatic(anyLong(), anyBoolean(), anyString());
    }

    @Test
    void theLampIsOffAtNightHoweverDarkItIs() {
        engine = buildEngine(Instant.parse("2026-08-27T15:00:00Z")); // 00:00 KST
        given(pot(CROP, DeviceStatus.ONLINE, true));
        when(measurements.findLatest(POT_ID)).thenReturn(Optional.of(sample(60.0, 0.0)));

        engine.evaluateOnce();

        // Plants need the dark as much as the light. A rule that only chases
        // PPFD would run the lamp all night and stop them flowering.
        verify(light).requestAutomatic(eqPot(), org.mockito.ArgumentMatchers.eq(false), anyString());
    }

    @Test
    void thePhotoperiodIsReadInTheGrowersZoneNotTheClocksZone() {
        // The shared Clock bean is Clock.systemUTC(), so LocalTime.now(clock)
        // is UTC no matter where the greenhouse is. Read that way, a 06:00-22:00
        // window in Seoul becomes 15:00-07:00 local — the lamp runs all night
        // and sits dark all day, which is precisely backwards.
        engine = new RuleEngine(
                pots, measurements, profiles, irrigation, light,
                new RuleProperties(null, null, null, null, null, null, null),
                // 04:36 KST, well outside the window. 19:36 UTC, well inside it.
                Clock.fixed(Instant.parse("2026-08-26T19:36:00Z"), ZoneOffset.UTC));
        given(pot(CROP, DeviceStatus.ONLINE, true));
        when(measurements.findLatest(POT_ID)).thenReturn(Optional.of(sample(60.0, 0.0)));

        engine.evaluateOnce();

        verify(light).requestAutomatic(eqPot(), org.mockito.ArgumentMatchers.eq(false), anyString());
    }

    @Test
    void aPotWithNoLightReadingIsLeftAlone() {
        given(pot(CROP, DeviceStatus.ONLINE, true));
        when(measurements.findLatest(POT_ID))
                .thenReturn(Optional.of(sample(60.0, null, true, true)));

        engine.evaluateOnce();

        verify(light, never()).requestAutomatic(anyLong(), anyBoolean(), anyString());
    }

    @Test
    void aCropWithNoScoreProfileGetsNoLightRule() {
        given(pot("no-such-crop", DeviceStatus.ONLINE, true));
        when(measurements.findLatest(POT_ID)).thenReturn(Optional.of(sample(60.0, 50.0)));
        when(profiles.findActiveByCropCode("no-such-crop")).thenReturn(Optional.empty());

        engine.evaluateOnce();

        // The thresholds are the crop's, not the engine's. With no profile there
        // is no threshold, and inventing one would light every pot the same.
        verify(light, never()).requestAutomatic(anyLong(), anyBoolean(), anyString());
    }

    // -- not repeating itself ----------------------------------------------

    @Test
    void anUnchangedLampStateIsNotCommandedAgain() {
        given(pot(CROP, DeviceStatus.ONLINE, true));
        when(measurements.findLatest(POT_ID)).thenReturn(Optional.of(sample(60.0, 50.0)));

        engine.evaluateOnce();
        engine.evaluateOnce();
        engine.evaluateOnce();

        // Once, not once per pass. A minute-by-minute re-send of a state the
        // lamp is already in fills device_command with 1,440 rows a day per pot,
        // buries real commands in the history screen, and makes the edge relay
        // journal every one of them.
        verify(light, times(1))
                .requestAutomatic(eqPot(), org.mockito.ArgumentMatchers.eq(true), anyString());
    }

    @Test
    void aChangedLampStateIsCommanded() {
        given(pot(CROP, DeviceStatus.ONLINE, true));
        when(measurements.findLatest(POT_ID)).thenReturn(Optional.of(sample(60.0, 50.0)));
        engine.evaluateOnce();

        // The sun came out; the band says the lamp is no longer needed.
        when(measurements.findLatest(POT_ID)).thenReturn(Optional.of(sample(60.0, 700.0)));
        engine.evaluateOnce();

        verify(light).requestAutomatic(eqPot(), org.mockito.ArgumentMatchers.eq(true), anyString());
        verify(light).requestAutomatic(eqPot(), org.mockito.ArgumentMatchers.eq(false), anyString());
    }

    @Test
    void aRefusedIrrigationIsNotReAskedBeforeItCouldSucceed() {
        Instant retryAt = NOON.plusSeconds(3600);
        when(irrigation.requestAutomatic(anyLong(), anyString()))
                .thenReturn(deniedUntil(retryAt));
        given(pot(CROP, DeviceStatus.ONLINE, true));
        when(measurements.findLatest(POT_ID)).thenReturn(Optional.of(sample(20.0, 400.0)));

        engine.evaluateOnce();
        engine.evaluateOnce();
        engine.evaluateOnce();

        // The Governor already said when it could next say yes. Asking again
        // every minute writes an hour of identical COOLDOWN rows that answer no
        // question the first one did not.
        verify(irrigation, times(1)).requestAutomatic(anyLong(), anyString());
    }

    @Test
    void aRefusalWithNoRetryTimeIsAskedAgain() {
        when(irrigation.requestAutomatic(anyLong(), anyString()))
                .thenReturn(deniedUntil(null));
        given(pot(CROP, DeviceStatus.ONLINE, true));
        when(measurements.findLatest(POT_ID)).thenReturn(Optional.of(sample(20.0, 400.0)));

        engine.evaluateOnce();
        engine.evaluateOnce();

        // INPUT_STALE and SENSOR_INVALID carry no next_available_at because they
        // clear the moment a good reading lands, which could be the next pass.
        verify(irrigation, times(2)).requestAutomatic(anyLong(), anyString());
    }

    // -- failure isolation -------------------------------------------------

    @Test
    void onePotThatThrowsDoesNotStopTheNextOne() {
        Pot broken = new Pot(POT_ID, 1L, "node-a", "화분 1", CROP, NOON,
                DeviceStatus.ONLINE, NOON, NOON, 2000, true);
        Pot healthy = new Pot(POT_ID + 1, 1L, "node-b", "화분 2", CROP, NOON,
                DeviceStatus.ONLINE, NOON, NOON, 2000, true);
        when(pots.findAllUnderAutomaticControl()).thenReturn(List.of(broken, healthy));
        when(measurements.findLatest(POT_ID)).thenThrow(new IllegalStateException("influx down"));
        when(measurements.findLatest(POT_ID + 1)).thenReturn(Optional.of(sample(20.0, 400.0)));

        engine.evaluateOnce();

        verify(irrigation).requestAutomatic(
                org.mockito.ArgumentMatchers.eq(POT_ID + 1), anyString());
    }

    // -- fixtures ----------------------------------------------------------

    /** The real record: IrrigationOutcome is final, so Mockito cannot stand in. */
    private IrrigationOutcome deniedUntil(Instant nextAvailableAt) {
        return IrrigationOutcome.denied(DenyReason.COOLDOWN, "쿨다운", nextAvailableAt);
    }

    private long eqPot() {
        return org.mockito.ArgumentMatchers.eq(POT_ID);
    }

    private void given(Pot pot) {
        when(pots.findAllUnderAutomaticControl()).thenReturn(List.of(pot));
    }

    private Pot pot(String cropCode, DeviceStatus status, boolean autoControl) {
        return new Pot(POT_ID, 1L, "node-a", "화분 1", cropCode, NOON, status, NOON, NOON,
                2000, autoControl);
    }

    private TelemetrySample sample(double soilMoisturePct, Double ppfd) {
        return sample(soilMoisturePct, ppfd, true, true);
    }

    private TelemetrySample sample(
            double soilMoisturePct, Double ppfd, boolean soilValid, boolean lightValid) {
        return new TelemetrySample(
                POT_ID, 1L, "node-a", CROP, "orangepi-pro-01", "evt-1", NOON, 1L,
                soilMoisturePct, 500L, 24.0, 55.0, null, ppfd, 21.0,
                soilValid, true, lightValid, null);
    }

    /** PPFD optimal band 200–600, which the light rule reads its thresholds from. */
    private CropScoreProfile profile() {
        return new CropScoreProfile(
                CROP, "상추", 5.0, 15.0, 24.0, 35.0, 20.0, 50.0, 80.0, 95.0,
                50.0, 200.0, 600.0, 1200.0);
    }
}
