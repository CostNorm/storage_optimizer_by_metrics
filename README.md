# EBS Storage Optimizer by Metrics

AWS EBS 볼륨 사용 패턴을 분석하여 비용 최적화를 위한 추천 사항을 제공하고 자동화 조치를 실행하는 시스템입니다.

## 주요 기능

- **유휴 볼륨 감지**: CloudWatch 메트릭을 사용하여 사용되지 않는 EBS 볼륨 식별
- **과대 프로비저닝 볼륨 감지**: 장기간 낮은 사용률을 보이는 볼륨 식별
- **자동화된 권장 조치**: Slack 인터페이스를 통해 직접 조치 실행 가능
  - 스냅샷 생성 후 볼륨 삭제
  - 볼륨 타입 변경 (io1/io2 → gp3)
  - 볼륨 크기 조정
  - 스냅샷만 생성

## 시스템 아키텍처

![아키텍처 다이어그램](docs/architecture_diagram.png)

시스템은 하나의 통합 Lambda 함수로 구성되어 있으며, 이벤트 유형에 따라 세 가지 주요 기능을 수행합니다:

1. **분석 및 알림 기능**: 볼륨 사용 패턴 분석 및 Slack 알림
2. **요청 처리 기능**: Slack 요청 처리 및 직접 작업 수행
3. **액션 실행 기능**: SQS 큐에서 작업을 가져와 실제 EBS 볼륨 변경 실행

통합 Lambda 함수는 이벤트 소스(API Gateway, CloudWatch Events, SQS)에 따라 적절한 기능을 분기하여 실행합니다.

자세한 아키텍처는 [ARCHITECTURE.md](docs/ARCHITECTURE.md)를 참조하세요.

## 설정 방법

### 사전 요구사항

- AWS 계정 및 적절한 IAM 권한
- Slack 워크스페이스 관리자 권한
- Python 3.8 이상

### 1. AWS 리소스 설정

#### S3 버킷 생성

```bash
aws s3 mb s3://ebs-optimizer-results
```

#### SQS 대기열 생성

```bash
aws sqs create-queue --queue-name ebs-optimizer-actions
```

#### Lambda 함수 배포 정책 설정

[lambda_execution_policy.json](ebs/lambda_execution_policy.json) 파일을 참조하여 필요한 IAM 정책을 설정합니다.

### 2. Slack 앱 설정

1. [Slack API 사이트](https://api.slack.com/apps)에서 새로운 앱 생성
2. **Incoming Webhooks** 기능 활성화 및 웹훅 URL 생성
3. **Slash Commands** 설정:
   - 명령어: `/ebs-optimize`
   - 요청 URL: API Gateway URL (Lambda 함수 배포 후 확인)
   - 설명: "EBS 볼륨 최적화 도구"
4. **Interactivity & Shortcuts** 활성화:
   - 요청 URL: API Gateway URL (Lambda 함수 배포 후 확인)

### 3. 환경 변수 설정

`.env` 파일 생성 (`.env.template`을 복사하여 사용):

```bash
cp config/.env.template config/.env
```

이후 다음 설정을 적절히 변경하세요:

- `AWS_REGION`: 기본 AWS 리전
- `AWS_REGIONS`: 분석할 모든 리전 (쉼표로 구분)
- `S3_BUCKET_NAME`: 결과를 저장할 S3 버킷 이름
- `SQS_QUEUE_URL`: SQS 큐 URL
- `SLACK_WEBHOOK_URL`: Slack 웹훅 URL
- `SLACK_BOT_TOKEN`: Slack Bot 토큰
- `SLACK_SIGNING_SECRET`: Slack 앱 서명 비밀키
- 기타 분석 기준 파라미터

### 4. 통합 Lambda 함수 배포

통합 Lambda 함수를 AWS Lambda 콘솔 또는 AWS CLI를 통해 배포합니다:

- **통합 Lambda 함수**:
  - 소스 코드: `lambdas/consolidated_lambda.py`
  - 핸들러: `lambdas.consolidated_lambda.lambda_handler`
  - 트리거:
    1. CloudWatch Events 스케줄 (분석 기능용, 예: 일 1회)
    2. API Gateway (Slack 요청 처리용)
    3. SQS (액션 실행용)
  - 환경 변수: 위에서 설정한 변수들
  - 메모리: 최소 256MB (권장 512MB)
  - 타임아웃: 최소 1분 (전체 분석의 경우 3-5분 권장)

## 사용 방법

### Slack 슬래시 커맨드

모든 EBS 볼륨 분석:

```
/ebs-optimize analyze
```

특정 볼륨 분석:

```
/ebs-optimize analyze vol-1234abcd
```

특정 볼륨에 조치 실행:

```
/ebs-optimize execute vol-1234abcd snapshot_and_delete
```

사용 가능한 조치 목록:

- `snapshot_and_delete`: 스냅샷 생성 후 볼륨 삭제
- `snapshot_only`: 스냅샷만 생성
- `change_type`: 볼륨 타입 변경 (io1/io2 → gp3)
- `resize`: 볼륨 크기 조정

### Lambda 직접 호출

**분석 기능 직접 호출**:

```bash
# 모든 볼륨 분석
aws lambda invoke --function-name ebs-storage-optimizer --payload '{}' output.json

# 특정 볼륨 분석
aws lambda invoke --function-name ebs-storage-optimizer --payload '{"volume_id":"vol-1234abcd"}' output.json
```

**액션 실행 기능 직접 호출** (권장하지 않음, SQS를 통해 호출하는 것이 안전):

```bash
aws lambda invoke --function-name ebs-storage-optimizer --payload '{
  "Records": [{
    "body": "{\"action_type\":\"execute\",\"parameters\":{\"volume_id\":\"vol-1234abcd\",\"action_type\":\"snapshot_and_delete\"}}"
  }]
}' output.json
```

## 결과 해석

분석 결과는 다음 방법으로 확인할 수 있습니다:

1. **S3 버킷**: 자세한 분석 결과는 S3 버킷에 JSON 형식으로 저장됨
2. **Slack 알림**: 요약 결과 및 권장 조치가 포함된 메시지 전송

결과에는 다음 정보가 포함됩니다:

- 유휴 볼륨 목록 및 근거
- 과대 프로비저닝된 볼륨 목록 및 근거
- 각 볼륨에 대한 권장 조치
- 예상 월간 비용 절감액

## 구성 요소 설명

### 분석 모듈

- `ebs/ebs_analyzer.py`: 메인 분석 로직
- `ebs/idle_detector.py`: 유휴 볼륨 감지 로직
- `ebs/overprovisioned_detector.py`: 과대 프로비저닝 볼륨 감지 로직

### 통합 Lambda 함수

- `lambdas/consolidated_lambda.py`: 모든 기능을 통합한 Lambda 핸들러
  - 이벤트 타입에 따라 분석, Slack 요청 처리, 액션 실행 등 다양한 기능 수행

### 통합 모듈

- `integrations/slack/slack_verifier.py`: Slack 요청 검증
- `integrations/slack/slack_messenger.py`: Slack 메시지 전송
- `utils/sqs_helper.py`: SQS 큐 작업 처리

## 주의사항

- 프로덕션 환경에서는 충분한 테스트 후 사용하세요.
- EBS 볼륨 수정/삭제 전 항상 자동으로 스냅샷을 생성하지만, 중요 데이터에는 추가 백업을 권장합니다.
- 과대 프로비저닝 분석을 위해서는 CloudWatch 에이전트가 설치되어 디스크 사용률 메트릭을 수집해야 합니다.

## 로깅 및 모니터링

Lambda 함수는 CloudWatch Logs에 로그를 기록합니다:

- 정보 로그: 정상적인 작업 흐름
- 경고 로그: 비정상적이지만 치명적이지 않은 상황
- 오류 로그: 조치가 필요한 문제

오류 발생 시 자동으로 Slack으로도 알림이 전송됩니다.

## 개발 및 확장

이 프로젝트는 다음과 같은 방향으로 확장할 수 있습니다:

1. 추가 스토리지 유형(EFS, S3 등) 지원
2. 지능형 분석 알고리즘 (머신러닝 기반)
3. 다른 메시징 플랫폼 통합 (MS Teams, Discord 등)
4. 더 많은 자동화 작업 추가

## 라이센스

이 프로젝트는 내부 사용 목적으로 개발되었습니다.

## 기여하기

버그 신고, 기능 요청 및 풀 리퀘스트를 환영합니다.
