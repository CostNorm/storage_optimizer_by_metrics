import os
import json
import logging
import sys
import time
import hmac
import hashlib
import base64
import boto3
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
SLACK_VERIFICATION_TOKEN = os.environ.get('SLACK_VERIFICATION_TOKEN')
LAMBDA_FUNCTION_NAME = os.environ.get('AWS_LAMBDA_FUNCTION_NAME')  # 현재 람다 함수 이름

# Lambda 클라이언트 초기화 (전역 변수로 재사용)
try:
    LAMBDA_CLIENT = boto3.client('lambda')
except Exception as e:
    logger.error(f"Lambda 클라이언트 초기화 중 오류 발생: {str(e)}")
    LAMBDA_CLIENT = None

def lambda_handler(event, context):
    """
    Slack으로부터 이벤트를 수신하고 처리하는 Lambda 함수
    - 즉시 응답을 반환하고, 자기 자신을 비동기적으로 호출하여 처리
    
    :param event: API Gateway로부터 전달된 이벤트 객체
    :param context: Lambda 컨텍스트
    :return: Slack에 대한 응답
    """
    # 디버깅 - 전체 이벤트 로깅
    logger.info(f"받은 이벤트 데이터: {json.dumps(event)}")
    
    # 헤더 정보 로깅
    headers = event.get("headers", {})
    logger.info(f"요청 헤더: {json.dumps(headers)}")
    
    # 재시도 헤더 확인
    is_retry, retry_count = check_retry_header(headers)
    if is_retry:
        logger.info(f"Slack 재시도 요청 감지: 재시도 횟수={retry_count}")
        
    # 비동기 처리를 위한 내부 호출 여부 확인
    is_async_processing = event.get('__async_processing', False)
    
    if is_async_processing:
        # 비동기 처리 모드 - 실제 처리 로직 실행
        return process_event_async(event)
    else:
        # 초기 호출 모드 - 즉시 응답 후 자기 자신을 비동기로 호출
        return handle_initial_request(event, context)

def check_retry_header(headers):
    """
    Slack의 재시도 요청 헤더를 확인합니다.
    
    :param headers: 요청 헤더
    :return: (재시도 여부, 재시도 횟수)
    """
    retry_count = None
    
    # 대소문자 구분 없이 재시도 헤더 확인
    for header_key in headers:
        if header_key.lower() == 'x-slack-retry-num':
            retry_count = headers[header_key]
            logger.info(f"Slack 재시도 헤더 감지: {header_key}={retry_count}")
            break
    
    # 헤더 값을 정수로 변환 시도
    if retry_count is not None:
        try:
            retry_count = int(retry_count)
        except ValueError:
            logger.warning(f"재시도 횟수를 정수로 변환할 수 없습니다: {retry_count}")
    
    return retry_count is not None, retry_count

def handle_initial_request(event, context):
    """
    초기 Slack 요청 처리 - 즉시 응답 후 비동기 처리
    
    :param event: API Gateway 이벤트
    :param context: Lambda 컨텍스트
    :return: Slack에 대한 응답
    """
    logger.info("Slack 초기 요청 수신 - 즉시 응답 모드")
    
    # 1. 이벤트 본문 확인 (빠른 검사)
    if not event.get('body'):
        logger.error("이벤트 본문이 없습니다.")
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "No event body"})
        }

    # 2. 간단한 Slack 요청 유효성 검증 (최소한의 검증만 수행)
    headers = event.get("headers", {})
    if not (headers.get('x-slack-signature') or headers.get('X-Slack-Signature')):
        logger.error("Slack 서명이 없습니다.")
        return {
            "statusCode": 401,
            "body": json.dumps({"error": "Missing Slack signature"})
        }
    
    # 3. 본문 디코딩 (필요한 경우)
    body = event['body']
    if event.get('isBase64Encoded', False):
        body = base64.b64decode(body).decode('utf-8')
    
    # 4. Slack에 즉시 응답 (3초 타임아웃 방지)
    response = {
        "statusCode": 200,
        "body": json.dumps({
            "response_type": "ephemeral",
            "text": "요청이 접수되었습니다. 처리 중입니다..."
        })
    }
    
    # 재시도 요청인 경우 처리 여부 검토
    is_retry, retry_count = check_retry_header(headers)
    if is_retry:
        logger.info(f"초기 요청 단계에서 Slack 재시도 감지: 재시도 횟수={retry_count}")
        # 여기서 추가적인 로직을 둘 수 있음 (예: 특정 재시도 횟수에서만 처리)
    
    # 5. 자기 자신을 비동기적으로 호출하여 실제 처리 수행
    if LAMBDA_FUNCTION_NAME and LAMBDA_CLIENT:
        try:
            # 원본 이벤트에 비동기 처리 플래그 추가
            async_event = dict(event)
            async_event['__async_processing'] = True
            
            # 자기 자신을 비동기적으로 호출
            LAMBDA_CLIENT.invoke(
                FunctionName=LAMBDA_FUNCTION_NAME,
                InvocationType='Event',  # 비동기 호출
                Payload=json.dumps(async_event).encode()
            )
            logger.info(f"비동기 처리를 위해 Lambda 함수({LAMBDA_FUNCTION_NAME})를 호출했습니다.")
        except Exception as e:
            logger.error(f"비동기 Lambda 호출 중 오류 발생: {str(e)}", exc_info=True)
            # 오류가 발생해도 사용자에게는 이미 응답했으므로 계속 진행
    else:
        logger.warning("Lambda 함수 이름이 설정되지 않았거나 클라이언트가 초기화되지 않아 비동기 처리가 불가능합니다.")
    
    # 사용자에게 즉시 응답 반환
    return response

def process_event_async(event):
    """
    비동기 모드에서 Slack 요청을 실제로 처리합니다.
    
    :param event: Lambda 이벤트 (비동기 처리 플래그 포함)
    :return: 처리 결과
    """
    logger.info("비동기 모드에서 Slack 요청 처리 시작")
    
    try:
        # 헤더 확인 및 재시도 요청 여부 체크
        headers = event.get("headers", {})
        is_retry, retry_count = check_retry_header(headers)
        
        if is_retry:
            logger.info(f"비동기 처리 단계에서 Slack 재시도 감지: 재시도 횟수={retry_count}")
            # 재시도 요청은 건너뛰기
            return {
                "statusCode": 200,
                "body": json.dumps({"message": "재시도 요청 무시됨", "retry_count": retry_count})
            }
        
        # 1. Slack 요청 검증 (자세한 검증)
        is_valid = validate_slack_request(event)
        if not is_valid:
            logger.error("Slack 요청 검증 실패")
            return {"statusCode": 401, "body": json.dumps({"error": "Invalid Slack request"})}
        
        # 2. 본문 디코딩
        body = event['body']
        if event.get('isBase64Encoded', False):
            body = base64.b64decode(body).decode('utf-8')
        
        # 3. SQS 메시지 준비
        action_data = {
            "event_type": "slack_interaction",
            "timestamp": int(time.time()),
            "raw_event": event
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
                        
                        # 원본 메시지의 타임스탬프 추가
                        container = payload.get('container', {})
                        message_ts = container.get('message_ts')
                        if message_ts:
                            parameters['message_ts'] = message_ts
                            logger.info(f"원본 메시지 타임스탬프 추가: {message_ts}")
                        
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
        
        # 4. SQS에 메시지 전송
        logger.info(f"SQS에 전송할 액션 데이터: {json.dumps(action_data)}")
        enqueue_result = enqueue_action(action_data)
        
        if not enqueue_result:
            logger.warning("SQS 대기열에 메시지 추가 실패")
            return {"statusCode": 500, "body": json.dumps({"error": "Failed to enqueue message"})}
        
        return {
            "statusCode": 200,
            "body": json.dumps({"success": True, "message": "Message successfully processed and enqueued"})
        }
    
    except Exception as e:
        logger.error(f"비동기 이벤트 처리 중 오류 발생: {str(e)}", exc_info=True)
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}

def validate_slack_request(event):
    """
    Slack 요청 서명을 검증합니다.
    
    :param event: API Gateway 이벤트
    :return: 검증 성공 여부
    """
    headers = event.get("headers", {})
    # 대소문자 무관하게 헤더 찾기
    slack_signature = None
    slack_request_timestamp = None
    
    for key, value in headers.items():
        if key.lower() == 'x-slack-signature':
            slack_signature = value
        elif key.lower() == 'x-slack-request-timestamp':
            slack_request_timestamp = value
    
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