-- PPFD 0점 하한의 미입력 자리표시자 0을 광보상점 근사값으로 교체한 프로필 v3.
-- 작물별 실측값이 아니라 기존 PPFD 최적 하한의 10%를 정수로 반올림하는 단일 규칙을
-- 적용했다. 산출값은 해당 내음성 등급에 공개된 광보상점 범위 안에 있다
-- (그늘 전문 작물 약 5-10, 엽채류 15-25, 과채류 30-40 µmol/m²/s).
-- 특정 논문의 작물별 측정값을 인용한 것이 아니며 직접 측정 전까지 사용하는 근사값이다.

PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

INSERT OR IGNORE INTO crop_score_profile VALUES
  ('basil-general-v3','basil','바질','warm_herb','general_vegetative',
   7,18,29,36, 30,50,80,95, 26,260,600,1000,16,
   40,25,35,'A','C','A','hybrid',
   'FAO 7/18-27/36°C와 바질 29°C 생육시험; DLI 15-25 및 500-600 PPFD 시험; RH 50-80 CEA 일반범위와 85% 이상 노균병 위험; 광보상점 26은 작물별 실측이 아니라 기존 PPFD 최적 하한의 10%를 정수 반올림한 단일 규칙값이며, 내음성 등급별 공개 범위(그늘 전문 5-10, 엽채 15-25, 과채 30-40) 안의 직접 측정 전 근사값',
   '3.0.0','2026-08-24T00:00:00Z'),
  ('peppermint-general-v3','peppermint','페퍼민트','moderate_light_herb','general_vegetative',
   4,15,25,35, 30,50,80,95, 15,150,200,250,14,
   40,25,35,'B','C','A','hybrid',
   'FAO 4/15-25/35°C; Mentha 150-200 PPFD 적합 및 250 PPFD 스트레스 시험; RH는 CEA 일반범위; 광보상점 15는 작물별 실측이 아니라 기존 PPFD 최적 하한의 10%를 정수 반올림한 단일 규칙값이며, 내음성 등급별 공개 범위(그늘 전문 5-10, 엽채 15-25, 과채 30-40) 안의 직접 측정 전 근사값',
   '3.0.0','2026-08-24T00:00:00Z'),
  ('cherry-tomato-general-v3','cherry_tomato','방울토마토','warm_fruiting','general_fruiting',
   7,18.5,26.5,35, 30,65,75,90, 30,300,521,800,16,
   40,25,35,'B','B','B','general_reference',
   'FAO 절대 7-35°C와 온실 토마토 18.5-26.5°C·RH 65-75%; 토마토 DLI 20-30 및 300 PPFD 효율 시험; 광보상점 30은 작물별 실측이 아니라 기존 PPFD 최적 하한의 10%를 정수 반올림한 단일 규칙값이며, 내음성 등급별 공개 범위(그늘 전문 5-10, 엽채 15-25, 과채 30-40) 안의 직접 측정 전 근사값',
   '3.0.0','2026-08-24T00:00:00Z'),
  ('welsh-onion-general-v3','welsh_onion','대파','cool_leafy_herb','general_vegetative',
   6,12,25,30, 30,50,80,95, 21,208,347,600,16,
   40,25,35,'B','C','C','category_fallback',
   'FAO 대파 절대 6-30°C·최적 12-25°C; RH와 PPFD는 직접 구배시험 부재로 CEA·엽채류 휴리스틱 유지; 광보상점 21은 작물별 실측이 아니라 기존 PPFD 최적 하한의 10%를 정수 반올림한 단일 규칙값이며, 내음성 등급별 공개 범위(그늘 전문 5-10, 엽채 15-25, 과채 30-40) 안의 직접 측정 전 근사값',
   '3.0.0','2026-08-24T00:00:00Z'),
  ('arugula-general-v3','arugula','아루굴라','cool_leafy_herb','general_vegetative',
   8,15,25,29, 30,50,80,95, 20,200,250,600,16,
   40,25,35,'B','C','A','hybrid',
   'FAO 아루굴라 절대 8-29°C·최적 15-25°C; 성숙 로켓 250 PPFD·DLI 14.4 비교시험; RH는 CEA 일반범위; 광보상점 20은 작물별 실측이 아니라 기존 PPFD 최적 하한의 10%를 정수 반올림한 단일 규칙값이며, 내음성 등급별 공개 범위(그늘 전문 5-10, 엽채 15-25, 과채 30-40) 안의 직접 측정 전 근사값',
   '3.0.0','2026-08-24T00:00:00Z'),
  ('wasabi-general-v3','wasabi','와사비','cool_shade','general_vegetative',
   5,12,18,26, 40,60,80,95, 9,90,140,250,12,
   40,25,35,'B','C','A','hybrid',
   '와사비 5°C 야간 생육정체·12-18°C 적온·고온 민감성; Daruma 90-140 PPFD 고광합성·140 PPFD 최고 생체중; RH 68% 시험조건; 광보상점 9는 작물별 실측이 아니라 기존 PPFD 최적 하한의 10%를 정수 반올림한 단일 규칙값이며, 내음성 등급별 공개 범위(그늘 전문 5-10, 엽채 15-25, 과채 30-40) 안의 직접 측정 전 근사값',
   '3.0.0','2026-08-24T00:00:00Z'),
  ('lettuce-general-v3','lettuce','상추','cool_leafy_herb','general_vegetative',
   5,12,24,30, 30,60,75,90, 20,200,295,500,16,
   40,25,35,'B','B','A','hybrid',
   'FAO 절대 5-30°C·최적 12-21°C와 CEA 24°C 효율시험; RH 70-75% 연구; 상추 DLI 12-17을 16h PPFD로 환산; 광보상점 20은 작물별 실측이 아니라 기존 PPFD 최적 하한의 10%를 정수 반올림한 단일 규칙값이며, 내음성 등급별 공개 범위(그늘 전문 5-10, 엽채 15-25, 과채 30-40) 안의 직접 측정 전 근사값',
   '3.0.0','2026-08-24T00:00:00Z'),
  ('coriander-general-v3','coriander','고수','cool_leafy_herb','fresh_leaf',
   4,15,26,32, 30,50,70,90, 20,200,200,400,16,
   40,25,35,'B','C','A','hybrid',
   'FAO 절대 4-32°C·최적 15-25°C와 표준 고수 약 26°C 생체중 최적; 200 PPFD·16h 직접 권고; RH는 CEA 일반범위; 광보상점 20은 작물별 실측이 아니라 기존 PPFD 최적 하한의 10%를 정수 반올림한 단일 규칙값이며, 내음성 등급별 공개 범위(그늘 전문 5-10, 엽채 15-25, 과채 30-40) 안의 직접 측정 전 근사값',
   '3.0.0','2026-08-24T00:00:00Z');

INSERT OR IGNORE INTO crop_score_model_config
  (model_id,profile_id,crop_code,contract_version,aggregation_family,
   temperature_exponent,humidity_exponent,plant_light_exponent,curve_family,
   validation_status,evidence_revision,change_reason,created_at_utc)
SELECT replace(model_id, '-general-v2-score-v1', '-general-v3-score-v1'),
       replace(profile_id, '-general-v2', '-general-v3'),
       crop_code,contract_version,aggregation_family,
       temperature_exponent,humidity_exponent,plant_light_exponent,curve_family,
       validation_status,evidence_revision,
       'PPFD 0점 하한을 최적 하한의 10%로 반올림한 광보상점 근사값으로 교체',
       '2026-08-24T00:00:00Z'
FROM crop_score_model_config
WHERE profile_id IN (
  'basil-general-v2','peppermint-general-v2','cherry-tomato-general-v2',
  'welsh-onion-general-v2','arugula-general-v2','wasabi-general-v2',
  'lettuce-general-v2','coriander-general-v2'
);

UPDATE crop_score_profile_activation
SET profile_id = CASE crop_code
  WHEN 'basil' THEN 'basil-general-v3'
  WHEN 'peppermint' THEN 'peppermint-general-v3'
  WHEN 'cherry_tomato' THEN 'cherry-tomato-general-v3'
  WHEN 'welsh_onion' THEN 'welsh-onion-general-v3'
  WHEN 'arugula' THEN 'arugula-general-v3'
  WHEN 'wasabi' THEN 'wasabi-general-v3'
  WHEN 'lettuce' THEN 'lettuce-general-v3'
  WHEN 'coriander' THEN 'coriander-general-v3'
END,
activated_at_utc = '2026-08-24T00:00:00Z'
WHERE crop_code IN (
  'basil','peppermint','cherry_tomato','welsh_onion',
  'arugula','wasabi','lettuce','coriander'
);

COMMIT;
