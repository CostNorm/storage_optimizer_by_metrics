# EBS 스토리지 최적화 툴

AWS Elastic Block Store(EBS) 볼륨의 사용 패턴을 분석하여 비용 최적화를 위한 추천 사항을 제공하고 실행하는 툴입니다.

## 기능

이 툴은 다음과 같은 기능을 제공합니다:

1. **유휴 상태 볼륨 감지 (시나리오 2)**

   - `VolumeIdleTime` 95% 이상
   - 일 평균 I/O 작업 수 10 미만 (현재 분석에서 제외)
   - 일 평균 데이터 처리량 5MB 미만 (현재 분석에서 제외)
   - `BurstBalance` 과도하게 높은 경우

2. **과대 프로비저닝된 볼륨 감지 (시나리오 3)**

   - 6개월 이상 파일시스템 사용률 20% 미만 유지

3. **권장 조치 실행 (신규 기능)**
   - 유휴 볼륨: 스냅샷 생성 후 볼륨 삭제
   - 유휴 볼륨: 스냅샷만 생성
   - 유휴 볼륨: 볼륨 타입 변경 (io1/io2 -> gp3)
   - 과대 프로비저닝된 볼륨: 볼륨 크기 조정
   - 안전한 EBS 볼륨 Detach/Attach 작업 지원

## 프로젝트 구조

## 설치 요구사항

- Python 3.8 이상
- AWS 계정 및 적절한 IAM 권한
- boto3 라이브러리
- CloudWatch 에이전트 (인스턴스 내부 파일시스템 사용량 수집용)

## 설정

1. `config.py` 파일에서 다음 설정을 변경하세요:
   - `REGIONS`: 분석할 AWS 리전 목록
   - `S3_BUCKET_NAME`: 분석 결과를 저장할 S3 버킷 이름
   - 필요에 따라 유휴 볼륨 및 과대 프로비저닝 감지 기준 조정

## IAM 권한

이 툴을 실행하기 위해 필요한 IAM 권한은 아래와 같습니다. Lambda 실행 역할에 이 권한을 부여해야 합니다.

### 실행 정책 (Execution Policy)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeVolumes",
        "ec2:DescribeVolumeAttribute",
        "ec2:DescribeVolumeStatus",
        "ec2:DescribeInstances",
        "ec2:DescribeTags"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "cloudwatch:GetMetricStatistics",
        "cloudwatch:GetMetricData",
        "cloudwatch:ListMetrics"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": ["cloudtrail:LookupEvents"],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "ssm:SendCommand",
        "ssm:GetCommandInvocation",
        "ssm:DescribeInstanceInformation"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::${S3_BUCKET_NAME}",
        "arn:aws:s3:::${S3_BUCKET_NAME}/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:*:*:*"
    }
  ]
}
```

### 권한 설명

- **EC2 권한**: EBS 볼륨 및 인스턴스 정보를 조회합니다.
- **CloudWatch 권한**: 볼륨 성능 및 사용 지표를 수집합니다.
- **CloudTrail 권한**: 볼륨 연결/분리 이벤트 이력을 조회합니다.
- **SSM 권한**: EC2 인스턴스의 파일시스템 정보를 원격으로 수집합니다.
- **S3 권한**: 분석 결과를 S3 버킷에 저장합니다.
- **CloudWatch Logs 권한**: Lambda 로그를 기록합니다.

## EC2 인스턴스 요구사항

과대 프로비저닝 볼륨 감지를 위해서는 각 EC2 인스턴스에 다음이 필요합니다:

1. SSM Agent 설치 및 실행
2. CloudWatch 에이전트 설치 및 구성 (디스크 사용률 지표 수집)
3. `AmazonSSMManagedInstanceCore` 관리형 정책이 연결된 IAM 역할

## 실행 방법

이 코드는 AWS Lambda에서 실행되도록 설계되었습니다:

1. 코드를 Lambda 함수로 배포합니다.
2. 필요한 IAM 권한을 Lambda 실행 역할에 부여합니다.
3. 정기적인 실행을 위해 EventBridge 스케줄러로 트리거합니다.

## 결과 해석

분석 결과는 S3 버킷에 JSON 형식으로 저장됩니다:

- `idle_volumes`: 유휴 상태로 감지된 볼륨 목록
- `overprovisioned_volumes`: 과대 프로비저닝된 것으로 감지된 볼륨 목록

각 볼륨에 대해 현재 상태, 비용, 권장사항이 포함됩니다.

## 주의사항

- EBS 볼륨을 수정하거나 삭제하기 전에 항상 스냅샷을 생성하세요.
- 중요 시스템의 볼륨을 변경할 때는 충분한 테스트를 수행하세요.
- CloudWatch 에이전트 설치 및 설정이 필요합니다(과대 프로비저닝 감지).
