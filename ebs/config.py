# 분석할 AWS 리전 목록
REGIONS = ['us-east-1', 'ap-northeast-2']  # 필요에 따라 리전 추가/수정

# 결과 저장용 S3 버킷 이름
S3_BUCKET_NAME = 'your-ebs-optimization-results-bucket'

# 유휴 볼륨 감지 기준 (시나리오 2)
IDLE_VOLUME_CRITERIA = {
    'days_to_check': 30,  # 최근 몇일 데이터를 확인할지
    'idle_time_threshold': 95,  # VolumeIdleTime 임계값 (%)
    'io_ops_threshold': 10,  # 일 평균 IO 작업 수 임계값
    'throughput_threshold': 5 * 1024 * 1024,  # 일 평균 처리량 임계값 (5MB)
    'burst_balance_threshold': 90  # BurstBalance 임계값 (%)
}

# 과대 프로비저닝 볼륨 감지 기준 (시나리오 3)
OVERPROVISIONED_CRITERIA = {
    'months_to_check': 6,  # 최근 몇개월 데이터를 확인할지
    'disk_used_percent_threshold': 20  # 디스크 사용률 임계값 (%)
}

# CloudWatch 지표 수집 주기 (초)
METRIC_PERIOD = 86400  # 1일
