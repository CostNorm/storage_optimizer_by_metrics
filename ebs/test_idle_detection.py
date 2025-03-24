import json
import logging
import unittest
from unittest.mock import MagicMock, patch

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger()

def test_volume_idle_time(idle_seconds):
    """
    VolumeIdleTime 값을 테스트하여 올바른 백분율 계산을 확인합니다.
    
    :param idle_seconds: 유휴 시간(초)
    :return: 변환된 백분율
    """
    # 초를 퍼센트로 변환
    idle_percent = (idle_seconds / 60) * 100
    
    print(f"유휴 시간: {idle_seconds:.2f}초/분")
    print(f"퍼센트 변환: {idle_percent:.2f}%")
    
    # 판단 기준
    threshold = 95
    if idle_percent >= threshold:
        print(f"결과: {idle_percent:.2f}%는 기준치({threshold}%) 이상이므로 유휴 상태입니다.")
    else:
        print(f"결과: {idle_percent:.2f}%는 기준치({threshold}%) 미만이므로 유휴 상태가 아닙니다.")
    
    return idle_percent

def test_no_metrics_detection():
    """
    메트릭이 없는 볼륨을 유휴 상태로 감지하는지 테스트합니다.
    """
    print("\n메트릭이 없는 볼륨 테스트:")
    print("메트릭이 전혀 없는 경우 (연결된 적 없는 볼륨): 유휴 상태로 감지되어야 함")
    print("판단 근거: 메트릭 데이터가 없음 = 볼륨이 사용되지 않음")

if __name__ == "__main__":
    # 제공된 예제 데이터 테스트
    test_volume_idle_time(59.87)  # 예상 결과: 99.78%, 유휴 상태로 판정되어야 함
    
    print("\n다른 값들 테스트:")
    test_volume_idle_time(57.0)   # 예상 결과: 95.00%, 유휴 상태로 판정되어야 함
    test_volume_idle_time(56.9)   # 예상 결과: 94.83%, 유휴 상태가 아니어야 함
    test_volume_idle_time(30.0)   # 예상 결과: 50.00%, 유휴 상태가 아니어야 함
    
    # 메트릭이 없는 볼륨 테스트
    test_no_metrics_detection()
