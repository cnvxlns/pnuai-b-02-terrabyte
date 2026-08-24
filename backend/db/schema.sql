-- TerraByte — 생육환경 근거/측정/점수 SQLite 스키마
-- 생성일: 2026-07-16
-- 근거: backend/docs/newcropinfo/ (검증 13 레코드, 격리 17 후보)
-- 점수 계약: backend/docs/spec/backend_handoff_spec.json (schema_version 2.0.0)
--
-- 설계 원칙
--  1. 근거 문서는 불변. SQLite는 근거의 사본이지 출처가 아니다.
--  2. 점수 입력은 완전 보정된 canonical 단위(°C, RH %, PPFD)만 받는다.
--  3. 점수 프로필은 버전형 불변 데이터다. 경계 변경은 새 프로필로 배포한다.
--  4. 점수는 작물 생육환경 적합도 휴리스틱이며 생존율·수확량 예측값이 아니다.
--  5. 결측은 0점이 아니다. 완전한 세 축 입력이 없으면 점수 행을 만들지 않는다.
--
-- SQLite >= 3.37 (STRICT 테이블)

PRAGMA foreign_keys = ON;

-- ============================================================================
-- 1. 축 카탈로그
-- ============================================================================

CREATE TABLE axis_catalog (
  axis  TEXT PRIMARY KEY CHECK (axis IN (
          'air_temperature','relative_humidity','plant_light',
          'particulate_matter','noise')),
  scope TEXT NOT NULL CHECK (scope IN ('crop','common')),
  CHECK (
    (axis IN ('air_temperature','relative_humidity','plant_light') AND scope='crop') OR
    (axis IN ('particulate_matter','noise')                        AND scope='common')
  )
) STRICT, WITHOUT ROWID;

INSERT INTO axis_catalog VALUES
  ('air_temperature','crop'), ('relative_humidity','crop'), ('plant_light','crop'),
  ('particulate_matter','common'), ('noise','common');

-- 평가 상태. 결측/차단은 점수가 아니라 상태다.
CREATE TABLE evaluation_state_catalog (
  state       TEXT PRIMARY KEY,
  meaning_ko  TEXT NOT NULL,
  is_terminal INTEGER NOT NULL CHECK (is_terminal IN (0,1))
) STRICT, WITHOUT ROWID;

INSERT INTO evaluation_state_catalog VALUES
  ('reference_available',        '앵커와 비교 가능. 등급이 아니라 거리다.', 0),
  ('no_verified_evidence',       '검증된 근거가 없다. 0점이 아니다.', 1),
  ('not_applicable_context',     '품종·생육단계·재배방식이 근거의 맥락과 다르다.', 1),
  ('blocked_measurement_basis',  '측정 기반이 근거와 호환되지 않는다(예: lux vs PPFD).', 0),
  ('blocked_missing_context',    '배치 메타데이터 미확정(예: 소음 용도지역).', 0),
  ('blocked_companion_unknown',  '근거가 요구하는 동반인자를 측정할 수 없다.', 0),
  ('insufficient_window',        '시간 창이 아직 완성되지 않았다.', 0),
  ('condition_met',              '명시된 환경 전제조건이 관측됐다(병해 등).', 0),
  ('condition_not_met',          '명시된 환경 전제조건이 관측되지 않았다.', 0),
  ('missing_sensor',             '필요한 센서가 배치되지 않았다.', 0);

-- ============================================================================
-- 2. 근거 레이어 — 불변
-- ============================================================================

CREATE TABLE evidence_document (
  document_name  TEXT PRIMARY KEY CHECK (document_name IN (
                   'verified_numeric_evidence','insufficient_evidence','collection_contract')),
  schema_version TEXT NOT NULL,
  sha256         TEXT NOT NULL CHECK (length(sha256)=64),
  payload_json   TEXT NOT NULL CHECK (json_valid(payload_json)),
  loaded_at_utc  TEXT NOT NULL
) STRICT, WITHOUT ROWID;

CREATE TRIGGER evidence_document_immutable_update
BEFORE UPDATE ON evidence_document BEGIN
  SELECT RAISE(ABORT,'evidence documents are immutable: edit the JSON and reload');
END;

CREATE TRIGGER evidence_document_immutable_delete
BEFORE DELETE ON evidence_document BEGIN
  SELECT RAISE(ABORT,'evidence documents are immutable: edit the JSON and reload');
END;

-- ============================================================================
-- 3. 타입 분리 코퍼스 투영
--    단위마다 테이블이 다르다. 제네릭 value/unit 컬럼은 의도적으로 없다.
-- ============================================================================

-- 3.1 온도 --------------------------------------------------------------------
CREATE TABLE corpus_temperature_reference (
  record_id        TEXT PRIMARY KEY,
  crop             TEXT NOT NULL,
  claim_role       TEXT NOT NULL CHECK (claim_role='comparative_best_tested_point'),
  regime_kind      TEXT NOT NULL CHECK (regime_kind IN ('single_value','day_night_tuple')),
  best_value_c     REAL,          -- single_value 전용
  best_day_c       REAL,          -- day_night_tuple 전용
  best_night_c     REAL,
  tested_grid_json TEXT NOT NULL CHECK (json_valid(tested_grid_json)),
  -- 0이면 시험 격자의 내부 처리점이 원문에 열거돼 있지 않다는 뜻.
  -- 격자 마크를 합성해서는 안 된다(agy-explorer의 브리프 정정).
  tested_grid_enumerated INTEGER NOT NULL CHECK (tested_grid_enumerated IN (0,1)),
  tested_low_c     REAL NOT NULL,
  tested_high_c    REAL NOT NULL CHECK (tested_high_c >= tested_low_c),
  applicability_status TEXT NOT NULL,
  context_key      TEXT NOT NULL,
  prohibited_use   TEXT NOT NULL,
  CHECK (
    (regime_kind='single_value'   AND best_value_c IS NOT NULL
       AND best_day_c IS NULL AND best_night_c IS NULL) OR
    (regime_kind='day_night_tuple' AND best_value_c IS NULL
       AND best_day_c IS NOT NULL AND best_night_c IS NOT NULL)
  )
) STRICT, WITHOUT ROWID;

-- 3.2 광 — PPFD / DLI ---------------------------------------------------------
CREATE TABLE corpus_ppfd_reference (
  record_id       TEXT PRIMARY KEY,
  crop            TEXT NOT NULL,
  claim_role      TEXT NOT NULL CHECK (claim_role IN (
                    'author_recommended_target','comparative_best_tested_point',
                    'comparative_suitable_band')),
  point_ppfd_umol_m2_s     REAL,
  band_low_ppfd_umol_m2_s  REAL,
  band_high_ppfd_umol_m2_s REAL,
  tested_low_ppfd_umol_m2_s  REAL NOT NULL,
  tested_high_ppfd_umol_m2_s REAL NOT NULL CHECK (tested_high_ppfd_umol_m2_s >= tested_low_ppfd_umol_m2_s),
  -- 시험 상한이 곧 최고 처리점이면 그 위는 '모름'이지 '나쁨'이 아니다(와사비 140).
  bounded_above   INTEGER NOT NULL CHECK (bounded_above IN (0,1)),
  photoperiod_h   REAL NOT NULL CHECK (photoperiod_h > 0 AND photoperiod_h <= 24),
  objective       TEXT NOT NULL,   -- 바질 500 = 전기요금 최적이지 생물학적 최적이 아니다
  context_key     TEXT NOT NULL,
  prohibited_use  TEXT NOT NULL,
  CHECK (record_id <> 'all-crops-universal-lux-threshold-q001'),
  CHECK (
    (claim_role='comparative_suitable_band' AND point_ppfd_umol_m2_s IS NULL
       AND band_low_ppfd_umol_m2_s IS NOT NULL AND band_high_ppfd_umol_m2_s IS NOT NULL
       AND band_low_ppfd_umol_m2_s <= band_high_ppfd_umol_m2_s) OR
    (claim_role IN ('author_recommended_target','comparative_best_tested_point')
       AND point_ppfd_umol_m2_s IS NOT NULL
       AND band_low_ppfd_umol_m2_s IS NULL AND band_high_ppfd_umol_m2_s IS NULL)
  )
) STRICT, WITHOUT ROWID;

CREATE TABLE corpus_dli_reference (
  record_id  TEXT PRIMARY KEY,
  crop       TEXT NOT NULL,
  claim_role TEXT NOT NULL CHECK (claim_role='author_recommended_target'),
  point_dli_mol_m2_day     REAL NOT NULL CHECK (point_dli_mol_m2_day >= 0),
  tested_low_dli_mol_m2_day  REAL NOT NULL,
  tested_high_dli_mol_m2_day REAL NOT NULL,
  photoperiod_h REAL NOT NULL,
  context_key   TEXT NOT NULL,
  prohibited_use TEXT NOT NULL
) STRICT, WITHOUT ROWID;

-- 3.3 습도 — 직접 근거 코퍼스에는 생육 최적 RH 테이블이 없다.
--     점수 프로필의 B/C등급 RH 기본값과 혼동하지 않는다.
CREATE TABLE corpus_rh_disease_condition (
  record_id  TEXT PRIMARY KEY,
  crop       TEXT NOT NULL,
  claim_role TEXT NOT NULL CHECK (claim_role='disease_risk_condition'),
  hazard     TEXT NOT NULL,
  predicate_key TEXT NOT NULL,   -- canopy_rh / night_rh_sustained / leaf_wetness
  operator      TEXT NOT NULL CHECK (operator IN ('>=','>')),
  threshold_value REAL NOT NULL,
  threshold_unit  TEXT NOT NULL CHECK (threshold_unit IN ('percent','hours')),
  duration_hours  REAL,          -- NULL이면 원문에 지속시간 규칙이 없다 = rule_pending
  requires_canopy_interior INTEGER NOT NULL CHECK (requires_canopy_interior IN (0,1)),
  prohibited_use TEXT NOT NULL
) STRICT, WITHOUT ROWID;

-- 3.4 미세먼지 — 세 시간기준을 절대 섞지 않는다 --------------------------------
CREATE TABLE corpus_pm_reference (
  record_id     TEXT NOT NULL,
  authority     TEXT NOT NULL CHECK (authority IN ('who','korea_forecast','korea_alert')),
  pollutant     TEXT NOT NULL CHECK (pollutant IN ('pm2_5','pm10')),
  temporal_basis TEXT NOT NULL CHECK (temporal_basis IN
                   ('annual_mean','24_hour_mean','daily_mean_forecast','hourly_mean_sustained_2h')),
  -- PK에 들어가므로 NULL 대신 빈 문자열을 쓴다(SQLite는 PK에 표현식을 금지한다).
  band_label    TEXT NOT NULL DEFAULT '',   -- korea_forecast 전용
  alert_level   TEXT NOT NULL DEFAULT '' CHECK (alert_level IN ('','advisory','warning')),
  low_ug_m3     REAL,
  high_ug_m3    REAL,
  claim_role    TEXT NOT NULL,
  applies_to    TEXT NOT NULL DEFAULT 'human_health'
                  CHECK (applies_to='human_health'),  -- 작물 데이터가 아니다
  PRIMARY KEY (record_id, authority, pollutant, temporal_basis, band_label, alert_level)
) STRICT;

-- 3.5 소음 — 부지 분류 전까지 어느 행도 선택할 수 없다 -------------------------
CREATE TABLE corpus_noise_standard (
  record_id  TEXT NOT NULL,
  area_kind  TEXT NOT NULL CHECK (area_kind IN ('general','roadside')),
  zone_class TEXT NOT NULL,
  period     TEXT NOT NULL CHECK (period IN ('day','night')),
  laeq_db_a  REAL NOT NULL,
  claim_role TEXT NOT NULL CHECK (claim_role='environmental_standard'),
  PRIMARY KEY (record_id, area_kind, zone_class, period)
) STRICT;

-- ============================================================================
-- 4. 작물 생육환경 점수 프로필 — 버전형 불변 데이터
--    네 경계는 [0점 하한, 100점 하한, 100점 상한, 0점 상한] 순서다.
-- ============================================================================

CREATE TABLE crop_score_profile (
  profile_id   TEXT PRIMARY KEY,
  crop_code    TEXT NOT NULL CHECK (crop_code IN (
                 'basil','peppermint','cherry_tomato','welsh_onion',
                 'arugula','wasabi','lettuce','coriander')),
  crop_name_ko TEXT NOT NULL,
  profile_group TEXT NOT NULL CHECK (profile_group IN (
                  'cool_leafy_herb','warm_herb','moderate_light_herb',
                  'warm_fruiting','cool_shade')),
  growth_stage TEXT NOT NULL,

  temperature_zero_low_c REAL NOT NULL,
  temperature_optimal_low_c REAL NOT NULL,
  temperature_optimal_high_c REAL NOT NULL,
  temperature_zero_high_c REAL NOT NULL,

  humidity_zero_low_pct REAL NOT NULL CHECK (humidity_zero_low_pct >= 0),
  humidity_optimal_low_pct REAL NOT NULL,
  humidity_optimal_high_pct REAL NOT NULL,
  humidity_zero_high_pct REAL NOT NULL CHECK (humidity_zero_high_pct <= 100),

  ppfd_zero_low_umol_m2_s REAL NOT NULL CHECK (ppfd_zero_low_umol_m2_s >= 0),
  ppfd_optimal_low_umol_m2_s REAL NOT NULL,
  ppfd_optimal_high_umol_m2_s REAL NOT NULL,
  ppfd_zero_high_umol_m2_s REAL NOT NULL,
  reference_photoperiod_h REAL NOT NULL
    CHECK (reference_photoperiod_h > 0 AND reference_photoperiod_h <= 24),

  temperature_weight INTEGER NOT NULL CHECK (temperature_weight BETWEEN 0 AND 100),
  humidity_weight    INTEGER NOT NULL CHECK (humidity_weight BETWEEN 0 AND 100),
  plant_light_weight INTEGER NOT NULL CHECK (plant_light_weight BETWEEN 0 AND 100),
  temperature_evidence_grade TEXT NOT NULL
    CHECK (temperature_evidence_grade IN ('A','B','C')),
  humidity_evidence_grade TEXT NOT NULL
    CHECK (humidity_evidence_grade IN ('A','B','C')),
  plant_light_evidence_grade TEXT NOT NULL
    CHECK (plant_light_evidence_grade IN ('A','B','C')),
  profile_kind TEXT NOT NULL CHECK (profile_kind IN (
                 'crop_specific','general_reference','category_fallback','hybrid')),
  source_summary TEXT NOT NULL,
  profile_version TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,

  UNIQUE (profile_id, crop_code),
  UNIQUE (crop_code, profile_version),
  CHECK (temperature_zero_low_c < temperature_optimal_low_c
     AND temperature_optimal_low_c <= temperature_optimal_high_c
     AND temperature_optimal_high_c < temperature_zero_high_c),
  CHECK (humidity_zero_low_pct < humidity_optimal_low_pct
     AND humidity_optimal_low_pct <= humidity_optimal_high_pct
     AND humidity_optimal_high_pct < humidity_zero_high_pct),
  CHECK (ppfd_zero_low_umol_m2_s < ppfd_optimal_low_umol_m2_s
     AND ppfd_optimal_low_umol_m2_s <= ppfd_optimal_high_umol_m2_s
     AND ppfd_optimal_high_umol_m2_s < ppfd_zero_high_umol_m2_s),
  CHECK (temperature_weight + humidity_weight + plant_light_weight = 100)
) STRICT, WITHOUT ROWID;

CREATE TRIGGER crop_score_profile_immutable_update
BEFORE UPDATE ON crop_score_profile BEGIN
  SELECT RAISE(ABORT,'score profiles are immutable: insert a new version');
END;

CREATE TRIGGER crop_score_profile_immutable_delete
BEFORE DELETE ON crop_score_profile BEGIN
  SELECT RAISE(ABORT,'score profiles are immutable: keep historical score profiles');
END;

INSERT INTO crop_score_profile VALUES
  ('basil-general-v1','basil','바질','warm_herb','general_vegetative',
   15,24,30,36, 30,50,70,90, 0,260,500,750,16,
   40,25,35,'A','C','A','hybrid',
   '온도 직접시험 29°C 중심, 바질 DLI 일반지침과 500 PPFD 직접시험, RH 대분류 기본값',
   '1.0.0','2026-07-16T00:00:00Z'),
  ('peppermint-general-v1','peppermint','페퍼민트','moderate_light_herb','general_vegetative',
   10,18,24,30, 30,50,70,90, 0,150,200,300,14,
   40,25,35,'C','C','A','hybrid',
   'Mentha 150-200 PPFD 직접시험, 온도와 RH는 중광성 허브 대분류 기본값',
   '1.0.0','2026-07-16T00:00:00Z'),
  ('cherry-tomato-general-v1','cherry_tomato','방울토마토','warm_fruiting','general_fruiting',
   12,18.5,26.5,32, 30,50,70,90, 0,347,521,800,16,
   40,25,35,'B','B','B','general_reference',
   '일반 온실 토마토 온도·RH·VPD 종설과 토마토 DLI 20-30 일반지침',
   '1.0.0','2026-07-16T00:00:00Z'),
  ('welsh-onion-general-v1','welsh_onion','대파','cool_leafy_herb','general_vegetative',
   10,20,24,30, 30,50,70,90, 0,208,347,600,16,
   40,25,35,'C','C','C','category_fallback',
   '냉량성 엽채·허브 대분류 기본값과 DLI 12-20 환산값',
   '1.0.0','2026-07-16T00:00:00Z'),
  ('arugula-general-v1','arugula','아루굴라','cool_leafy_herb','general_vegetative',
   10,20,24,30, 30,50,70,90, 0,208,347,600,16,
   40,25,35,'C','C','C','category_fallback',
   '냉량성 엽채·허브 대분류 기본값과 DLI 12-20 환산값',
   '1.0.0','2026-07-16T00:00:00Z'),
  ('wasabi-general-v1','wasabi','와사비','cool_shade','general_vegetative',
   10,16,18,26, 40,60,75,95, 0,120,140,250,12,
   40,25,35,'B','C','A','hybrid',
   '유묘 18/18°C 앵커, Daruma 140 PPFD 직접시험, RH는 고정 68%를 포함한 대분류 기본값',
   '1.0.0','2026-07-16T00:00:00Z'),
  ('lettuce-general-v1','lettuce','상추','cool_leafy_herb','general_vegetative',
   10,20,24,30, 30,50,70,90, 0,208,295,500,16,
   40,25,35,'B','B','A','hybrid',
   'Cornell 24/19°C·RH 50-70%, 상추 DLI 12-17 및 빙산상추 직접시험',
   '1.0.0','2026-07-16T00:00:00Z'),
  ('coriander-general-v1','coriander','고수','cool_leafy_herb','fresh_leaf',
   10,20,24,30, 30,50,70,90, 0,200,347,550,16,
   40,25,35,'C','C','A','hybrid',
   '고수 200 PPFD·16h 직접시험과 cilantro DLI 15-20 일반지침, 온도·RH 대분류 기본값',
   '1.0.0','2026-07-16T00:00:00Z');

-- 2026-07-25 웹 재검증 프로필.
-- 온도는 FAO EcoCrop의 absolute/optimal 경계를 기본으로 작물별 CEA 비교시험을
-- 보완했다. RH의 0점 경계와 직접시험 상한 밖 PPFD 0점 경계는 생물학적
-- 고사 한계가 아니라 제품용 감점 앵커이며, 상세 근거와 제한은
-- backend/docs/score_boundary_validation_2026-07-25.md에 기록한다.
INSERT INTO crop_score_profile VALUES
  ('basil-general-v2','basil','바질','warm_herb','general_vegetative',
   7,18,29,36, 30,50,80,95, 0,260,600,1000,16,
   40,25,35,'A','C','A','hybrid',
   'FAO 7/18-27/36°C와 바질 29°C 생육시험; DLI 15-25 및 500-600 PPFD 시험; RH 50-80 CEA 일반범위와 85% 이상 노균병 위험',
   '2.0.0','2026-07-25T00:00:00Z'),
  ('peppermint-general-v2','peppermint','페퍼민트','moderate_light_herb','general_vegetative',
   4,15,25,35, 30,50,80,95, 0,150,200,250,14,
   40,25,35,'B','C','A','hybrid',
   'FAO 4/15-25/35°C; Mentha 150-200 PPFD 적합 및 250 PPFD 스트레스 시험; RH는 CEA 일반범위',
   '2.0.0','2026-07-25T00:00:00Z'),
  ('cherry-tomato-general-v2','cherry_tomato','방울토마토','warm_fruiting','general_fruiting',
   7,18.5,26.5,35, 30,65,75,90, 0,300,521,800,16,
   40,25,35,'B','B','B','general_reference',
   'FAO 절대 7-35°C와 온실 토마토 18.5-26.5°C·RH 65-75%; 토마토 DLI 20-30 및 300 PPFD 효율 시험',
   '2.0.0','2026-07-25T00:00:00Z'),
  ('welsh-onion-general-v2','welsh_onion','대파','cool_leafy_herb','general_vegetative',
   6,12,25,30, 30,50,80,95, 0,208,347,600,16,
   40,25,35,'B','C','C','category_fallback',
   'FAO 대파 절대 6-30°C·최적 12-25°C; RH와 PPFD는 직접 구배시험 부재로 CEA·엽채류 휴리스틱 유지',
   '2.0.0','2026-07-25T00:00:00Z'),
  ('arugula-general-v2','arugula','아루굴라','cool_leafy_herb','general_vegetative',
   8,15,25,29, 30,50,80,95, 0,200,250,600,16,
   40,25,35,'B','C','A','hybrid',
   'FAO 아루굴라 절대 8-29°C·최적 15-25°C; 성숙 로켓 250 PPFD·DLI 14.4 비교시험; RH는 CEA 일반범위',
   '2.0.0','2026-07-25T00:00:00Z'),
  ('wasabi-general-v2','wasabi','와사비','cool_shade','general_vegetative',
   5,12,18,26, 40,60,80,95, 0,90,140,250,12,
   40,25,35,'B','C','A','hybrid',
   '와사비 5°C 야간 생육정체·12-18°C 적온·고온 민감성; Daruma 90-140 PPFD 고광합성·140 PPFD 최고 생체중; RH 68% 시험조건',
   '2.0.0','2026-07-25T00:00:00Z'),
  ('lettuce-general-v2','lettuce','상추','cool_leafy_herb','general_vegetative',
   5,12,24,30, 30,60,75,90, 0,200,295,500,16,
   40,25,35,'B','B','A','hybrid',
   'FAO 절대 5-30°C·최적 12-21°C와 CEA 24°C 효율시험; RH 70-75% 연구; 상추 DLI 12-17을 16h PPFD로 환산',
   '2.0.0','2026-07-25T00:00:00Z'),
  ('coriander-general-v2','coriander','고수','cool_leafy_herb','fresh_leaf',
   4,15,26,32, 30,50,70,90, 0,200,200,400,16,
   40,25,35,'B','C','A','hybrid',
   'FAO 절대 4-32°C·최적 15-25°C와 표준 고수 약 26°C 생체중 최적; 200 PPFD·16h 직접 권고; RH는 CEA 일반범위',
   '2.0.0','2026-07-25T00:00:00Z');

-- PPFD 0점 하한의 미입력 자리표시자 0을 광보상점 근사값으로 교체한 프로필 v3.
-- 작물별 실측값이 아니라 기존 PPFD 최적 하한의 10%를 정수로 반올림하는 단일 규칙을
-- 적용했다. 산출값은 해당 내음성 등급에 공개된 광보상점 범위 안에 있다
-- (그늘 전문 작물 약 5-10, 엽채류 15-25, 과채류 30-40 µmol/m²/s).
-- 특정 논문의 작물별 측정값을 인용한 것이 아니며 직접 측정 전까지 사용하는 근사값이다.
INSERT INTO crop_score_profile
  (profile_id,crop_code,crop_name_ko,profile_group,growth_stage,
   temperature_zero_low_c,temperature_optimal_low_c,
   temperature_optimal_high_c,temperature_zero_high_c,
   humidity_zero_low_pct,humidity_optimal_low_pct,
   humidity_optimal_high_pct,humidity_zero_high_pct,
   ppfd_zero_low_umol_m2_s,ppfd_optimal_low_umol_m2_s,
   ppfd_optimal_high_umol_m2_s,ppfd_zero_high_umol_m2_s,
   reference_photoperiod_h,temperature_weight,humidity_weight,
   plant_light_weight,temperature_evidence_grade,humidity_evidence_grade,
   plant_light_evidence_grade,profile_kind,source_summary,profile_version,
   created_at_utc)
SELECT replace(profile_id, '-general-v2', '-general-v3'),
       crop_code,crop_name_ko,profile_group,growth_stage,
       temperature_zero_low_c,temperature_optimal_low_c,
       temperature_optimal_high_c,temperature_zero_high_c,
       humidity_zero_low_pct,humidity_optimal_low_pct,
       humidity_optimal_high_pct,humidity_zero_high_pct,
       CASE crop_code
         WHEN 'basil' THEN 26
         WHEN 'peppermint' THEN 15
         WHEN 'cherry_tomato' THEN 30
         WHEN 'welsh_onion' THEN 21
         WHEN 'arugula' THEN 20
         WHEN 'wasabi' THEN 9
         WHEN 'lettuce' THEN 20
         WHEN 'coriander' THEN 20
       END,
       ppfd_optimal_low_umol_m2_s,ppfd_optimal_high_umol_m2_s,
       ppfd_zero_high_umol_m2_s,reference_photoperiod_h,
       temperature_weight,humidity_weight,plant_light_weight,
       temperature_evidence_grade,humidity_evidence_grade,
       plant_light_evidence_grade,profile_kind,
       source_summary || '; 광보상점 ' || CASE crop_code
         WHEN 'basil' THEN '26은'
         WHEN 'peppermint' THEN '15는'
         WHEN 'cherry_tomato' THEN '30은'
         WHEN 'welsh_onion' THEN '21은'
         WHEN 'arugula' THEN '20은'
         WHEN 'wasabi' THEN '9는'
         WHEN 'lettuce' THEN '20은'
         WHEN 'coriander' THEN '20은'
       END || ' 작물별 실측이 아니라 기존 PPFD 최적 하한의 10%를 정수 반올림한 단일 규칙값이며, 내음성 등급별 공개 범위(그늘 전문 5-10, 엽채 15-25, 과채 30-40) 안의 직접 측정 전 근사값',
       '3.0.0','2026-08-24T00:00:00Z'
FROM crop_score_profile
WHERE profile_version = '2.0.0'
  AND crop_code IN (
    'basil','peppermint','cherry_tomato','welsh_onion',
    'arugula','wasabi','lettuce','coriander'
  );

-- 신규 재배 컨텍스트가 사용할 기본 버전. 기존 컨텍스트는 profile_id를 직접
-- 참조하므로 활성 버전이 바뀌어도 과거 점수가 재해석되지 않는다.
CREATE TABLE crop_score_profile_activation (
  crop_code TEXT PRIMARY KEY,
  profile_id TEXT NOT NULL,
  activated_at_utc TEXT NOT NULL,
  FOREIGN KEY (profile_id, crop_code)
    REFERENCES crop_score_profile(profile_id, crop_code)
) STRICT, WITHOUT ROWID;

INSERT INTO crop_score_profile_activation VALUES
  ('basil','basil-general-v3','2026-08-24T00:00:00Z'),
  ('peppermint','peppermint-general-v3','2026-08-24T00:00:00Z'),
  ('cherry_tomato','cherry-tomato-general-v3','2026-08-24T00:00:00Z'),
  ('welsh_onion','welsh-onion-general-v3','2026-08-24T00:00:00Z'),
  ('arugula','arugula-general-v3','2026-08-24T00:00:00Z'),
  ('wasabi','wasabi-general-v3','2026-08-24T00:00:00Z'),
  ('lettuce','lettuce-general-v3','2026-08-24T00:00:00Z'),
  ('coriander','coriander-general-v3','2026-08-24T00:00:00Z');

-- 종합점수 모델은 경계 프로필 버전에 1:1로 결합한다. 현재 활성 계약은
-- equal_geometric_v1 + 1/1/1이며, 정규화된 실제 지수는 각각 1/3이다.
-- 기존 crop_score_profile의 40/25/35 weight 열은 legacy 조화평균 계약의
-- 데이터이므로 이 모델의 지수로 재해석하지 않는다.
CREATE TABLE crop_score_model_config (
  model_id TEXT PRIMARY KEY,
  profile_id TEXT NOT NULL,
  crop_code TEXT NOT NULL,
  contract_version TEXT NOT NULL CHECK (contract_version='score-v1'),
  aggregation_family TEXT NOT NULL CHECK (aggregation_family IN (
    'equal_geometric_v1','weighted_geometric_v1')),
  temperature_exponent REAL NOT NULL
    CHECK (temperature_exponent > 0 AND temperature_exponent <= 100),
  humidity_exponent REAL NOT NULL
    CHECK (humidity_exponent > 0 AND humidity_exponent <= 100),
  plant_light_exponent REAL NOT NULL
    CHECK (plant_light_exponent > 0 AND plant_light_exponent <= 100),
  curve_family TEXT NOT NULL CHECK (curve_family='trapezoid_v1'),
  validation_status TEXT NOT NULL CHECK (validation_status IN ('draft','validated')),
  evidence_revision TEXT NOT NULL,
  change_reason TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  FOREIGN KEY (profile_id, crop_code)
    REFERENCES crop_score_profile(profile_id, crop_code),
  UNIQUE (profile_id, contract_version),
  UNIQUE (model_id, crop_code),
  CHECK (aggregation_family <> 'equal_geometric_v1'
    OR (temperature_exponent = humidity_exponent
      AND humidity_exponent = plant_light_exponent))
) STRICT, WITHOUT ROWID;

CREATE TRIGGER crop_score_model_config_immutable_update
BEFORE UPDATE ON crop_score_model_config BEGIN
  SELECT RAISE(ABORT,'score model configs are immutable: insert a new profile version');
END;

CREATE TRIGGER crop_score_model_config_immutable_delete
BEFORE DELETE ON crop_score_model_config BEGIN
  SELECT RAISE(ABORT,'score model configs are immutable: insert a new profile version');
END;

INSERT INTO crop_score_model_config
  (model_id,profile_id,crop_code,contract_version,aggregation_family,
   temperature_exponent,humidity_exponent,plant_light_exponent,curve_family,
   validation_status,evidence_revision,change_reason,created_at_utc)
SELECT profile_id || '-score-v1', profile_id, crop_code, 'score-v1',
       'equal_geometric_v1', 1.0, 1.0, 1.0, 'trapezoid_v1', 'validated',
       'aggregation-baseline-2026-07-25',
       CASE WHEN profile_version = '3.0.0'
         THEN 'PPFD 0점 하한을 최적 하한의 10%로 반올림한 광보상점 근사값으로 교체'
         ELSE '기존 1/3·1/3·1/3 기하평균과 사다리꼴 축 점수를 변경 없이 명시'
       END,
       CASE WHEN profile_version = '3.0.0'
         THEN '2026-08-24T00:00:00Z'
         ELSE '2026-07-25T00:00:00Z'
       END
FROM crop_score_profile;

-- ============================================================================
-- 5. 배치 (deployment)
-- ============================================================================

CREATE TABLE site (
  site_id  TEXT PRIMARY KEY,
  name     TEXT NOT NULL,
  timezone TEXT NOT NULL,
  -- 소음 기준 행 선택에 필요. 미정이면 NULL → noise = blocked_missing_context
  legal_zone_class TEXT,
  is_roadside      INTEGER CHECK (is_roadside IN (0,1))
) STRICT, WITHOUT ROWID;

CREATE TABLE zone (
  zone_id TEXT PRIMARY KEY,
  site_id TEXT NOT NULL REFERENCES site(site_id),
  indoor_or_outdoor TEXT NOT NULL CHECK (indoor_or_outdoor IN ('indoor','outdoor'))
) STRICT, WITHOUT ROWID;

-- ============================================================================
-- 6. 레거시 광 수집 축 — lux 네이티브 배치 (BH1750)
--    canonical 완전 보정 입력과 별개인 기존 엄격 교정 경로다.
-- ============================================================================

CREATE TABLE light_regime (
  regime_id     TEXT PRIMARY KEY,
  zone_id       TEXT NOT NULL,
  spectrum_mode TEXT NOT NULL CHECK (spectrum_mode IN (
                  'fixed_artificial','mixed_daylight_artificial','variable_or_unknown')),
  daylight_excluded INTEGER NOT NULL CHECK (daylight_excluded IN (0,1)),
  source_fingerprint   TEXT,   -- LED 모델·스펙트럼·거리
  effective_from_utc   TEXT NOT NULL,
  effective_to_utc     TEXT,
  -- 변환 예외의 두 전제는 AND로 묶인다. 보광을 '더하는' 것으로는 1이 되지 않는다.
  -- 자연광을 '배제'해야 1이 된다.
  fixed_spectrum_eligible INTEGER GENERATED ALWAYS AS (
    CASE WHEN spectrum_mode='fixed_artificial' AND daylight_excluded=1 THEN 1 ELSE 0 END
  ) STORED,
  FOREIGN KEY (zone_id) REFERENCES zone(zone_id),
  UNIQUE (regime_id, zone_id),
  UNIQUE (regime_id, fixed_spectrum_eligible),
  CHECK (spectrum_mode <> 'mixed_daylight_artificial' OR daylight_excluded=0),
  CHECK (spectrum_mode <> 'fixed_artificial'
         OR (daylight_excluded=1 AND source_fingerprint IS NOT NULL))
) STRICT, WITHOUT ROWID;

CREATE TABLE bh1750_sensor (
  sensor_id     TEXT PRIMARY KEY,
  sensor_model  TEXT NOT NULL CHECK (instr(upper(sensor_model),'BH1750') > 0),
  serial_number TEXT NOT NULL,
  measurement_height_m REAL NOT NULL CHECK (measurement_height_m >= 0),
  verification_date TEXT NOT NULL
) STRICT, WITHOUT ROWID;

CREATE TABLE quantum_reference_instrument (
  instrument_id TEXT PRIMARY KEY,
  model         TEXT NOT NULL,
  serial_number TEXT NOT NULL,
  calibration_certificate_id TEXT NOT NULL,
  calibration_valid_through  TEXT NOT NULL
) STRICT, WITHOUT ROWID;

-- 교정 행: 합성 FK가 fixed_spectrum_eligible=1 인 regime만 참조하도록 강제한다.
-- 자연광 zone에서는 이 테이블에 INSERT가 물리적으로 불가능하다.
CREATE TABLE lux_ppfd_validation (
  validation_id TEXT PRIMARY KEY,
  regime_id     TEXT NOT NULL,
  required_regime_eligibility INTEGER NOT NULL DEFAULT 1
    CHECK (required_regime_eligibility=1),
  lux_sensor_id TEXT NOT NULL,
  quantum_instrument_id TEXT NOT NULL,
  status        TEXT NOT NULL CHECK (status IN ('draft','accepted','rejected')),
  ppfd_per_lux  REAL NOT NULL CHECK (ppfd_per_lux > 0),
  ppfd_intercept REAL NOT NULL,
  valid_lux_min REAL NOT NULL CHECK (valid_lux_min >= 0),
  valid_lux_max REAL NOT NULL CHECK (valid_lux_max > valid_lux_min),
  paired_sample_count INTEGER NOT NULL CHECK (paired_sample_count > 0),
  validation_max_abs_error_ppfd REAL NOT NULL CHECK (validation_max_abs_error_ppfd >= 0),
  acceptance_max_abs_error_ppfd REAL NOT NULL CHECK (acceptance_max_abs_error_ppfd > 0),
  valid_from_utc TEXT NOT NULL,
  valid_to_utc   TEXT,
  FOREIGN KEY (regime_id, required_regime_eligibility)
    REFERENCES light_regime(regime_id, fixed_spectrum_eligible),
  FOREIGN KEY (lux_sensor_id) REFERENCES bh1750_sensor(sensor_id),
  FOREIGN KEY (quantum_instrument_id) REFERENCES quantum_reference_instrument(instrument_id),
  UNIQUE (validation_id, lux_sensor_id, regime_id, status),
  CHECK (valid_to_utc IS NULL OR valid_to_utc > valid_from_utc),
  CHECK (status <> 'accepted'
         OR validation_max_abs_error_ppfd <= acceptance_max_abs_error_ppfd)
) STRICT, WITHOUT ROWID;

CREATE TABLE lux_ppfd_validation_pair (
  validation_id TEXT NOT NULL REFERENCES lux_ppfd_validation(validation_id),
  pair_number   INTEGER NOT NULL,
  captured_at_utc TEXT NOT NULL,
  bh1750_lx     REAL NOT NULL CHECK (bh1750_lx >= 0),
  reference_ppfd_umol_m2_s REAL NOT NULL CHECK (reference_ppfd_umol_m2_s >= 0),
  PRIMARY KEY (validation_id, pair_number)
) STRICT, WITHOUT ROWID;

-- 실측 페어 없이 accepted로 들어오는 것을 막는다.
CREATE TRIGGER validation_no_direct_accept
BEFORE INSERT ON lux_ppfd_validation
WHEN NEW.status='accepted' BEGIN
  SELECT RAISE(ABORT,'insert as draft, load paired field samples, then accept');
END;

CREATE TRIGGER validation_accept_guard
BEFORE UPDATE OF status ON lux_ppfd_validation
WHEN NEW.status='accepted' BEGIN
  SELECT CASE WHEN OLD.status <> 'draft'
    THEN RAISE(ABORT,'only a draft validation can be accepted') END;
  SELECT CASE WHEN (SELECT count(*) FROM lux_ppfd_validation_pair
                     WHERE validation_id=NEW.validation_id) <> NEW.paired_sample_count
    THEN RAISE(ABORT,'paired sample count mismatch') END;
END;

-- 관측: illuminance_lx 컬럼 하나뿐이다.
-- raw_value 도, unit 도, ppfd 도, dli 도 없다. 조인할 컬럼이 존재하지 않는다.
CREATE TABLE raw_lux_observation (
  lux_observation_id INTEGER PRIMARY KEY,
  sensor_id     TEXT NOT NULL,
  regime_id     TEXT NOT NULL,
  zone_id       TEXT NOT NULL,
  captured_at_utc TEXT NOT NULL,
  local_day     TEXT NOT NULL CHECK (length(local_day)=10),
  integration_seconds REAL NOT NULL CHECK (integration_seconds > 0),
  illuminance_lx REAL NOT NULL CHECK (illuminance_lx >= 0 AND illuminance_lx <= 65535),
  quality_flag  TEXT NOT NULL CHECK (quality_flag IN ('ok','suspect','saturated')),
  FOREIGN KEY (sensor_id) REFERENCES bh1750_sensor(sensor_id),
  FOREIGN KEY (regime_id, zone_id) REFERENCES light_regime(regime_id, zone_id),
  UNIQUE (sensor_id, captured_at_utc),
  UNIQUE (lux_observation_id, sensor_id, regime_id),
  CHECK ((quality_flag='saturated' AND illuminance_lx=65535)
         OR (quality_flag IN ('ok','suspect') AND illuminance_lx < 65535))
) STRICT;

CREATE TABLE lux_conversion_assignment (
  lux_observation_id INTEGER PRIMARY KEY,
  sensor_id     TEXT NOT NULL,
  regime_id     TEXT NOT NULL,
  validation_id TEXT NOT NULL,
  required_status TEXT NOT NULL DEFAULT 'accepted' CHECK (required_status='accepted'),
  FOREIGN KEY (lux_observation_id, sensor_id, regime_id)
    REFERENCES raw_lux_observation(lux_observation_id, sensor_id, regime_id),
  FOREIGN KEY (validation_id, sensor_id, regime_id, required_status)
    REFERENCES lux_ppfd_validation(validation_id, lux_sensor_id, regime_id, status)
) STRICT;

-- PPFD/DLI 평가에 노출되는 유일한 입력. illuminance_lx 는 절대 노출하지 않는다.
CREATE VIEW derived_ppfd_observation AS
SELECT l.sensor_id, l.zone_id, l.regime_id, l.captured_at_utc, l.local_day,
       l.integration_seconds, a.validation_id,
       max(0.0, v.ppfd_per_lux*l.illuminance_lx + v.ppfd_intercept)
         AS ppfd_estimate_umol_m2_s,
       v.validation_max_abs_error_ppfd AS ppfd_abs_error_umol_m2_s
FROM raw_lux_observation AS l
JOIN lux_conversion_assignment AS a USING (lux_observation_id)
JOIN lux_ppfd_validation AS v ON v.validation_id = a.validation_id
WHERE l.quality_flag='ok'
  AND v.status='accepted'
  AND l.illuminance_lx BETWEEN v.valid_lux_min AND v.valid_lux_max
  AND l.captured_at_utc >= v.valid_from_utc
  AND (v.valid_to_utc IS NULL OR l.captured_at_utc < v.valid_to_utc);

-- DLI는 하루 전체가 유효해야 한다. 일부라도 혼합 자연광이면 그날은 무효다.
CREATE TABLE admitted_dli_day (
  sensor_id TEXT NOT NULL REFERENCES bh1750_sensor(sensor_id),
  local_day TEXT NOT NULL,
  temporal_window_complete INTEGER NOT NULL CHECK (temporal_window_complete=1),
  reviewed_by TEXT NOT NULL,
  PRIMARY KEY (sensor_id, local_day)
) STRICT, WITHOUT ROWID;

CREATE VIEW derived_dli_day AS
SELECT d.sensor_id, d.zone_id, d.local_day,
       sum(d.ppfd_estimate_umol_m2_s * d.integration_seconds)/1000000.0
         AS dli_estimate_mol_m2_day,
       sum(d.integration_seconds) AS covered_seconds
FROM derived_ppfd_observation AS d
JOIN admitted_dli_day AS a ON a.sensor_id=d.sensor_id AND a.local_day=d.local_day
GROUP BY d.sensor_id, d.zone_id, d.local_day;

-- ============================================================================
-- 7. 온도 / 습도 관측
-- ============================================================================

CREATE TABLE th_sensor (
  sensor_id     TEXT PRIMARY KEY,
  sensor_model  TEXT NOT NULL,
  serial_number TEXT NOT NULL,
  measurement_height_m REAL NOT NULL CHECK (measurement_height_m >= 0),
  -- 바질 병해 조건은 군락 '내부' RH를 요구한다. 실내 일반 RH로 대체 불가.
  placement     TEXT NOT NULL CHECK (placement IN ('canopy_height','canopy_interior','room_ambient')),
  calibration_date TEXT NOT NULL
) STRICT, WITHOUT ROWID;

-- 온도와 RH는 같은 미기후 지점에서 짝지어 측정한다(collection_contract).
CREATE TABLE raw_th_observation (
  th_observation_id INTEGER PRIMARY KEY,
  sensor_id TEXT NOT NULL REFERENCES th_sensor(sensor_id),
  zone_id   TEXT NOT NULL REFERENCES zone(zone_id),
  captured_at_utc TEXT NOT NULL,
  local_day TEXT NOT NULL CHECK (length(local_day)=10),
  air_temperature_c  REAL NOT NULL,
  relative_humidity_pct REAL NOT NULL CHECK (relative_humidity_pct >= 0 AND relative_humidity_pct <= 100),
  condensation_flag INTEGER NOT NULL CHECK (condensation_flag IN (0,1)),
  quality_flag TEXT NOT NULL CHECK (quality_flag IN ('ok','suspect','saturated')),
  UNIQUE (sensor_id, captured_at_utc)
) STRICT;

-- 잎 젖음: 바질 병해 4h 조건에 필수. 미배치면 그 predicate는 missing_sensor.
CREATE TABLE raw_leaf_wetness_observation (
  lw_observation_id INTEGER PRIMARY KEY,
  sensor_id TEXT NOT NULL,
  zone_id   TEXT NOT NULL REFERENCES zone(zone_id),
  captured_at_utc TEXT NOT NULL,
  is_wet    INTEGER NOT NULL CHECK (is_wet IN (0,1)),
  UNIQUE (sensor_id, captured_at_utc)
) STRICT;

-- ============================================================================
-- 8. 미세먼지 / 소음 관측 (사람·법률 축, 작물 축이 아니다)
-- ============================================================================

CREATE TABLE pm_sensor (
  sensor_id  TEXT PRIMARY KEY,
  sensor_model TEXT NOT NULL,
  -- 2026-04-01 시행 고시. 미인증이면 공식 기준과 같은 품질등급으로 표시 금지.
  kr_certified INTEGER NOT NULL CHECK (kr_certified IN (0,1)),
  certification_grade  TEXT,
  certification_number TEXT,
  indoor_or_outdoor TEXT NOT NULL CHECK (indoor_or_outdoor IN ('indoor','outdoor')),
  inlet_height_m REAL NOT NULL,
  CHECK (kr_certified=0 OR (certification_grade IS NOT NULL AND certification_number IS NOT NULL))
) STRICT, WITHOUT ROWID;

CREATE TABLE raw_pm_observation (
  pm_observation_id INTEGER PRIMARY KEY,
  sensor_id TEXT NOT NULL REFERENCES pm_sensor(sensor_id),
  zone_id   TEXT NOT NULL REFERENCES zone(zone_id),
  captured_at_utc TEXT NOT NULL,
  local_day TEXT NOT NULL CHECK (length(local_day)=10),
  local_hour INTEGER NOT NULL CHECK (local_hour BETWEEN 0 AND 23),
  pm2_5_ug_m3 REAL NOT NULL CHECK (pm2_5_ug_m3 >= 0),
  pm10_ug_m3  REAL NOT NULL CHECK (pm10_ug_m3 >= 0),
  paired_relative_humidity_pct REAL NOT NULL,
  paired_air_temperature_c     REAL NOT NULL,
  quality_flag TEXT NOT NULL CHECK (quality_flag IN ('ok','suspect')),
  UNIQUE (sensor_id, captured_at_utc)
) STRICT;

-- 소음: 적분시간 없는 단일 dB는 환경기준과 비교할 수 없다. 그래서 NOT NULL이다.
CREATE TABLE raw_noise_observation (
  noise_observation_id INTEGER PRIMARY KEY,
  sensor_id TEXT NOT NULL,
  zone_id   TEXT NOT NULL REFERENCES zone(zone_id),
  captured_at_utc TEXT NOT NULL,
  laeq_db_a REAL NOT NULL,
  integration_period_seconds REAL NOT NULL CHECK (integration_period_seconds > 0),
  lmax_db_a REAL,
  time_weighting TEXT CHECK (time_weighting IN ('F','S')),
  instrument_class TEXT NOT NULL,
  period TEXT NOT NULL CHECK (period IN ('day','night')),
  CHECK (lmax_db_a IS NULL OR time_weighting IS NOT NULL),
  UNIQUE (sensor_id, captured_at_utc)
) STRICT;

-- ============================================================================
-- 9. 작물 맥락 + 근거 admission (default-deny)
--    근거는 맥락이 일치할 때만 들어온다. 일치는 사람이 검토해 기록한다.
-- ============================================================================

CREATE TABLE crop_context (
  context_id  TEXT PRIMARY KEY,
  zone_id     TEXT NOT NULL REFERENCES zone(zone_id),
  crop        TEXT NOT NULL,
  score_profile_id TEXT NOT NULL,
  cultivar    TEXT,
  growth_stage TEXT NOT NULL,
  cultivation_system TEXT NOT NULL,
  photoperiod_h REAL,
  valid_from_utc TEXT NOT NULL,
  valid_to_utc   TEXT,
  FOREIGN KEY (score_profile_id, crop)
    REFERENCES crop_score_profile(profile_id, crop_code),
  CHECK (valid_to_utc IS NULL OR valid_to_utc > valid_from_utc)
) STRICT, WITHOUT ROWID;

-- 과거 점수 재현성을 위해 작물과 프로필 결합은 컨텍스트 생성 후 바꿀 수 없다.
-- 프로필을 교체하려면 기존 컨텍스트를 닫고 새 context_id를 만든다.
CREATE TRIGGER crop_context_scoring_identity_immutable
BEFORE UPDATE OF crop, score_profile_id ON crop_context
WHEN NEW.crop <> OLD.crop OR NEW.score_profile_id <> OLD.score_profile_id BEGIN
  SELECT RAISE(ABORT,'crop and score_profile_id are immutable for a crop context');
END;

-- 온도 앵커 admission. 다섯 게이트가 모두 1이어야만 행이 존재할 수 있다.
-- companion_measurable=0 이면 INSERT 불가 → 상추 온도(PPFD 결합)가 여기서 걸린다.
CREATE TABLE admitted_temperature_reference (
  context_id TEXT NOT NULL REFERENCES crop_context(context_id),
  record_id  TEXT NOT NULL REFERENCES corpus_temperature_reference(record_id),
  cultivar_match       INTEGER NOT NULL CHECK (cultivar_match=1),
  growth_stage_match   INTEGER NOT NULL CHECK (growth_stage_match=1),
  environment_match    INTEGER NOT NULL CHECK (environment_match=1),
  photoperiod_match    INTEGER NOT NULL CHECK (photoperiod_match=1),
  companion_measurable INTEGER NOT NULL CHECK (companion_measurable=1),
  reviewed_by  TEXT NOT NULL,
  reviewed_at_utc TEXT NOT NULL,
  PRIMARY KEY (context_id, record_id)
) STRICT, WITHOUT ROWID;

CREATE TABLE admitted_ppfd_reference (
  context_id TEXT NOT NULL REFERENCES crop_context(context_id),
  record_id  TEXT NOT NULL REFERENCES corpus_ppfd_reference(record_id),
  cultivar_match     INTEGER NOT NULL CHECK (cultivar_match=1),
  growth_stage_match INTEGER NOT NULL CHECK (growth_stage_match=1),
  environment_match  INTEGER NOT NULL CHECK (environment_match=1),
  photoperiod_match  INTEGER NOT NULL CHECK (photoperiod_match=1),
  objective_match    INTEGER NOT NULL CHECK (objective_match=1),
  reviewed_by TEXT NOT NULL,
  reviewed_at_utc TEXT NOT NULL,
  PRIMARY KEY (context_id, record_id)
) STRICT, WITHOUT ROWID;

-- ============================================================================
-- 10. 완전 보정 입력 + 결정적 생육환경 점수
-- ============================================================================

-- API/수집 파이프라인이 교정·단위변환·품질검사를 끝낸 뒤 기록하는 canonical 경계다.
-- 세 축이 모두 NOT NULL이므로 부분 입력이나 결측을 0점으로 바꿀 수 없다.
CREATE TABLE crop_environment_observation (
  environment_observation_id INTEGER PRIMARY KEY,
  context_id TEXT NOT NULL REFERENCES crop_context(context_id),
  captured_at_utc TEXT NOT NULL,
  air_temperature_c REAL NOT NULL CHECK (
    air_temperature_c BETWEEN -50 AND 80),
  relative_humidity_pct REAL NOT NULL CHECK (
    relative_humidity_pct BETWEEN 0 AND 100),
  ppfd_umol_m2_s REAL NOT NULL CHECK (
    ppfd_umol_m2_s BETWEEN 0 AND 5000),
  input_contract TEXT NOT NULL DEFAULT 'perfect_calibrated_v1'
    CHECK (input_contract='perfect_calibrated_v1'),
  UNIQUE (context_id, captured_at_utc)
) STRICT;

CREATE TRIGGER crop_environment_observation_context_window_guard
BEFORE INSERT ON crop_environment_observation BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM crop_context AS c
    WHERE c.context_id=NEW.context_id
      AND NEW.captured_at_utc >= c.valid_from_utc
      AND (c.valid_to_utc IS NULL OR NEW.captured_at_utc < c.valid_to_utc)
  ) THEN RAISE(ABORT,'observation timestamp is outside crop context window') END;
END;

-- 각 축은 사다리꼴 함수다.
-- 0점 하한→100점 하한은 선형 증가, 목표 구간은 100점,
-- 100점 상한→0점 상한은 선형 감소, 바깥은 0점이다.
CREATE VIEW crop_environment_axis_score AS
SELECT o.environment_observation_id,
       o.context_id,
       c.zone_id,
       c.crop,
       c.score_profile_id AS profile_id,
       p.profile_version,
       o.captured_at_utc,
       o.air_temperature_c,
       o.relative_humidity_pct,
       o.ppfd_umol_m2_s,
       p.reference_photoperiod_h,
       round(
         CASE
           WHEN o.air_temperature_c <= p.temperature_zero_low_c
             OR o.air_temperature_c >= p.temperature_zero_high_c THEN 0.0
           WHEN o.air_temperature_c < p.temperature_optimal_low_c
             THEN 100.0 * (o.air_temperature_c - p.temperature_zero_low_c)
                    / (p.temperature_optimal_low_c - p.temperature_zero_low_c)
           WHEN o.air_temperature_c <= p.temperature_optimal_high_c THEN 100.0
           ELSE 100.0 * (p.temperature_zero_high_c - o.air_temperature_c)
                    / (p.temperature_zero_high_c - p.temperature_optimal_high_c)
         END, 1
       ) AS temperature_score,
       round(
         CASE
           WHEN o.relative_humidity_pct <= p.humidity_zero_low_pct
             OR o.relative_humidity_pct >= p.humidity_zero_high_pct THEN 0.0
           WHEN o.relative_humidity_pct < p.humidity_optimal_low_pct
             THEN 100.0 * (o.relative_humidity_pct - p.humidity_zero_low_pct)
                    / (p.humidity_optimal_low_pct - p.humidity_zero_low_pct)
           WHEN o.relative_humidity_pct <= p.humidity_optimal_high_pct THEN 100.0
           ELSE 100.0 * (p.humidity_zero_high_pct - o.relative_humidity_pct)
                    / (p.humidity_zero_high_pct - p.humidity_optimal_high_pct)
         END, 1
       ) AS humidity_score,
       round(
         CASE
           WHEN o.ppfd_umol_m2_s <= p.ppfd_zero_low_umol_m2_s
             OR o.ppfd_umol_m2_s >= p.ppfd_zero_high_umol_m2_s THEN 0.0
           WHEN o.ppfd_umol_m2_s < p.ppfd_optimal_low_umol_m2_s
             THEN 100.0 * (o.ppfd_umol_m2_s - p.ppfd_zero_low_umol_m2_s)
                    / (p.ppfd_optimal_low_umol_m2_s - p.ppfd_zero_low_umol_m2_s)
           WHEN o.ppfd_umol_m2_s <= p.ppfd_optimal_high_umol_m2_s THEN 100.0
           ELSE 100.0 * (p.ppfd_zero_high_umol_m2_s - o.ppfd_umol_m2_s)
                    / (p.ppfd_zero_high_umol_m2_s - p.ppfd_optimal_high_umol_m2_s)
         END, 1
       ) AS plant_light_score,
       p.temperature_weight,
       p.humidity_weight,
       p.plant_light_weight,
       p.temperature_evidence_grade,
       p.humidity_evidence_grade,
       p.plant_light_evidence_grade
FROM crop_environment_observation AS o
JOIN crop_context AS c ON c.context_id=o.context_id
JOIN crop_score_profile AS p ON p.profile_id=c.score_profile_id
                            AND p.crop_code=c.crop
WHERE o.captured_at_utc >= c.valid_from_utc
  AND (c.valid_to_utc IS NULL OR o.captured_at_utc < c.valid_to_utc);

-- 종합점수는 프로필에 결합된 가중 기하평균이다. 1/1/1은 정규화 후
-- 기존 1/3·1/3·1/3과 동일하다. 어느 축이 0점이면 종합도 0점이다.
CREATE VIEW crop_environment_score AS
WITH modeled AS (
  SELECT a.*,
         m.model_id,
         m.aggregation_family,
         m.temperature_exponent,
         m.humidity_exponent,
         m.plant_light_exponent,
         m.curve_family,
         (m.temperature_exponent
          + m.humidity_exponent
          + m.plant_light_exponent) AS exponent_sum
  FROM crop_environment_axis_score AS a
  JOIN crop_score_model_config AS m
    ON m.profile_id=a.profile_id
   AND m.crop_code=a.crop
   AND m.contract_version='score-v1'
   AND m.validation_status='validated'
), combined AS (
  SELECT m.*,
         CASE
           WHEN min(m.temperature_score,m.humidity_score,m.plant_light_score) <= 0
             THEN 0.0
           ELSE 100.0
             * pow(m.temperature_score/100.0,
                   m.temperature_exponent/m.exponent_sum)
             * pow(m.humidity_score/100.0,
                   m.humidity_exponent/m.exponent_sum)
             * pow(m.plant_light_score/100.0,
                   m.plant_light_exponent/m.exponent_sum)
         END AS overall_score_raw
  FROM modeled AS m
)
SELECT environment_observation_id,
       context_id,
       zone_id,
       crop,
       profile_id,
       profile_version,
       captured_at_utc,
       air_temperature_c,
       relative_humidity_pct,
       ppfd_umol_m2_s,
       reference_photoperiod_h,
       temperature_score,
       humidity_score,
       plant_light_score,
       model_id,
       aggregation_family,
       temperature_exponent,
       humidity_exponent,
       plant_light_exponent,
       curve_family,
       round(overall_score_raw,1) AS overall_score,
       CASE
         WHEN overall_score_raw >= 80 THEN 'GOOD'
         WHEN overall_score_raw >= 60 THEN 'NORMAL'
         ELSE 'BAD'
       END AS score_grade,
       temperature_evidence_grade,
       humidity_evidence_grade,
       plant_light_evidence_grade
FROM combined;

CREATE VIEW latest_crop_environment_score AS
SELECT s.*
FROM crop_environment_score AS s
WHERE s.captured_at_utc = (
  SELECT max(s2.captured_at_utc)
  FROM crop_environment_score AS s2
  WHERE s2.context_id=s.context_id
);

-- ============================================================================
-- 11. 근거 비교용 레거시 지표
-- ============================================================================

-- 온도: 관측을 시험 격자 위에 놓는다. 사이 구간은 '모름'이지 '나쁨'이 아니다.
CREATE VIEW temperature_reference_indicator AS
SELECT c.context_id, c.crop, o.captured_at_utc, r.record_id, r.claim_role,
       o.air_temperature_c,
       CASE
         WHEN r.tested_grid_enumerated=0 THEN 'tested_grid_not_enumerated'
         WHEN o.air_temperature_c < r.tested_low_c  THEN 'below_tested_range_unknown'
         WHEN o.air_temperature_c > r.tested_high_c THEN 'above_tested_range_unknown'
         WHEN EXISTS (SELECT 1 FROM json_each(r.tested_grid_json) AS g
                       WHERE g.value = o.air_temperature_c)
           THEN 'matches_tested_treatment'
         ELSE 'untested_between_levels'
       END AS indicator_state,
       -- 최고 처리점까지의 거리. 등급이 아니라 °C 단위의 거리다.
       CASE WHEN r.regime_kind='single_value'
            THEN o.air_temperature_c - r.best_value_c END AS distance_to_best_c,
       r.prohibited_use
FROM raw_th_observation AS o
JOIN zone AS z ON z.zone_id = o.zone_id
JOIN crop_context AS c ON c.zone_id = o.zone_id
  AND o.captured_at_utc >= c.valid_from_utc
  AND (c.valid_to_utc IS NULL OR o.captured_at_utc < c.valid_to_utc)
JOIN admitted_temperature_reference AS a ON a.context_id = c.context_id
JOIN corpus_temperature_reference  AS r ON r.record_id = a.record_id AND r.crop = c.crop
WHERE o.quality_flag='ok';

-- 광: derived_ppfd_observation 에서만 나온다. 교정이 없으면 이 뷰는 비어 있다.
CREATE VIEW ppfd_reference_indicator AS
SELECT c.context_id, c.crop, d.captured_at_utc, r.record_id, r.claim_role, r.objective,
       d.ppfd_estimate_umol_m2_s, d.ppfd_abs_error_umol_m2_s,
       CASE
         WHEN d.ppfd_estimate_umol_m2_s > r.tested_high_ppfd_umol_m2_s AND r.bounded_above=0
           THEN 'above_tested_range_unknown'
         WHEN d.ppfd_estimate_umol_m2_s NOT BETWEEN r.tested_low_ppfd_umol_m2_s
                                                AND r.tested_high_ppfd_umol_m2_s
           THEN 'outside_tested_grid_unknown'
         ELSE 'distance_only'
       END AS indicator_state,
       CASE
         WHEN r.point_ppfd_umol_m2_s IS NOT NULL
           THEN d.ppfd_estimate_umol_m2_s - r.point_ppfd_umol_m2_s
         WHEN d.ppfd_estimate_umol_m2_s < r.band_low_ppfd_umol_m2_s
           THEN d.ppfd_estimate_umol_m2_s - r.band_low_ppfd_umol_m2_s
         WHEN d.ppfd_estimate_umol_m2_s > r.band_high_ppfd_umol_m2_s
           THEN d.ppfd_estimate_umol_m2_s - r.band_high_ppfd_umol_m2_s
         ELSE 0
       END AS distance_ppfd_umol_m2_s,
       r.prohibited_use
FROM derived_ppfd_observation AS d
JOIN crop_context AS c ON c.zone_id = d.zone_id
  AND d.captured_at_utc >= c.valid_from_utc
  AND (c.valid_to_utc IS NULL OR d.captured_at_utc < c.valid_to_utc)
JOIN admitted_ppfd_reference AS a ON a.context_id = c.context_id
JOIN corpus_ppfd_reference  AS r ON r.record_id = a.record_id AND r.crop = c.crop;

-- 아래 상태 뷰는 기존 BH1750 교정 파이프라인의 진단용이다.
-- crop_environment_score의 입력 가능 여부나 점수 상태를 뜻하지 않는다.
CREATE VIEW legacy_lux_ingestion_status AS
SELECT z.zone_id, lr.regime_id,
       CASE
         WHEN lr.spectrum_mode='mixed_daylight_artificial' THEN 'blocked_measurement_basis'
         WHEN lr.spectrum_mode='variable_or_unknown'       THEN 'blocked_measurement_basis'
         WHEN NOT EXISTS (SELECT 1 FROM lux_ppfd_validation v
                           WHERE v.regime_id=lr.regime_id AND v.status='accepted')
           THEN 'blocked_measurement_basis'
         ELSE 'reference_available'
       END AS axis_state,
       CASE
         WHEN lr.spectrum_mode <> 'fixed_artificial' OR lr.daylight_excluded=0
           THEN '자연광이 섞여 lux를 PPFD로 변환할 수 없습니다. 차광 구간을 만들어야 합니다.'
         WHEN NOT EXISTS (SELECT 1 FROM lux_ppfd_validation v
                           WHERE v.regime_id=lr.regime_id AND v.status='accepted')
           THEN '양자센서 교정이 필요합니다. lux 값은 수집 중입니다.'
         ELSE NULL
       END AS blocked_reason_ko
FROM zone AS z
JOIN light_regime AS lr ON lr.zone_id = z.zone_id;

CREATE VIEW noise_axis_status AS
SELECT s.site_id,
       CASE WHEN s.legal_zone_class IS NULL OR s.is_roadside IS NULL
            THEN 'blocked_missing_context' ELSE 'reference_available' END AS axis_state,
       CASE WHEN s.legal_zone_class IS NULL OR s.is_roadside IS NULL
            THEN '부지의 법적 용도지역과 도로변 여부를 설정해야 평가할 수 있습니다.'
            END AS blocked_reason_ko
FROM site AS s;

-- ============================================================================
-- 12. 점수 계약 메모
-- ============================================================================
-- * 점수 입력 경계는 crop_environment_observation 하나다.
-- * raw lux/센서 상태를 점수 뷰에 직접 JOIN하지 않는다.
-- * 경계·가중치 변경은 crop_score_profile 새 버전 INSERT로만 배포한다.
-- * evidence grade C는 대분류 추정값임을 API/UI에서 반드시 노출한다.
-- * overall_score는 작물 생육환경 적합도 휴리스틱이지 생존율·수확량이 아니다.
