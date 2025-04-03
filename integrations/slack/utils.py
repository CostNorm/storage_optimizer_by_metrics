import logging

logger = logging.getLogger()

def check_slack_retry_header(headers):
    """
    Slack 재시도 헤더를 확인하여 재시도 요청인지 판별합니다.
    
    :param headers: Lambda 이벤트의 헤더 딕셔너리
    :return: 재시도 요청이면 True, 아니면 False
    """
    if not headers:
        return False
        
    retry_num = headers.get('X-Slack-Retry-Num') or headers.get('x-slack-retry-num')
    retry_reason = headers.get('X-Slack-Retry-Reason') or headers.get('x-slack-retry-reason')
    
    if retry_num:
        logger.warning(f"Slack 재시도 감지: Retry-Num={retry_num}, Reason={retry_reason}")
        # 특정 이유 (예: http_timeout)에 따라 다르게 처리할 수도 있음
        return True
        
    return False

def format_analysis_result_for_slack(result_data):
    """
    분석 결과를 Slack 메시지 형식으로 간단히 변환합니다.
    (현재는 예시이며, 실제 필요에 따라 확장해야 함)
    """
    # TODO: slack_messenger의 _build_*_blocks 함수와 유사하게 포맷팅 로직 구현 필요
    # 임시로 간단한 텍스트 반환
    if isinstance(result_data, dict) and 'recommendation' in result_data:
        return f"분석 완료: {result_data.get('volume_id', 'N/A')}\n권장 사항: {result_data['recommendation']}"
    elif isinstance(result_data, list):
         return f"총 {len(result_data)}개 볼륨 분석 완료."
    else:
         return str(result_data)

# 여기에 format_single_volume_details 등 다른 유틸리티 함수 추가 가능 