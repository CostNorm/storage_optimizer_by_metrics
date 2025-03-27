# EBS 스토리지 최적화 시스템 아키텍처

## 개요

EBS 스토리지 최적화 시스템은 AWS EBS 볼륨의 사용 패턴을 분석하여 비용 최적화를 위한 추천 사항을 제공하고 실행하는 시스템입니다. 이 시스템은 서버리스 아키텍처로 구성되어 있으며, AWS Lambda, SQS, Slack 등의 서비스를 활용합니다.

## 통합 아키텍처 구성

이전에는 시스템이 3개의 개별 Lambda 함수로 구성되었지만, 이제는 하나의 통합된 Lambda 함수(`consolidated_lambda.py`)로 모든 기능을 처리합니다. 이 통합 함수는 이벤트 유형을 감지하여 적절한 핸들러 함수를 호출합니다:

1. **분석 및 알림 기능** (`handle_analyze_request`)

   - EBS 볼륨의 사용 패턴을 분석
   - 유휴 상태 볼륨 및 과대 프로비저닝된 볼륨 감지
   - 분석 결과를 S3에 저장
   - Slack으로 알림 전송 (Block Kit UI 사용)
   - CloudWatch Events에 의해 주기적으로 트리거됨

2. **Slack 요청 처리 기능** (`handle_slack_request`)

   - API Gateway를 통해 Slack 요청 수신
   - Slack 요청 검증 및 처리
   - 인터랙티브 요청 처리 (버튼 클릭 등)
   - 슬래시 커맨드 처리 (`/ebs-optimize`)
   - 빠른 응답 반환 또는 액션 직접 실행

3. **액션 실행 기능** (`handle_sqs_message`)
   - SQS 큐에서 액션 요청을 가져와 처리
   - 요청된 액션 실행 (스냅샷 생성, 볼륨 삭제 등)
   - 실행 결과를 Slack으로 알림

## 폴더 구조

```
storage_optimizer_by_metrics/
├── config/             # 설정 관련 파일
│   ├── config.py       # 주요 설정 값
│   └── .env.template   # 환경 변수 템플릿
├── docs/               # 문서 파일
│   ├── ARCHITECTURE.md # 아키텍처 문서
│   └── USAGE.md        # 사용 설명서
├── lambdas/            # Lambda 함수
│   └── consolidated_lambda.py  # 통합된 Lambda 핸들러
├── utils/              # 공통 유틸리티
│   ├── ebs_analyzer.py # EBS 볼륨 분석기
│   └── utils.py        # 공통 유틸리티 함수
├── integrations/       # 외부 서비스 연동
│   └── slack/          # Slack 관련 기능
│       ├── slack_messenger.py  # Slack 메시지 관련 함수
│       └── slack_verifier.py   # Slack 요청 검증 함수
└── actions/            # 볼륨 액션 관련 코드
    ├── recommendation_executor.py  # 권장 조치 실행기
    └── ebs_actions.py              # EBS 볼륨 액션 처리
```

## 시스템 흐름

1. **분석 흐름**

   - CloudWatch Events가 스케줄에 따라 Lambda 함수 호출 (분석 요청 이벤트)
   - Lambda가 `handle_analyze_request` 함수 실행
   - 모든 리전의 EBS 볼륨 분석 수행
   - 분석 결과를 S3에 저장
   - 요약 결과를 Slack으로 전송
   - 권장 조치를 Slack 메시지에 버튼으로 표시

2. **액션 요청 흐름**

   - 사용자가 Slack에서 버튼 클릭 또는 슬래시 커맨드 입력
   - API Gateway를 통해 Lambda 함수 호출 (Slack 요청 이벤트)
   - Lambda가 `handle_slack_request` 함수 실행
   - 요청을 검증하고 직접 처리 또는 SQS 큐에 추가
   - 사용자에게 요청 접수 확인 메시지 전송

3. **액션 실행 흐름**
   - SQS 트리거가 Lambda 함수 호출 (SQS 메시지 이벤트)
   - Lambda가 `handle_sqs_message` 함수 실행
   - 요청된 액션 실행 (EBS 볼륨에 대한 작업)
   - 실행 결과를 Slack으로 전송

## 통합 구성 요소

1. **AWS 서비스**

   - Lambda: 통합 서버리스 함수 실행
   - SQS: 메시지 큐잉
   - S3: 분석 결과 저장
   - CloudWatch: 로깅 및 스케줄링
   - API Gateway: Slack 요청 처리

2. **Slack 통합**
   - Incoming Webhooks: 알림 전송
   - Slash Commands: `/ebs-optimize` 커맨드
   - Interactive Components: 버튼 및 액션 처리

## 설정 방법

### 사전 요구 사항

- AWS 계정
- Slack 워크스페이스 관리자 권한
- Python 3.8 이상

### 1. AWS 리소스 설정

1. **S3 버킷 생성**

   - 분석 결과를 저장할 S3 버킷 생성

2. **SQS 대기열 생성**

   - 액션 요청을 저장할 SQS 대기열 생성

3. **IAM 역할 설정**
   - Lambda 함수에 필요한 권한 설정 (S3, CloudWatch, EC2, SQS 등)

### 2. Slack 앱 설정

1. **Slack 앱 생성**

   - Slack API 사이트에서 새로운 앱 생성

2. **Incoming Webhooks 설정**

   - 웹훅 URL 발급

3. **슬래시 커맨드 설정**

   - `/ebs-optimize` 커맨드 등록
   - 엔드포인트를 API Gateway URL로 설정

4. **인터랙티브 컴포넌트 설정**
   - 요청 URL을 API Gateway URL로 설정

### 3. 통합 Lambda 함수 배포

1. **Lambda 함수 배포**

   - `consolidated_lambda.py` 파일 업로드
   - 환경 변수 설정
   - 여러 트리거 설정:
     - CloudWatch Events 트리거 (분석 기능용)
     - API Gateway 트리거 (Slack 요청 처리용)
     - SQS 트리거 (액션 실행용)

2. **Lambda 구성**
   - 메모리: 최소 256MB (권장 512MB)
   - 타임아웃: 최소 1분 (전체 분석의 경우 3-5분 권장)
   - 동시성: 필요에 따라 조정

### 4. 환경 변수 설정

Lambda 함수에 필요한 환경 변수를 설정합니다. `.env.template` 파일을 참고하여 설정하세요.

## 보안 고려사항

1. **Slack 요청 검증**

   - 요청 서명 검증을 통한 요청 무결성 확인

2. **IAM 권한 최소화**

   - Lambda 함수에 최소 필요 권한만 부여

3. **민감 정보 관리**
   - Slack 토큰 및 비밀 키는 환경 변수로 관리
   - AWS Secrets Manager 활용 권장

## 모니터링 및 유지 관리

1. **로깅**

   - CloudWatch Logs를 통한 Lambda 함수 로깅
   - 이벤트 유형별 로그 구분을 통한 문제 추적 용이

2. **알림**

   - Lambda 함수 오류 시 알림 설정
   - CloudWatch Alarms를 통한 모니터링

3. **정기적 검토**
   - 권장 조치의 정확성 및 효과성 검토
   - 시스템 파라미터 조정 (필요 시)
   - Lambda 성능 지표 모니터링 및 최적화
