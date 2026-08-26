-- 화분별 자동 제어 스위치.
--
-- 룰 엔진이 순회 대상에서 제외하는 근거이며, 기본값은 TRUE 다. 기존 화분은
-- 이 컬럼이 생기기 전부터 자동 제어를 기대하고 있었고, FALSE 로 백필하면
-- 마이그레이션 하나로 모든 화분의 관수가 조용히 멈춘다.
--
-- 수동 관수와 조명 명령은 이 스위치와 무관하다. 스위치를 끄는 것은 "내가
-- 직접 하겠다" 이지 "아무것도 하지 말라" 가 아니다.
ALTER TABLE pot ADD COLUMN auto_control_enabled BOOLEAN NOT NULL DEFAULT TRUE;
