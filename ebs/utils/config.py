"""
EBS 볼륨 최적화를 위한 설정

EBS 볼륨 분석 및 최적화에 사용되는 설정값들입니다.
"""

import os

# AWS 설정
REGIONS = ['us-east-1', 'ap-northeast-2']  # 분석할 AWS 리전 목록
S3_BUCKET_NAME = 'ebs-optimizer-results'    # 결과를 저장할 S3 버킷 이름

# CloudWatch 메트릭 수집 설정
METRIC_PERIOD = 86400  # 일일 데이터 (초 단위)

# 유휴 볼륨 감지 기준
IDLE_VOLUME_CRITERIA = {
    'days_to_check': 7,                   # 감지 기간 (일)
    'idle_time_threshold': 95,            # 유휴 시간 임계값 (%)
    'io_ops_threshold': 10,               # 일 평균 IO 작업 수 임계값
    'throughput_threshold': 5 * 1024 * 1024,  # 일 평균 데이터 처리량 임계값 (5MB)
    'burst_balance_threshold': 90,        # 버스트 밸런스 임계값 (%)
    'detached_days_threshold': 7          # 분리 상태로 유지된 일 수 임계값
}

# 과대 프로비저닝 볼륨 감지 기준
OVERPROVISIONED_CRITERIA = {
    'months_to_check': 1,                 # 감지 기간 (월) - 6개월에서 1개월로 완화
    'disk_used_percent_threshold': 20,    # 디스크 사용률 임계값 (%)
    'min_size_gb': 100                    # 최소 볼륨 크기 (GB) - 작은 볼륨은 제외할 수 있음
}

# 비용 계산을 위한 리전별 EBS 가격 (USD/GB/월)
EBS_PRICING = {
    'us-east-1': {
        'gp2': 0.10,
        'gp3': 0.08,
        'io1': 0.125,
        'io2': 0.125,
        'st1': 0.045,
        'sc1': 0.025,
        'standard': 0.05
    },
    'ap-northeast-2': {
        'gp2': 0.114,
        'gp3': 0.0912,
        'io1': 0.138,
        'io2': 0.138,
        'st1': 0.051,
        'sc1': 0.028,
        'standard': 0.08
    }
}

# 디버깅 관련 설정
DEBUG_MODE = False  # 디버그 모드 활성화
LOG_LEVEL = 'INFO'  # 로깅 레벨