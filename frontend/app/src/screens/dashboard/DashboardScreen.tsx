import { useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { font } from '../../appTheme/glass';
import { palette } from '../../appTheme/palette';
import { scaleTypography } from '../../appTheme/scaleTypography';
import { ActionButton } from '../../components/ActionButton';
import { LineChart } from '../../components/LineChart';
import { SectionHeader } from '../../components/SectionHeader';
import { SensorSummary } from '../../components/SensorSummary';
import { SuitabilityFormulaModal } from '../../components/SuitabilityFormulaModal';
import { Surface } from '../../components/Surface';
import { chartMetrics, crops, factors, latest, sensors } from '../../data';
import type { Page } from '../../navigation/types';
import { useDeviceEnvironment } from '../../shared/device-environment/DeviceEnvironmentProvider';
import { getGradeLabel, getIssueFactors } from '../../shared/factorPresentation';
import { useDisclosure } from '../../shared/hooks/useDisclosure';

function makeWavePoints(seed: number, amplitude: number, center: number): number[] {
  return Array.from(
    { length: 36 },
    (_, index) => center + Math.sin(index * 0.42 + seed) * amplitude + Math.sin(index * 0.13) * 6,
  );
}

const extendedMetricSensors: Array<[label: string, model: string]> = [
  ['토양 온도', 'DS18B20'],
  ['CO₂', 'SCD40'],
  ['미세먼지', 'PMS5003'],
  ['소음', 'SEN0232'],
];

export function DashboardScreen({
  compact,
  onNavigate,
  selectedCrop,
}: {
  compact: boolean;
  onNavigate: (page: Page) => void;
  selectedCrop: number;
}) {
  const currentCrop = crops[selectedCrop] ?? crops[0];
  const [chartRange, setChartRange] = useState<'1h' | '24h' | '7d' | '30d'>('24h');
  const { score: scoreData, measurements: latestData } = useDeviceEnvironment();
  const formulaDisclosure = useDisclosure();
  const ppfd = latestData?.measurements.plantLightPpfdUmolM2S;

  const factorDetail = (key: string) => {
    const factor = scoreData?.factors.find((item) => item.key === key);
    return factor
      ? `적정 범위 ${factor.optimalMin.toLocaleString('ko-KR')}~${factor.optimalMax.toLocaleString('ko-KR')}${factor.unit} · ${factor.score}점`
      : '점수 데이터를 기다리는 중';
  };
  const stats = [
    { label: '온도', value: latestData ? `${latestData.measurements.airTemperatureC}℃` : '--', detail: factorDetail('temperature') },
    { label: '습도', value: latestData ? `${latestData.measurements.airHumidityPct}%` : '--', detail: factorDetail('humidity') },
    { label: '광량', value: ppfd == null ? '--' : `${ppfd.toLocaleString('ko-KR')} PPFD`, detail: ppfd == null ? '광량 센서 측정 불가' : factorDetail('plantLight') },
    { label: '토양수분', value: latestData ? `${latestData.measurements.soilMoisturePct}%` : '--', detail: '종합 적합도 산식에서는 제외' },
  ];
  const displayFactors = scoreData?.factors ?? factors.slice(0, 3);
  const issueFactors = getIssueFactors(scoreData?.factors ?? []);
  const gradeText = getGradeLabel(scoreData?.grade);
  const extendedStats = extendedMetricSensors.map(([label, model]) => {
    const metric = latest.find((item) => item.label === label);
    return { label, value: metric?.value ?? '--', detail: `${model} · 정상` };
  });

  return (
    <View style={styles.pageBody}>
      <Surface style={styles.spaceIdentityPanel}>
        <View style={[styles.spaceIdentityTop, compact && styles.stack]}>
          <View style={styles.spaceIdentityCopy}>
            <Text style={styles.reportLabel}>REGISTERED SPACE</Text>
            <Text style={styles.spaceIdentityTitle}>부산 도심 옥상 A</Text>
            <Text style={styles.spaceIdentityMeta}>옥상 · 42m² · 남동향 · 공간분석 세트 1대 · 토양분석 세트 1대</Text>
          </View>
          <View style={styles.spaceOperatingBadge}><View style={styles.onlineDot} /><Text style={styles.spaceOperatingText}>재배환경 모니터링 중</Text></View>
        </View>
        <View style={[styles.serviceFlow, compact && styles.stack]}>
          <View style={styles.serviceFlowStep}><Text style={styles.serviceFlowNumber}>01</Text><Text style={styles.serviceFlowLabel}>공간 등록</Text><Text style={styles.serviceFlowState}>완료</Text></View>
          <View style={styles.serviceFlowLine} />
          <View style={styles.serviceFlowStep}><Text style={styles.serviceFlowNumber}>02</Text><Text style={styles.serviceFlowLabel}>공간 진단</Text><Text style={styles.serviceFlowState}>완료</Text></View>
          <View style={styles.serviceFlowLine} />
          <View style={styles.serviceFlowStepActive}><Text style={styles.serviceFlowNumberActive}>03</Text><Text style={styles.serviceFlowLabel}>환경 모니터링</Text><Text style={styles.serviceFlowStateActive}>운영 중</Text></View>
        </View>
      </Surface>

      <Surface style={[styles.scoreHero, compact && styles.scoreHeroCompact]}>
        <View style={styles.scoreHeroCopy}>
          <Text style={styles.scoreHeroEyebrow}>스마트팜 전환 적합도</Text>
          <View style={styles.scoreHeroValueRow}>
            <Text style={styles.scoreHeroValue}>{scoreData?.total ?? '--'}</Text>
            <Text style={styles.scoreHeroUnit}>/ 100</Text>
          </View>
          <Text style={styles.scoreHeroGrade}>{gradeText} · {scoreData?.cropName ?? currentCrop.name} 재배 기준</Text>
        </View>
        <Pressable
          accessibilityRole="button"
          onPress={formulaDisclosure.show}
          style={({ pressed }) => [styles.formulaLink, styles.formulaLinkTop, pressed && styles.pressed]}
        >
          <Text style={styles.formulaLinkText}>적합도 계산식</Text>
          <Text style={styles.formulaLinkArrow}>→</Text>
        </Pressable>
      </Surface>

      <SuitabilityFormulaModal onClose={formulaDisclosure.hide} scoreData={scoreData} visible={formulaDisclosure.open} />

      <Surface style={styles.dashboardAlertPanel}>
        <View style={[styles.dashboardAlertHeader, compact && styles.stack]}>
          <View style={styles.dashboardAlertCopy}>
            <Text style={styles.dashboardAlertEyebrow}>현재 확인이 필요한 항목</Text>
            <Text style={styles.dashboardAlertTitle}>확인 필요한 환경 {issueFactors.length}건</Text>
          </View>
          <ActionButton label="분석 보고서 보기" onPress={() => onNavigate('analysis')} quiet />
        </View>
        <View style={[styles.dashboardAlertRows, compact && styles.stack]}>
          {issueFactors.length ? issueFactors.map((factor) => (
            <View key={factor.key} style={styles.dashboardAlertItem}>
              <Text style={styles.dashboardAlertItemLabel}>{factor.label} {factor.status === 'LOW' ? '부족' : '초과'}</Text>
              <Text style={styles.dashboardAlertItemValue}>{factor.current.toLocaleString('ko-KR')}{factor.unit}</Text>
              <Text style={styles.dashboardAlertItemBody}>적정 범위와 {factor.gap.toLocaleString('ko-KR')}{factor.unit} 차이 · 축 점수 {factor.score}점</Text>
            </View>
          )) : <Text style={styles.dashboardAlertItemBody}>현재 세 지표가 모두 적정 범위입니다.</Text>}
        </View>
      </Surface>

      <View style={[styles.metricChartGrid, compact && styles.stack]}>
        <Surface style={[styles.metricsColumn, compact && styles.fullWidth]}>
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
        <Surface style={styles.chartPanel}>
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
            description={`최근 ${chartRange === '1h' ? '1시간' : chartRange === '24h' ? '24시간' : chartRange === '7d' ? '7일' : '30일'} 센서 측정 추이`}
          />
          <LineChart
            axisLabels={['00:00', '06:00', '12:00', '18:00', '현재']}
            height={180}
            series={chartMetrics.map((metric) => ({
              label: metric.label,
              color: metric.color,
              values: makeWavePoints(metric.seed, metric.amp, metric.mid),
            }))}
          />
        </Surface>
      </View>

      <Surface style={styles.extendedMetricsPanel}>
        <SectionHeader title="확장 환경 지표" description="공간분석·토양분석 세트에서 함께 수집하는 운영 지표입니다." />
        <View style={[styles.extendedMetricsGrid, compact && styles.stack]}>
          {extendedStats.map((item) => (
            <View key={item.label} style={styles.extendedMetricItem}>
              <Text style={styles.extendedMetricLabel}>{item.label}</Text>
              <Text style={styles.extendedMetricValue}>{item.value}</Text>
              <Text style={styles.extendedMetricDetail}>{item.detail}</Text>
            </View>
          ))}
        </View>
      </Surface>

      <View style={[styles.dashboardBottomGrid, compact && styles.stack]}>
        <Surface style={styles.tablePanel}>
          <SectionHeader title="환경 상태" description="현재 측정값과 작물별 권장 범위 비교" />
          <View style={styles.tableHeader}>
            <Text style={[styles.tableHeaderText, styles.tableName]}>항목</Text>
            <Text style={styles.tableHeaderText}>현재 값</Text>
            <Text style={styles.tableHeaderText}>권장 범위</Text>
            <Text style={styles.tableHeaderText}>상태</Text>
          </View>
          {displayFactors.map((factor) => (
            <View key={factor.label} style={styles.tableRow}>
              <Text style={[styles.tableCellStrong, styles.tableName]}>{factor.label}</Text>
              <Text style={styles.tableCell}>{factor.current.toLocaleString('ko-KR')}{factor.unit}</Text>
              <Text style={styles.tableCell}>{factor.optimalMin.toLocaleString('ko-KR')}~{factor.optimalMax.toLocaleString('ko-KR')}{factor.unit}</Text>
              <View style={[styles.statusBadge, factor.status !== 'OK' && styles.statusBadgeWarn]}>
                <Text style={[styles.statusBadgeText, factor.status !== 'OK' && styles.statusBadgeTextWarn]}>
                  {factor.status === 'OK' ? '적정' : '확인 필요'}
                </Text>
              </View>
            </View>
          ))}
        </Surface>

        <Surface style={[styles.deviceStatusPanel, compact && styles.fullWidth]}>
          <SectionHeader title="하드웨어 키트" description="공간분석 세트 + 토양분석 세트" />
          <SensorSummary sensors={sensors} statusLabel="정상 수신" />
        </Surface>
      </View>
    </View>
  );
}

const styles = StyleSheet.create(scaleTypography({
  pressed: { opacity: 0.78 },
  onlineDot: { backgroundColor: '#3aad70', borderRadius: 999, height: 7, width: 7 },
  pageBody: { gap: 30, maxWidth: 1320, width: '100%' },
  stack: { flexDirection: 'column' },
  fullWidth: { flexBasis: 'auto', width: '100%' },
  spaceIdentityPanel: { gap: 28, padding: 36 },
  spaceIdentityTop: { alignItems: 'flex-start', flexDirection: 'row', gap: 32, justifyContent: 'space-between' },
  spaceIdentityCopy: { flex: 1, gap: 7 },
  spaceIdentityTitle: { color: palette.text, fontFamily: font, fontSize: 25, fontWeight: '900', letterSpacing: -0.6 },
  spaceIdentityMeta: { color: palette.secondary, fontFamily: font, fontSize: 15, fontWeight: '500', lineHeight: 25, maxWidth: 820 },
  spaceOperatingBadge: { alignItems: 'center', backgroundColor: palette.greenSoft, borderColor: '#c9dfd1', borderRadius: 999, borderWidth: 1, flexDirection: 'row', gap: 8, paddingHorizontal: 15, paddingVertical: 9 },
  spaceOperatingText: { color: palette.greenDark, fontFamily: font, fontSize: 15, fontWeight: '900' },
  serviceFlow: { alignItems: 'center', flexDirection: 'row', gap: 12 },
  serviceFlowStep: { backgroundColor: palette.panelMuted, borderColor: palette.lineStrong, borderRadius: 12, borderWidth: 1, flex: 1, gap: 4, padding: 18 },
  serviceFlowStepActive: { backgroundColor: palette.greenSoft, borderColor: '#b8d7c3', borderRadius: 12, borderWidth: 1, flex: 1, gap: 4, padding: 18 },
  serviceFlowLine: { backgroundColor: '#b8d7c3', height: 1, width: 30 },
  serviceFlowNumber: { color: palette.muted, fontFamily: font, fontSize: 14, fontWeight: '900' },
  serviceFlowNumberActive: { color: palette.greenDark, fontFamily: font, fontSize: 14, fontWeight: '900' },
  serviceFlowLabel: { color: palette.text, fontFamily: font, fontSize: 17, fontWeight: '900' },
  serviceFlowState: { color: palette.secondary, fontFamily: font, fontSize: 14, fontWeight: '700' },
  serviceFlowStateActive: { color: palette.greenDark, fontFamily: font, fontSize: 14, fontWeight: '900' },
  scoreHero: { alignItems: 'center', flexDirection: 'row', gap: 44, justifyContent: 'space-between', padding: 38, position: 'relative' },
  scoreHeroCompact: { alignItems: 'flex-start', flexDirection: 'column' },
  scoreHeroCopy: { flex: 1, gap: 12 },
  scoreHeroEyebrow: { color: palette.greenDark, fontFamily: font, fontSize: 17, fontWeight: '900', letterSpacing: 0.5 },
  scoreHeroValueRow: { alignItems: 'flex-end', flexDirection: 'row', gap: 7 },
  scoreHeroValue: { color: palette.text, fontFamily: font, fontSize: 62, fontWeight: '900', letterSpacing: -2.2, lineHeight: 70 },
  scoreHeroUnit: { color: palette.muted, fontFamily: font, fontSize: 16, marginBottom: 11 },
  scoreHeroGrade: { color: palette.secondary, fontFamily: font, fontSize: 18, fontWeight: '700', lineHeight: 27 },
  formulaLink: { alignItems: 'center', flexDirection: 'row', gap: 8, paddingHorizontal: 10, paddingVertical: 10 },
  formulaLinkTop: { position: 'absolute', right: 28, top: 22 },
  formulaLinkText: { color: palette.greenDark, fontFamily: font, fontSize: 16, fontWeight: '500' },
  formulaLinkArrow: { color: palette.green, fontFamily: font, fontSize: 20, fontWeight: '500' },
  dashboardAlertPanel: { backgroundColor: 'rgba(251,241,223,0.70)', borderColor: 'rgba(201,139,47,0.24)', gap: 24, padding: 30 },
  dashboardAlertHeader: { alignItems: 'center', flexDirection: 'row', gap: 24, justifyContent: 'space-between' },
  dashboardAlertCopy: { flex: 1, gap: 5 },
  dashboardAlertEyebrow: { color: '#8b5d1d', fontFamily: font, fontSize: 15, fontWeight: '900' },
  dashboardAlertTitle: { color: palette.text, fontFamily: font, fontSize: 26, fontWeight: '900' },
  dashboardAlertRows: { flexDirection: 'row', gap: 14 },
  dashboardAlertItem: { backgroundColor: 'rgba(255,255,255,0.46)', borderColor: 'rgba(201,139,47,0.18)', borderRadius: 12, borderWidth: 1, flex: 1, gap: 5, padding: 18 },
  dashboardAlertItemLabel: { color: '#8b5d1d', fontFamily: font, fontSize: 15, fontWeight: '900' },
  dashboardAlertItemValue: { color: palette.text, fontFamily: font, fontSize: 30, fontWeight: '900' },
  dashboardAlertItemBody: { color: palette.secondary, fontFamily: font, fontSize: 16, fontWeight: '500', lineHeight: 25 },
  metricChartGrid: { alignItems: 'stretch', flexDirection: 'row', gap: 24 },
  metricsColumn: { overflow: 'hidden', paddingHorizontal: 30, width: 330 },
  statCardVertical: { gap: 12, justifyContent: 'center', minHeight: 142, paddingVertical: 24 },
  statCardDivider: { borderBottomColor: palette.lineStrong, borderBottomWidth: 1 },
  statLabel: { color: palette.secondary, fontFamily: font, fontSize: 18, fontWeight: '800' },
  statValue: { color: palette.text, fontFamily: font, fontSize: 38, fontWeight: '900', letterSpacing: -0.8 },
  statDetail: { color: palette.muted, fontFamily: font, fontSize: 16, fontWeight: '500', lineHeight: 25 },
  chartPanel: { flex: 1, gap: 32, padding: 34 },
  extendedMetricsPanel: { gap: 26, padding: 34 },
  extendedMetricsGrid: { flexDirection: 'row', gap: 14 },
  extendedMetricItem: { backgroundColor: palette.panelMuted, borderColor: palette.lineStrong, borderRadius: 12, borderWidth: 1, flex: 1, gap: 6, padding: 20 },
  extendedMetricLabel: { color: palette.secondary, fontFamily: font, fontSize: 17, fontWeight: '800' },
  extendedMetricValue: { color: palette.text, fontFamily: font, fontSize: 31, fontWeight: '900' },
  extendedMetricDetail: { color: palette.muted, fontFamily: font, fontSize: 15, fontWeight: '500', lineHeight: 24 },
  rangeControl: { backgroundColor: palette.panelMuted, borderColor: palette.line, borderRadius: 7, borderWidth: 1, flexDirection: 'row', padding: 3 },
  rangeButton: { borderRadius: 5, paddingHorizontal: 9, paddingVertical: 6 },
  rangeButtonActive: { backgroundColor: palette.panel },
  rangeButtonText: { color: palette.muted, fontFamily: font, fontSize: 14, fontWeight: '700' },
  rangeButtonTextActive: { color: palette.greenDark, fontWeight: '900' },
  dashboardBottomGrid: { alignItems: 'stretch', flexDirection: 'row', gap: 24 },
  tablePanel: { flex: 1, padding: 34 },
  tableHeader: { borderBottomColor: palette.lineStrong, borderBottomWidth: 1, flexDirection: 'row', marginTop: 24, paddingBottom: 14 },
  tableHeaderText: { color: palette.muted, flex: 1, fontFamily: font, fontSize: 15, fontWeight: '700' },
  tableName: { flex: 1.2 },
  tableRow: { alignItems: 'center', borderBottomColor: palette.line, borderBottomWidth: 1, flexDirection: 'row', minHeight: 68 },
  tableCell: { color: palette.secondary, flex: 1, fontFamily: font, fontSize: 17 },
  tableCellStrong: { color: palette.text, flex: 1, fontFamily: font, fontSize: 17, fontWeight: '800' },
  statusBadge: { alignItems: 'center', backgroundColor: palette.greenSoft, borderRadius: 999, flex: 1, maxWidth: 104, paddingHorizontal: 8, paddingVertical: 7 },
  statusBadgeWarn: { backgroundColor: palette.amberSoft },
  statusBadgeText: { color: palette.greenDark, fontFamily: font, fontSize: 15, fontWeight: '800' },
  statusBadgeTextWarn: { color: palette.amber },
  deviceStatusPanel: { flexBasis: 380, flexGrow: 0, gap: 26, padding: 32 },
  reportLabel: { color: palette.greenDark, fontFamily: font, fontSize: 15, fontWeight: '900', letterSpacing: 1.2 },
}));
