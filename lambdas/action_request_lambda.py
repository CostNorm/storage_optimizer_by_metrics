import os
import json
import logging
import sys
import time
import hmac
import hashlib
import base64
from pathlib import Path
from urllib.parse import parse_qs

# 루트 디렉토리를 Python 경로에 추가
root_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(root_dir))

from dotenv import load_dotenv
from utils.sqs_helper import enqueue_action

# 환경 변수 로드
load_dotenv()

# 로깅 설정
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 환경 변수에서 필요한 값 가져오기
SLACK_SIGNING_SECRET = os.environ.get('SLACK_SIGNING_SECRET')
SQS_QUEUE_URL = os.environ.get('SQS_QUEUE_URL')
SLACK_VERIFICATION_TOKEN = os.environ.get('SLACK_VERIFICATION_TOKEN')  # 이전 방식(선택적)

def lambda_handler(event, context):
    """
    Slack으로부터 이벤트를 수신하고 처리하는 Lambda 함수
    - Slack 요청을 검증하고 SQS Queue로 전달
    - 즉시 200 OK 응답 반환
    
    :param event: API Gateway로부터 전달된 이벤트 객체
    :param context: Lambda 컨텍스트
    :return: Slack에 대한 응답
    """
    logger.info("Slack 이벤트 수신")
    
    try:
        # API Gateway를 통해 전달된 이벤트 처리
        if not event.get('body'):
            logger.error("이벤트 본문이 없습니다.")
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "No event body"})
            }

        # Slack 요청 검증
        if not validate_slack_request(event):
            logger.error("Slack 요청 검증 실패")
            return {
                "statusCode": 401,
                "body": json.dumps({"error": "Invalid request signature"})
            }
        
        # 본문 파싱
        body = event['body']
        if event.get('isBase64Encoded', False):
            body = base64.b64decode(body).decode('utf-8')
        
        # SQS 메시지 준비
        action_data = {
            "event_type": "slack_interaction",
            "timestamp": int(time.time()),
            "raw_event": event,
            "parsed_body": body
        }
        
        # 이벤트 유형에 따른 기본 분석
        if "payload" in body:
            # 인터랙티브 컴포넌트(버튼 클릭 등)
            payload = json.loads(parse_qs(body)['payload'][0])
            action_data["interaction_type"] = "interactive_component"
            action_data["payload"] = payload
            
            # 버튼 액션 등을 식별하여 ActionType 설정
            if payload.get('type') == 'block_actions' and payload.get('actions'):
                action_id = payload['actions'][0].get('action_id', '')
                if action_id.startswith('execute_'):
                    action_type = action_id.replace('execute_', '')
                    action_data["action_type"] = action_type
                    action_data["ActionType"] = action_type  # 일관성을 위해 두 필드 모두 설정
                    
                    # 버튼 값에서 파라미터 추출
                    try:
                        button_value = payload['actions'][0].get('value', '{}')
                        parameters = json.loads(button_value)
                        action_data["parameters"] = parameters
                        
                        # 로깅
                        logger.info(f"버튼 액션 파라미터: {parameters}, 액션 타입: {action_type}")
                    except Exception as e:
                        logger.error(f"버튼 값 파싱 오류: {e}")
                        action_data["parameters"] = {}
                
                # 사용자 및 채널 정보 추가
                action_data["requested_by"] = payload.get('user', {}).get('id', 'unknown')
                action_data["channel_id"] = payload.get('channel', {}).get('id')
                action_data["response_url"] = payload.get('response_url')
        
        elif "command" in parse_qs(body):
            # 슬래시 커맨드
            params = parse_qs(body)
            command = params.get('command', [''])[0]
            text = params.get('text', [''])[0]
            
            action_data["interaction_type"] = "slash_command"
            action_data["command"] = command
            action_data["text"] = text
            action_data["action_type"] = "slash_command"  # 새로 추가
            action_data["ActionType"] = "slash_command"  # 기존 필드 유지
            
            # 사용자 및 채널 정보 추가
            action_data["requested_by"] = params.get('user_id', ['unknown'])[0]
            action_data["channel_id"] = params.get('channel_id', [''])[0]
            action_data["response_url"] = params.get('response_url', [''])[0]
            
            # 명령어 파싱 (이 부분은 consolidated_lambda에서 처리)
        
        # SQS에 메시지 전송
        logger.info(f"SQS에 전송할 액션 데이터: {action_data}")
        enqueue_result = enqueue_action(action_data)
        
        if not enqueue_result:
            logger.warning("SQS 대기열에 메시지 추가 실패")
            # 실패해도 Slack에게는 200 응답 (중복 요청 방지)
        
        # Slack에 즉시 응답 (3초 타임아웃 방지)
        return {
            "statusCode": 200,
            "body": json.dumps({
                "response_type": "ephemeral",
                "text": "요청이 접수되었습니다. 처리 중입니다..."
            })
        }
    
    except Exception as e:
        logger.error(f"이벤트 처리 중 오류 발생: {str(e)}", exc_info=True)
        # 오류가 발생해도 Slack에는 200으로 응답 (중복 요청 방지)
        return {
            "statusCode": 200,
            "body": json.dumps({
                "response_type": "ephemeral",
                "text": "요청 처리 중 오류가 발생했습니다. 관리자에게 문의하세요."
            })
        }

def validate_slack_request(event):
    """
    Slack 요청 서명을 검증합니다.
    
    :param event: API Gateway 이벤트
    :return: 검증 성공 여부
    """
    headers = event.get("headers", {})
    slack_signature = headers.get('x-slack-signature', '')
    slack_request_timestamp = headers.get('x-slack-request-timestamp', '')
    
    if not slack_signature or not slack_request_timestamp:
        logger.error("Missing Slack signature or timestamp in headers")
        return False
    
    # 타임스탬프 검증 (요청이 5분 이내인지 확인)
    current_ts = int(time.time())
    try:
        req_ts = int(slack_request_timestamp)
    except ValueError:
        logger.error("Invalid timestamp format")
        return False
    
    if abs(current_ts - req_ts) > 300:  # 5분 = 300초
        logger.error("Request timestamp is out of the allowed range")
        return False
    
    # 요청 본문(body) 추출 및 디코딩
    body = event.get("body", "")
    if event.get("isBase64Encoded", False):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except Exception as e:
            logger.error(f"Error decoding base64 body: {e}")
            return False
    
    # Slack 서명 검증을 위한 base string 생성
    sig_basestring = f"v0:{slack_request_timestamp}:{body}"
    
    # HMAC-SHA256 방식으로 해시 계산
    if SLACK_SIGNING_SECRET:
        computed_signature = "v0=" + hmac.new(
            SLACK_SIGNING_SECRET.encode("utf-8"),
            sig_basestring.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        
        # 서명 비교 (상수 시간 비교로 타이밍 공격 방지)
        if not hmac.compare_digest(computed_signature, slack_signature):
            logger.error("Invalid Slack signature")
            return False
    else:
        # 서명 검증을 위한 시크릿 키가 없을 경우 로깅
        logger.warning("SLACK_SIGNING_SECRET is not set, signature validation skipped")
        # 개발 환경에서는 검증을 건너뛸 수 있지만, 프로덕션에서는 항상 검증해야 함
        if os.environ.get('DEBUG') != 'True':
            return False
    
    return True