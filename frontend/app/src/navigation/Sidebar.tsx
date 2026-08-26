import { useEffect, useState } from 'react';
import { Modal, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';

import { glassWebStyle, font } from '../appTheme/glass';
import { palette } from '../appTheme/palette';
import { scaleTypography } from '../appTheme/scaleTypography';
import { typeScale } from '../appTheme/typography';
import { SensorSummary } from '../components/SensorSummary';
import { Surface } from '../components/Surface';
import type { DeviceResponse } from '../device/deviceApi';
import { getDeviceSensors, type DeviceSensorStatus } from '../sensor/sensorApi';
import type { Page } from './types';

const navItems: Array<{ key: Page; label: string }> = [
  { key: 'dashboard', label: '공간 개요' },
  { key: 'analysis', label: '공간 진단' },
  { key: 'live', label: '환경 모니터링' },
  { key: 'history', label: '진단 이력' },
  { key: 'guide', label: '관리 가이드' },
  { key: 'shop', label: '제품 추가 구매' },
];

export function Sidebar({ compact, cropName, device, onHide, onLogout, onNavigate, page }: {
  compact: boolean;
  cropName: string;
  device?: DeviceResponse;
  onHide?: () => void;
  onLogout: () => void;
  onNavigate: (page: Page) => void;
  page: Page;
}) {
  const [farmInfoOpen, setFarmInfoOpen] = useState(false);
  const [deviceStatusOpen, setDeviceStatusOpen] = useState(false);
  const [deviceSensors, setDeviceSensors] = useState<DeviceSensorStatus[]>([]);
  const [sensorsLoading, setSensorsLoading] = useState(false);
  const [sensorsError, setSensorsError] = useState<string | null>(null);
  const online = device?.status === 'ONLINE';
  const connectionStatus = device ? (online ? '정상 연결' : '오프라인') : '기기 정보 없음';
  const spaceName = device?.space?.name ?? '등록된 공간 없음';
  const lastSeen = device?.lastSeenAt
    ? new Date(device.lastSeenAt).toLocaleString('ko-KR', { dateStyle: 'short', timeStyle: 'short' })
    : '최근 수신 정보 없음';
  const farmInfoRows = [
    { label: '공간 유형', value: device?.space?.spaceType ?? '-' },
    { label: '공간 면적', value: device?.space ? `${device.space.areaSquareMeters}m²` : '-' },
    { label: '재배 작물', value: cropName },
    { label: '등록 기기', value: device?.serialCode ?? '-' },
    { label: '연결 화분', value: `${device?.pots?.length ?? 0}개` },
    { label: '마지막 수신', value: lastSeen },
  ];

  useEffect(() => {
    if (!device?.id) {
      setDeviceSensors([]);
      return undefined;
    }
    let active = true;
    setSensorsLoading(true);
    setSensorsError(null);
    void getDeviceSensors(device.id)
      .then((response) => {
        if (active) setDeviceSensors(response.sensors);
      })
      .catch((error) => {
        if (active) setSensorsError(error instanceof Error ? error.message : '센서 상태를 불러오지 못했습니다.');
      })
      .finally(() => {
        if (active) setSensorsLoading(false);
      });
    return () => {
      active = false;
    };
  }, [device?.id]);

  if (compact) {
    return (
      <View style={[styles.mobileNav, glassWebStyle]}>
        <Text style={styles.mobileBrandName}>TerraByte</Text>
        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.mobileNavItems}>
          {navItems.map((item) => (
            <Pressable key={item.key} onPress={() => onNavigate(item.key)} style={styles.mobileNavItem}>
              <Text style={[styles.mobileNavText, page === item.key && styles.mobileNavTextActive]}>{item.label}</Text>
            </Pressable>
          ))}
        </ScrollView>
      </View>
    );
  }

  return (
    <>
    <View style={[styles.sidebar, glassWebStyle]}>
      <View style={styles.brandRow}>
        <Text style={styles.brandName}>TerraByte</Text>
        {onHide ? (
          <Pressable accessibilityLabel="사이드바 숨기기" accessibilityRole="button" onPress={onHide} style={({ pressed }) => [styles.hideButton, pressed && styles.pressed]}>
            <Text style={styles.hideButtonText}>☰</Text>
          </Pressable>
        ) : null}
      </View>

      <ScrollView showsVerticalScrollIndicator style={styles.sidebarScroll}>
      <Text style={styles.navCaption}>메뉴</Text>
      <View style={styles.navList}>
        {navItems.map((item) => {
          const active = item.key === page;
          return (
            <Pressable
              accessibilityRole="button"
              key={item.key}
              onPress={() => onNavigate(item.key)}
              style={[styles.navItem, active && styles.navItemActive]}
            >
              <Text style={[styles.navItemText, active && styles.navItemTextActive]}>{item.label}</Text>
            </Pressable>
          );
        })}
      </View>

      <View style={styles.sidebarBottom}>
        <View style={styles.devicePanel}>
          <View style={styles.deviceStatusRow}>
            <View style={[styles.onlineDot, !online && styles.offlineDot]} />
            <Text style={styles.deviceStatus}>{connectionStatus}</Text>
          </View>
          <Text style={styles.deviceTitle}>{spaceName}</Text>
          <Text style={styles.deviceDetail}>{lastSeen}</Text>
        </View>
        <Pressable accessibilityRole="button" onPress={() => setFarmInfoOpen(true)} style={({ pressed }) => [styles.sidebarActionButton, pressed && styles.pressed]}>
          <Text style={styles.sidebarActionText}>스마트팜 정보 보기</Text>
        </Pressable>
        <Pressable accessibilityRole="button" onPress={() => setDeviceStatusOpen(true)} style={({ pressed }) => [styles.sidebarActionButton, pressed && styles.pressed]}>
          <Text style={styles.sidebarActionText}>디바이스 상태 보기</Text>
        </Pressable>
        <Pressable accessibilityRole="button" onPress={onLogout} style={({ pressed }) => [styles.sidebarActionButton, pressed && styles.pressed]}>
          <Text style={styles.sidebarActionTextMuted}>로그아웃</Text>
        </Pressable>
      </View>
      </ScrollView>
    </View>
    <Modal animationType="fade" onRequestClose={() => setFarmInfoOpen(false)} transparent visible={farmInfoOpen}>
      <View style={styles.modalBackdrop}>
        <Surface style={styles.infoModal}>
          <View style={styles.modalHeader}>
            <View style={styles.modalHeaderCopy}>
              <Text style={styles.modalTitle}>{spaceName}</Text>
            </View>
            <Pressable onPress={() => setFarmInfoOpen(false)} style={styles.modalClose}>
              <Text style={styles.modalCloseText}>닫기</Text>
            </Pressable>
          </View>
          <ScrollView contentContainerStyle={styles.modalScrollContent} showsVerticalScrollIndicator style={styles.modalScroll}>
            <View style={styles.farmStatusSummary}>
              <View style={[styles.onlineDot, !online && styles.offlineDot]} />
              <View style={styles.farmStatusCopy}>
                <Text style={styles.farmStatusTitle}>{connectionStatus}</Text>
                <Text style={styles.farmStatusBody}>{device ? '기기와 연결된 화분 정보를 API에서 불러왔습니다.' : '등록된 기기 정보를 불러오지 못했습니다.'}</Text>
              </View>
            </View>
            <View style={styles.productInfoList}>
              {farmInfoRows.map((row) => (
                <View key={row.label} style={styles.productInfoRow}><Text style={styles.productInfoLabel}>{row.label}</Text><Text style={styles.productInfoValue}>{row.value}</Text></View>
              ))}
            </View>
          </ScrollView>
        </Surface>
      </View>
    </Modal>
    <Modal animationType="fade" onRequestClose={() => setDeviceStatusOpen(false)} transparent visible={deviceStatusOpen}>
      <View style={styles.modalBackdrop}>
        <Surface style={styles.infoModal}>
          <View style={styles.modalHeader}>
            <View style={styles.modalHeaderCopy}>
              <Text style={styles.modalTitle}>디바이스 상태</Text>
            </View>
            <Pressable onPress={() => setDeviceStatusOpen(false)} style={styles.modalClose}>
              <Text style={styles.modalCloseText}>닫기</Text>
            </Pressable>
          </View>
          <ScrollView contentContainerStyle={styles.modalScrollContent} showsVerticalScrollIndicator style={styles.modalScroll}>
            {sensorsLoading ? <Text style={styles.sensorStatusText}>센서 상태를 불러오는 중입니다.</Text> : null}
            {!sensorsLoading && sensorsError ? <Text style={styles.sensorStatusText}>{sensorsError}</Text> : null}
            {!sensorsLoading && !sensorsError ? <SensorSummary sensors={deviceSensors} statusLabel={connectionStatus} /> : null}
          </ScrollView>
        </Surface>
      </View>
    </Modal>
    </>
  );
}

const styles = StyleSheet.create(scaleTypography({
  pressed: { opacity: 0.78 },
  sidebar: { backgroundColor: 'rgba(255,255,255,0.44)', borderColor: palette.line, borderRightWidth: 1, paddingBottom: 30, paddingHorizontal: 22, paddingTop: 38, width: 240, zIndex: 2 },
  sidebarScroll: { flex: 1, marginTop: 54, minHeight: 0 },
  brandRow: { alignItems: 'center', flexDirection: 'row', flexShrink: 0, justifyContent: 'space-between', paddingHorizontal: 10 },
  brandName: { ...typeScale.cardTitle, color: palette.text, fontFamily: font },
  hideButton: { alignItems: 'center', borderColor: palette.line, borderRadius: 8, borderWidth: 1, height: 30, justifyContent: 'center', marginRight: -8, width: 30 },
  hideButtonText: { color: palette.secondary, fontFamily: font, fontSize: 17, fontWeight: '700' },
  navCaption: { ...typeScale.label, color: palette.muted, fontFamily: font, fontWeight: '700', letterSpacing: 0.3, marginBottom: 16, marginTop: 0, paddingHorizontal: 13 },
  navList: { gap: 8 },
  navItem: { borderLeftColor: 'transparent', borderLeftWidth: 3, borderRadius: 9, justifyContent: 'center', minHeight: 48, paddingHorizontal: 15 },
  navItemActive: { backgroundColor: palette.greenSoft, borderLeftColor: palette.green },
  navItemText: { ...typeScale.bodyStrong, color: palette.secondary, fontFamily: font },
  navItemTextActive: { color: palette.greenDark, fontWeight: '700' },
  sidebarBottom: { gap: 10, marginTop: 40, paddingTop: 0 },
  devicePanel: { backgroundColor: palette.panelMuted, borderColor: palette.line, borderRadius: 10, borderWidth: 1, padding: 14 },
  deviceStatusRow: { alignItems: 'center', flexDirection: 'row', gap: 7 },
  onlineDot: { backgroundColor: '#3aad70', borderRadius: 999, height: 7, width: 7 },
  offlineDot: { backgroundColor: palette.muted },
  deviceStatus: { ...typeScale.caption, color: palette.greenDark, fontFamily: font },
  deviceTitle: { ...typeScale.bodyStrong, color: palette.text, fontFamily: font, fontWeight: '700', marginTop: 8 },
  deviceDetail: { ...typeScale.caption, color: palette.muted, fontFamily: font, marginTop: 5 },
  sensorStatusText: { ...typeScale.body, color: palette.muted, fontFamily: font },
  sidebarActionButton: { alignItems: 'center', borderColor: palette.line, borderRadius: 8, borderWidth: 1, minHeight: 42, justifyContent: 'center', paddingHorizontal: 12, paddingVertical: 7 },
  sidebarActionText: { ...typeScale.caption, color: palette.greenDark, fontFamily: font, fontWeight: '700' },
  sidebarActionTextMuted: { ...typeScale.caption, color: palette.secondary, fontFamily: font, fontWeight: '700' },
  mobileNav: { alignItems: 'center', backgroundColor: 'rgba(255,255,255,0.48)', borderBottomColor: palette.line, borderBottomWidth: 1, flexDirection: 'row', gap: 20, minHeight: 68, paddingHorizontal: 22, zIndex: 2 },
  mobileBrandName: { ...typeScale.cardTitle, color: palette.text, fontFamily: font },
  mobileNavItems: { alignItems: 'center', gap: 18 },
  mobileNavItem: { paddingVertical: 12 },
  mobileNavText: { ...typeScale.bodyStrong, color: palette.muted, fontFamily: font },
  mobileNavTextActive: { color: palette.greenDark },
  modalBackdrop: { alignItems: 'center', backgroundColor: 'rgba(21, 46, 35, 0.34)', flex: 1, justifyContent: 'center', padding: 22 },
  infoModal: { gap: 0, maxHeight: '84%', maxWidth: 580, padding: 28, width: '100%' },
  modalHeader: { alignItems: 'flex-start', flexDirection: 'row', gap: 18, justifyContent: 'space-between' },
  modalHeaderCopy: { flex: 1, gap: 5 },
  modalTitle: { ...typeScale.dialogTitle, color: palette.text, fontFamily: font },
  modalClose: { alignItems: 'center', borderColor: palette.line, borderRadius: 8, borderWidth: 1, justifyContent: 'center', minHeight: 36, paddingHorizontal: 12 },
  modalCloseText: { ...typeScale.button, color: palette.secondary, fontFamily: font },
  modalScroll: { flexShrink: 1, marginTop: 22 },
  modalScrollContent: { gap: 22, paddingBottom: 2 },
  productInfoList: { borderColor: palette.line, borderRadius: 12, borderWidth: 1, overflow: 'hidden' },
  productInfoRow: { alignItems: 'center', borderBottomColor: palette.line, borderBottomWidth: 1, flexDirection: 'row', justifyContent: 'space-between', minHeight: 50, paddingHorizontal: 16 },
  productInfoLabel: { ...typeScale.label, color: palette.muted, fontFamily: font },
  productInfoValue: { ...typeScale.bodyStrong, color: palette.text, fontFamily: font, fontWeight: '700' },
  farmStatusSummary: { alignItems: 'flex-start', backgroundColor: palette.greenSoft, borderColor: '#c9dfd1', borderRadius: 12, borderWidth: 1, flexDirection: 'row', gap: 12, padding: 17 },
  farmStatusCopy: { flex: 1, gap: 4 },
  farmStatusTitle: { ...typeScale.cardTitle, color: palette.greenDark, fontFamily: font },
  farmStatusBody: { ...typeScale.body, color: palette.secondary, fontFamily: font },
}, { fontSizeScale: 0.84 }));
