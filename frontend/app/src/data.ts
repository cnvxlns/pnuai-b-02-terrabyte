import type { EnvironmentScore, LatestMeasurements, MeasurementSeries, ScoreFactor } from './measurement/measurementApi';
import type { SoilRecommendation } from './soil/soilApi';

export type Crop = {
  code: string;
  name: string;
  emoji: string;
  desc: string;
};

export type FactorStatus = 'OK' | 'LOW' | 'HIGH';

export type Factor = {
  label: string;
  unit: string;
  current: number;
  avg24h: number;
  min24h: number;
  max24h: number;
  optimalMin: number;
  optimalMax: number;
  axisMin: number;
  axisMax: number;
  status: FactorStatus;
  gap?: number;
};

export type ChartRange = '1h' | '24h' | '7d' | '30d';

export type ShopCategory = 'parts' | 'soil' | 'seeds';

export type ShopProduct = {
  id: string;
  category: ShopCategory;
  name: string;
  emoji: string;
  desc: string;
  price: number;
  badge?: string;
};

export const dashboardSpace = {
  name: '부산 스마트팜 옥상 A',
  meta: '옥상 · 42m² · 자동화 · 공간분석 키트 1대 · 토양분석 키트 1대',
  operatingLabel: '작물환경 모니터링 중',
  flow: [
    { label: '공간 등록', state: '완료' },
    { label: '공간 진단', state: '완료' },
    { label: '환경 모니터링', state: '운영 중' },
  ],
} as const;

export const dashboardChartMetrics = [
  { key: 'air_temperature_c', label: '온도', color: '#d9822b' },
  { key: 'air_humidity_pct', label: '습도', color: '#2b8fae' },
  { key: 'plant_light_ppfd_umol_m2_s', label: '조도', color: '#e0b23a' },
  { key: 'soil_moisture_pct', label: '토양 수분', color: '#3fae6f' },
  { key: 'soil_temperature_c', label: '토양 온도', color: '#8b6f47' },
] as const;

export const liveMetricDefinitions = [
  { key: 'air_temperature_c', label: '온도', unit: '℃', color: '#d9822b', rangeLabel: '최근 1시간' },
  { key: 'air_humidity_pct', label: '습도', unit: '%', color: '#2b8fae', rangeLabel: '최근 1시간' },
  { key: 'plant_light_ppfd_umol_m2_s', label: '조도', unit: ' PPFD', color: '#e0b23a', rangeLabel: '최근 1시간' },
  { key: 'soil_moisture_pct', label: '토양 수분', unit: '%', color: '#3fae6f', rangeLabel: '최근 1시간' },
  { key: 'soil_temperature_c', label: '토양 온도', unit: '℃', color: '#8b6f47', rangeLabel: '최근 1시간' },
] as const;

export const fallbackSoilTemperatureReport = {
  label: '토양 온도',
  unit: '℃',
  avg24h: 22.8,
  axisMax: 35,
  status: 'REFERENCE' as const,
  finding: '현재 토양 온도는 22.8℃입니다. 뿌리 주변 온도 변화를 확인하는 참고 지표입니다.',
  recommendation: '급격한 온도 변화가 없도록 직사광선과 냉기를 피하고 배지 온도를 함께 관찰하세요.',
} as const;

export const historyRecords = [
  { date: '2026. 07. 22', score: 82, isInitial: false, summary: '조도 보완 후 전환 적합도 상승', issues: '습도 관리 필요' },
  { date: '2026. 07. 15', score: 75, isInitial: false, summary: '토양분석 키트 연결 및 관리 기준 설정', issues: '조도·습도 보완 필요' },
  { date: '2026. 07. 08', score: 68, isInitial: true, summary: '옥상 공간 현장 점검 및 환경 조건 분석', issues: '조도·습도·배수 보완 필요' },
] as const;

export const historyComparison = {
  title: '보조 조명 설치 후 조도 점수가 가장 크게 개선됐습니다',
  body: '일평균 조도가 9,600lux에서 11,800lux로 상승했고, 권장 범위 미달 시간은 하루 8.2시간에서 4.6시간으로 줄었습니다.',
  before: 54,
  after: 71,
} as const;

export const managementTasks = [
  {
    id: 'grow-light',
    priority: '높음',
    title: '성장등 작동 상태 확인',
    body: '현재 조도가 8,000lux까지 낮아졌습니다. 조명 전원과 작물 사이 30cm 거리를 확인하세요.',
    time: '예상 5분',
  },
  {
    id: 'humidity',
    priority: '보통',
    title: '오후 습도 조정',
    body: '관수 직후 환기를 10분 권장하고, 습도가 50% 이상 회복하는지 확인하세요.',
    time: '예상 10분',
  },
] as const;

export const sidebarDeviceInfo = {
  connectionStatus: '정상 연결',
  title: '내 스마트팜',
  detail: '마지막 수신 방금 전',
  lastSync: '방금 전',
  farmName: '내 스마트팜',
  farmStatusTitle: '모든 장치가 정상 작동 중입니다',
  farmStatusBody: '등록된 센서에서 환경 데이터를 정상적으로 받고 있어요.',
  registeredDevice: 'TerraByte Hub 01',
  connectedSensorCount: '7개',
} as const;

export const getSidebarFarmInfoRows = (cropName: string) => [
  { label: '농장 이름', value: sidebarDeviceInfo.farmName },
  { label: '재배 작물', value: cropName },
  { label: '등록 기기', value: sidebarDeviceInfo.registeredDevice },
  { label: '연결 센서', value: sidebarDeviceInfo.connectedSensorCount },
  { label: '마지막 동기화', value: sidebarDeviceInfo.lastSync },
];

export const crops: Crop[] = [
  { code: 'cherry_tomato', name: '방울토마토', emoji: '🍅', desc: '초보자에게 인기 있는 실내 작물' },
  { code: 'lettuce', name: '상추', emoji: '🥬', desc: '빠르게 자라고 관리가 쉬워요' },
  { code: 'basil', name: '바질', emoji: '🌿', desc: '햇빛을 좋아하는 허브' },
  { code: 'peppermint', name: '페퍼민트', emoji: '🌱', desc: '상쾌한 향이 특징인 허브' },
  { code: 'welsh_onion', name: '대파', emoji: '🧅', desc: '잎과 줄기를 활용하는 향신 채소' },
  { code: 'arugula', name: '루꼴라', emoji: '🥗', desc: '톡 쏘는 풍미가 특징인 잎채소' },
  { code: 'wasabi', name: '와사비', emoji: '🌿', desc: '알싸한 맛이 특징인 향신 작물' },
  { code: 'coriander', name: '고수', emoji: '☘️', desc: '독특한 향을 지닌 향신 허브' },
];

export const factors: Factor[] = [
  {
    label: '온도',
    unit: '℃',
    current: 24.5,
    avg24h: 24.1,
    min24h: 21.8,
    max24h: 26.3,
    optimalMin: 20,
    optimalMax: 26,
    axisMin: 10,
    axisMax: 35,
    status: 'OK',
  },
  {
    label: '습도',
    unit: '%',
    current: 45,
    avg24h: 52,
    min24h: 43,
    max24h: 64,
    optimalMin: 60,
    optimalMax: 75,
    axisMin: 0,
    axisMax: 100,
    status: 'LOW',
    gap: 8,
  },
  {
    label: '조도',
    unit: 'lux',
    current: 8000,
    avg24h: 11800,
    min24h: 1200,
    max24h: 18900,
    optimalMin: 15000,
    optimalMax: 20000,
    axisMin: 0,
    axisMax: 25000,
    status: 'LOW',
    gap: 3200,
  },
  {
    label: '토양 수분',
    unit: '%',
    current: 38,
    avg24h: 36,
    min24h: 31,
    max24h: 42,
    optimalMin: 30,
    optimalMax: 45,
    axisMin: 0,
    axisMax: 100,
    status: 'OK',
  },
];

export const score = {
  value: 68,
  grade: '보통',
  measuredAt: '2026-07-14T10:30:00+09:00',
  windowStart: '2026-07-13T10:30:00+09:00',
  windowEnd: '2026-07-14T10:30:00+09:00',
};

export const altCrops = [
  {
    name: '상추',
    emoji: '🥬',
    expectedScore: 85,
    setsCropIndex: 1,
    reason: '현재 온도와 토양 수분 조건에 가장 잘 맞고, 조도 보완 효과를 빠르게 받을 수 있습니다.',
    caution: '습도가 45% 아래로 내려가지 않도록 관리가 필요합니다.',
  },
  {
    name: '페퍼민트',
    emoji: '🌱',
    expectedScore: 79,
    setsCropIndex: 3,
    reason: '환경 변화에 대한 적응력이 높아 현재 조건에서도 비교적 안정적인 생장이 예상됩니다.',
    caution: '과도한 번식을 막기 위해 단독 화분 재배를 권장합니다.',
  },
  {
    name: '바질',
    emoji: '🌿',
    expectedScore: 74,
    setsCropIndex: 2,
    reason: '온도 조건은 적합하지만 현재 조도로는 줄기가 가늘게 자랄 가능성이 있습니다.',
    caution: '생장등 보완 후 선택하면 예상 적합도가 83점까지 상승합니다.',
  },
];

export type Sensor = { label: string; model: string };

export const sensors: Sensor[] = [
  { label: '온·습도 센서', model: 'DHT22' },
  { label: '조도 센서', model: 'BH1750' },
  { label: '토양 수분 센서', model: 'EF04027' },
  { label: '토양 온도 센서', model: 'DS18B20' },
];

export const factorProductMap: Partial<Record<ScoreFactor['key'], string[]>> = {
  plantLight: ['grow-light'],
  humidity: ['watering-kit'],
};

export const shopTabs: Array<{ key: ShopCategory; label: string }> = [
  { key: 'parts', label: '부품' },
  { key: 'soil', label: '흙' },
  { key: 'seeds', label: '씨앗' },
];

export const shopProducts: ShopProduct[] = [
  {
    id: 'grow-light',
    category: 'parts',
    name: '식물 생장등 (LED)',
    emoji: '💡',
    desc: '실내 재배 공간에 설치하기 좋은 바 타입 조명',
    price: 29900,
    badge: '추천',
  },
  {
    id: 'watering-kit',
    category: 'parts',
    name: '자동 관수 키트',
    emoji: '💧',
    desc: '설정한 주기에 맞춰 물을 공급하는 소형 관수 세트',
    price: 39900,
  },
  {
    id: 'soil-probe',
    category: 'parts',
    name: '토양 수분 센서 프로브',
    emoji: '🌡️',
    desc: '기존 기기에 연결해 교체할 수 있는 센서 프로브',
    price: 12900,
  },
  {
    id: 'power-adapter',
    category: 'parts',
    name: 'USB-C 전원 어댑터',
    emoji: '🔌',
    desc: '센서와 관수 장치에 사용할 수 있는 전원 어댑터',
    price: 9900,
  },
  {
    id: 'herb-soil',
    category: 'soil',
    name: '실내 허브용 배양토 10L',
    emoji: '🪴',
    desc: '허브와 잎채소 화분에 바로 사용할 수 있는 배양토',
    price: 12500,
    badge: '인기',
  },
  {
    id: 'perlite',
    category: 'soil',
    name: '펄라이트 3L',
    emoji: '⚪',
    desc: '배수성과 통기성을 보완할 때 섞어 쓰는 토양 개량재',
    price: 6900,
  },
  {
    id: 'coco-peat',
    category: 'soil',
    name: '코코피트 블록 5L',
    emoji: '🥥',
    desc: '물을 흡수하면 부피가 늘어나는 가벼운 재배 배지',
    price: 7500,
  },
  {
    id: 'gravel',
    category: 'soil',
    name: '마사토 소립 5L',
    emoji: '🪨',
    desc: '화분 바닥 배수층과 흙 배합에 활용하는 소립 마사토',
    price: 8900,
  },
  {
    id: 'vermiculite',
    category: 'soil',
    name: '버미큘라이트 3L',
    emoji: '🟤',
    desc: '수분·양분 보유력을 높여 건조가 빠른 배지를 보완하는 토양 개량재',
    price: 7900,
  },
  {
    id: 'basil-seeds',
    category: 'seeds',
    name: '바질 씨앗',
    emoji: '🌿',
    desc: '향긋한 잎을 수확하는 실내 허브 재배용 씨앗',
    price: 2500,
    badge: '초보 추천',
  },
  {
    id: 'lettuce-seeds',
    category: 'seeds',
    name: '상추 씨앗',
    emoji: '🥬',
    desc: '화분과 소형 재배기에서 키우기 좋은 잎채소 씨앗',
    price: 2500,
  },
  {
    id: 'arugula-seeds',
    category: 'seeds',
    name: '루꼴라 씨앗',
    emoji: '🥗',
    desc: '톡 쏘는 풍미의 어린잎을 재배할 수 있는 씨앗',
    price: 2800,
  },
  {
    id: 'coriander-seeds',
    category: 'seeds',
    name: '고수 씨앗',
    emoji: '☘️',
    desc: '독특한 향의 잎과 줄기를 수확하는 허브 씨앗',
    price: 2800,
  },
  {
    id: 'welsh-onion-seeds',
    category: 'seeds',
    name: '대파 씨앗',
    emoji: '🧅',
    desc: '베란다와 텃밭 화분에서 재배할 수 있는 채소 씨앗',
    price: 3000,
  },
  {
    id: 'peppermint-seeds',
    category: 'seeds',
    name: '페퍼민트 씨앗',
    emoji: '🌱',
    desc: '상쾌한 향을 즐길 수 있는 다년생 허브 씨앗',
    price: 3200,
  },
];

export const equipment = [
  {
    name: '식물 생장등 (LED)',
    emoji: '💡',
    reason: '조도 부족',
    source: '환경 분석',
    expectedGain: '+15점',
  },
  {
    name: '스마트 관수 시스템',
    emoji: '💧',
    reason: '물 주기 편의성 개선',
    source: '맞춤 추천',
  },
];

export const dailyAvg = [
  { label: '온도', emoji: '🌡️', value: '24.1℃', sub: '24h 평균 · 최저 21.8 · 최고 26.3' },
  { label: '습도', emoji: '💧', value: '52%', sub: '24h 평균 · 최저 43 · 최고 64' },
  { label: '조도', emoji: '☀️', value: '11,800 lux', sub: '24h 평균 · 최저 1,200 · 최고 18,900' },
  { label: '토양 수분', emoji: '🪴', value: '36%', sub: '24h 평균 · 최저 31 · 최고 42' },
];

export const rangeTabs: Array<{ key: ChartRange; label: string }> = [
  { key: '1h', label: '1시간' },
  { key: '24h', label: '24시간' },
  { key: '7d', label: '7일' },
  { key: '30d', label: '30일' },
];

export const chartMetrics = [
  { label: '온도', unit: '℃', color: '#d9822b', seed: 1, amp: 16, mid: 55 },
  { label: '습도', unit: '%', color: '#2b8fae', seed: 3, amp: 22, mid: 60 },
  { label: '조도', unit: 'lux', color: '#e0b23a', seed: 5, amp: 30, mid: 55 },
  { label: '토양 수분', unit: '%', color: '#3fae6f', seed: 7, amp: 14, mid: 50 },
];

export const storybookAnalysisScore: EnvironmentScore = {
  deviceId: 1,
  cropCode: 'TOMATO',
  cropName: '방울토마토',
  total: 68,
  grade: 'NORMAL',
  measuredAt: '2026-07-29T09:00:00+09:00',
  formula: 'mock formula',
  factors: [
    {
      key: 'plantLight',
      label: '조도',
      unit: 'PPFD',
      current: 80,
      optimalMin: 300,
      optimalMax: 600,
      status: 'LOW',
      gap: 220,
      score: 42,
    },
    {
      key: 'humidity',
      label: '습도',
      unit: '%',
      current: 46,
      optimalMin: 50,
      optimalMax: 70,
      status: 'LOW',
      gap: 4,
      score: 70,
    },
  ],
};

export const storybookAnalysisMeasurements: LatestMeasurements = {
  deviceId: 1,
  hardwareDeviceId: 'TB-STORY-001',
  observedAt: '2026-07-29T09:00:00+09:00',
  sequence: 42,
  measurements: {
    soilMoisturePct: 34,
    soilMoistureRawAdc: 1840,
    airTemperatureC: 24.6,
    airHumidityPct: 46,
    plantLightPpfdUmolM2S: 80,
    soilTemperatureC: 21.4,
  },
  quality: {
    soilSensorValid: true,
    airSensorValid: true,
    lightSensorValid: true,
  },
};

export const storybookDashboardScore: EnvironmentScore = {
  deviceId: 1,
  cropCode: 'TOMATO',
  cropName: '방울토마토',
  total: 68,
  grade: 'NORMAL',
  measuredAt: '2026-07-29T09:00:00+09:00',
  formula: 'mock formula',
  factors: [
    {
      key: 'plantLight',
      label: '조도',
      unit: 'PPFD',
      current: 80,
      optimalMin: 300,
      optimalMax: 600,
      status: 'LOW',
      gap: 220,
      score: 42,
    },
    {
      key: 'humidity',
      label: '습도',
      unit: '%',
      current: 46,
      optimalMin: 50,
      optimalMax: 70,
      status: 'LOW',
      gap: 4,
      score: 70,
    },
  ],
};

export const storybookDashboardMeasurements: LatestMeasurements = {
  deviceId: 1,
  hardwareDeviceId: 'TB-STORY-001',
  observedAt: '2026-07-29T09:00:00+09:00',
  sequence: 42,
  measurements: {
    soilMoisturePct: 34,
    soilMoistureRawAdc: 1840,
    airTemperatureC: 24.6,
    airHumidityPct: 46,
    plantLightPpfdUmolM2S: 80,
    soilTemperatureC: 21.4,
  },
  quality: {
    soilSensorValid: true,
    airSensorValid: true,
    lightSensorValid: true,
  },
};

export const storybookDashboardSeries: MeasurementSeries = {
  deviceId: 1,
  metric: 'soil_temperature_c',
  unit: '℃',
  range: '24h',
  points: Array.from({ length: 24 }, (_, index) => ({
    time: new Date(Date.UTC(2026, 6, 28, index)).toISOString(),
    value: 21.8 + Math.sin(index / 4) * 0.9,
  })),
};

export const storybookGuideScore: EnvironmentScore = {
  deviceId: 1,
  cropCode: 'TOMATO',
  cropName: '방울토마토',
  total: 68,
  grade: 'NORMAL',
  measuredAt: '2026-07-29T09:00:00+09:00',
  formula: 'mock formula',
  factors: [
    {
      key: 'plantLight',
      label: '조도',
      unit: 'PPFD',
      current: 80,
      optimalMin: 300,
      optimalMax: 600,
      status: 'LOW',
      gap: 220,
      score: 42,
    },
    {
      key: 'humidity',
      label: '습도',
      unit: '%',
      current: 46,
      optimalMin: 50,
      optimalMax: 70,
      status: 'LOW',
      gap: 4,
      score: 70,
    },
  ],
};

export const storybookGuideMeasurements: LatestMeasurements = {
  deviceId: 1,
  hardwareDeviceId: 'TB-STORY-001',
  observedAt: '2026-07-29T09:00:00+09:00',
  sequence: 42,
  measurements: {
    soilMoisturePct: 34,
    soilMoistureRawAdc: 1840,
    airTemperatureC: 24.6,
    airHumidityPct: 46,
    plantLightPpfdUmolM2S: 80,
    soilTemperatureC: 21.4,
  },
  quality: {
    soilSensorValid: true,
    airSensorValid: true,
    lightSensorValid: true,
  },
};

export const storybookGuideSoilRecommendation: SoilRecommendation = {
  deviceId: 1,
  cropCode: 'cherry_tomato',
  cropName: '방울토마토',
  targetCondition: 'NORMAL',
  profileId: 'cherry-tomato-normal-v1',
  materials: [
    { name: '원예용 상토', parts: 5, role: '수분·양분 보유, 뿌리 지지' },
    { name: '펄라이트', parts: 1, role: '배수·통기 보완, 배지 다짐 완화' },
  ],
  mixRatio: '5:1',
  mixRatioText: '원예용 상토 5 : 펄라이트 1',
  reason: '일정한 수분 공급을 유지하면서 배수와 통기성을 보완한다.',
  environmentSignals: [
    '관수 후 토양 수분이 상승함',
    '이후 수분이 점진적으로 감소함',
    '배수구에서 물이 정상적으로 빠짐',
  ],
  preChecks: [],
  cautions: [
    '상토에 펄라이트가 충분하면 추가량을 줄이거나 생략한다.',
    '받침에 고인 물은 제거한다.',
  ],
  assumptionNotice: [
    '작물의 일반 생육 특성과 현재 환경정보에 기반한 가정값입니다.',
    '배합비는 공식 표준이 아닌 서비스 내부 추론값입니다.',
  ],
};

export const storybookLiveScore: EnvironmentScore = {
  deviceId: 1,
  cropCode: 'TOMATO',
  cropName: '방울토마토',
  total: 86,
  grade: 'GOOD',
  measuredAt: '2026-07-29T09:00:00+09:00',
  formula: 'mock formula',
  factors: [
    {
      key: 'temperature',
      label: '온도',
      unit: '°C',
      current: 24.6,
      optimalMin: 20,
      optimalMax: 28,
      status: 'OK',
      gap: 0,
      score: 92,
    },
    {
      key: 'humidity',
      label: '습도',
      unit: '%',
      current: 61,
      optimalMin: 50,
      optimalMax: 70,
      status: 'OK',
      gap: 0,
      score: 88,
    },
    {
      key: 'plantLight',
      label: '조도',
      unit: 'PPFD',
      current: 420,
      optimalMin: 300,
      optimalMax: 600,
      status: 'OK',
      gap: 0,
      score: 78,
    },
  ],
};

export const storybookLiveMeasurements: LatestMeasurements = {
  deviceId: 1,
  hardwareDeviceId: 'TB-STORY-001',
  observedAt: '2026-07-29T09:00:00+09:00',
  sequence: 42,
  measurements: {
    soilMoisturePct: 56,
    soilMoistureRawAdc: 1840,
    airTemperatureC: 24.6,
    airHumidityPct: 61,
    plantLightPpfdUmolM2S: 420,
    soilTemperatureC: 21.4,
  },
  quality: {
    soilSensorValid: true,
    airSensorValid: true,
    lightSensorValid: true,
  },
};

export const storybookLiveSeries: MeasurementSeries = {
  deviceId: 1,
  metric: 'soil_temperature_c',
  unit: '℃',
  range: '1h',
  points: Array.from({ length: 24 }, (_, index) => ({
    time: new Date(Date.UTC(2026, 6, 29, 0, index * 3)).toISOString(),
    value: 21.4 + Math.sin(index / 4) * 0.8,
  })),
};

export const storybookShopScore: EnvironmentScore = {
  deviceId: 1,
  cropCode: 'TOMATO',
  cropName: '방울토마토',
  total: 68,
  grade: 'NORMAL',
  measuredAt: '2026-07-29T09:00:00+09:00',
  formula: 'mock formula',
  factors: [
    {
      key: 'plantLight',
      label: '조도',
      unit: 'PPFD',
      current: 80,
      optimalMin: 300,
      optimalMax: 600,
      status: 'LOW',
      gap: 220,
      score: 42,
    },
    {
      key: 'humidity',
      label: '습도',
      unit: '%',
      current: 46,
      optimalMin: 50,
      optimalMax: 70,
      status: 'LOW',
      gap: 4,
      score: 70,
    },
  ],
};

export const storybookShopMeasurements: LatestMeasurements = {
  deviceId: 1,
  hardwareDeviceId: 'TB-STORY-001',
  observedAt: '2026-07-29T09:00:00+09:00',
  sequence: 42,
  measurements: {
    soilMoisturePct: 34,
    soilMoistureRawAdc: 1840,
    airTemperatureC: 24.6,
    airHumidityPct: 46,
    plantLightPpfdUmolM2S: 80,
    soilTemperatureC: null,
  },
  quality: {
    soilSensorValid: true,
    airSensorValid: true,
    lightSensorValid: true,
  },
};
