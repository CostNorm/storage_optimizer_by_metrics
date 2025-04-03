import logging
import os
import json

logger = logging.getLogger()

def calculate_monthly_cost(size_gb, volume_type, region):
    """
    EBS 볼륨의 월간 비용을 계산합니다.
    
    :param size_gb: 볼륨 크기 (GB)
    :param volume_type: 볼륨 유형 (gp2, gp3, io1, io2, st1, sc1, standard)
    :param region: AWS 리전
    :return: 월간 비용 (USD)
    """
    # 리전별 EBS 가격 (USD/GB-월)
    # 실제 프로덕션 환경에서는 AWS Price List API를 사용하여 동적으로 가격 가져오는 것을 권장
    prices = {
        'us-east-1': {
            'gp2': 0.10,
            'gp3': 0.08,
            'io1': 0.125,
            'io2': 0.125,
            'st1': 0.045,
            'sc1': 0.025,
            'standard': 0.05
        },
        'default': {
            'gp2': 0.10,
            'gp3': 0.08,
            'io1': 0.125,
            'io2': 0.125,
            'st1': 0.045,
            'sc1': 0.025,
            'standard': 0.05
        }
    }
    
    # 리전 가격이 없으면 기본 가격 사용
    region_prices = prices.get(region, prices['default'])
    
    # 볼륨 유형 가격이 없으면 gp2 가격 사용
    price_per_gb = region_prices.get(volume_type, region_prices['gp2'])
    
    # 월간 비용 계산
    monthly_cost = size_gb * price_per_gb
    
    # io1/io2의 경우 프로비저닝된 IOPS에 대한 추가 비용도 계산해야 함 (현재 구현에서는 생략)
    
    return monthly_cost

def get_tags_as_dict(tags_list):
    """
    AWS 리소스의 태그 리스트를 딕셔너리로 변환합니다.
    
    :param tags_list: AWS 리소스의 태그 리스트 [{'Key': 'Name', 'Value': 'value'}, ...]
    :return: 태그 딕셔너리 {'Name': 'value', ...}
    """
    if not tags_list:
        return {}
    
    return {tag['Key']: tag['Value'] for tag in tags_list}


def format_bytes(size_bytes):
    """
    바이트 값을 사람이 읽기 쉬운 형식으로 변환합니다.
    
    :param size_bytes: 바이트 단위의 크기
    :return: 변환된 문자열 (B, KB, MB, GB, TB)
    """
    if size_bytes < 0:
        raise ValueError("바이트 값은 음수일 수 없습니다.")
    
    power = 2**10  # 1024
    n = 0
    power_labels = {0: 'B', 1: 'KB', 2: 'MB', 3: 'GB', 4: 'TB'}
    
    while size_bytes >= power and n < 4:
        size_bytes /= power
        n += 1
    
    return f"{size_bytes:.2f} {power_labels[n]}"
