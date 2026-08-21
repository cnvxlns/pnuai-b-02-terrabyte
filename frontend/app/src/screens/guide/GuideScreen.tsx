import { useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { font } from '../../appTheme/glass';
import { palette } from '../../appTheme/palette';
import { scaleTypography } from '../../appTheme/scaleTypography';
import { typeScale } from '../../appTheme/typography';
import { ActionButton } from '../../components/ActionButton';
import { SectionHeader } from '../../components/SectionHeader';
import { Surface } from '../../components/Surface';
import { managementTasks, shopProducts, type ShopProduct } from '../../data';
import type { PotResponse } from '../../device/deviceApi';
import type { Page } from '../../navigation/types';
import { useDeviceEnvironment } from '../../shared/device-environment/DeviceEnvironmentProvider';
import { getFactorRecommendation, getRecommendedProductIds } from '../../shared/factorPresentation';

function formatCultivationDay(cropSelectedAt: string | undefined): string | null {
  if (!cropSelectedAt) return null;
  const selected = new Date(cropSelectedAt);
  if (Number.isNaN(selected.getTime())) return null;
  // 시각이 아니라 날짜 단위로 센다 — 선택 당일이 1일차다.
  const startOfDay = (date: Date) => Date.UTC(date.getFullYear(), date.getMonth(), date.getDate());
  const elapsedDays = Math.floor((startOfDay(new Date()) - startOfDay(selected)) / 86_400_000);
  return `재배 ${Math.max(elapsedDays, 0) + 1}일차`;
}

export function GuideScreen({ compact, onNavigate, pot }: {
  compact: boolean;
  onNavigate: (page: Page) => void;
  pot?: PotResponse;
}) {
  const [completedTaskIds, setCompletedTaskIds] = useState<Record<string, boolean>>({});
  const { score, soilRecommendation } = useDeviceEnvironment();
  // 예전에는 "유묘기 · 4주차 / 다음 점검 3일 후"가 그대로 박혀 있었다. 생육 단계를 판정할
  // 데이터가 저장소에 없으므로 단계 이름을 지어내지 않고, 실제로 아는 값만 보여준다 —
  // 작물 선택일로 센 경과일과, 적합도 점수가 쓰는 작물별 권장 범위다.
  const cultivationDayLabel = formatCultivationDay(pot?.cropSelectedAt);
  const cultivationCriteria = [
    ...(cultivationDayLabel
      ? [{
          label: '재배 경과',
          title: cultivationDayLabel,
          body: `${new Date(pot!.cropSelectedAt!).toLocaleDateString('ko-KR')}에 ${score?.cropName ?? '작물'}을(를) 선택했습니다.`,
        }]
      : []),
    ...(score?.factors ?? []).map((factor) => ({
      label: factor.label,
      title: `${factor.optimalMin}~${factor.optimalMax}${factor.unit}`,
      body: `현재 ${factor.current}${factor.unit} · ${getFactorRecommendation(factor.key)}`,
    })),
  ];
  const environmentProducts = getRecommendedProductIds(score?.factors ?? [])
    .map((id) => shopProducts.find((product) => product.id === id))
    .filter((product): product is ShopProduct => Boolean(product));
  const recommendedProducts = environmentProducts.length
    ? environmentProducts
    : shopProducts.filter((product) => product.badge?.includes('추천')).slice(0, 3);
  const completedTaskCount = managementTasks.filter((task) => completedTaskIds[task.id]).length;

  const toggleTask = (taskId: string) => {
    setCompletedTaskIds((current) => ({ ...current, [taskId]: !current[taskId] }));
  };

  return (
    <View style={styles.pageBody}>
      <Surface flat style={styles.guideTasksPanel}>
        <View style={[styles.guideTaskHeader, compact && styles.stack]}>
          <View style={styles.guideHeroCopy}><Text style={styles.guideHeroTitle}>오늘의 관리 작업</Text><Text style={styles.guideHeroBody}>환경 이상 알림과 현재 생육 단계를 기준으로 우선순위를 정했습니다.</Text></View>
          <View style={styles.guideProgress}><Text style={styles.guideProgressValue}>{completedTaskCount} / {managementTasks.length}</Text><Text style={styles.guideProgressLabel}>완료한 작업</Text></View>
        </View>
        <View style={styles.guideTaskList}>
          {managementTasks.map((task, index) => {
            const completed = Boolean(completedTaskIds[task.id]);
            return (
              <Pressable
                key={task.id}
                accessibilityLabel={`${task.title} ${completed ? '완료됨' : '완료 처리'}`}
                accessibilityRole="checkbox"
                accessibilityState={{ checked: completed }}
                onPress={() => toggleTask(task.id)}
                style={({ pressed }) => [
                  styles.guideTaskRow,
                  compact && styles.stack,
                  completed && styles.guideTaskRowCompleted,
                  pressed && styles.guideTaskRowPressed,
                ]}
              >
                <View style={styles.guideTaskNumber}><Text style={styles.guideTaskNumberText}>{String(index + 1).padStart(2, '0')}</Text></View>
                <View style={styles.guideTaskCopy}><Text style={styles.guideTaskPriority}>{task.priority}</Text><Text style={[styles.guideTaskTitle, completed && styles.guideTaskTitleCompleted]}>{task.title}</Text><Text style={[styles.guideTaskBody, completed && styles.guideTaskBodyCompleted]}>{task.body}</Text></View>
                <Text style={styles.guideTaskTime}>{task.time}</Text>
                <View style={[styles.guideTaskCheck, completed && styles.guideTaskCheckCompleted]}><Text style={styles.guideTaskCheckText}>{completed ? '✓' : ''}</Text></View>
              </Pressable>
            );
          })}
        </View>
      </Surface>

      <Surface flat style={styles.guidePanel}>
        <SectionHeader title="재배 기준" description="작물 선택일부터의 경과일과, 적합도 점수가 사용하는 작물별 권장 범위입니다." />
        <View style={styles.guideTaskList}>
          {cultivationCriteria.length ? null : (
            <Text style={styles.guideTaskBody}>작물을 선택하고 측정값이 도착하면 재배 기준이 표시됩니다.</Text>
          )}
          {cultivationCriteria.map((item, index) => (
            <View key={item.label} style={[styles.guideTaskRow, compact && styles.guideTaskRowCompact]}>
              <View style={styles.guideTaskNumber}><Text style={styles.guideTaskNumberText}>{String(index + 1).padStart(2, '0')}</Text></View>
              <View style={styles.guideTaskCopy}>
                <Text style={styles.guideTaskPriority}>{item.label}</Text>
                <Text style={styles.guideTaskTitle}>{item.title}</Text>
                <Text style={styles.guideTaskBody}>{item.body}</Text>
              </View>
            </View>
          ))}
        </View>
      </Surface>

      {soilRecommendation ? (
        <Surface flat style={styles.guidePanel}>
          <SectionHeader
            title="토양 배합 추천"
            description={`${soilRecommendation.cropName} 재배 기준 · ${soilRecommendation.targetCondition}`}
          />
          <View style={styles.soilMixRow}>
            <Text style={styles.soilMixRatio}>{soilRecommendation.mixRatioText}</Text>
            <Text style={styles.soilReason}>{soilRecommendation.reason}</Text>
          </View>
          <View style={[styles.soilMaterialGrid, compact && styles.stack]}>
            {soilRecommendation.materials.map((material) => (
              <View key={material.name} style={styles.soilMaterialCard}>
                <Text style={styles.soilMaterialName}>{material.name} · {material.parts}비율</Text>
                <Text style={styles.soilMaterialRole}>{material.role}</Text>
              </View>
            ))}
          </View>
          {soilRecommendation.preChecks.length ? (
            <View style={styles.soilPreCheckList}>
              {soilRecommendation.preChecks.map((item) => <Text key={item} style={styles.soilPreCheckItem}>· {item}</Text>)}
            </View>
          ) : null}
          {soilRecommendation.cautions.length ? (
            <View style={styles.soilCautionList}>
              {soilRecommendation.cautions.map((item) => <Text key={item} style={styles.soilCautionItem}>· {item}</Text>)}
            </View>
          ) : null}
          {soilRecommendation.assumptionNotice.map((item) => <Text key={item} style={styles.soilAssumptionNotice}>{item}</Text>)}
        </Surface>
      ) : null}

      <Surface flat style={styles.guideProductsPanel}>
        <SectionHeader title="현재 환경 추천 제품" description="현재 환경에서 보완이 필요한 지표를 기준으로 선정했습니다." />
        <View style={styles.guideTaskList}>
          {recommendedProducts.map((product, index) => (
            <View key={product.id} style={[styles.guideTaskRow, compact && styles.guideTaskRowCompact]}>
              <View style={styles.guideTaskNumber}><Text style={styles.guideTaskNumberText}>{String(index + 1).padStart(2, '0')}</Text></View>
              <View style={styles.guideTaskCopy}>
                <Text style={styles.guideTaskTitle}>{product.name}</Text>
                <Text style={styles.guideTaskBody}>{product.desc}</Text>
              </View>
              <Text style={styles.guideProductPrice}>{product.price.toLocaleString('ko-KR')}원</Text>
            </View>
          ))}
        </View>
        <ActionButton label="추천 제품 구매하러 가기" onPress={() => onNavigate('shop')} />
      </Surface>
    </View>
  );
}

const styles = StyleSheet.create(scaleTypography({
  pageBody: { gap: 30, maxWidth: 1320, width: '100%' },
  stack: { flexDirection: 'column' },
  guideTasksPanel: { gap: 28, padding: 36 },
  guidePanel: { gap: 28, padding: 36 },
  guideTaskHeader: { alignItems: 'center', flexDirection: 'row', gap: 30, justifyContent: 'space-between' },
  guideHeroCopy: { flex: 1, gap: 8 },
  guideHeroTitle: { ...typeScale.sectionTitle, color: palette.text, fontFamily: font },
  guideHeroBody: { ...typeScale.body, color: palette.secondary, fontFamily: font, maxWidth: 760 },
  guideProgress: { gap: 4, minWidth: 150, padding: 22 },
  guideProgressValue: { ...typeScale.cardTitle, color: palette.greenDark, fontFamily: font },
  guideProgressLabel: { ...typeScale.label, color: palette.secondary, fontFamily: font },
  guideTaskList: { gap: 0 },
  guideTaskRow: { alignItems: 'center', borderBottomColor: palette.lineStrong, borderBottomWidth: 1, flexDirection: 'row', gap: 20, padding: 23 },
  guideTaskRowCompact: { alignItems: 'flex-start', flexDirection: 'column' },
  guideTaskRowCompleted: { backgroundColor: 'rgba(228,241,233,0.42)' },
  guideTaskRowPressed: { opacity: 0.78 },
  guideTaskNumber: { alignItems: 'center', backgroundColor: palette.greenSoft, borderRadius: 10, height: 46, justifyContent: 'center', width: 46 },
  guideTaskNumberText: { ...typeScale.label, color: palette.greenDark, fontFamily: font, fontWeight: '700' },
  guideTaskCopy: { flex: 1, gap: 8, maxWidth: 850 },
  guideTaskPriority: { ...typeScale.label, color: palette.amber, fontFamily: font },
  guideTaskTitle: { ...typeScale.cardTitle, color: palette.text, fontFamily: font, fontWeight: '600' },
  guideTaskTitleCompleted: { color: palette.muted, textDecorationLine: 'line-through' },
  guideTaskBody: { ...typeScale.body, color: palette.secondary, fontFamily: font },
  guideTaskBodyCompleted: { color: palette.muted },
  guideTaskTime: { ...typeScale.label, color: palette.muted, fontFamily: font },
  guideTaskCheck: { alignItems: 'center', borderColor: palette.greenDark, borderRadius: 8, borderWidth: 1.5, height: 32, justifyContent: 'center', width: 32 },
  guideTaskCheckCompleted: { backgroundColor: palette.green, borderColor: palette.green },
  guideTaskCheckText: { color: '#ffffff', fontFamily: font, fontSize: 20, fontWeight: '900', lineHeight: 24 },
  soilMixRow: { gap: 6 },
  soilMixRatio: { ...typeScale.cardTitle, color: palette.text, fontFamily: font },
  soilReason: { ...typeScale.body, color: palette.secondary, fontFamily: font },
  soilMaterialGrid: { flexDirection: 'row', gap: 0 },
  soilMaterialCard: { borderRightColor: palette.lineStrong, borderRightWidth: 1, flex: 1, gap: 6, padding: 20 },
  soilMaterialName: { ...typeScale.cardTitle, color: palette.text, fontFamily: font, fontWeight: '600' },
  soilMaterialRole: { ...typeScale.body, color: palette.secondary, fontFamily: font },
  soilMaterialBuyLink: { ...typeScale.button, color: palette.greenDark, fontFamily: font, marginTop: 8 },
  soilCautionList: { gap: 6 },
  soilCautionItem: { ...typeScale.body, color: palette.amber, fontFamily: font },
  soilPreCheckToggle: { ...typeScale.button, color: palette.greenDark, fontFamily: font },
  soilPreCheckList: { gap: 4, marginTop: 10 },
  soilPreCheckItem: { ...typeScale.caption, color: palette.secondary, fontFamily: font },
  soilAssumptionNotice: { ...typeScale.caption, color: palette.muted, fontFamily: font },
  guideProductsPanel: { gap: 28, padding: 36 },
  guideProductPrice: { ...typeScale.cardTitle, color: palette.greenDark, fontFamily: font, minWidth: 104, textAlign: 'right' },
}));
