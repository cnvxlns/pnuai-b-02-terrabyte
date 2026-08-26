# Frontend


## 폴더 구조

```text
frontend/
└─ app/                         # Expo React Native 앱 루트
  ├─ App.tsx                    # 앱 전체 흐름을 묶는 최상위 컴포넌트
  ├─ index.ts                   # 앱 진입점
  ├─ app.config.ts              # Firebase 설정을 연결하는 동적 Expo 설정
  ├─ app.json                   # Expo 기본 설정
  ├─ eas.json                   # EAS Android APK 빌드 프로필
  ├─ assets/                    # 앱 아이콘·이미지 등 정적 에셋
  ├─ src/                       # 실제 화면과 기능 코드
    ├─ appTheme/                # 색상, 글꼴, 스타일 토큰
    ├─ analysis/                # 작물 대체 추천 API
    ├─ auth/                    # 로그인/세션 API
    ├─ care/                    # Gemini 관리 계획 API
    ├─ cart/                    # 장바구니 API
    ├─ components/              # 재사용 UI 컴포넌트
    ├─ crop/                    # 작물 API와 목록 조회 훅
    ├─ device/                  # 기기 관련 API
    ├─ history/                 # 진단 이력 API
    ├─ measurement/             # 환경 측정 API
    ├─ navigation/              # 화면 이동과 레이아웃
    ├─ notification/            # 알림함·푸시 토큰 처리
    ├─ onboarding/              # 로그인 및 초기 설정 화면
    ├─ order/                   # 주문 API
    ├─ payment/                 # 토스 결제 처리
    ├─ pot/                     # 화분 조회 API
    ├─ screens/                 # 실제 서비스 화면
    ├─ sensor/                  # 기기별 센서 상태 API
    ├─ shared/                  # 공용 훅/가공 로직
    ├─ shop/                    # 상품 목록 API
    ├─ space/                   # 재배 공간 API
    └─ soil/                    # 토양 추천 API
  └─ package.json               
└─ README.md
```

## 실행 방법

### Docker 로 실행 (권장)

저장소 루트에서 아래 한 줄이면 백엔드·DB 와 함께 Expo 웹 개발 서버(http://localhost:8081)가 뜹니다.
Node.js/npm 버전은 컨테이너에 고정되어 있어 별도 설치가 필요 없습니다.

```bash
cp .env.example .env
docker compose up --build
```

iOS 시뮬레이터·Android 에뮬레이터 실행은 네이티브 SDK 가 필요해 컨테이너에서 지원되지 않으므로
아래의 호스트 실행 방법을 사용합니다. 자세한 내용은
[`docs/docker_dev_environment.md`](../docs/docker_dev_environment.md) 를 참고하세요.

### 사전 준비

처음 받았거나 의존성 상태가 불일치하면 lockfile 기준으로 다시 설치합니다.

```powershell
cd frontend/app
npm ci
```

패키지를 추가하거나 버전을 변경한 경우에만 `npm install`을 사용합니다. `expo-notifications` 관련
플러그인 해석 오류가 나면 `node_modules`가 lockfile과 일치하지 않는 상태이므로 `npm ci`를 다시 실행하세요.

## 실행

Expo 개발 서버 실행:

```powershell
cd frontend/app
npx expo start
```

웹 브라우저로 실행:

```powershell
cd frontend/app
npm run web
```

Android 실행:

```powershell
cd frontend/app
npm run android
```

iOS 실행:

```powershell
cd frontend/app
npm run ios
```

## Android 푸시 알림과 APK 빌드

Android 푸시는 Expo Go가 아니라 Firebase 설정이 포함된 네이티브 빌드에서 동작합니다. Firebase Console에서
Android 앱을 만들 때 패키지 이름은 `com.terrabyte.app`을 사용합니다.

### Firebase 클라이언트 설정

Firebase Console에서 Android 앱의 `google-services.json`을 내려받아 로컬에서는
`frontend/app/google-services.json`에 둡니다. 이 파일은 `.gitignore`에 포함되어 있으므로 커밋하지 않습니다.
EAS Build에서는 preview 환경에 이름이 `GOOGLE_SERVICES_JSON`인 **File** 환경변수를 만들고 같은 파일을
업로드합니다. `app.config.ts`가 EAS의 임시 파일 경로 또는 로컬 파일을 자동으로 `android.googleServicesFile`에
연결합니다.

실제 휴대폰에서 개발 서버를 사용할 때 `localhost`는 휴대폰 자신을 가리킵니다. 휴대폰과 개발 PC를 같은
네트워크에 연결하고 `frontend/app/.env.local`에 개발 PC의 내부 IP를 지정합니다.

```text
EXPO_PUBLIC_API_BASE_URL=http://192.168.0.10:8080
```

### 설치 가능한 preview APK

`eas.json`의 `preview` 프로필은 Android `apk`를 생성하도록 설정되어 있습니다.

```powershell
cd frontend/app
npx eas-cli build --platform android --profile preview
```

빌드가 끝나면 EAS가 제공하는 URL로 APK를 내려받아 Android 기기 또는 Google Play services가 포함된
에뮬레이터에 설치합니다. 실제 기기는 APK 파일을 직접 열어 설치하거나 다음 명령을 사용할 수 있습니다.

```powershell
adb install path\to\terrabyte.apk
```

### 푸시 End-to-End 확인

1. 백엔드에 Firebase 서비스 계정을 주입하고 `FIREBASE_ENABLED=true`로 실행합니다.
2. 설치한 앱에서 로그인하고 Android 알림 권한을 허용합니다.
3. 백엔드의 `push_registration`에 해당 사용자의 Android 토큰이 활성 상태로 저장됐는지 확인합니다.
4. 알림 하나를 발생시킵니다. 세 종류 모두 확인하려면 센서 quality 오류, MQTT gateway offline,
   그리고 관수를 실행해 게이트웨이가 `completed` ack를 올려보내게 합니다.
5. 앱의 포그라운드, 백그라운드, 종료 상태에서 알림 수신을 각각 확인합니다.
6. 알림을 눌렀을 때 관련 기기와 화분이 선택되고, 헤더 알림함의 읽지 않은 개수가 일치하는지 확인합니다.
7. 로그아웃 후 같은 기기로 푸시가 더 이상 전송되지 않는지 확인합니다.

저장소에서 실행 가능한 TypeScript·Expo 설정·웹 번들 검사는 자동화할 수 있지만, 위 End-to-End 확인은
Firebase/EAS 자격 증명과 Android 실행 환경을 준비한 뒤 수행합니다.

## API 연동 현황

### 공통 요청 처리

`app/src/auth/authApi.ts`의 `apiRequest`가 모든 HTTP 요청을 처리하고, `authenticatedRequest`가 저장된 JWT를 `Authorization: Bearer <token>` 헤더에 넣습니다. API 서버 주소는 `EXPO_PUBLIC_API_BASE_URL` 또는 기본값(`http://localhost:8080`)을 사용합니다.

### API 클라이언트와 호출 대상

| 프론트 API 코드 | 실제 호출 화면/흐름 | 호출 API |
| --- | --- | --- |
| `app/src/auth/authApi.ts` | 로그인, 회원가입, 세션 복원 | `POST /api/auth/login`, `POST /api/auth/signup`, `GET /api/me` |
| `app/src/device/deviceApi.ts` | 기기 등록·조회, 화분 생성·수정 | `POST /api/devices`, `GET /api/devices/:deviceId`, `POST /api/devices/:deviceId/pots`, `PATCH /api/pots/:potId` |
| `app/src/space/spaceApi.ts` | 공간 목록 조회, 공간 등록 | `GET /api/spaces`, `POST /api/spaces` |
| `app/src/pot/potApi.ts` | 화분 목록·상세 조회 및 선택 상태 갱신 | `GET /api/pots`, `GET /api/pots/:potId` |
| `app/src/sensor/sensorApi.ts` | 기기별 센서 목록·상태 조회 | `GET /api/devices/:deviceId/sensors` |
| `app/src/crop/cropApi.ts` | 작물 검색·선택 | `GET /api/crops`, `PATCH /api/pots/:potId/crop` |
| `app/src/measurement/measurementApi.ts` | 최신 측정값, 환경 점수, 5개 지표 시계열 | `GET /api/pots/:potId/measurements/latest`, `GET /api/pots/:potId/score`, `GET /api/pots/:potId/measurements` |
| `app/src/soil/soilApi.ts` | 토양 배합 추천 | `GET /api/pots/:potId/soil-recommendation` |
| `app/src/analysis/analysisApi.ts` | 대체 작물 추천 | `GET /api/pots/:potId/crop-recommendations` |
| `app/src/history/historyApi.ts` | 측정 이력 기반 진단 점수 이력 | `GET /api/pots/:potId/diagnostic-history` |
| `app/src/shop/shopApi.ts` | 상품 목록 | `GET /api/products` |
| `app/src/cart/cartApi.ts` | 상품 구매 화면의 장바구니 조회·추가·수량 변경·삭제 | `GET /api/cart`, `POST /api/cart/items`, `PATCH /api/cart/items/:productId`, `DELETE /api/cart/items/:productId`, `DELETE /api/cart` |
| `app/src/order/orderApi.ts` | 주문 생성, 주문 내역·상세 조회, 결제 전 주문 취소 | `POST /api/orders`, `GET /api/orders`, `GET /api/orders/:orderId`, `POST /api/orders/:orderId/cancel` |
| `app/src/payment/paymentApi.ts` | 토스 결제 준비·승인·실패 처리, 결제 완료 주문의 전체 취소 | `POST /api/payments/ready`, `POST /api/payments/confirm`, `POST /api/payments/fail`, `GET /api/orders/:orderId/payment`, `POST /api/payments/:paymentId/cancel` |
| `app/src/care/carePlanApi.ts` | Gemini 기반 관리 우선순위·지표별 상세 진단·오늘 할 일·재배 기준·개선 방안·예상 변화·상품 추천 | `GET /api/pots/:potId/care-plan` |
| `app/src/notification/notificationApi.ts` | 공통 헤더 알림함과 Android 푸시 등록 | `POST/DELETE /api/push-tokens`, `DELETE /api/push-tokens/all`, `GET/PATCH /api/notifications`, `GET /api/notifications/unread-count` |

### 주문 취소 정책

주문 상세에서 상태에 따라 사용할 수 있는 취소 기능이 달라집니다.

| 주문 상태 | 화면 동작 | 호출 API |
| --- | --- | --- |
| `PENDING` | 주문 취소 또는 결제 계속 | `POST /api/orders/:orderId/cancel` |
| `PAID` | 취소 사유를 입력한 뒤 결제 취소 확인 | `GET /api/orders/:orderId/payment` → `POST /api/payments/:paymentId/cancel` |
| `PREPARING`, `SHIPPED`, `DELIVERED`, `CANCELLED` | 취소 버튼을 표시하지 않음 | - |

결제 완료 주문은 전체 취소만 지원합니다. 결제 취소가 완료되면 주문 상태도 `CANCELLED`로 갱신되며, 주문 상세와 목록을 다시 조회해 화면에 반영합니다.

### 화면별 연동 상태

| 화면 | 상태 | 실제 API 연동 범위 | 비고 |
| --- | --- | --- | --- |
| 대시보드 | 완료 | 공간·기기 연결 상태, 기기별 센서 상태, 최신값, 환경 점수 factor(온도·습도·조도·토양 수분·토양 온도), 5개 지표 시계열 | 차트 색상·단위만 화면 표현 규칙으로 관리 |
| 실시간 모니터링 | 완료 | 최신값과 5개 지표의 시계열, 최저·최고값 | 없음 |
| 공간 진단 | 완료 (Gemini 설정 기준) | 공간명, 환경 점수 factor 5종, 최신 측정값, 토양 추천, 대체 작물 추천, Gemini 관리 우선순위·지표별 상세 진단·개선 방안·예상 변화 | 관리 계획 API 응답이 없으면 결과를 표시하지 않음 |
| 진단 이력 | 완료 | 최근 30일 측정 샘플을 기준으로 재계산한 적합도 이력 | 측정 데이터가 없으면 이력도 없음 |
| 관리 가이드 | 완료 (Gemini 설정 기준) | 토양 배합·재료·주의사항, Gemini 오늘 할 일·재배 기준·상품 추천 | 관리 계획 API 응답이 없으면 결과를 표시하지 않음. 작업 완료 상태는 화면 메모리에만 유지 |
| 상품 구매 | 완료 (웹 결제 기준) | 상품 목록, 서버 장바구니, 주문 생성·내역·상세, 결제 전 주문 취소, 결제 완료 주문의 전체 취소, 토스 결제 준비·승인·실패 반환 처리 | 토스 결제창은 웹에서만 지원하며, `TOSS_PAYMENTS_ENABLED`와 토스 키·성공/실패 URL 설정이 필요. 부분 취소와 배송 준비 이후 취소는 지원하지 않음 |
| 사이드바 | 완료 | 기기 상태, 공간, 화분 수, 마지막 수신 시각, 기기별 센서 상태 | 없음 |
| 기기 등록 온보딩 | 완료 | 공간 목록 조회·기존 공간 선택, 새 공간 저장 후 기기 연결, 작물 목록·검색·선택 | 기존 공간은 `spaceId`로 연결하고, 새 공간은 `POST /api/spaces`로 저장 |
| 화분 선택 메뉴 | 완료 | 현재 기기의 화분 목록·상세, 작물 목록, 화분 생성·수정 | 작물 목록은 `GET /api/crops`를 사용 |
