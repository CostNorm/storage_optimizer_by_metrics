import os
import json
import logging
import time
import hmac
import hashlib
import base64
import sys
from pathlib import Path
from urllib.parse import parse_qs
import boto3 # For LAMBDA_CLIENT

# Add project root to Python path
try:
    root_dir = Path(__file__).resolve().parent.parent.parent # Adjust path based on new location
    sys.path.append(str(root_dir))

    from dotenv import load_dotenv
    from utils.sqs_helper import enqueue_action
    from integrations.slack.slack_messenger import send_original_command, send_slack_message # Assuming this exists or will be created

    # Load environment variables
    load_dotenv(dotenv_path=root_dir / '.env') # Specify path to .env if needed

except ImportError as e:
    logging.error(f"Failed to import modules in slack_handler: {e}")
    # Define fallbacks or re-raise depending on requirements
    enqueue_action = None
    send_original_command = None
    load_dotenv = None

# Logging setup
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
# Add handler if logging doesn't work automatically in Lambda
# handler = logging.StreamHandler()
# formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# handler.setFormatter(formatter)
# logger.addHandler(handler)

# Load necessary environment variables
SLACK_SIGNING_SECRET = os.environ.get('SLACK_SIGNING_SECRET')
LAMBDA_FUNCTION_NAME = os.environ.get('AWS_LAMBDA_FUNCTION_NAME')
SLACK_BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN') # Needed for send_original_command

# Initialize Lambda client (consider moving client initialization to a shared utility)
try:
    LAMBDA_CLIENT = boto3.client('lambda')
except Exception as e:
    logger.error(f"Error initializing Lambda client in slack_handler: {str(e)}")
    LAMBDA_CLIENT = None

# --- Functions moved from consolidated_lambda.py ---

def handle_slack_request_initial(event, context):
    """
    Slack 요청 초기 처리 - 즉시 응답 후 비동기 처리

    :param event: Lambda 이벤트
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
    logger.info(f"Slack 요청 헤더: {json.dumps(headers)}") # Debugging

    # 재시도 요청인지 확인
    is_retry, retry_count = check_slack_retry_header(headers)
    if is_retry:
        logger.info(f"Slack 재시도 요청 감지 - 초기 응답 단계: 재시도 횟수={retry_count}")
        return {
            "statusCode": 200,
            "body": json.dumps({
                "response_type": "ephemeral",
                "text": "요청이 이미 처리 중입니다..."
            })
        }

    if not (headers.get('x-slack-signature') or headers.get('X-Slack-Signature')):
        logger.error("Slack 서명이 없습니다.")
        return {
            "statusCode": 401,
            "body": json.dumps({"error": "Missing Slack signature"})
        }

    # 3. 본문 디코딩 (필요한 경우) - 비동기 처리에서 수행하도록 이동 가능

    # 4. Slack에 즉시 응답 (3초 타임아웃 방지)
    response = {
        "statusCode": 200,
        "body": json.dumps({
            "response_type": "ephemeral",
            "text": "요청이 접수되었습니다. 처리 중입니다..."
        })
    }

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
            # 오류 발생 시 처리 (예: Slack 알림) - 여기서는 이미 응답했으므로 로깅만
    else:
        logger.warning("Lambda 함수 이름 또는 클라이언트가 없어 비동기 처리 불가.")
        # 비동기 호출 실패 시 대체 처리 로직 (예: 동기 처리 시도 또는 에러 응답) 필요할 수 있음

    # 사용자에게 즉시 응답 반환
    return response

def process_slack_request_async(event, context):
    """
    비동기 모드에서 Slack 요청을 실제로 처리합니다.

    :param event: Lambda 이벤트 (비동기 처리 플래그 포함)
    :param context: Lambda 컨텍스트
    :return: 처리 결과 (실제 처리는 SQS로 넘겨지므로 성공/실패 위주)
    """
    logger.info("비동기 모드에서 Slack 요청 처리 시작")

    try:
        headers = event.get("headers", {})

        # 재시도 요청인지 확인 (SQS에서도 확인하지만 여기서도 방어적으로 확인)
        is_retry, retry_count = check_slack_retry_header(headers)
        if is_retry:
            logger.info(f"Slack 재시도 요청 감지 (비동기 처리 단계): 재시도 횟수={retry_count}. 건너뜁니다.")
            return {"statusCode": 200, "body": json.dumps({"message": "Ignoring retry request"})}

        # 1. Slack 요청 검증 (자세한 검증)
        if not validate_slack_request(event):
            logger.error("Slack 요청 검증 실패")
            # TODO: 실패 시 사용자에게 알림? (response_url 사용 고려)
            return {"statusCode": 401, "body": json.dumps({"error": "Invalid Slack request"})}

        # 2. 본문 디코딩 및 파싱
        body = event['body']
        if event.get('isBase64Encoded', False):
            body = base64.b64decode(body).decode('utf-8')

        # 3. 액션 데이터 준비 (기존 parse_slack_request 로직 통합 또는 호출)
        action_data = parse_slack_request(body) # 파싱 함수 호출
        if "error" in action_data:
             logger.error(f"Slack 요청 파싱 오류: {action_data['error']}")
             # TODO: 파싱 오류 시 사용자 알림
             return {"statusCode": 400, "body": json.dumps({"error": f"Failed to parse request: {action_data['error']}"})}

        # 필요한 정보 추가 (원본 이벤트 등)
        action_data["event_type"] = "slack_interaction"
        action_data["timestamp"] = int(time.time())
        action_data["raw_event"] = event # SQS 메시지에서 재시도 확인 등을 위해 원본 이벤트 포함

        # 슬래시 커맨드인 경우, 초기 메시지 전송 및 ts 저장
        initial_message_ts = None # Initialize ts
        if action_data.get("interaction_type") == "slash_command":
            channel_id = action_data.get("channel_id")
            requested_by = action_data.get("requested_by")

            if channel_id and SLACK_BOT_TOKEN:
                # Use send_slack_message from slack_messenger
                # Ensure send_slack_message is imported correctly
                try:
                     # Import send_slack_message if not already globally imported and available
                     if 'send_slack_message' not in globals() or send_slack_message is None:
                         from integrations.slack.slack_messenger import send_slack_message
                     
                     if send_slack_message:
                        # Send an initial message like "Analysis starting..."
                        initial_message_text = f"<@{requested_by}> requested EBS analysis. Starting..."
                        success, initial_message_ts = send_slack_message(
                            channel_id,
                            initial_message_text,
                            SLACK_BOT_TOKEN,
                            thread_ts=None # Post as a new message
                        )
                        if success and initial_message_ts:
                             action_data["initial_message_ts"] = initial_message_ts # Store the ts
                             logger.info(f"Initial analysis message sent. ts: {initial_message_ts}")
                        else:
                             logger.warning("Failed to send initial analysis message or get its timestamp.")
                     else:
                          logger.error("send_slack_message function is not available after import attempt.")
                except ImportError:
                     logger.error("Failed to import send_slack_message from integrations.slack.slack_messenger")
                except Exception as send_err:
                     logger.error(f"Error sending initial Slack message: {send_err}", exc_info=True)
            else:
                 logger.warning("Cannot send initial message: Missing Channel ID or Bot Token.")

        # 4. SQS에 메시지 전송 (이제 action_data에 initial_message_ts 포함 가능)
        if enqueue_action:
            logger.info(f"SQS에 전송할 액션 데이터: {action_data}")
            enqueue_result = enqueue_action(action_data)

            if not enqueue_result:
                logger.error("SQS 대기열에 메시지 추가 실패")
                # TODO: SQS 전송 실패 시 사용자 알림 (response_url 사용)
                return {"statusCode": 500, "body": json.dumps({"error": "Failed to enqueue action"})}
        else:
             logger.error("enqueue_action 함수를 사용할 수 없습니다.")
             return {"statusCode": 500, "body": json.dumps({"error": "SQS helper not available"})}

        # 비동기 처리는 별도 응답이 필요 없음 (이미 초기 응답 보냄)
        return {
            "statusCode": 200, # HTTP 상태 코드는 성공으로 반환
            "body": json.dumps({"success": True, "message": "Action enqueued successfully"}) # 내부 처리 결과
        }

    except Exception as e:
        logger.error(f"비동기 Slack 이벤트 처리 중 오류 발생: {str(e)}", exc_info=True)
        # TODO: 처리 중 예외 발생 시 사용자 알림 (response_url 사용 고려)
        # 여기서의 반환값은 Lambda 실행 자체의 성공/실패를 의미할 수 있음
        return {"statusCode": 500, "body": json.dumps({"error": f"Internal server error: {str(e)}"}) }


def validate_slack_request(event):
    """
    Slack 요청 서명을 검증합니다.

    :param event: API Gateway 이벤트
    :return: 검증 성공 여부
    """
    headers = event.get("headers", {})
    # 헤더 키 대소문자 구분 문제 방지
    slack_signature = headers.get('x-slack-signature') or headers.get('X-Slack-Signature')
    slack_request_timestamp = headers.get('x-slack-request-timestamp') or headers.get('X-Slack-Request-Timestamp')

    if not slack_signature or not slack_request_timestamp:
        logger.error("Slack 서명 또는 타임스탬프가 헤더에 없습니다.")
        return False

    # 타임스탬프 검증 (요청이 5분 이내인지 확인)
    current_ts = int(time.time())
    try:
        req_ts = int(slack_request_timestamp)
    except ValueError:
        logger.error("잘못된 타임스탬프 형식입니다.")
        return False

    if abs(current_ts - req_ts) > 300:  # 5분 = 300초
        logger.error("요청 타임스탬프가 허용 범위를 벗어났습니다.")
        # Slack 재시도의 경우에도 오래된 타임스탬프로 올 수 있으므로 로깅 레벨 조절 고려
        return False

    # 요청 본문(body) 추출 및 디코딩
    body = event.get("body", "")
    if event.get("isBase64Encoded", False):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except Exception as e:
            logger.error(f"Base64 본문 디코딩 오류: {e}")
            return False

    # Slack 서명 검증을 위한 base string 생성
    sig_basestring = f"v0:{slack_request_timestamp}:{body}"

    # HMAC-SHA256 방식으로 해시 계산
    if SLACK_SIGNING_SECRET:
        try:
            computed_signature = "v0=" + hmac.new(
                SLACK_SIGNING_SECRET.encode("utf-8"),
                sig_basestring.encode("utf-8"),
                hashlib.sha256
            ).hexdigest()

            # 서명 비교 (상수 시간 비교로 타이밍 공격 방지)
            if not hmac.compare_digest(computed_signature, slack_signature):
                logger.error(f"잘못된 Slack 서명입니다. 계산된 값: {computed_signature}, 받은 값: {slack_signature}")
                return False
        except Exception as e:
             logger.error(f"서명 계산 중 오류 발생: {e}")
             return False
    else:
        # 서명 검증을 위한 시크릿 키가 없을 경우 로깅
        logger.warning("SLACK_SIGNING_SECRET이 설정되지 않아 서명 검증을 건너뜁니다.")
        # 개발 환경 외에서는 실패 처리하는 것이 안전
        if os.environ.get('ENV', 'production').lower() != 'development':
             logger.error("프로덕션 환경에서 SLACK_SIGNING_SECRET 없이 요청을 수신했습니다.")
             return False # 프로덕션에서는 실패 처리

    logger.info("Slack 요청 서명 검증 성공")
    return True

def parse_slack_request(body):
    """
    Slack 요청 본문(인터랙션 페이로드 또는 슬래시 커맨드 데이터)을 파싱하여
    SQS 메시지로 보낼 표준화된 액션 데이터 구조를 생성합니다.

    :param body: Slack 요청의 raw body (URL-encoded string)
    :return: 파싱된 액션 데이터 딕셔너리 또는 오류 정보 포함 딕셔너리
    """
    action_data = {}
    try:
        params = parse_qs(body)

        if 'payload' in params:
            # 인터랙티브 컴포넌트(버튼 클릭 등)
            payload = json.loads(params['payload'][0])
            action_data["interaction_type"] = "interactive_component"
            action_data["payload"] = payload # 원본 페이로드 저장 (디버깅 등)

            if payload.get('type') == 'block_actions' and payload.get('actions'):
                action = payload['actions'][0]
                action_id = action.get('action_id', '')

                # 'execute_' 접두사로 시작하는 버튼 액션 처리
                if action_id.startswith('execute_'):
                    action_type_suffix = action_id.replace('execute_', '')
                    action_data["action_type"] = action_type_suffix # 예: 'snapshot_and_delete'

                    # 버튼 값(value)에서 파라미터 추출 (JSON 형식 가정)
                    try:
                        button_value_str = action.get('value', '{}')
                        parameters = json.loads(button_value_str)
                        action_data["parameters"] = parameters
                    except json.JSONDecodeError as e:
                        logger.error(f"버튼 값 JSON 파싱 오류: {e}, 값: {button_value_str}")
                        action_data["parameters"] = {"error": "Invalid button value format"}

                # 다른 종류의 인터랙션 action_id 처리 (필요한 경우)
                else:
                    action_data["action_type"] = action_id # 일반 action_id

                # 공통 정보 추출
                action_data["requested_by"] = payload.get('user', {}).get('id', 'unknown')
                action_data["channel_id"] = payload.get('channel', {}).get('id')
                action_data["response_url"] = payload.get('response_url')

                # 스레드 정보 추출
                container = payload.get('container', {})
                message_ts = container.get('message_ts') # 버튼이 달린 원본 메시지 ts
                thread_ts = payload.get('message', {}).get('thread_ts') or container.get('thread_ts') # 메시지가 스레드에 속한 경우 그 스레드 ts
                action_data["thread_ts"] = thread_ts or message_ts # 스레드 댓글이면 thread_ts, 아니면 원본 메시지 ts를 스레드 시작점으로 간주
                # 액션 실행 시 원본 메시지 업데이트를 위해 message_ts를 파라미터에 포함
                if "parameters" in action_data and isinstance(action_data["parameters"], dict):
                     action_data["parameters"]['message_ts'] = message_ts
                elif "parameters" not in action_data :
                     action_data["parameters"] = {'message_ts': message_ts}

                logger.info(f"인터랙션 파싱됨: type={action_data.get('action_type')}, params={action_data.get('parameters')}, thread={action_data.get('thread_ts')}")

            # 다른 인터랙션 타입 처리 (view_submission 등) - 필요한 경우 추가
            else:
                logger.info(f"처리되지 않은 페이로드 타입: {payload.get('type')}")
                action_data["action_type"] = payload.get('type') # 타입 정보만 저장

        elif 'command' in params:
            # 슬래시 커맨드
            command = params.get('command', [''])[0]
            text = params.get('text', [''])[0]
            action_data["interaction_type"] = "slash_command"
            action_data["command"] = command
            action_data["text"] = text
            # 슬래시 커맨드의 경우, 실제 액션은 SQS 핸들러의 process_action에서 결정됨
            # 여기서 action_type을 'slash_command'로 설정하거나 비워둘 수 있음.
            action_data["action_type"] = "slash_command" # SQS 핸들러에서 분기하기 위한 타입

            # 공통 정보 추출
            action_data["requested_by"] = params.get('user_id', ['unknown'])[0]
            action_data["channel_id"] = params.get('channel_id', [''])[0]
            action_data["response_url"] = params.get('response_url', [''])[0]
            # 슬래시 커맨드는 기본적으로 스레드 정보 없음 (SQS 핸들러에서 메시지 보내며 생성)
            action_data["thread_ts"] = None

            # 커맨드 텍스트에서 파라미터 파싱 시도
            action_data["parameters"] = parse_command_parameters(text)
            logger.info(f"슬래시 커맨드 파싱됨: cmd={command}, text='{text}', params={action_data['parameters']}")

        else:
            logger.warning(f"알 수 없는 Slack 요청 형식: {list(params.keys())}")
            action_data["error"] = "Unknown request format"

    except json.JSONDecodeError as e:
        logger.error(f"Slack 페이로드 JSON 파싱 오류: {e}")
        action_data["error"] = "Invalid JSON payload"
    except Exception as e:
        logger.error(f"Slack 요청 파싱 중 예외 발생: {str(e)}", exc_info=True)
        action_data["error"] = f"Parsing exception: {str(e)}"

    return action_data

def parse_command_parameters(text):
    """
    슬래시 커맨드 텍스트를 파라미터 딕셔너리로 파싱합니다.
    예: "analyze vol-123 --region=us-east-1 --detailed"
    -> {"sub_command": "analyze", "volume_id": "vol-123", "region": "us-east-1", "detailed_report": True}

    :param text: 슬래시 커맨드 뒤의 텍스트 문자열
    :return: 파라미터 딕셔너리
    """
    params = {}
    words = text.split()
    logger.info(f"슬래시 커맨드 파라미터 파싱 시작: text='{text}'")

    if not words:
        logger.info("파싱할 커맨드 텍스트 없음")
        return params # 빈 딕셔너리 반환

    # 첫 단어는 하위 명령어(sub_command)로 가정
    params["sub_command"] = words[0].lower()
    logger.info(f"하위 명령어 감지: {params['sub_command']}")

    # 나머지 단어들을 순회하며 파라미터 추출
    remaining_words = words[1:]
    i = 0
    while i < len(remaining_words):
        word = remaining_words[i]

        if word.startswith("--"):
            # 옵션 형태 (--key=value 또는 --flag)
            if "=" in word:
                key_value = word.split("=", 1)
                key = key_value[0][2:].replace("-", "_") # '--some-option' -> 'some_option'
                value = key_value[1]
                params[key] = value
                logger.info(f"옵션 감지 (값 포함): {key}={value}")
            else:
                key = word[2:].replace("-", "_")
                params[key] = True # 플래그 형태 옵션
                logger.info(f"옵션 감지 (플래그): {key}=True")
            i += 1
        elif word.startswith("vol-") and "volume_id" not in params:
            # 위치 기반 인자: 볼륨 ID (이미 추출되지 않은 경우)
            params["volume_id"] = word
            logger.info(f"볼륨 ID 감지: {params['volume_id']}")
            i += 1
        elif params["sub_command"] == "execute" and "action_type_param" not in params and "volume_id" in params:
             # execute 명령어의 두 번째 위치 인자: action_type
             params["action_type_param"] = word # SQS 핸들러에서 실제 action_type으로 사용
             logger.info(f"Execute 액션 타입 감지: {params['action_type_param']}")
             i += 1
        else:
            # 인식되지 않은 인자 (무시하거나 오류 처리)
            logger.warning(f"인식되지 않은 인자/옵션: {word}")
            i += 1 # 다음 단어로 이동

    # 파싱된 파라미터 정리 (예: detailed_report 키 이름 통일 등)
    if params.get("detailed"):
        params["detailed_report"] = True
        del params["detailed"] # 'detailed' 키 제거

    logger.info(f"파싱된 최종 파라미터: {params}")
    return params

def check_slack_retry_header(headers):
    """
    Slack의 재시도 요청 헤더를 확인합니다.

    :param headers: 요청 헤더 딕셔너리
    :return: (재시도 여부, 재시도 횟수 (int or None))
    """
    retry_count = None
    is_retry = False

    # 대소문자 구분 없이 헤더 검색
    retry_num_key = next((k for k in headers if k.lower() == 'x-slack-retry-num'), None)
    retry_reason_key = next((k for k in headers if k.lower() == 'x-slack-retry-reason'), None)

    if retry_num_key:
        retry_count_str = headers[retry_num_key]
        try:
            retry_count = int(retry_count_str)
            is_retry = True
            reason = headers.get(retry_reason_key, "N/A")
            logger.info(f"Slack 재시도 감지: count={retry_count}, reason={reason}")
        except ValueError:
            logger.warning(f"Slack 재시도 횟수 파싱 오류: '{retry_count_str}'")
            is_retry = True # 헤더가 존재하므로 재시도로 간주
            retry_count = -1 # 오류 표시 값

    return is_retry, retry_count

# --- End of moved functions ---

# 로컬 테스트 등을 위한 진입점 (선택 사항)
# if __name__ == "__main__":
#     # 테스트용 이벤트 데이터 생성 및 함수 호출
#     pass 