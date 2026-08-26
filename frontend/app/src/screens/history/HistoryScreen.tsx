import { useEffect, useState } from 'react';
import { StyleSheet, Text, View } from 'react-native';

import { font } from '../../appTheme/glass';
import { palette } from '../../appTheme/palette';
import { scaleTypography } from '../../appTheme/scaleTypography';
import { typeScale } from '../../appTheme/typography';
import { ActionButton } from '../../components/ActionButton';
import { SectionHeader } from '../../components/SectionHeader';
import {
  describeCommandState,
  getCommandHistory,
  type CommandHistoryEntry,
} from '../../irrigation/commandApi';
import { Surface } from '../../components/Surface';
import { getDiagnosticHistory, type DiagnosticHistoryRecord } from '../../history/historyApi';
import type { Page } from '../../navigation/types';

export function HistoryScreen({ compact, onNavigate, potId }: { compact: boolean; onNavigate: (page: Page) => void; potId?: number }) {
  const [records, setRecords] = useState<DiagnosticHistoryRecord[]>([]);
  const [commands, setCommands] = useState<CommandHistoryEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    if (!potId) {
      setRecords([]);
      setLoading(false);
      return () => { active = false; };
    }
    setLoading(true);
    setError(null);
    void getDiagnosticHistory(potId)
      .then((history) => { if (active) setRecords(history); })
      .catch((caught) => {
        if (active) setError(caught instanceof Error ? caught.message : '진단 이력을 불러오지 못했습니다.');
      })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [potId]);

  useEffect(() => {
    if (potId === undefined) {
      setCommands([]);
      return undefined;
    }
    let active = true;
    void getCommandHistory(potId, 20)
      .then((loaded) => { if (active) setCommands(loaded); })
      // Silent: the diagnostic history above is the reason this screen exists,
      // and an error banner for the secondary panel would sit on top of it.
      .catch(() => { if (active) setCommands([]); });
    return () => { active = false; };
  }, [potId]);

  const formatCommandTime = (iso: string) =>
    new Date(iso).toLocaleString('ko-KR', {
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
    });

  const describeCommand = (entry: CommandHistoryEntry) => {
    if (entry.actuator === 'light') {
      return entry.action === 'on' ? '조명 켜기' : '조명 끄기';
    }
    // What came out, falling back to what was authorised. The two differ
    // whenever the firmware cut a run short, and the difference is the point.
    const ml = entry.actualMl ?? entry.grantedMl;
    return ml === null ? '관수' : `관수 ${ml} mL`;
  };

  const initialScore = records[records.length - 1]?.score ?? 0;
  const currentScore = records[0]?.score ?? 0;
  const accumulatedImprovement = currentScore - initialScore;
  const observedDates = records.map((record) => new Date(record.observedAt));
  const collectedDays = observedDates.length > 1
    ? Math.max(1, Math.ceil((observedDates[0].getTime() - observedDates[observedDates.length - 1].getTime()) / (1000 * 60 * 60 * 24)))
    : 0;
  const formatDate = (observedAt: string) => new Date(observedAt).toLocaleDateString('ko-KR', { year: 'numeric', month: '2-digit', day: '2-digit' });

  return (
    <View style={styles.pageBody}>
      <Surface flat style={styles.historySummaryPanel}>
        <SectionHeader title="환경 진단 변화" description="최초 공간 진단부터 현재 모니터링까지의 변화입니다." />
        <View style={[styles.historySummaryGrid, compact && styles.stack]}>
          <View style={styles.historySummaryItem}><Text style={styles.historySummaryLabel}>현재 적합도</Text><Text style={[styles.historySummaryValue, styles.historyScoreCurrent]}>{records.length ? `${Math.round(currentScore)}점` : '--'}</Text></View>
          <View style={styles.historySummaryItem}><Text style={styles.historySummaryLabel}>최초 적합도</Text><Text style={[styles.historySummaryValue, styles.historyScoreInitial]}>{records.length ? `${Math.round(initialScore)}점` : '--'}</Text></View>
          <View style={styles.historySummaryItem}><Text style={styles.historySummaryLabel}>누적 개선</Text><Text style={[styles.historySummaryValue, accumulatedImprovement > 0 ? styles.historyScoreUp : accumulatedImprovement < 0 ? styles.historyScoreDown : styles.historyScoreInitial]}>{records.length ? `${accumulatedImprovement > 0 ? '+' : ''}${Math.round(accumulatedImprovement)}점` : '--'}</Text></View>
          <View style={styles.historySummaryItem}><Text style={styles.historySummaryLabel}>수집 데이터</Text><Text style={styles.historySummaryValue}>{records.length ? `${collectedDays}일` : '--'}</Text></View>
        </View>
      </Surface>

      <Surface flat style={styles.historyPanel}>
        <SectionHeader title="진단 기록" description="측정 조건과 개선 전후 결과를 시간순으로 확인할 수 있습니다." />
        <View style={styles.historyList}>
          {records.map((record, index) => (
            <View key={record.observedAt} style={[styles.historyRow, compact && styles.stack]}>
              <View style={styles.historyDateBlock}><Text style={styles.historyDate}>{formatDate(record.observedAt)}</Text></View>
              <View style={styles.historyScoreBlock}><Text style={[styles.historyScore, record.score > initialScore ? styles.historyScoreUp : record.score < initialScore ? styles.historyScoreDown : styles.historyScoreInitial]}>{Math.round(record.score)}</Text><Text style={styles.historyScoreUnit}>점</Text></View>
              <View style={styles.historyCopy}><Text style={styles.historyTitle}>{record.summary}</Text><Text style={styles.historyIssue}>주요 결과 · {record.issues}</Text></View>
              {index === 0 ? <ActionButton label="보고서 열기" onPress={() => onNavigate('analysis')} quiet /> : <Text style={styles.historyArchived}>보관된 보고서</Text>}
            </View>
          ))}
          {loading ? <Text style={styles.historyIssue}>진단 이력을 불러오는 중입니다.</Text> : null}
          {error ? <Text accessibilityRole="alert" style={styles.historyIssue}>{error}</Text> : null}
          {!loading && !error && !records.length ? <Text style={styles.historyIssue}>표시할 진단 이력이 아직 없습니다.</Text> : null}
        </View>
      </Surface>

      <Surface flat style={styles.historyPanel}>
        <SectionHeader
          title="제어 기록"
          description="펌프와 조명에 보낸 명령과 그 결과입니다. 거절된 요청도 함께 남습니다."
        />
        <View style={styles.historyList}>
          {commands.map((entry) => (
            <View key={entry.commandId} style={[styles.commandRow, compact && styles.stack]}>
              <View style={styles.historyDateBlock}>
                <Text style={styles.commandTime}>{formatCommandTime(entry.issuedAt)}</Text>
              </View>
              <View style={styles.historyCopy}>
                <Text style={styles.historyTitle}>{describeCommand(entry)}</Text>
                <Text style={styles.historyIssue}>
                  {describeCommandState(entry.state)}
                  {entry.stopCause ? ` · ${entry.stopCause}` : ''}
                  {entry.origin === 'EDGE_FALLBACK' ? ' · 오프라인 자율 관수' : ''}
                </Text>
              </View>
            </View>
          ))}
          {!commands.length ? (
            <Text style={styles.historyIssue}>표시할 제어 기록이 아직 없습니다.</Text>
          ) : null}
        </View>
      </Surface>

      <Surface flat style={styles.historyComparePanel}>
          <View style={styles.historyCompareCopy}><Text style={styles.historyCompareTitle}>최초 측정 이후 환경 적합도 변화를 확인할 수 있습니다.</Text><Text style={styles.historyCompareBody}>저장된 측정 시점의 온도·습도·광량으로 적합도를 다시 계산한 기록입니다.</Text></View>
          <View style={styles.historyCompareValue}><Text style={styles.historyCompareValueLabel}>적합도 변화</Text><Text style={styles.historyCompareValueNumber}><Text style={styles.historyCompareValueBaseline}>{records.length ? Math.round(initialScore) : '--'}</Text><Text style={styles.historyCompareValueArrow}> → </Text><Text style={styles.historyScoreUp}>{records.length ? Math.round(currentScore) : '--'}</Text></Text></View>
      </Surface>
    </View>
  );
}

const styles = StyleSheet.create(scaleTypography({
  pageBody: { gap: 30, maxWidth: 1320, width: '100%' },
  stack: { flexDirection: 'column' },
  historySummaryPanel: { gap: 28, padding: 36 },
  historySummaryGrid: { flexDirection: 'row', gap: 0 },
  historySummaryItem: { borderRightColor: palette.lineStrong, borderRightWidth: 1, flex: 1, gap: 7, padding: 22 },
  historySummaryLabel: { ...typeScale.label, color: palette.muted, fontFamily: font },
  historySummaryValue: { ...typeScale.cardTitle, color: palette.text, fontFamily: font },
  historyPanel: { gap: 28, padding: 36 },
  historyList: { overflow: 'hidden' },
  historyRow: { alignItems: 'center', borderBottomColor: palette.lineStrong, borderBottomWidth: 1, flexDirection: 'row', gap: 22, minHeight: 124, padding: 24 },
  historyDateBlock: { gap: 5, width: 120 },
  commandRow: {
    alignItems: 'center', borderBottomColor: palette.lineStrong, borderBottomWidth: 1,
    flexDirection: 'row', gap: 22, minHeight: 68, paddingHorizontal: 24, paddingVertical: 16,
  },
  commandTime: { ...typeScale.caption, color: palette.muted, fontFamily: font },
  historyDate: { ...typeScale.cardTitle, color: palette.text, fontFamily: font, fontSize: 16, fontWeight: '600', lineHeight: 22 },
  historyScoreBlock: { alignItems: 'flex-end', flexDirection: 'row', minWidth: 100 },
  historyScore: { ...typeScale.metric, color: palette.greenDark, fontFamily: font },
  historyScoreUp: { color: palette.red },
  historyScoreDown: { color: '#2f6ea6' },
  historyScoreInitial: { color: palette.text },
  historyScoreCurrent: { color: palette.greenDark },
  historyScoreUnit: { ...typeScale.caption, color: palette.muted, marginBottom: 5 },
  historyCopy: { flex: 1, gap: 7 },
  historyTitle: { ...typeScale.cardTitle, color: palette.text, fontFamily: font, fontWeight: '600' },
  historyIssue: { ...typeScale.body, color: palette.secondary, fontFamily: font },
  historyArchived: { ...typeScale.button, color: palette.muted, fontFamily: font },
  historyComparePanel: { alignItems: 'center', flexDirection: 'row', gap: 32, justifyContent: 'space-between', padding: 36 },
  historyCompareCopy: { flex: 1, gap: 8 },
  historyCompareTitle: { ...typeScale.cardTitle, color: palette.text, fontFamily: font },
  historyCompareBody: { ...typeScale.body, color: palette.secondary, fontFamily: font, maxWidth: 760 },
  historyCompareValue: { gap: 7, minWidth: 180, padding: 24 },
  historyCompareValueLabel: { ...typeScale.label, color: palette.greenDark, fontFamily: font },
  historyCompareValueNumber: { ...typeScale.metric, color: palette.greenDark, fontFamily: font },
  historyCompareValueBaseline: { color: palette.text },
  historyCompareValueArrow: { color: palette.muted },
}));
