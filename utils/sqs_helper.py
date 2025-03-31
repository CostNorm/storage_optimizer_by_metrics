import boto3
import json
import logging
import os
import uuid
from dotenv import load_dotenv

# 환경 변수 로드
load_dotenv()

# 로깅 설정
logger = logging.getLogger()

def enqueue_action(action_data):
    """
    액션 요청을 SQS 큐에 추가합니다.
    
    :param action_data: 액션 데이터
    :return: SQS 응답 또는 False
    """
    SQS_QUEUE_URL = os.environ.get('SQS_QUEUE_URL')
    
    if not SQS_QUEUE_URL:
        logger.error("SQS_QUEUE_URL이 설정되지 않았습니다.")
        return False
    
    # FIFO 큐 사용 여부 확인 (URL이 .fifo로 끝나는지 확인)
    is_fifo = SQS_QUEUE_URL.endswith('.fifo')
    
    try:
        # SQS 클라이언트 생성
        sqs_client = boto3.client('sqs')
        
        message_params = {
            'QueueUrl': SQS_QUEUE_URL,
            'MessageBody': json.dumps(action_data)
        }
        
        # FIFO 큐인 경우 필수 파라미터 추가
        if is_fifo:
            # 고유한 MessageDeduplicationId 생성
            message_params['MessageDeduplicationId'] = str(uuid.uuid4())
            
            # MessageGroupId 설정 (단일 그룹을 사용하거나 action_type에 따라 그룹화)
            group_id = action_data.get('action_type', 'default')
            message_params['MessageGroupId'] = group_id
            logger.info(f"FIFO 큐 사용: MessageGroupId={group_id}")
        
        # SQS에 메시지 전송
        response = sqs_client.send_message(**message_params)
        logger.info(f"액션 요청이 SQS 큐에 추가되었습니다: {response.get('MessageId')}")
        return response
        
    except Exception as e:
        logger.error(f"SQS에 액션 요청 추가 중 오류 발생: {str(e)}")
        return False

def get_action_status(message_id):
    """
    SQS 메시지의 상태를 확인합니다.
    
    :param message_id: SQS 메시지 ID
    :return: 메시지 상태 또는 None
    """
    # 이 기능은 현재 구현되지 않았습니다.
    # SQS는 메시지 상태를 직접적으로 조회하는 기능을 제공하지 않기 때문에
    # 별도의 DynamoDB 테이블 등을 사용하여 상태 추적이 필요합니다.
    logger.warning("메시지 상태 확인 기능은 현재 구현되지 않았습니다.")
    return None
