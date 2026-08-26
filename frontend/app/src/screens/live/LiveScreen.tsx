import { useCallback, useEffect, useState } from 'react';
import { Pressable, StyleSheet, Text, TextInput, View } from 'react-native';

import { font } from '../../appTheme/glass';
import { palette } from '../../appTheme/palette';
import { scaleTypography } from '../../appTheme/scaleTypography';
import { typeScale } from '../../appTheme/typography';
import { ApiRequestError } from '../../auth/authApi';
import { LineChart } from '../../components/LineChart';
import { PrimaryButton } from '../../components/PrimaryButton';
import { Surface } from '../../components/Surface';
import { liveMetricDefinitions } from '../../data';
import {
  describeCommandState,
  getActuatorStatus,
  isCommandPending,
  type ActuatorStatus,
  type CommandHistoryEntry,
} from '../../irrigation/commandApi';
import { requestIrrigation } from '../../irrigation/irrigationApi';
import { requestLight } from '../../light/lightApi';
import { getPot, setAutoControl } from '../../pot/potApi';
import {
  useDeviceEnvironment,
  useMeasurementSeries,
} from '../../shared/device-environment/DeviceEnvironmentProvider';

const DEFAULT_IRRIGATION_VOLUME_ML = 20;

/** Matches the backend's dose bounds, so an impossible request never leaves. */
const MIN_IRRIGATION_VOLUME_ML = 1;
const MAX_IRRIGATION_VOLUME_ML = 500;

/**
 * "다시 가능한 시간" as a person would say it.
 *
 * A refusal that only says "cooldown" leaves the user tapping; the time is the
 * part that tells them to stop.
 */
function describeRetryAt(iso: string | null): string | null {
  if (!iso) return null;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return null;
  const minutes = Math.ceil((at.getTime() - Date.now()) / 60000);
  if (minutes <= 0) return '지금 다시 시도할 수 있습니다';
  if (minutes < 60) return `약 ${minutes}분 뒤 다시 가능합니다`;
  return `${at.getHours()}시 ${String(at.getMinutes()).padStart(2, '0')}분 이후 다시 가능합니다`;
}

function describeElapsed(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return '';
  const minutes = Math.floor((Date.now() - at.getTime()) / 60000);
  if (minutes < 1) return '방금';
  if (minutes < 60) return `${minutes}분 전`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}시간 전`;
  return `${Math.floor(hours / 24)}일 전`;
}

/** One actuator's last command, or the honest absence of one. */
function ActuatorCard({ title, entry }: { title: string; entry: CommandHistoryEntry | null }) {
  if (!entry) {
    return (
      <View style={styles.statusCard}>
        <Text style={styles.statusTitle}>{title}</Text>
        {/* Not "꺼짐": nothing has ever commanded it, and the server does not
            read the firmware's actuator state. */}
        <Text style={styles.statusValue}>기록 없음</Text>
        <Text style={styles.liveCaption}>아직 명령을 보낸 적이 없습니다</Text>
      </View>
    );
  }

  const pending = isCommandPending(entry.state);
  const detail = entry.actuator === 'pump'
    ? `${entry.actualMl ?? entry.grantedMl ?? 0} mL`
    : entry.action === 'on' ? '켜기' : '끄기';

  return (
    <View style={styles.statusCard}>
      <Text style={styles.statusTitle}>{title}</Text>
      <Text
        style={[
          styles.statusValue,
          pending && styles.statusPending,
          entry.state === 'REJECTED' && styles.actionRefused,
        ]}
      >
        {detail} · {describeCommandState(entry.state)}
      </Text>
      <Text style={styles.liveCaption}>
        {describeElapsed(entry.completedAt ?? entry.issuedAt)}
        {entry.stopCause ? ` · ${entry.stopCause}` : ''}
        {entry.origin === 'EDGE_FALLBACK' ? ' · 오프라인 자율 관수' : ''}
      </Text>
    </View>
  );
}

type IrrigationResult = {
  message: string;
  status: 'success' | 'refused' | 'error';
};

type LightResult = {
  message: string;
  status: 'success' | 'refused' | 'error';
};

export function LiveScreen({ compact }: { compact: boolean }) {
  const { measurements: measurement, potId, refetch } = useDeviceEnvironment();
  const [irrigationLoading, setIrrigationLoading] = useState(false);
  const [irrigationResult, setIrrigationResult] = useState<IrrigationResult | null>(null);
  const [pendingLightState, setPendingLightState] = useState<boolean | null>(null);
  const [lightResult, setLightResult] = useState<LightResult | null>(null);
  const [volumeText, setVolumeText] = useState(String(DEFAULT_IRRIGATION_VOLUME_ML));
  const [actuators, setActuators] = useState<ActuatorStatus | null>(null);
  const [autoControl, setAutoControlState] = useState<boolean | null>(null);
  const [autoControlPending, setAutoControlPending] = useState(false);
  const airTemperatureSeries = useMeasurementSeries('air_temperature_c', '1h');
  const airHumiditySeries = useMeasurementSeries('air_humidity_pct', '1h');
  const plantLightSeries = useMeasurementSeries('plant_light_ppfd_umol_m2_s', '1h');
  const soilMoistureSeries = useMeasurementSeries('soil_moisture_pct', '1h');
  const soilTemperatureSeries = useMeasurementSeries('soil_temperature_c', '1h');

  const refreshActuators = useCallback(async () => {
    if (potId === undefined) return;
    try {
      setActuators(await getActuatorStatus(potId));
    } catch {
      // A status card that cannot load is not worth an error banner over the
      // controls it sits above; the cards fall back to "기록 없음".
      setActuators(null);
    }
  }, [potId]);

  useEffect(() => {
    void refreshActuators();
  }, [refreshActuators]);

  useEffect(() => {
    if (potId === undefined) return undefined;
    // Polled rather than pushed: a command in flight settles within seconds and
    // this is the screen the user is watching while it does.
    const timer = setInterval(() => { void refreshActuators(); }, 5000);
    return () => { clearInterval(timer); };
  }, [potId, refreshActuators]);

  useEffect(() => {
    if (potId === undefined) return;
    // The switch lives on the pot, which this screen does not otherwise load.
    // Read once rather than polled: it only changes when someone taps it, and a
    // poll would fight the optimistic update in toggleAutoControl.
    void getPot(potId)
      .then((loaded) => { setAutoControlState(loaded.autoControlEnabled); })
      .catch(() => { setAutoControlState(null); });
  }, [potId]);

  const requestedVolumeMl = Number.parseInt(volumeText, 10);
  const volumeIsUsable =
    Number.isFinite(requestedVolumeMl)
    && requestedVolumeMl >= MIN_IRRIGATION_VOLUME_ML
    && requestedVolumeMl <= MAX_IRRIGATION_VOLUME_ML;

  const values = measurement?.measurements;
  const currentValues = {
    air_temperature_c: values?.airTemperatureC,
    air_humidity_pct: values?.airHumidityPct,
    plant_light_ppfd_umol_m2_s: values?.plantLightPpfdUmolM2S,
    soil_moisture_pct: values?.soilMoisturePct,
    soil_temperature_c: values?.soilTemperatureC,
  };
  const measurementSeriesByMetric = {
    air_temperature_c: airTemperatureSeries,
    air_humidity_pct: airHumiditySeries,
    plant_light_ppfd_umol_m2_s: plantLightSeries,
    soil_moisture_pct: soilMoistureSeries,
    soil_temperature_c: soilTemperatureSeries,
  };
  const liveMetrics = liveMetricDefinitions.map((metric) => {
    const current = currentValues[metric.key];
    const series = measurementSeriesByMetric[metric.key];
    const seriesValues = series.points.map((point) => point.value);
    return {
      ...metric,
      current,
      series,
      seriesValues,
      value: current == null ? '--' : `${current.toLocaleString('ko-KR')}${metric.unit}`,
    };
  });

  const irrigate = async () => {
    if (potId === undefined || irrigationLoading) return;

    setIrrigationLoading(true);
    setIrrigationResult(null);
    try {
      const outcome = await requestIrrigation(potId, {
        volumeMl: requestedVolumeMl,
        cooldownOverride: false,
        overrideReason: null,
      });

      if (!outcome.granted) {
        // The reason and the time are the two things that stop a user tapping
        // again. Either one alone leaves them guessing.
        const retry = describeRetryAt(outcome.nextAvailableAt);
        setIrrigationResult({
          message: [
            outcome.detail ?? '안전 조건에 따라 관수가 거부되었습니다.',
            retry,
          ].filter(Boolean).join(' · '),
          status: 'refused',
        });
        return;
      }

      const granted = outcome.grantedMl ?? requestedVolumeMl;
      const clamped = granted !== requestedVolumeMl
        ? ` (요청 ${requestedVolumeMl} mL에서 조정)`
        : '';
      setIrrigationResult({
        message: `${granted} mL 관수를 시작했습니다${clamped}`,
        status: 'success',
      });
      await Promise.all([refetch(), refreshActuators()]);
    } catch (caught) {
      setIrrigationResult({
        message: caught instanceof Error ? caught.message : '관수 요청을 처리하지 못했습니다.',
        status: caught instanceof ApiRequestError && caught.status === 409 ? 'refused' : 'error',
      });
    } finally {
      setIrrigationLoading(false);
    }
  };

  const toggleAutoControl = async () => {
    if (potId === undefined || autoControl === null || autoControlPending) return;
    setAutoControlPending(true);
    try {
      const updated = await setAutoControl(potId, !autoControl);
      setAutoControlState(updated.autoControlEnabled);
      await refetch();
    } catch {
      // Left as it was. Showing the switch in the position the user tapped when
      // the server never agreed is worse than showing it unchanged.
    } finally {
      setAutoControlPending(false);
    }
  };

  const setLight = async (on: boolean) => {
    if (potId === undefined || pendingLightState !== null) return;

    setPendingLightState(on);
    setLightResult(null);
    try {
      const outcome = await requestLight(potId, { on });

      if (!outcome.issued) {
        setLightResult({
          message: outcome.detail ?? '안전 조건에 따라 조명 요청이 거부되었습니다.',
          status: 'refused',
        });
        return;
      }

      setLightResult({
        message: `조명을 ${outcome.on ? '켰습니다' : '껐습니다'}`,
        status: 'success',
      });
      await refreshActuators();
    } catch (caught) {
      setLightResult({
        message: caught instanceof Error ? caught.message : '조명 요청을 처리하지 못했습니다.',
        status: caught instanceof ApiRequestError && caught.status === 409 ? 'refused' : 'error',
      });
    } finally {
      setPendingLightState(null);
    }
  };

  return (
    <View style={styles.pageBody}>
      <Text style={styles.liveRefresh}>3초마다 자동 갱신{measurement ? ` · 업데이트 #${measurement.sequence}` : ''}</Text>
      <View style={[styles.liveGrid, compact && styles.stack]}>
        {liveMetrics.map((metric) => (
          <Surface flat key={metric.label} style={styles.liveCard}>
            <View style={styles.liveCardHeader}>
              <Text style={styles.liveLabel}>{metric.label}</Text>
              <Text style={styles.liveRange}>{metric.rangeLabel}</Text>
            </View>
            <Text style={styles.liveValue}>{metric.value}</Text>
            <Text style={styles.liveCaption}>{metric.current == null && metric.key === 'soil_temperature_c' ? '프로브 미연결' : '현재 측정값'}</Text>
          {metric.seriesValues.length >= 2 ? (
            <>
              <LineChart
                gridLines={1}
                height={72}
                series={[{ color: metric.color, values: metric.seriesValues }]}
                showLegend={false}
              />
              <View style={styles.liveFooter}>
                <Text style={styles.liveFooterText}>최저 {Math.min(...metric.seriesValues).toLocaleString('ko-KR')}{metric.unit}</Text>
                <Text style={styles.liveFooterText}>최고 {Math.max(...metric.seriesValues).toLocaleString('ko-KR')}{metric.unit}</Text>
              </View>
            </>
          ) : (
            <Text style={styles.liveCaption}>
              {metric.series.loading
                ? '최근 1시간 추이를 불러오는 중'
                : metric.series.error
                  ? '최근 1시간 추이를 불러오지 못했습니다'
                  : '표시할 추이 데이터가 아직 없습니다'}
            </Text>
          )}
        </Surface>
        ))}
      </View>
      <Surface flat style={styles.statusStrip}>
        <View style={styles.statusRow}>
          <ActuatorCard title="펌프" entry={actuators?.pump ?? null} />
          <ActuatorCard title="조명" entry={actuators?.light ?? null} />
        </View>
        <View style={styles.autoRow}>
          <View style={styles.actionCopy}>
            <Text style={styles.actionTitle}>
              {autoControl === false ? '수동 제어' : '자동 제어'}
            </Text>
            <Text style={styles.liveCaption}>
              {autoControl === null
                ? '설정을 불러오는 중입니다'
                : autoControl
                  ? '센서값이 기준을 벗어나면 알아서 관수하고 조명을 조절합니다.'
                  : '자동 판단을 멈춥니다. 아래 버튼은 그대로 동작합니다.'}
            </Text>
          </View>
          <Pressable
            accessibilityRole="switch"
            accessibilityState={{ checked: autoControl === true, disabled: autoControl === null }}
            disabled={autoControl === null || autoControlPending}
            onPress={() => { void toggleAutoControl(); }}
            style={[styles.switchTrack, autoControl && styles.switchTrackOn]}
          >
            <View style={[styles.switchThumb, autoControl && styles.switchThumbOn]} />
          </Pressable>
        </View>
      </Surface>
      <View style={[styles.actionGrid, compact && styles.stack]}>
        <Surface flat style={[styles.actionCard, compact && styles.actionCardCompact]}>
          <View style={styles.actionCopy}>
            <Text style={styles.actionTitle}>수동 관수</Text>
            <View style={styles.volumeRow}>
              <TextInput
                accessibilityLabel="관수량(mL)"
                inputMode="numeric"
                keyboardType="number-pad"
                maxLength={3}
                onChangeText={(next) => { setVolumeText(next.replace(/[^0-9]/g, '')); }}
                placeholder={String(DEFAULT_IRRIGATION_VOLUME_ML)}
                placeholderTextColor={palette.muted}
                style={styles.volumeInput}
                value={volumeText}
              />
              <Text style={styles.liveCaption}>mL</Text>
            </View>
            <Text style={styles.liveCaption}>
              {actuators?.pump?.grantedMl
                ? `직전 승인량 ${actuators.pump.grantedMl} mL`
                : `${MIN_IRRIGATION_VOLUME_ML}–${MAX_IRRIGATION_VOLUME_ML} mL 사이로 입력하세요`}
            </Text>
            {irrigationResult ? (
              <Text
                style={[
                  styles.actionResult,
                  irrigationResult.status === 'success' && styles.actionSuccess,
                  irrigationResult.status === 'refused' && styles.actionRefused,
                  irrigationResult.status === 'error' && styles.actionError,
                ]}
              >
                {irrigationResult.message}
              </Text>
            ) : null}
          </View>
          <PrimaryButton
            disabled={potId === undefined || irrigationLoading || !volumeIsUsable}
            label={irrigationLoading
              ? '관수 요청 중…'
              : volumeIsUsable ? `${requestedVolumeMl} mL 관수하기` : '관수량을 입력하세요'}
            onPress={() => { void irrigate(); }}
            style={styles.actionButton}
          />
        </Surface>
        <Surface flat style={[styles.actionCard, compact && styles.actionCardCompact]}>
          <View style={styles.actionCopy}>
            <Text style={styles.actionTitle}>조명</Text>
            <Text style={styles.liveCaption}>조명을 켜거나 끕니다.</Text>
            {lightResult ? (
              <Text
                style={[
                  styles.actionResult,
                  lightResult.status === 'success' && styles.actionSuccess,
                  lightResult.status === 'refused' && styles.actionRefused,
                  lightResult.status === 'error' && styles.actionError,
                ]}
              >
                {lightResult.message}
              </Text>
            ) : null}
          </View>
          {/* Two buttons rather than a toggle. The card above reports the last
              command, not live hardware state, so a toggle would claim to know
              something nobody has read back from the firmware. */}
          <View style={styles.lightButtons}>
            <PrimaryButton
              disabled={potId === undefined || pendingLightState !== null}
              label={pendingLightState === true ? '조명 켜는 중…' : '조명 켜기'}
              onPress={() => { void setLight(true); }}
              style={styles.actionButton}
            />
            <PrimaryButton
              disabled={potId === undefined || pendingLightState !== null}
              label={pendingLightState === false ? '조명 끄는 중…' : '조명 끄기'}
              onPress={() => { void setLight(false); }}
              style={styles.actionButton}
            />
          </View>
        </Surface>
      </View>
    </View>
  );
}

const styles = StyleSheet.create(scaleTypography({
  pageBody: { gap: 30, maxWidth: 1320, width: '100%' },
  liveRefresh: { ...typeScale.caption, alignSelf: 'flex-end', color: palette.muted, fontFamily: font },
  liveGrid: { flexDirection: 'row', flexWrap: 'wrap', gap: 24 },
  statusStrip: { gap: 20, padding: 24 },
  statusRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 24 },
  statusCard: { flexGrow: 1, flexBasis: 200, gap: 4 },
  statusTitle: { ...typeScale.caption, color: palette.muted, fontFamily: font },
  statusValue: { ...typeScale.body, color: palette.text, fontFamily: font },
  statusPending: { color: palette.muted },
  autoRow: { alignItems: 'center', flexDirection: 'row', gap: 16, justifyContent: 'space-between' },
  switchTrack: {
    backgroundColor: palette.muted, borderRadius: 999, height: 28, justifyContent: 'center',
    padding: 3, width: 52,
  },
  switchTrackOn: { backgroundColor: palette.green },
  switchThumb: { backgroundColor: palette.lineStrong, borderRadius: 999, height: 22, width: 22 },
  switchThumbOn: { alignSelf: 'flex-end' },
  volumeRow: { alignItems: 'center', flexDirection: 'row', gap: 8 },
  volumeInput: {
    ...typeScale.body, borderColor: palette.muted, borderRadius: 10, borderWidth: 1,
    color: palette.text, fontFamily: font, paddingHorizontal: 12, paddingVertical: 8, width: 90,
  },
  stack: { flexDirection: 'column' },
  liveCard: { flexBasis: '47%', flexGrow: 1, gap: 14, minWidth: 280, padding: 34 },
  liveCardHeader: { alignItems: 'flex-start', flexDirection: 'row', gap: 12, justifyContent: 'space-between' },
  liveLabel: { ...typeScale.cardTitle, color: palette.text, fontFamily: font, fontWeight: '700' },
  liveRange: { ...typeScale.caption, color: palette.muted, fontFamily: font, textAlign: 'right' },
  liveValue: { ...typeScale.metric, color: palette.text, fontFamily: font },
  liveCaption: { ...typeScale.body, color: palette.muted, fontFamily: font },
  liveFooter: { borderTopColor: palette.line, borderTopWidth: 1, flexDirection: 'row', justifyContent: 'space-between', paddingTop: 10 },
  liveFooterText: { ...typeScale.caption, color: palette.muted, fontFamily: font },
  actionGrid: { flexDirection: 'row', flexWrap: 'wrap', gap: 24 },
  actionCard: { alignItems: 'center', flexBasis: '47%', flexDirection: 'row', flexGrow: 1, gap: 24, justifyContent: 'space-between', minWidth: 280, padding: 34 },
  actionCardCompact: { alignItems: 'stretch', flexDirection: 'column' },
  actionCopy: { flex: 1, gap: 8 },
  actionTitle: { ...typeScale.cardTitle, color: palette.text, fontFamily: font, fontWeight: '700' },
  actionResult: { ...typeScale.body, fontFamily: font },
  actionSuccess: { color: palette.green },
  actionRefused: { color: palette.amber },
  actionError: { color: palette.red },
  actionButton: { minWidth: 180 },
  lightButtons: { gap: 12 },
}));
