import { useEffect, useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { controlTextTokens, controlTokens } from '../../appTheme/controls';
import { font } from '../../appTheme/glass';
import { palette } from '../../appTheme/palette';
import { scaleTypography } from '../../appTheme/scaleTypography';
import { typeScale } from '../../appTheme/typography';
import { ActionButton } from '../../components/ActionButton';
import { LineChart } from '../../components/LineChart';
import { SectionHeader } from '../../components/SectionHeader';
import { SensorSummary } from '../../components/SensorSummary';
import { SuitabilityFormulaModal } from '../../components/SuitabilityFormulaModal';
import { Surface } from '../../components/Surface';
import { dashboardChartMetrics } from '../../data';
import type { DeviceResponse } from '../../device/deviceApi';
import type { Page } from '../../navigation/types';
import { getDeviceSensors, type DeviceSensorStatus } from '../../sensor/sensorApi';
import {
  useDeviceEnvironment,
  useMeasurementSeries,
} from '../../shared/device-environment/DeviceEnvironmentProvider';
import { getGradeLabel, getIssueFactors } from '../../shared/factorPresentation';
import { soilConditionText } from '../../soil/soilCondition';
import { useDisclosure } from '../../shared/hooks/useDisclosure';

function makeAxisLabels(points: Array<{ time: string }>, range: '1h' | '24h' | '7d' | '30d'): string[] {
  const labelCount = Math.min(5, points.length);
  if (!labelCount) return [];
  const indexes = Array.from(
    { length: labelCount },
    (_, index) => Math.round((index / Math.max(labelCount - 1, 1)) * (points.length - 1)),
  );
  return indexes.map((index) => {
    const date = new Date(points[index].time);
    return range === '1h' || range === '24h'
      ? date.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', hour12: false })
      : date.toLocaleDateString('ko-KR', { month: 'numeric', day: 'numeric' });
  });
}

export function DashboardScreen({
  compact,
  device,
  onNavigate,
}: {
  compact: boolean;
  device?: DeviceResponse;
  onNavigate: (page: Page) => void;
}) {
  const [chartRange, setChartRange] = useState<'1h' | '24h' | '7d' | '30d'>('24h');
  const [selectedChartMetric, setSelectedChartMetric] = useState('all');
  const [kitSensors, setKitSensors] = useState<DeviceSensorStatus[]>([]);
  const [kitSensorsLoading, setKitSensorsLoading] = useState(false);
  const [kitSensorsError, setKitSensorsError] = useState<string | null>(null);
  const {
    potId,
    score: scoreData,
    measurements: latestData,
    soilRecommendation,
  } = useDeviceEnvironment();
  const airTemperatureSeries = useMeasurementSeries('air_temperature_c', chartRange);
  const airHumiditySeries = useMeasurementSeries('air_humidity_pct', chartRange);
  const plantLightSeries = useMeasurementSeries('plant_light_ppfd_umol_m2_s', chartRange);
  const soilMoistureSeries = useMeasurementSeries('soil_moisture_pct', chartRange);
  const soilTemperatureSeries = useMeasurementSeries('soil_temperature_c', chartRange);
  const formulaDisclosure = useDisclosure();

  useEffect(() => {
    if (!device?.id) {
      setKitSensors([]);
      return undefined;
    }
    let active = true;
    setKitSensorsLoading(true);
    setKitSensorsError(null);
    const loadSensors = async () => {
      try {
        const response = await getDeviceSensors(device.id);
        if (active) {
          setKitSensors(response.sensors);
          setKitSensorsError(null);
        }
      } catch (error) {
        if (active) setKitSensorsError(error instanceof Error ? error.message : '키트 상태를 불러오지 못했습니다.');
      } finally {
        if (active) setKitSensorsLoading(false);
      }
    };
    void loadSensors();
    const interval = setInterval(() => void loadSensors(), 3000);
    return () => {
      active = false;
      clearInterval(interval);
    };
  }, [device?.id]);

  const soilMoisture = latestData?.measurements.soilMoisturePct;
  const soilTemperature = latestData?.measurements.soilTemperatureC;
  const soilMoistureReceived = Boolean(latestData?.quality.soilSensorValid && soilMoisture != null);
  const soilTemperatureReceived = Boolean(
    latestData?.quality.soilSensorValid && soilTemperature != null,
  );
  const realtimeStatus = (value: number | null | undefined) => (
    value == null ? '데이터 수신 대기 중' : '실시간 측정 중'
  );
  const stats = [
    {
      label: '온도',
      value: latestData ? `${latestData.measurements.airTemperatureC}℃` : '--',
      detail: realtimeStatus(latestData?.measurements.airTemperatureC),
    },
    {
      label: '습도',
      value: latestData ? `${latestData.measurements.airHumidityPct}%` : '--',
      detail: realtimeStatus(latestData?.measurements.airHumidityPct),
    },
    {
      label: '조도',
      value: latestData ? `${latestData.measurements.plantLightPpfdUmolM2S.toLocaleString('ko-KR')} PPFD` : '--',
      detail: realtimeStatus(latestData?.measurements.plantLightPpfdUmolM2S),
    },
    {
      label: '토양 수분',
      value: soilMoistureReceived ? `${soilMoisture?.toLocaleString('ko-KR')}%` : '--',
      detail: realtimeStatus(soilMoistureReceived ? soilMoisture : undefined),
    },
    {
      label: '토양 온도',
      value: soilTemperatureReceived ? `${soilTemperature?.toLocaleString('ko-KR', { maximumFractionDigits: 1 })}℃` : '--',
      detail: realtimeStatus(soilTemperatureReceived ? soilTemperature : undefined),
    },
  ];
  const displayFactors = scoreData?.factors ?? [];
  const missingScoreFactors = [
    { key: 'temperature', label: '온도', unit: '℃' },
    { key: 'humidity', label: '습도', unit: '%' },
    { key: 'plantLight', label: '조도', unit: 'PPFD' },
  ].filter(({ key }) => !displayFactors.some((factor) => factor.key === key));
  const sensorStatus = (metric: string, hasMeasurement: boolean) => {
    const sensor = kitSensors.find((item) => item.potId === potId && item.metric === metric);
    if (sensor?.status === 'ONLINE') return { label: '정상 수신', warning: false };
    if (sensor?.status === 'OFFLINE') return { label: '오프라인', warning: true };
    if (sensor?.status === 'UNAVAILABLE') return { label: '사용 불가', warning: true };
    if (sensor?.status === 'UNKNOWN') return { label: '상태 미확인', warning: true };
    return hasMeasurement ? { label: '정상 수신', warning: false } : { label: '수신 대기', warning: true };
  };
  const soilMoistureStatus = sensorStatus('soil_moisture_pct', soilMoistureReceived);
  const soilTemperatureStatus = sensorStatus('soil_temperature_c', soilTemperatureReceived);
  const environmentStatusRows = [
    ...displayFactors.map((factor) => ({
      key: factor.key,
      label: factor.label,
      current: `${factor.current.toLocaleString('ko-KR')}${factor.unit}`,
      range: `${factor.optimalMin.toLocaleString('ko-KR')}~${factor.optimalMax.toLocaleString('ko-KR')}${factor.unit}`,
      status: factor.status === 'OK' ? '적정' : '확인 필요',
      warning: factor.status !== 'OK',
    })),
    ...missingScoreFactors.map((factor) => ({
      key: factor.key,
      label: factor.label,
      current: '--',
      range: `-- ${factor.unit}`,
      status: '수신 대기',
      warning: true,
    })),
    {
      key: 'soilMoisture',
      label: '토양 수분',
      current: soilMoistureReceived ? `${soilMoisture?.toLocaleString('ko-KR')}%` : '--',
      // 작물별 적정 토양수분 범위는 crop_score_profile 에 존재하지 않는다(설계 갭 G3).
      // 대신 같은 화분의 관수 주기를 비교해 나온 환경 판정을 보여준다.
      range: soilConditionText(soilRecommendation),
      status: soilMoistureStatus.label,
      warning: soilMoistureStatus.warning,
    },
    {
      key: 'soilTemperature',
      label: '토양 온도',
      current: soilTemperatureReceived ? `${soilTemperature?.toLocaleString('ko-KR', { maximumFractionDigits: 1 })}℃` : '--',
      // 토양 온도는 작물별 기준도, 판정 규칙도 아직 없다. 있는 척하지 않는다.
      range: '작물별 기준 없음',
      status: soilTemperatureStatus.label,
      warning: soilTemperatureStatus.warning,
    },
  ];
  const issueFactors = getIssueFactors(scoreData?.factors ?? []);
  const gradeText = getGradeLabel(scoreData?.grade);
  const spaceName = device?.space?.name ?? '등록된 공간 없음';
  const spaceMeta = device?.space
    ? `${device.space.spaceType} · ${device.space.areaSquareMeters}m² · 등록 기기 ${device.serialCode}`
    : '공간 정보를 불러오는 중입니다.';
  const deviceOnline = device?.status === 'ONLINE';
  const operatingLabel = !device ? '기기 정보 확인 중' : deviceOnline ? '기기 연결됨' : '기기 오프라인';
  const environmentActive = Boolean(scoreData || latestData);
  const flow = [
    { label: '공간 등록', state: device?.space ? '완료' : '대기' },
    { label: '기기 연결', state: device ? (deviceOnline ? '정상' : '오프라인') : '대기' },
    { label: '환경 모니터링', state: environmentActive ? '수신 중' : '수신 대기' },
  ];
  const measurementSeriesByMetric = {
    air_temperature_c: airTemperatureSeries,
    air_humidity_pct: airHumiditySeries,
    plant_light_ppfd_umol_m2_s: plantLightSeries,
    soil_moisture_pct: soilMoistureSeries,
    soil_temperature_c: soilTemperatureSeries,
  };
  const chartSeries = dashboardChartMetrics
    .map((metric) => ({
      ...metric,
      points: measurementSeriesByMetric[metric.key].points,
      values: measurementSeriesByMetric[metric.key].points.map((point) => point.value),
    }))
    .filter((series) => series.values.length >= 2);
  const visibleChartSeries = selectedChartMetric === 'all'
    ? chartSeries
    : chartSeries.filter((series) => series.label === selectedChartMetric);
  const chartAxisPoints = visibleChartSeries[0]?.points ?? [];
  const chartPeriodLabel = chartRange === '1h' ? '1시간' : chartRange === '24h' ? '24시간' : chartRange === '7d' ? '7일' : '30일';

  return (
    <View style={styles.pageBody}>
      <Surface flat style={styles.dashboardIdentityPanel}>
        <View style={styles.dashboardIdentitySection}>
          <View style={[styles.spaceIdentityTop, compact && styles.stack]}>
            <View style={styles.spaceIdentityCopy}>
              <Text style={styles.spaceIdentityTitle}>{spaceName}</Text>
              <Text style={styles.spaceIdentityMeta}>{spaceMeta}</Text>
            </View>
            <View style={styles.spaceOperatingBadge}><View style={[styles.onlineDot, !deviceOnline && styles.offlineDot]} /><Text style={styles.spaceOperatingText}>{operatingLabel}</Text></View>
          </View>
          <View style={[styles.serviceFlow, compact && styles.stack]}>
            <View style={[styles.serviceFlowStep, compact && styles.serviceFlowStepCompact]}><Text style={styles.serviceFlowNumber}>01</Text><Text style={styles.serviceFlowLabel}>{flow[0].label}</Text><Text style={styles.serviceFlowState}>{flow[0].state}</Text></View>
            <View style={[styles.serviceFlowLine, compact && styles.serviceFlowLineCompact]} />
            <View style={[styles.serviceFlowStep, compact && styles.serviceFlowStepCompact]}><Text style={styles.serviceFlowNumber}>02</Text><Text style={styles.serviceFlowLabel}>{flow[1].label}</Text><Text style={styles.serviceFlowState}>{flow[1].state}</Text></View>
            <View style={[styles.serviceFlowLine, compact && styles.serviceFlowLineCompact]} />
            <View style={[styles.serviceFlowStepActive, compact && styles.serviceFlowStepCompact]}><Text style={styles.serviceFlowNumberActive}>03</Text><Text style={styles.serviceFlowLabel}>{flow[2].label}</Text><Text style={styles.serviceFlowStateActive}>{flow[2].state}</Text></View>
          </View>
        </View>
      </Surface>

      <Surface flat style={styles.scoreHeroPanel}>
        <View style={[styles.scoreHero, compact && styles.scoreHeroCompact]}>
          <View style={styles.scoreHeroCopy}>
            <Text style={styles.scoreHeroEyebrow}>스마트팜 전환 적합도</Text>
            <View style={styles.scoreHeroValueRow}>
              <Text style={styles.scoreHeroValue}>{scoreData?.total ?? '--'}</Text>
              <Text style={styles.scoreHeroUnit}>/ 100</Text>
            </View>
            <Text style={styles.scoreHeroGrade}>{gradeText} · {scoreData?.cropName ?? '작물'} 재배 기준</Text>
          </View>
          <Pressable
            accessibilityRole="button"
            onPress={formulaDisclosure.show}
            style={({ pressed }) => [styles.formulaLink, styles.formulaLinkTop, pressed && styles.pressed]}
          >
            <Text style={styles.formulaLinkText}>적합도 계산식</Text>
            <Text style={styles.formulaLinkArrow}>→</Text>
          </Pressable>
        </View>
      </Surface>

      <SuitabilityFormulaModal onClose={formulaDisclosure.hide} scoreData={scoreData} visible={formulaDisclosure.open} />

      <Surface flat style={styles.dashboardAlertPanel}>
        <View style={[styles.dashboardAlertHeader, compact && styles.stack]}>
          <View style={styles.dashboardAlertCopy}>
            <Text style={styles.dashboardAlertEyebrow}>현재 확인이 필요한 항목</Text>
            <Text style={styles.dashboardAlertTitle}>{scoreData ? `확인 필요한 환경 ${issueFactors.length}건` : '환경 점수 수신 대기'}</Text>
          </View>
          <ActionButton label="분석 보고서 보기" onPress={() => onNavigate('analysis')} quiet />
        </View>
        <View style={[styles.dashboardAlertRows, compact && styles.stack]}>
          {!scoreData ? <Text style={styles.dashboardAlertItemBody}>환경 점수 데이터를 불러오는 중이거나 아직 수집된 값이 없습니다.</Text> : issueFactors.length ? issueFactors.map((factor) => (
            <View key={factor.key} style={styles.dashboardAlertItem}>
              <Text style={styles.dashboardAlertItemLabel}>{factor.label} {factor.status === 'LOW' ? '부족' : '초과'}</Text>
              <Text style={styles.dashboardAlertItemValue}>{factor.current.toLocaleString('ko-KR')}{factor.unit}</Text>
              <Text style={styles.dashboardAlertItemBody}>적정 범위와 {factor.gap.toLocaleString('ko-KR')}{factor.unit} 차이 · 축 점수 {factor.score}점</Text>
            </View>
          )) : <Text style={styles.dashboardAlertItemBody}>현재 세 지표가 모두 적정 범위입니다.</Text>}
        </View>
      </Surface>

      <View style={[styles.metricChartGrid, compact && styles.stack]}>
        <Surface flat style={[styles.metricsColumn, compact && styles.fullWidth]}>
          {stats.map((item, index) => (
            <View key={item.label} style={[styles.statCardVertical, index < stats.length - 1 && styles.statCardDivider]}>
              <View>
                <Text style={styles.statLabel}>{item.label}</Text>
                <Text style={styles.statDetail}>{item.detail}</Text>
              </View>
              <Text style={styles.statValue}>{item.value}</Text>
            </View>
          ))}
        </Surface>
        <Surface flat style={[styles.chartPanel, compact && styles.chartPanelCompact]}>
          <SectionHeader
            action={(
              <View style={styles.rangeControl}>
                {(['1h', '24h', '7d', '30d'] as const).map((range) => (
                  <Pressable key={range} onPress={() => setChartRange(range)} style={[styles.rangeButton, chartRange === range && styles.rangeButtonActive]}>
                    <Text style={[styles.rangeButtonText, chartRange === range && styles.rangeButtonTextActive]}>{range}</Text>
                  </Pressable>
                ))}
              </View>
            )}
            title="환경 변화"
            description={`${selectedChartMetric === 'all' ? '전체 지표' : selectedChartMetric} · 최근 ${chartPeriodLabel} 측정 추이`}
          />
          <View style={styles.chartMetricFilter}>
            <Text style={styles.chartMetricFilterLabel}>지표별 보기</Text>
            <View style={styles.chartMetricOptions}>
              {['전체', ...dashboardChartMetrics.map((metric) => metric.label)].map((label) => {
                const metricKey = label === '전체' ? 'all' : label;
                const active = selectedChartMetric === metricKey;
                return (
                  <Pressable
                    accessibilityRole="button"
                    accessibilityState={{ selected: active }}
                    key={metricKey}
                    onPress={() => setSelectedChartMetric(metricKey)}
                    style={[styles.chartMetricButton, active && styles.chartMetricButtonActive]}
                  >
                    <Text style={[styles.chartMetricButtonText, active && styles.chartMetricButtonTextActive]}>{label}</Text>
                  </Pressable>
                );
              })}
            </View>
          </View>
          <View style={[styles.chartVisual, !compact && styles.chartVisualExpanded]}>
            {visibleChartSeries.length ? (
              <LineChart
                axisLabels={makeAxisLabels(chartAxisPoints, chartRange)}
                height={180}
                series={visibleChartSeries}
              />
            ) : (
              <Text style={styles.chartStateText}>환경 데이터를 불러오는 중이거나 아직 수집된 값이 없습니다.</Text>
            )}
          </View>
        </Surface>
      </View>

      <View style={[styles.dashboardBottomGrid, compact && styles.stack]}>
        <Surface flat style={styles.tablePanel}>
          <SectionHeader title="환경 상태" description="현재 측정값과 작물별 권장 범위 비교 · 토양은 관수 주기로 판정" />
          <View style={styles.tableHeader}>
            <Text style={[styles.tableHeaderText, styles.tableName]}>항목</Text>
            <Text style={styles.tableHeaderText}>현재 값</Text>
            <Text style={styles.tableHeaderText}>권장 범위 · 판정</Text>
            <Text style={styles.tableHeaderText}>상태</Text>
          </View>
          {environmentStatusRows.map((row) => (
            <View key={row.key} style={styles.tableRow}>
              <Text style={[styles.tableCellStrong, styles.tableName]}>{row.label}</Text>
              <Text style={styles.tableCell}>{row.current}</Text>
              <Text style={styles.tableCell}>{row.range}</Text>
              <View style={[styles.statusBadge, row.warning && styles.statusBadgeWarn]}>
                <Text style={[styles.statusBadgeText, row.warning && styles.statusBadgeTextWarn]}>{row.status}</Text>
              </View>
            </View>
          ))}
          <Text style={styles.tableNote}>토양 수분·토양 온도는 종합 적합도 점수에 포함하지 않습니다. 토양 수분 판정은 고정 기준치가 아니라 같은 화분의 이전 관수 주기와 비교해 산출하며, 비교할 주기가 쌓이기 전에는 판정하지 않습니다.</Text>
        </Surface>

        <Surface flat style={[styles.deviceStatusPanel, compact && styles.fullWidth]}>
          <SectionHeader title="키트 상태" description="공간분석 세트 + 토양분석 세트" />
          {kitSensorsLoading ? <Text style={styles.kitStatusText}>키트 상태를 불러오는 중입니다.</Text> : null}
          {!kitSensorsLoading && kitSensorsError ? <Text style={styles.kitStatusText}>{kitSensorsError}</Text> : null}
          {!kitSensorsLoading && !kitSensorsError ? <SensorSummary sensors={kitSensors} statusLabel={operatingLabel} /> : null}
        </Surface>
      </View>
    </View>
  );
}

const styles = StyleSheet.create(scaleTypography({
  pressed: { opacity: 0.78 },
  onlineDot: { backgroundColor: '#3aad70', borderRadius: 999, height: 7, width: 7 },
  offlineDot: { backgroundColor: palette.muted },
  kitStatusText: { ...typeScale.body, color: palette.muted, fontFamily: font },
  pageBody: { gap: 30, maxWidth: 1320, width: '100%' },
  stack: { flexDirection: 'column' },
  fullWidth: { flexBasis: 'auto', width: '100%' },
  dashboardIdentityPanel: { overflow: 'hidden', padding: 0 },
  scoreHeroPanel: { overflow: 'hidden' },
  dashboardIdentitySection: { gap: 28, padding: 36 },
  spaceIdentityTop: { alignItems: 'flex-start', flexDirection: 'row', gap: 32, justifyContent: 'space-between' },
  spaceIdentityCopy: { flex: 1, gap: 7 },
  spaceIdentityTitle: { ...typeScale.cardTitle, color: palette.text, fontFamily: font },
  spaceIdentityMeta: { ...typeScale.body, color: palette.secondary, fontFamily: font, maxWidth: 820 },
  spaceOperatingBadge: { alignItems: 'center', backgroundColor: palette.greenSoft, borderColor: '#c9dfd1', borderRadius: 999, borderWidth: 1, flexDirection: 'row', gap: 8, paddingHorizontal: 15, paddingVertical: 9 },
  spaceOperatingText: { ...typeScale.label, color: palette.greenDark, fontFamily: font },
  serviceFlow: { alignItems: 'stretch', flexDirection: 'row', gap: 0, overflow: 'hidden', padding: 0 },
  serviceFlowStep: { flex: 1, gap: 4, padding: 18 },
  serviceFlowStepCompact: { flex: 0, width: '100%' },
  serviceFlowStepActive: { backgroundColor: palette.greenSoft, borderColor: '#b9d8c4', borderRadius: 12, borderWidth: 1, flex: 1, gap: 4, padding: 18 },
  serviceFlowLine: { alignSelf: 'center', backgroundColor: '#b8d7c3', height: 1, width: 30 },
  serviceFlowLineCompact: { height: 30, width: 1 },
  serviceFlowNumber: { ...typeScale.caption, color: palette.muted, fontFamily: font, fontWeight: '600' },
  serviceFlowNumberActive: { ...typeScale.caption, color: palette.greenDark, fontFamily: font, fontWeight: '600' },
  serviceFlowLabel: { ...typeScale.cardTitle, color: palette.text, fontFamily: font, fontWeight: '700' },
  serviceFlowState: { ...typeScale.label, color: palette.secondary, fontFamily: font },
  serviceFlowStateActive: { ...typeScale.label, color: palette.greenDark, fontFamily: font },
  scoreHero: { alignItems: 'center', flexDirection: 'row', gap: 44, justifyContent: 'space-between', padding: 38, position: 'relative' },
  scoreHeroCompact: { alignItems: 'flex-start', flexDirection: 'column' },
  scoreHeroCopy: { flex: 1, gap: 12 },
  scoreHeroEyebrow: { ...typeScale.label, color: palette.greenDark, fontFamily: font, letterSpacing: 0.5 },
  scoreHeroValueRow: { alignItems: 'flex-end', flexDirection: 'row', gap: 7 },
  scoreHeroValue: { ...typeScale.score, color: palette.text, fontFamily: font },
  scoreHeroUnit: { ...typeScale.caption, color: palette.muted, marginBottom: 11 },
  scoreHeroGrade: { ...typeScale.bodyStrong, color: palette.secondary, fontFamily: font },
  formulaLink: { alignItems: 'center', flexDirection: 'row', gap: 8, paddingHorizontal: 10, paddingVertical: 10 },
  formulaLinkTop: { position: 'absolute', right: 28, top: 22 },
  formulaLinkText: { ...typeScale.button, color: palette.greenDark, fontFamily: font },
  formulaLinkArrow: { color: palette.green, fontFamily: font, fontSize: 20, fontWeight: '500' },
  dashboardAlertPanel: { borderTopColor: '#ead8b9', borderTopWidth: 1, gap: 24, padding: 30 },
  dashboardAlertHeader: { alignItems: 'center', flexDirection: 'row', gap: 24, justifyContent: 'space-between' },
  dashboardAlertCopy: { flex: 1, gap: 5 },
  dashboardAlertEyebrow: { ...typeScale.label, color: '#8b5d1d', fontFamily: font },
  dashboardAlertTitle: { ...typeScale.sectionTitle, color: palette.text, fontFamily: font },
  dashboardAlertRows: { flexDirection: 'row', gap: 0 },
  dashboardAlertItem: { borderRightColor: 'rgba(201,139,47,0.24)', borderRightWidth: 1, flex: 1, gap: 5, padding: 18 },
  dashboardAlertItemLabel: { ...typeScale.label, color: '#8b5d1d', fontFamily: font },
  dashboardAlertItemValue: { ...typeScale.metric, color: palette.text, fontFamily: font },
  dashboardAlertItemBody: { ...typeScale.body, color: palette.secondary, fontFamily: font },
  metricChartGrid: { alignItems: 'stretch', flexDirection: 'row', gap: 24 },
  metricsColumn: { overflow: 'hidden', paddingHorizontal: 30, width: 330 },
  statCardVertical: { gap: 12, justifyContent: 'center', minHeight: 142, paddingVertical: 24 },
  statCardDivider: { borderBottomColor: palette.lineStrong, borderBottomWidth: 1 },
  statLabel: { ...typeScale.cardTitle, color: palette.secondary, fontFamily: font, fontWeight: '700' },
  statValue: { ...typeScale.metric, color: palette.text, fontFamily: font },
  statDetail: { ...typeScale.body, color: palette.muted, fontFamily: font },
  chartPanel: { alignSelf: 'stretch', flex: 1, gap: 28, padding: 38 },
  chartPanelCompact: { alignSelf: 'stretch', flex: 0, width: '100%' },
  chartStateText: { ...typeScale.body, color: palette.muted, paddingVertical: 28 },
  chartMetricFilter: { gap: 10 },
  chartMetricFilterLabel: { ...typeScale.label, color: palette.muted, fontFamily: font },
  chartMetricOptions: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  chartVisual: { marginTop: 36 },
  chartVisualExpanded: { flex: 1, justifyContent: 'center' },
  chartMetricButton: { ...controlTokens.filter, paddingHorizontal: 13, paddingVertical: 7 },
  chartMetricButtonActive: { backgroundColor: palette.greenSoft, borderColor: '#aad2ba' },
  chartMetricButtonText: { ...typeScale.label, ...controlTextTokens.filter, fontFamily: font },
  chartMetricButtonTextActive: { color: palette.greenDark, fontWeight: '700' },
  rangeControl: { backgroundColor: palette.panelMuted, borderColor: palette.line, borderRadius: 7, borderWidth: 1, flexDirection: 'row', padding: 3 },
  rangeButton: { ...controlTokens.filter, minHeight: 32, paddingHorizontal: 9, paddingVertical: 5 },
  rangeButtonActive: { backgroundColor: palette.panel },
  rangeButtonText: { ...typeScale.label, ...controlTextTokens.filter, color: palette.muted, fontFamily: font },
  rangeButtonTextActive: { color: palette.greenDark, fontWeight: '700' },
  dashboardBottomGrid: { alignItems: 'stretch', flexDirection: 'row', gap: 24 },
  tablePanel: { flex: 1, padding: 34 },
  tableHeader: { borderBottomColor: palette.lineStrong, borderBottomWidth: 1, flexDirection: 'row', marginTop: 24, paddingBottom: 14 },
  tableHeaderText: { ...typeScale.label, color: palette.muted, flex: 1, fontFamily: font },
  tableName: { flex: 1.2 },
  tableRow: { alignItems: 'center', borderBottomColor: palette.line, borderBottomWidth: 1, flexDirection: 'row', minHeight: 68 },
  tableCell: { ...typeScale.body, color: palette.secondary, flex: 1, fontFamily: font },
  tableCellStrong: { ...typeScale.bodyStrong, color: palette.text, flex: 1, fontFamily: font },
  tableNote: { ...typeScale.caption, color: palette.muted, fontFamily: font, lineHeight: 19, marginTop: 14 },
  statusBadge: { alignItems: 'center', backgroundColor: palette.greenSoft, borderRadius: 999, flex: 1, maxWidth: 104, paddingHorizontal: 8, paddingVertical: 7 },
  statusBadgeWarn: { backgroundColor: palette.amberSoft },
  statusBadgeText: { ...typeScale.label, color: palette.greenDark, fontFamily: font },
  statusBadgeTextWarn: { color: palette.amber },
  deviceStatusPanel: { flexBasis: 380, flexGrow: 0, gap: 26, padding: 32 },
}));
