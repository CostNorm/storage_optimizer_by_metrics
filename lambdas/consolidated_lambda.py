import os
import json
import boto3
import logging
import sys
from pathlib import Path
import base64

# 루트 디렉토리를 Python 경로에 추가
root_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(root_dir))

from dotenv import load_dotenv
# 핸들러 및 서비스 import
from ebs.ebs_service import analyze_specific_volume, analyze_all_regions, save_result_to_s3
from lambdas.handlers.slack_handler import handle_slack_request_initial, process_slack_request_async
from lambdas.handlers.sqs_handler import handle_sqs_message
# 메인 핸들러에서 필요한 Slack 함수 import
from integrations.slack.slack_messenger import send_slack_error, send_analysis_summary_to_slack

# 환경 변수 로드
load_dotenv()

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 환경 변수 (Main handler needs these)
SLACK_WEBHOOK_URL = os.environ.get('SLACK_WEBHOOK_URL')
SLACK_BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN')

# --- Main Lambda Handler ---
def lambda_handler(event, context):
    """
    통합 Lambda 핸들러: 이벤트 유형을 감지하고 적절한 핸들러로 라우팅합니다.
    """
    logger.info("EBS 스토리지 최적화 통합 Lambda 핸들러 시작")
    logger.debug(f"수신 이벤트: {event}")

    try:
        is_async_processing = event.get('__async_processing', False)
        event_type = determine_event_type(event)
        logger.info(f"감지된 이벤트 유형: {event_type}, 비동기 플래그: {is_async_processing}")

        # --- Event Routing Logic ---

        if is_async_processing:
            # 비동기 재호출 처리
            if event_type == "analyze_request":
                 logger.info("비동기 분석 요청 처리 위임 -> handle_analyze_request")
                 return handle_analyze_request(event, context)
            elif event_type == "slack_request":
                 logger.info("비동기 Slack 요청 처리 위임 -> process_slack_request_async")
                 return process_slack_request_async(event, context)
            elif event_type == "sqs_message":
                 logger.info("SQS 메시지 처리 위임 -> handle_sqs_message")
                 return handle_sqs_message(event, context)
            else:
                logger.error(f"지원되지 않는 비동기 이벤트 타입: {event_type}")
                return # No standard response for failed async invocation needed
        else:
            # 최초 동기 호출 처리
            if event_type == "analyze_request":
                logger.info("동기 분석 요청 처리 위임 -> handle_analyze_request")
                return handle_analyze_request(event, context)
            elif event_type == "slack_request":
                logger.info("동기 Slack 요청 처리 위임 -> handle_slack_request_initial")
                return handle_slack_request_initial(event, context)
            elif event_type == "sqs_message":
                logger.info("동기 SQS 메시지 처리 위임 -> handle_sqs_message")
                return handle_sqs_message(event, context)
            else:
                logger.error(f"지원되지 않는 동기 이벤트 타입: {event_type}")
                return {
                    "statusCode": 400,
                    "body": json.dumps({"error": f"지원되지 않는 이벤트 타입: {event_type}"})
                }

    except Exception as e:
        logger.critical(f"Lambda 핸들러 최상위 예외 발생: {str(e)}", exc_info=True)
        try:
            if SLACK_WEBHOOK_URL:
                send_slack_error(SLACK_WEBHOOK_URL, f"Lambda 핸들러 치명적 오류: {str(e)}")
        except Exception as slack_err:
            logger.error(f"Slack 오류 알림 전송 실패: {str(slack_err)}")

        return {
            "statusCode": 500,
            "body": json.dumps({"message": "처리 중 예기치 않은 서버 오류가 발생했습니다."})
        }

def determine_event_type(event):
    """
    Lambda 이벤트 객체를 분석하여 이벤트 소스 유형을 결정합니다.
    (API Gateway HTTP API 및 REST API 호환성 개선)
    """
    logger.debug(f"Determining event type for keys: {list(event.keys())}")

    # 1. SQS Event Check
    if isinstance(event.get('Records'), list) and len(event['Records']) > 0 and event['Records'][0].get('eventSource') == 'aws:sqs':
        logger.debug("Event type determined: SQS")
        return "sqs_message"

    # 2. API Gateway Event Check (Handles both HTTP API and REST API)
    request_context = event.get('requestContext', {})
    headers = event.get('headers', {})
    body = event.get('body', '')
    http_info = request_context.get('http', {}) # For HTTP API
    http_method = event.get('httpMethod') or http_info.get('method') # Check both REST and HTTP API style

    if request_context and http_method:
        logger.debug("API Gateway event detected.")
        # Check for Slack specific characteristics
        # Case-insensitive header check
        slack_sig = next((headers[k] for k in headers if k.lower() == 'x-slack-signature'), None)
        # Body check (payload for interactions, command for slash commands)
        # Handle potential base64 encoding if necessary before check
        decoded_body = body
        if event.get('isBase64Encoded', False):
            try:
                decoded_body = base64.b64decode(body).decode('utf-8')
            except Exception:
                logger.warning("Failed to decode base64 body for Slack check.")
                # Proceed with signature check anyway
                pass # Use original body for basic string check if decode fails
        is_slack_body = 'payload=' in decoded_body or 'command=' in decoded_body
        # User-Agent check (less reliable but supplementary)
        ua = next((headers[k] for k in headers if k.lower() == 'user-agent'), '')
        is_slack_ua = 'Slackbot' in ua

        # Determine if it IS Slack
        if slack_sig or is_slack_body: # Signature is strong indicator, body content is good too
            logger.info("Event type determined: Slack (API Gateway)")
            return "slack_request"
        else:
            logger.debug("API Gateway event, but not identified as Slack.")
            # Potentially handle other non-Slack API Gateway events here
            # return "api_gateway_other"

    # 3. CloudWatch Scheduled Event Check
    if event.get('source') == 'aws.events':
        logger.debug("Event type determined: CloudWatch Scheduled Event")
        return "analyze_request"

    # 4. Direct Lambda Invocation Check (heuristic)
    if isinstance(event, dict) and not any(k in event for k in ['Records', 'requestContext', 'httpMethod', 'source']):
        # Check for keys typically used in direct invocation for analysis
        if any(k in event for k in ['volume_id', 'region', 'output_format', 'detailed_report']):
             logger.debug("Event type determined: Direct Invocation (Analyze Request)")
             return "analyze_request"

    # 5. Fallback for Unrecognized Events
    logger.warning(f"Unable to determine event type. Event keys: {list(event.keys())}")
    return "unknown_event"

# --- analyze_request Handler (Remains in this file) ---
def handle_analyze_request(event, context):
    """
    EBS 볼륨 분석 요청을 처리합니다. (ebs_service 및 slack_messenger 호출)
    """
    logger.info("--- handle_analyze_request 시작 ---")
    try:
        volume_id = event.get('volume_id')
        specific_region = event.get('region')
        output_format = event.get('output_format', 'both')
        detailed_report = event.get('detailed_report', False)
        channel_id = event.get('channel_id')
        logger.debug(f"분석 파라미터: volume={volume_id}, region={specific_region}, output={output_format}, detailed={detailed_report}, channel={channel_id}")

        if volume_id:
            logger.info(f"ebs_service.analyze_specific_volume 호출 (vol={volume_id}) ...")
            result = analyze_specific_volume(volume_id, specific_region, detailed_report)
        else:
            logger.info("ebs_service.analyze_all_regions 호출 ...")
            result = analyze_all_regions(detailed_report=detailed_report)
        logger.debug(f"분석 결과 수신: {result}")

        logger.info("ebs_service.save_result_to_s3 호출 ...")
        s3_location = save_result_to_s3(result)
        logger.info(f"S3 저장 위치: {s3_location}")

        if output_format in ['slack', 'both']:
            logger.info("slack_messenger.send_analysis_summary_to_slack 호출 ...")
            slack_send_response = send_analysis_summary_to_slack(
                analysis_result=result,
                channel_id=channel_id,
                bot_token=SLACK_BOT_TOKEN,
                webhook_url=SLACK_WEBHOOK_URL
            )
            logger.info(f"Slack 요약 전송 결과: {slack_send_response}")
        else:
            logger.info("Slack 출력 형식이 아니므로 알림을 건너뛰었습니다.")

        response_body = {
            "message": "EBS 볼륨 최적화 분석이 성공적으로 완료되었습니다.",
            "result_location": s3_location,
            "summary": result.get("summary") if not volume_id else {
                # Filter out None values for cleaner summary
                k: v for k, v in {
                   "volume_id": result.get("volume_id"),
                   "region": result.get("region"),
                   "status": result.get("status"),
                   "recommendation": result.get("recommendation"),
                   "is_idle": result.get("is_idle"),
                   "is_overprovisioned": result.get("is_overprovisioned")
                }.items() if v is not None
            }
        }
        logger.info("--- handle_analyze_request 종료 (성공) ---")
        return {"statusCode": 200, "body": json.dumps(response_body, ensure_ascii=False)}

    except Exception as e:
        logger.error(f"handle_analyze_request 중 오류 발생: {str(e)}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps({"error": f"분석 요청 처리 중 오류 발생: {str(e)}"}, ensure_ascii=False)
        }

# --- All other function definitions are REMOVED --- 

# Lambda 함수 진입점 (for local testing)
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG) # Enable debug logging for local tests
    logger.info("Local execution test started.")

    # === Test Case 1: Analyze Request (All Regions) ===
    test_event_analyze_all = {
        "output_format": "slack",
        "detailed_report": False,
        # "channel_id": "YOUR_TEST_CHANNEL_ID" # Set for Slack output
    }
    print("\n--- Testing Analyze Request (All Regions) ---")
    try:
        result_analyze_all = lambda_handler(test_event_analyze_all, None)
        print(json.dumps(result_analyze_all, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"Test failed: {e}")

    # === Test Case 2: Analyze Request (Specific Volume) ===
    test_event_analyze_one = {
        "volume_id": "vol-0c8714f53a5c0c1b0", # Replace with a valid volume ID in your test env
        "region": "ap-northeast-2", # Replace with the correct region
        "output_format": "both",
        "detailed_report": True,
        # "channel_id": "YOUR_TEST_CHANNEL_ID"
    }
    print("\n--- Testing Analyze Request (Specific Volume) ---")
    try:
        result_analyze_one = lambda_handler(test_event_analyze_one, None)
        print(json.dumps(result_analyze_one, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"Test failed: {e}")

    # === Test Case 3: SQS Event (Simulated) ===
    sqs_test_action_data = {
        "action_type": "analyze", # Example action
        "parameters": {"volume_id": "vol-0c8714f53a5c0c1b0", "region": "ap-northeast-2", "detailed_report": True},
        "requested_by": "U12345",
        "channel_id": "C12345",
        "thread_ts": "1678886400.000100",
        "raw_event": { # Include simulated raw event for retry check etc.
             "headers": {"X-Slack-Retry-Num": "1"} if os.environ.get("TEST_RETRY") else {}
        }
    }
    test_event_sqs = {
       "Records": [
           {
               "messageId": "test-sqs-msg-1",
               "receiptHandle": "test-receipt-handle-1",
               "body": json.dumps(sqs_test_action_data),
               "attributes": {},
               "messageAttributes": {},
               "md5OfBody": "...",
               "eventSource": "aws:sqs",
               "eventSourceARN": "arn:aws:sqs:ap-northeast-2:123456789012:MyQueue",
               "awsRegion": "ap-northeast-2"
           }
       ]
    }
    print("\n--- Testing SQS Request ---")
    try:
        result_sqs = lambda_handler(test_event_sqs, None)
        print(json.dumps(result_sqs, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"Test failed: {e}")

    # === Test Case 4: Slack Slash Command (Simulated API Gateway) ===
    # Note: Signature validation will likely fail in local tests without mocking/secrets
    import time # Import time here for local testing
    slack_test_body_command = "command=/ebs-optimize&text=analyze+vol-0c8714f53a5c0c1b0+--region%3Dap-northeast-2&user_id=U12345&channel_id=C12345&response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2F..."
    test_event_slack_cmd = {
        "httpMethod": "POST",
        "headers": {
            "x-slack-signature": "v0=dummy_signature_needs_real_one_for_validation",
            "x-slack-request-timestamp": str(int(time.time())),
            "content-type": "application/x-www-form-urlencoded",
            "user-agent": "Slackbot 1.0 (+https://api.slack.com/robots)"
         },
         "body": slack_test_body_command,
         "isBase64Encoded": False,
         "requestContext": { "http": { "method": "POST", "path": "/slack/events" } }
    }
    print("\n--- Testing Slack Slash Command Request (Initial) ---")
    try:
        # This should return the initial ephemeral response and trigger async invocation
        result_slack_cmd = lambda_handler(test_event_slack_cmd, None)
        print(json.dumps(result_slack_cmd, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"Test failed: {e}")

    logger.info("Local execution test finished.")