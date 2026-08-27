import { useEffect, useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { font } from '../../appTheme/glass';
import { palette } from '../../appTheme/palette';
import { scaleTypography } from '../../appTheme/scaleTypography';
import { typeScale } from '../../appTheme/typography';
import { ActionButton } from '../../components/ActionButton';
import { SectionHeader } from '../../components/SectionHeader';
import { Surface } from '../../components/Surface';
import { getCarePlan, type CarePlan } from '../../care/carePlanApi';
import type { Page } from '../../navigation/types';
import { useDeviceEnvironment } from '../../shared/device-environment/DeviceEnvironmentProvider';
import { soilConditionText } from '../../soil/soilCondition';

export function GuideScreen({ compact, onNavigate }: { compact: boolean; onNavigate: (page: Page) => void }) {
  const [completedTaskIds, setCompletedTaskIds] = useState<Record<string, boolean>>({});
  const [carePlan, setCarePlan] = useState<CarePlan | null>(null);
  const { potId, soilRecommendation } = useDeviceEnvironment();

  useEffect(() => {
    let active = true;
    setCarePlan(null);
    if (!potId) return () => { active = false; };
    void getCarePlan(potId)
      .then((plan) => { if (active) setCarePlan(plan); })
      // API 응답이 없으면 빈 상태로 두고 정적 데이터를 대체하지 않는다.
      .catch(() => { if (active) setCarePlan(null); });
    return () => { active = false; };
  }, [potId]);

  const recommendedProducts = carePlan?.recommendedProducts.map((product) => ({ ...product, id: product.productId })) ?? [];
  const tasks = carePlan?.todayTasks ?? [];
  const criteria = carePlan?.cultivationCriteria ?? [];
  const completedTaskCount = tasks.filter((task) => completedTaskIds[task.id]).length;

  const toggleTask = (taskId: string) => {
    setCompletedTaskIds((current) => ({ ...current, [taskId]: !current[taskId] }));
  };

  return (
    <View style={styles.pageBody}>
      <Surface flat style={styles.guideTasksPanel}>
        <View style={[styles.guideTaskHeader, compact && styles.stack]}>
          <View style={styles.guideHeroCopy}><Text style={styles.guideHeroTitle}>오늘의 관리 작업</Text><Text style={styles.guideHeroBody}>환경 이상 알림과 현재 생육 단계를 기준으로 우선순위를 정했습니다.</Text></View>
          <View style={styles.guideProgress}><Text style={styles.guideProgressValue}>{completedTaskCount} / {tasks.length}</Text><Text style={styles.guideProgressLabel}>완료한 작업</Text></View>
        </View>
        <View style={styles.guideTaskList}>
          {tasks.map((task, index) => {
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
        <SectionHeader title="재배 단계별 기준" description="현재는 정식 후 활착 단계로, 급격한 환경 변화를 피해야 합니다." />
        <View style={styles.guideTaskList}>
          {criteria.map((item, index) => (
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
            description={`${soilRecommendation.cropName} 재배 기준 · ${soilConditionText(soilRecommendation)}`}
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
                {'reason' in product ? <Text style={styles.guideTaskBody}>추천 이유 · {product.reason}</Text> : null}
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
