"""
EBS 볼륨 최적화를 위한 설정

EBS 볼륨 분석 및 최적화에 사용되는 설정값들입니다.
"""

import os
from dotenv import load_dotenv

# 환경 변수 로드
load_dotenv()

# 분석할 AWS 리전
REGIONS = os.environ.get('AWS_REGIONS', 'us-east-1,us-east-2').split(',')

# 결과를 저장할 S3 버킷
S3_BUCKET_NAME = os.environ.get('S3_BUCKET_NAME', 'ebs-optimizer-results')

# SQS 큐 URL
SQS_QUEUE_URL = os.environ.get('SQS_QUEUE_URL', '')

# 메트릭 수집 기간 (초)
METRIC_PERIOD = 3600  # 1시간

# 유휴 볼륨 감지 기준
IDLE_VOLUME_CRITERIA = {
    'volume_idle_time_threshold': 95,  # 유휴 시간 95% 이상
    'read_ops_threshold': 10,          # 읽기 작업 수 10 미만 (현재 사용 안함)
    'write_ops_threshold': 10,         # 쓰기 작업 수 10 미만 (현재 사용 안함)
    'total_bytes_threshold': 5242880,  # 총 처리량 5MB 미만 (현재 사용 안함)
    'burst_balance_threshold': 90      # 버스트 밸런스 90% 이상
}

# 과대 프로비저닝 볼륨 감지 기준
OVERPROVISIONED_CRITERIA = {
    'disk_usage_threshold': 20,        # 디스크 사용률 20% 미만
    'time_period_months': 6,           # 6개월 이상 지속
    'iops_usage_threshold': 20,        # IOPS 사용률 20% 미만
    'throughput_usage_threshold': 20,  # 처리량 사용률 20% 미만
    'buffer_percent': 20               # 변경 시 버퍼(여유) 비율 20%
}