# SQS 통합 아키텍처

## 개요

이 문서는 EBS Storage Optimizer 시스템에서의 SQS(Simple Queue Service) 통합에 대해 설명합니다. 이 아키텍처는 Slack 요청 처리와 실제 작업 실행을 분리하기 위해 SQS를 사용하며, 이를 통해 더 나은 확장성, 안정성 및 빠른 응답 시간을 제공합니다.

## SQS를 사용하는 이유

Slack API는 3초 이내에 응답이 필요하며, 그렇지 않으면 최대 3번까지 요청을 재시도합니다. 요청을 빠르게 검증하고 즉시 응답을 반환하는 게이트웨이 함수를 사용함으로써, 실제 작업은 비동기적으로 큐를 통해 처리하면서 이러한 재시도를 방지할 수 있습니다.

## 아키텍처 구성 요소

1. **API Gateway + Action Request Lambda**

   - Slack 요청 검증
   - SQS 큐에 메시지 추가
   - Slack에 즉시 응답 반환 (3초 이내)

2. **SQS 큐**

   - 액션 요청 저장
   - 순서대로 처리를 위한 FIFO(First-In-First-Out) 구성
   - 중복 처리 방지를 위한 중복 제거 기능 포함

3. **Action Executor Lambda**
   - SQS 큐의 메시지에 의해 트리거됨
   - 액션 요청 처리
   - 실제 작업 수행 (스냅샷 생성, 볼륨 유형 변경 등)
   - 응답 URL이나 웹훅을 통해 Slack으로 최종 결과 전송

## 메시지 흐름

1. Slack 사용자가 명령을 실행하거나 버튼을 클릭
2. API Gateway가 요청을 받아 Action Request Lambda로 전달
3. Action Request Lambda:
   - 요청 서명 검증
   - 요청 세부 정보를 SQS에 추가
   - Slack에 확인 응답 전송 (3초 이내)
4. SQS가 Action Executor Lambda를 트리거
5. Action Executor Lambda:
   - 요청 처리 (장시간 실행 작업)
   - AWS 리소스에 대한 요청된 작업 수행
   - 최종 결과를 Slack으로 전송

## SQS 구성

SQS 큐는 다음과 같은 속성으로 FIFO 큐로 구성됩니다:

- **MessageGroupId**: 관련 작업이 순서대로 처리되도록 액션 유형에 따라 설정
- **MessageDeduplicationId**: 중복 처리 방지를 위해 생성된 UUID
- **Visibility Timeout**: 예상되는 최대 처리 시간에 따라 설정
- **Retry Policy**: 처리 실패를 처리하도록 구성

## 재시도 및 실패 처리

1. **Slack 재시도**: `x-slack-retry-num` 헤더를 확인하여 감지하고 중복 처리 방지
2. **처리 실패**: 지속적인 실패에 대해 데드레터 큐(DLQ)와 함께 SQS의 재시도 메커니즘으로 처리
3. **타임아웃**: Lambda 함수의 역할에 맞는 적절한 타임아웃 값으로 구성

## 보안 고려사항

1. **서명 검증**: 모든 Slack 요청은 HMAC 서명을 사용하여 검증
2. **최소 권한**: IAM 역할은 최소 권한 원칙을 따름
3. **메시지 암호화**: SQS 메시지는 저장 시 암호화됨
4. **인증**: 모든 구성 요소는 AWS 서비스와 올바르게 인증

## 모니터링 및 로깅

1. **CloudWatch Metrics**: SQS 큐 길이, 가장 오래된 메시지 감시
2. **CloudWatch Logs**: Lambda 함수의 상세 로그
3. **Dead Letter Queue**: 실패한 메시지를 캡처하여 조사
4. **Alarms**: 큐 백로그, 오류율 등에 대한 알람 설정
