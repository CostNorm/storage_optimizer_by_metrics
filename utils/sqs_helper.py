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
            # ActionType이 있으면 그것을 사용하고, 없으면 action_type을 사용
            group_id = action_data.get('ActionType', action_data.get('action_type', 'default'))
            message_params['MessageGroupId'] = group_id
        
        # ActionType이 있을 경우 action_type으로도 추가 (일관성 유지)
        if 'ActionType' in action_data and 'action_type' not in action_data:
            action_data['action_type'] = action_data['ActionType']
        
        # MessageAttributes와 본문 모두 일관된 형식으로 설정
        if 'ActionType' in action_data:
            message_params['MessageAttributes'] = {
                'ActionType': {
                    'DataType': 'String',
                    'StringValue': action_data['ActionType']
                }
            }
        
        response = sqs_client.send_message(**message_params)
        
        logger.info(f"액션이 SQS 큐에 추가되었습니다: {response['MessageId']}")
        return response
    
    except Exception as e:
        logger.error(f"액션을 SQS 큐에 추가하는 중 오류 발생: {str(e)}", exc_info=True)
        return False
