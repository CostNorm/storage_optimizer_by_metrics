# EBS 스토리지 최적화 시스템 사용 설명서

## 개요

이 문서는 통합된 Lambda 함수를 사용하여 AWS EBS 볼륨을 분석하고 최적화하는 방법을 설명합니다. 이 통합 Lambda 함수(`lambdas/consolidated_lambda.py`)는 이벤트 유형에 따라 볼륨 분석, Slack 요청 처리, 그리고 SQS를 통한 권장 조치 실행의 세 가지 주요 기능을 조정합니다.

## 통합 Lambda 함수 아키텍처

통합 Lambda 함수는 이벤트 소스를 감지하고, 다음 핸들러 모듈로 작업을 위임합니다:

1. **분석 요청 처리** (`lambdas/consolidated_lambda.py`의 `handle_analyze_request`):
   - EBS 볼륨 사용 패턴 분석 및 권장 조치 생성 (주요 로직은 `ebs/ebs_service.py`에 위임)
   - 분석 결과를 S3에 저장
   - Slack으로 요약 알림 전송 (`integrations/slack/slack_messenger.py` 사용)
2. **Slack 요청 처리** (`lambdas/handlers/slack_handler.py`):
   - Slack에서의 슬래시 명령어 및 상호작용 처리
   - 빠른 초기 응답 후, 실제 처리를 위해 Lambda 자체를 비동기 호출하거나 SQS 큐에 작업 추가
3. **SQS 메시지 처리** (`lambdas/handlers/sqs_handler.py`):
   - SQS 큐에서 받은 메시지(주로 액션 실행 요청) 처리
   - 요청된 액션 실행 (스냅샷 생성, 볼륨 삭제 등, `ebs/actions/ebs_actions.py` 사용)
   - 실행 결과를 Slack으로 알림 (`integrations/slack/slack_messenger.py` 사용)

## 설정 방법

### 1. Lambda 함수 설정

1. **Lambda 함수 배포**:

   - 소스 코드: 프로젝트 전체 (의존성 포함하여 배포 패키지 생성)
   - 핸들러: `lambdas.consolidated_lambda.lambda_handler`
   - 런타임: Python 3.8 이상
   - 메모리: 최소 256MB (권장 512MB)
   - 타임아웃: 최소 1분 (전체 분석의 경우 3-5분 권장)

2. **환경 변수 설정**:

   - `AWS_REGION`: 기본 AWS 리전
   - `AWS_REGIONS`: 분석할 모든 리전 (쉼표로 구분)
   - `S3_BUCKET_NAME`: 결과를 저장할 S3 버킷 이름
   - `SQS_QUEUE_URL`: SQS 큐 URL
   - `SLACK_WEBHOOK_URL`: Slack 웹훅 URL
   - `SLACK_BOT_TOKEN`: Slack Bot 토큰
   - `SLACK_SIGNING_SECRET`: Slack 앱 서명 비밀키

3. **필요한 IAM 권한**:
   - EC2 권한 (볼륨 조회, 스냅샷 생성, 볼륨 수정 등)
   - CloudWatch 권한 (메트릭 조회)
   - S3 권한 (결과 저장)
   - SQS 권한 (메시지 처리)
   - SSM 권한 (인스턴스 정보 수집)

### 2. 트리거 설정

통합 Lambda 함수는 다음 세 가지 트리거로 호출될 수 있습니다:

1. **API Gateway**:

   - Slack 요청 처리용
   - HTTP API 또는 REST API 설정
   - 경로: `/slack` (또는 원하는 경로)
   - 메서드: POST
   - 인증: 없음 (Slack 서명으로 검증)

2. **EventBridge 규칙**:

   - 정기적인 분석 실행용
   - 스케줄 표현식 예: `cron(0 0 * * ? *)` (매일 자정 실행)
   - 이벤트 패턴: 없음 또는 커스텀 패턴

3. **SQS 대기열**:
   - 액션 실행 요청 처리용
   - 배치 크기: 1-10 (권장 5)
   - 부분 배치 실패 허용

## 사용 방법

### 1. Slack 슬래시 명령어

Slack에서 다음 명령어를 사용하여 시스템과 상호작용할 수 있습니다:

#### 모든 EBS 볼륨 분석:

```
/ebs-optimize analyze
```

#### 특정 볼륨 분석:

```
/ebs-optimize analyze vol-1234abcd [--region=ap-northeast-2] [--detailed]
```

#### 특정 볼륨에 조치 실행:

```
/ebs-optimize execute vol-1234abcd snapshot_and_delete [--region=ap-northeast-2]
```

#### 도움말 표시:

```
/ebs-optimize help
```

### 2. AWS CLI를 통한 직접 호출

**분석 요청 직접 호출**:

```bash
# 모든 볼륨 분석
aws lambda invoke --function-name ebs-storage-optimizer --payload '{}' output.json

# 특정 볼륨 분석
aws lambda invoke --function-name ebs-storage-optimizer --payload '{"volume_id":"vol-1234abcd", "region":"ap-northeast-2"}' output.json
```

**특정 액션 실행 요청**:

```bash
# SQS를 통한 액션 요청 추가
aws sqs send-message --queue-url $SQS_QUEUE_URL --message-body '{"action_type":"execute","parameters":{"volume_id":"vol-1234abcd","region":"ap-northeast-2","action_type":"snapshot_and_delete"}}'
```

### 3. EventBridge를 통한 정기 분석

EventBridge 콘솔에서 다음 설정으로 규칙을 생성합니다:

1. **일정 유형**: 스케줄 표현식 또는 율 표현식
2. **스케줄 표현식**: 원하는 일정 설정 (예: `cron(0 0 * * ? *)`)
3. **대상 유형**: AWS 서비스
4. **대상 선택**: Lambda 함수
5. **함수**: 통합 Lambda 함수 이름
6. **페이로드 구성** (선택 사항):
   ```json
   {
     "output_format": "slack",
     "detailed_report": true
   }
   ```

## 이벤트 페이로드 형식

### 1. 분석 요청 페이로드

```json
{
  "volume_id": "vol-1234abcd", // 선택적, 특정 볼륨 분석
  "region": "ap-northeast-2", // 선택적, 특정 리전
  "output_format": "slack", // 선택적, "slack", "json", "both"
  "detailed_report": true, // 선택적, 상세 보고서 여부
  "channel_id": "C01234ABCDE" // 선택적, Slack 채널 ID
}
```

### 2. Slack 요청 페이로드

Slack API Gateway 요청은 통합 Lambda가 자동으로 처리하므로 수동으로 구성할 필요가 없습니다.

### 3. SQS 액션 실행 메시지 형식

```json
{
  "action_type": "execute",
  "parameters": {
    "volume_id": "vol-1234abcd",
    "region": "ap-northeast-2",
    "action_type": "snapshot_and_delete" // 또는 "snapshot_only", "change_type", "resize"
  },
  "requested_by": "U01234ABCDE", // Slack 사용자 ID
  "channel_id": "C01234ABCDE" // 응답을 보낼 Slack 채널 ID
}
```

## 응답 형식

### 1. 분석 요청 응답

**성공 응답**:

```json
{
  "statusCode": 200,
  "body": {
    "message": "EBS 볼륨 최적화 분석이 성공적으로 완료되었습니다.",
    "result_location": "s3://ebs-optimizer-results/analysis-results-2023-03-01-12-00-00.json",
    "summary": {
      "total_idle_volumes": 10,
      "total_overprovisioned_volumes": 5,
      "total_estimated_savings": 123.45
    }
  }
}
```

**오류 응답**:

```json
{
  "statusCode": 500,
  "body": {
    "message": "EBS 볼륨 분석 중 오류가 발생했습니다.",
    "error": "오류 메시지"
  }
}
```

### 2. 액션 실행 응답

**성공 응답**:

```json
{
  "statusCode": 200,
  "body": {
    "processed": 1,
    "succeeded": 1,
    "failed": 0,
    "details": [
      {
        "message_id": "19dd0b57-b21e-4ac1-bd88-01bbb068cb78",
        "action_type": "execute",
        "result": {
          "success": true,
          "message": "볼륨 vol-1234abcd에 대한 snapshot_and_delete 액션이 완료되었습니다.",
          "result": {
            "snapshot_id": "snap-0abcdef1234567890",
            "deleted": true
          }
        }
      }
    ]
  }
}
```

## 주요 액션 유형

1. **유휴 볼륨 액션**:

   - `snapshot_and_delete`: 스냅샷 생성 후 볼륨 삭제 (_EC2 인스턴스에 연결되지 않은 유휴 볼륨에만 해당_)
   - `snapshot_only`: 스냅샷만 생성 (_모든 유휴 볼륨에 해당, 특히 EC2 인스턴스에 연결된 유휴 볼륨의 기본 옵션_)
   - `change_type`: 볼륨 타입 변경 (io1/io2 → gp3)

2. **과대 프로비저닝 볼륨 액션**:
   - `resize`: 볼륨 크기 조정
   - `change_type_and_resize`: 볼륨 타입 변경 및 크기 조정

## 트러블슈팅

### 1. 일반적인 문제

- **Lambda 타임아웃**: 전체 리전 분석 시 타임아웃이 발생할 수 있습니다. Lambda의 타임아웃을 늘리거나 리전별로 분리하여 실행하세요.
- **메모리 부족**: 분석 결과가 크면 메모리 부족 오류가 발생할 수 있습니다. Lambda의 메모리 할당을 늘리세요.
- **권한 오류**: Lambda 실행 역할에 필요한 권한이 없을 수 있습니다. IAM 정책을 확인하세요.

### 2. Slack 관련 문제

- **서명 검증 실패**: Slack 서명 비밀키가 올바르게 설정되었는지 확인하세요.
- **3초 타임아웃**: Slack은 3초 이내에 응답을 요구합니다. 오래 걸리는 작업은 SQS로 위임하고 즉시 응답하세요.

### 3. CloudWatch Logs 확인

문제 해결을 위해 CloudWatch Logs를 확인하세요. 로그 그룹 이름은 보통 `/aws/lambda/{lambda-function-name}`입니다.

## 보안 고려사항

1. **Slack 토큰 관리**: Slack 토큰과 서명 비밀키는 환경 변수로 안전하게 관리하세요.
2. **IAM 권한 최소화**: Lambda 함수에 필요한 최소 권한만 부여하세요.
3. **요청 검증**: 모든 Slack 요청은 서명을 검증하여 처리하세요.

## 모니터링 및 알림

1. **CloudWatch Metrics**: Lambda 함수의 호출 수, 오류, 지연 시간을 모니터링하세요.
2. **CloudWatch Alarms**: 오류 발생 시 알림을 설정하세요.
3. **X-Ray 추적**: 복잡한 문제 해결을 위해 X-Ray 추적을 활성화하세요.

## 변경 내용 정리

이 사용 설명서는 다음과 같은 주요 정보를 제공합니다:

1. 통합 Lambda 함수의 개요 및 아키텍처
2. Lambda 함수, 트리거, 환경 변수 설정 방법
3. Slack 명령어, AWS CLI, EventBridge를 통한 Lambda 함수 사용 방법
4. 다양한 이벤트 유형 및 응답 형식 설명
5. 지원되는 액션 유형 목록
6. 일반적인 문제와 트러블슈팅 방법
7. 보안 및 모니터링 관련 권장사항

이 문서는 사용자들이 통합된 Lambda 함수를 효과적으로 사용하고 문제를 해결하는 데 도움이 될 것입니다.
