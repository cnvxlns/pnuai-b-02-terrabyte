import { useState } from 'react';
import { Modal, Pressable, StyleSheet, Text, View } from 'react-native';

import { font } from '../appTheme/glass';
import { palette } from '../appTheme/palette';
import { scaleTypography } from '../appTheme/scaleTypography';
import { typeScale } from '../appTheme/typography';
import { Surface } from '../components/Surface';
import type { PotResponse } from '../device/deviceApi';
import { useDeviceEnvironment } from '../shared/device-environment/DeviceEnvironmentProvider';
import { deriveEnvironmentAlerts } from '../shared/factorPresentation';
import { PotMenu } from './PotMenu';
import type { Page } from './types';

const pageCopy: Record<Page, { title: string; description: string }> = {
  dashboard: { title: '공간 개요', description: '스마트팜 전환 적합도와 운영 중인 재배 환경을 확인하세요.' },
  analysis: { title: '공간 진단', description: '설치 전 공간 조건과 작물별 재배 가능성을 분석한 보고서입니다.' },
  live: { title: '환경 모니터링', description: '공간분석·토양분석 세트가 전송하는 최신 값을 확인하세요.' },
  history: { title: '진단 이력', description: '공간별 진단 결과와 환경 변화 기록을 비교하세요.' },
  guide: { title: '관리 가이드', description: '현재 환경과 재배 단계에 맞는 관리 작업을 확인하세요.' },
  shop: { title: '제품 추가 구매', description: '필요한 센서·장비·흙과 배지를 추가로 구매하세요.' },
};

type HeaderProps = {
  compact: boolean;
  page: Page;
  pots: PotResponse[];
  selectedPotId?: number;
  onSelectPot: (potId: number) => void;
  onCreatePot: (label: string, cropCode: string) => Promise<void>;
  onUpdatePot: (potId: number, label: string, cropCode: string) => Promise<void>;
};

export function Header({ compact, onCreatePot, onSelectPot, onUpdatePot, page, pots, selectedPotId }: HeaderProps) {
  const copy = pageCopy[page];
  const [alertsOpen, setAlertsOpen] = useState(false);
  const { score } = useDeviceEnvironment();
  const alerts = deriveEnvironmentAlerts(score?.factors ?? []);
  // 읽음 표시는 알림 id로만 기억한다. 알림 자체는 3초마다 점수에서 다시 파생되므로
  // 읽음 상태를 알림 객체에 얹으면 매번 지워진다. 지표가 정상으로 돌아왔다가 다시
  // 이탈하면 새로운 알림으로 보아야 하므로, 사라진 id의 읽음 표시는 정리한다.
  const [readAlertIds, setReadAlertIds] = useState<string[]>([]);
  const activeReadAlertIds = readAlertIds.filter((id) => alerts.some((alert) => alert.id === id));
  const unreadAlertCount = alerts.filter((alert) => !activeReadAlertIds.includes(alert.id)).length;

  const markAllAlertsRead = () => {
    setReadAlertIds(alerts.map((alert) => alert.id));
  };

  // "10분 전" 같은 상대 시각 대신 측정 시각을 그대로 보여준다. 알림이 만들어진 시점이
  // 따로 있는 게 아니라 이 측정값이 근거 전부이기 때문이다.
  const measuredAtLabel = score
    ? `${new Date(score.measuredAt).toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' })} 기준`
    : '';

  return (
    <>
      <View style={[styles.header, compact && styles.headerCompact]}>
        <View style={styles.headerCopy}>
          <Text style={styles.pageTitle}>{copy.title}</Text>
          <Text style={styles.pageDescription}>{copy.description}</Text>
        </View>
        <View style={[styles.headerActions, compact && styles.headerActionsCompact]}>
          <PotMenu
            compact={compact}
            onCreatePot={onCreatePot}
            onSelectPot={onSelectPot}
            onUpdatePot={onUpdatePot}
            pots={pots}
            selectedPotId={selectedPotId}
          />
          <Pressable
            accessibilityLabel={`알림${unreadAlertCount ? ` ${unreadAlertCount}건` : ''}`}
            accessibilityRole="button"
            onPress={() => setAlertsOpen(true)}
            style={styles.headerAlertButton}
          >
            <View style={styles.bellIcon}>
              <View style={styles.bellBody} />
              <View style={styles.bellBase} />
              <View style={styles.bellClapper} />
            </View>
            {unreadAlertCount ? <Text style={styles.headerAlertCount}>{unreadAlertCount}</Text> : null}
          </Pressable>
        </View>
      </View>
      <Modal animationType="fade" onRequestClose={() => setAlertsOpen(false)} transparent visible={alertsOpen}>
        <View style={styles.modalBackdrop}>
          <Surface style={styles.alertModal}>
            <View style={styles.modalHeader}>
              <View style={styles.modalHeaderCopy}>
                <Text style={styles.modalTitle}>알림</Text>
                <Text style={styles.modalDescription}>확인이 필요한 환경 알림을 모아봤어요.</Text>
              </View>
              <Pressable
                accessibilityLabel="알림 닫기"
                accessibilityRole="button"
                onPress={() => setAlertsOpen(false)}
                style={styles.modalCloseIcon}
              >
                <Text style={styles.modalCloseIconText}>×</Text>
              </Pressable>
            </View>
            <View style={styles.alertList}>
              {alerts.length ? alerts.map((alert, index) => (
                <View key={alert.id} style={[styles.alertItem, index < alerts.length - 1 && styles.alertItemDivider, activeReadAlertIds.includes(alert.id) && styles.alertItemRead]}>
                  <View style={styles.alertItemHeader}>
                    <Text style={[styles.alertSeverity, alert.severity === '주의' ? styles.alertSeverityWarning : styles.alertSeverityCheck]}>{alert.severity}</Text>
                    {measuredAtLabel ? <Text style={styles.alertTime}>{measuredAtLabel}</Text> : null}
                  </View>
                  <Text style={styles.alertTitle}>{alert.title}</Text>
                  <Text style={styles.alertBody}>{alert.body}</Text>
                </View>
              )) : <Text style={styles.emptyAlerts}>새 알림이 없습니다.</Text>}
            </View>
            {alerts.length ? <Text style={styles.alertPolicy}>알림 기준: 최신 측정값이 작물별 권장 범위를 벗어나면 안내합니다.</Text> : null}
            <View style={styles.modalFooter}>
              <Pressable disabled={!unreadAlertCount} onPress={markAllAlertsRead} style={[styles.modalAction, !unreadAlertCount && styles.modalActionDisabled]}>
                <Text style={styles.modalActionText}>모두 읽음</Text>
              </Pressable>
            </View>
          </Surface>
        </View>
      </Modal>
    </>
  );
}

const styles = StyleSheet.create(scaleTypography({
  header: { alignItems: 'flex-start', flexDirection: 'row', gap: 30, paddingBottom: 24, paddingLeft: 48, paddingRight: 48, paddingTop: 40 },
  headerCompact: { alignItems: 'flex-start', flexDirection: 'column', paddingHorizontal: 20, paddingTop: 24 },
  headerCopy: { flex: 1, gap: 8, maxWidth: 820 },
  pageTitle: { ...typeScale.pageTitle, color: palette.text, fontFamily: font },
  pageDescription: { ...typeScale.body, color: palette.secondary, fontFamily: font },
  headerActions: { alignItems: 'center', flexDirection: 'row', gap: 12, marginLeft: 'auto', paddingTop: 2 },
  headerActionsCompact: { alignSelf: 'flex-end', marginLeft: 0, paddingTop: 0 },
  headerAlertButton: { alignItems: 'center', height: 46, justifyContent: 'center', position: 'relative', width: 46 },
  headerAlertCount: { ...typeScale.caption, backgroundColor: palette.text, borderRadius: 999, color: '#ffffff', fontFamily: font, fontWeight: '700', minWidth: 18, overflow: 'hidden', paddingHorizontal: 4, paddingVertical: 2, position: 'absolute', right: 0, textAlign: 'center', top: 0 },
  bellIcon: { height: 23, position: 'relative', width: 23 },
  bellBody: { backgroundColor: 'transparent', borderColor: palette.text, borderTopLeftRadius: 9, borderTopRightRadius: 9, borderBottomLeftRadius: 5, borderBottomRightRadius: 5, borderWidth: 2, height: 14, left: 4, position: 'absolute', top: 2, width: 15 },
  bellBase: { backgroundColor: palette.text, borderRadius: 999, height: 2, left: 2, position: 'absolute', top: 16, width: 19 },
  bellClapper: { backgroundColor: 'transparent', borderColor: palette.text, borderRadius: 999, borderWidth: 1.5, height: 4, left: 9.5, position: 'absolute', top: 18, width: 4 },
  modalBackdrop: { alignItems: 'center', backgroundColor: 'rgba(21, 46, 35, 0.34)', flex: 1, justifyContent: 'center', padding: 22 },
  alertModal: { gap: 24, maxHeight: '84%', maxWidth: 680, padding: 30, width: '100%' },
  modalHeader: { alignItems: 'flex-start', flexDirection: 'row', gap: 18, justifyContent: 'space-between' },
  modalHeaderCopy: { flex: 1, gap: 5 },
  modalTitle: { ...typeScale.dialogTitle, color: palette.text, fontFamily: font },
  modalDescription: { ...typeScale.body, color: palette.secondary, fontFamily: font },
  modalFooter: { alignItems: 'flex-end' },
  modalAction: { alignItems: 'center', borderColor: palette.lineStrong, borderRadius: 8, borderWidth: 1, justifyContent: 'center', minHeight: 36, paddingHorizontal: 12 },
  modalActionDisabled: { opacity: 0.4 },
  modalActionText: { ...typeScale.button, color: palette.secondary, fontFamily: font },
  modalCloseIcon: { alignItems: 'center', height: 36, justifyContent: 'center', marginRight: -10, width: 36 },
  modalCloseIconText: { color: palette.secondary, fontFamily: font, fontSize: 34, fontWeight: '500', lineHeight: 36 },
  alertList: { backgroundColor: palette.panelMuted, borderColor: palette.lineStrong, borderRadius: 12, borderWidth: 1, overflow: 'hidden' },
  alertItem: { gap: 8, padding: 20 },
  alertItemDivider: { borderBottomColor: palette.lineStrong, borderBottomWidth: 1 },
  alertItemRead: { opacity: 0.62 },
  alertItemHeader: { alignItems: 'center', flexDirection: 'row', justifyContent: 'space-between' },
  alertSeverity: { ...typeScale.label, alignSelf: 'flex-start', borderRadius: 999, borderWidth: 1, fontFamily: font, paddingHorizontal: 10, paddingVertical: 4 },
  alertSeverityWarning: { backgroundColor: 'rgba(196,94,85,0.14)', borderColor: 'rgba(196,94,85,0.32)', color: palette.red },
  alertSeverityCheck: { backgroundColor: palette.amberSoft, borderColor: 'rgba(201,139,47,0.32)', color: palette.amber },
  alertTime: { ...typeScale.caption, color: palette.muted, fontFamily: font },
  alertTitle: { ...typeScale.cardTitle, color: palette.text, fontFamily: font, fontWeight: '800' },
  alertBody: { ...typeScale.body, color: palette.secondary, fontFamily: font },
  emptyAlerts: { ...typeScale.body, color: palette.muted, paddingVertical: 30, textAlign: 'center' },
  alertPolicy: { ...typeScale.caption, color: palette.muted, fontFamily: font },
}));
