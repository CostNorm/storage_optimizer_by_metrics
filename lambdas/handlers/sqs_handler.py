import json
import logging
import boto3
import os
import sys
from pathlib import Path
# import textwrap # Removed textwrap import

# Logging setup should be done early
logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

# Removed sys.path modification logic - rely on Lambda's default path
# root_dir = Path(__file__).resolve().parent.parent.parent
# sys.path.append(str(root_dir))
# print(f"Calculated root_dir: {root_dir}") # DEBUG
# print(f"Current sys.path: {sys.path}") # DEBUG
# print(f"Current working directory: {os.getcwd()}") # DEBUG

try:
    # Add debug print right before imports
    # print(f"DEBUG sys.path before imports: {sys.path}") # Keep commented for now

    # Removed dotenv loading from here. Manage env vars in Lambda config.
    # from dotenv import load_dotenv
    # load_dotenv(dotenv_path=root_dir / '.env')

    # Import necessary functions from other modules
    # Using absolute imports relative to the project root package
    from storage_optimizer_by_metrics.ebs.ebs_service import analyze_specific_volume, analyze_all_regions, save_result_to_s3
    from storage_optimizer_by_metrics.ebs.actions.recommendation_executor import RecommendationExecutor
    from storage_optimizer_by_metrics.integrations.slack.slack_messenger import (
        send_slack_message,
        send_analysis_summary_to_slack,
        send_execution_result_to_slack,
    )
    from storage_optimizer_by_metrics.integrations.slack.utils import check_slack_retry_header

except ImportError as e:
    # Log the detailed import error including the problematic module
    logger.error(f"Failed to import modules in sqs_handler: {e}", exc_info=True) # Add exc_info for traceback
    # Define fallbacks or re-raise
    analyze_specific_volume = None
    analyze_all_regions = None
    save_result_to_s3 = None
    RecommendationExecutor = None
    send_slack_message = None
    send_analysis_summary_to_slack = None
    send_execution_result_to_slack = None
    check_slack_retry_header = None
    # load_dotenv = None # Removed

# Load necessary environment variables (from Lambda config)
SQS_QUEUE_URL = os.environ.get('SQS_QUEUE_URL')
SLACK_BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN')
# ... load other required environment variables ...

# --- Functions moved from consolidated_lambda.py ---

def handle_sqs_message(event, context):
    """
    SQS 메시지 처리 - action_executor_lambda의 기능 구현

    :param event: Lambda SQS 이벤트
    :param context: Lambda 컨텍스트
    :return: 처리 결과
    """
    logger.info("SQS 메시지 처리 시작 (sqs_handler)")

    results = {
        "processed": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped": 0,
        "details": []
    }

    if 'Records' not in event:
        logger.error("SQS 레코드가 이벤트에 없습니다.")
        return {
            "statusCode": 400, # Indicate bad request
            "body": json.dumps({"error": "No SQS records in event"})
        }

    # Initialize SQS client only if needed (for delete_message)
    sqs = None

    for record in event['Records']:
        message_id = record.get('messageId', 'unknown')
        receipt_handle = record.get('receiptHandle')
        logger.info(f"메시지 처리 중: {message_id}")

        try:
            # 메시지 본문에서 액션 데이터 추출
            action_data = json.loads(record['body'])
            logger.debug(f"수신된 액션 데이터: {action_data}")
            initial_message_ts = action_data.get('initial_message_ts') # Extract initial ts

            # Slack 재시도 메시지인 경우 건너뛰고 메시지 삭제
            if is_retry_request(action_data):
                logger.info(f"Slack 재시도 요청 감지 (raw_event 헤더 확인), 메시지 건너<0xEB><0x9B><0x81>: {message_id}")

                # SQS 메시지 즉시 삭제
                if receipt_handle and SQS_QUEUE_URL:
                    try:
                        if sqs is None: sqs = boto3.client('sqs')
                        sqs.delete_message(
                            QueueUrl=SQS_QUEUE_URL,
                            ReceiptHandle=receipt_handle
                        )
                        logger.info(f"Slack 재시도 요청 메시지 삭제 완료: {message_id}")
                    except Exception as del_err:
                        # Log delete error but continue processing other messages
                        logger.error(f"메시지 삭제 중 오류 발생 (message_id: {message_id}): {str(del_err)}")
                else:
                     logger.warning(f"메시지 삭제 불가 (ReceiptHandle 또는 QueueUrl 없음): {message_id}")

                results["skipped"] += 1
                results["processed"] += 1
                results["details"].append({
                    "message_id": message_id,
                    "status": "skipped",
                    "reason": "slack_retry"
                })
                continue # Skip to the next record

            # Slack 재시도 확인
            event = record.get('attributes', {}) # 이벤트 속성에서 헤더 가져오기 (SQS 구조에 따라 다를 수 있음)
            headers = event.get('headers') # 실제 헤더 위치 확인 필요
            if check_slack_retry_header(headers):
                 logger.warning(f"메시지 {message_id}는 Slack 재시도 요청이므로 건너뜀니다.")
                 results["skipped"] += 1
                 results["processed"] += 1
                 results["details"].append({
                     "message_id": message_id,
                     "status": "skipped",
                     "reason": "slack_retry"
                 })
                 continue

            # action_type과 ActionType 일관성 유지
            if 'ActionType' in action_data and 'action_type' not in action_data:
                action_data['action_type'] = action_data['ActionType']
            elif 'action_type' in action_data and 'ActionType' not in action_data:
                action_data['ActionType'] = action_data['action_type']

            logger.info(f"액션 처리 시작: action_type={action_data.get('action_type')}, ActionType={action_data.get('ActionType')}")

            # 액션 처리 (핵심 로직)
            result = process_action(action_data)

            # 처리 결과 저장
            results["processed"] += 1
            if result.get('success', False):
                results["succeeded"] += 1
            else:
                results["failed"] += 1

            # Include detailed result for logging/debugging
            results["details"].append({
                "message_id": message_id,
                "action_type": action_data.get('action_type'),
                "result": result # Contains success status, message, error etc.
            })

            # Lambda는 성공적으로 처리된 메시지를 자동으로 삭제함
            # 실패한 경우 메시지는 재시도 정책에 따라 큐에 남게 됨
            # 따라서 여기서 delete_message는 재시도 건너뛰는 경우 외엔 호출 안 함

        except json.JSONDecodeError as json_err:
             logger.error(f"메시지 본문 JSON 파싱 오류 (message_id: {message_id}): {json_err}")
             results["processed"] += 1
             results["failed"] += 1
             results["details"].append({
                "message_id": message_id,
                "error": f"JSON Decode Error: {json_err}"
             })
             # 실패한 메시지는 삭제하지 않음 (재시도 또는 Dead Letter Queue로 이동)
        except Exception as e:
            logger.error(f"메시지 처리 중 예외 발생 (message_id: {message_id}): {str(e)}", exc_info=True)
            results["processed"] += 1
            results["failed"] += 1
            results["details"].append({
                "message_id": message_id,
                "error": str(e)
            })
            # 실패한 메시지는 삭제하지 않음

    logger.info(f"SQS 메시지 처리 완료: {results}")
    # Lambda 함수 자체는 성공적으로 완료되었음을 알리는 응답 반환
    # 개별 메시지 처리 성공/실패는 results에 기록됨
    return {
        "statusCode": 200,
        "body": json.dumps(results)
    }


def is_retry_request(action_data):
    """
    SQS 메시지 내의 원본 Slack 이벤트 헤더를 보고 재시도 요청인지 확인합니다.

    :param action_data: SQS 메시지 본문 (파싱된 JSON 객체)
    :return: 재시도 여부 (bool)
    """
    raw_event = action_data.get('raw_event')
    if not isinstance(raw_event, dict):
        return False # No raw_event, assume not a retry

    headers = raw_event.get('headers')
    if not isinstance(headers, dict):
        return False # No headers in raw_event

    # Use the imported check_slack_retry_header function
    if check_slack_retry_header is None:
        logger.error("check_slack_retry_header function is not available (Import failed?). Cannot check retry status.")
        return False # Import failed, assume not a retry for safety

    try:
        # check_slack_retry_header should ideally just return True/False
        if check_slack_retry_header(headers):
             logger.info("Slack retry detected via raw_event headers in SQS message.")
             return True
        else:
             return False
    except Exception as e:
        logger.error(f"Error checking retry header in is_retry_request: {e}", exc_info=True)
        return False # Error during check, assume not a retry


def process_action(action_data):
    """
    액션 데이터를 받아 적절한 처리 함수로 라우팅합니다.

    :param action_data: SQS 메시지에서 파싱된 액션 데이터
    :return: 처리 결과 딕셔너리 ({'success': bool, 'message': str, ...})
    """
    action_type = action_data.get('action_type')
    parameters = action_data.get('parameters', {})
    text = action_data.get('text', '') # 슬래시 커맨드용
    requested_by = action_data.get('requested_by', 'unknown')
    channel_id = action_data.get('channel_id')
    response_url = action_data.get('response_url') # 필요시 사용
    thread_ts = action_data.get('thread_ts') # 스레드 식별자
    initial_message_ts = action_data.get('initial_message_ts') # Extract initial ts again for passing

    logger.info(f"액션 처리 라우팅: type={action_type}, params={parameters}, thread_ts={thread_ts}, initial_ts={initial_message_ts}")

    # 슬래시 커맨드 파라미터 재파싱 로직 (필요한 경우)
    # slack_handler에서 이미 파싱되었지만, SQS 메시지에 파라미터가 없는 경우 대비
    if action_type == 'slash_command' and not parameters and text:
        if parse_command_parameters:
            logger.info(f"슬래시 커맨드 파라미터 재파싱 시도: text='{text}'")
            try:
                parameters = parse_command_parameters(text)
                action_data['parameters'] = parameters # 업데이트
                logger.info(f"재파싱된 파라미터: {parameters}")
                # 슬래시 커맨드의 실제 액션은 parameters['sub_command'] 로 결정됨
                # process_slash_command 함수가 이를 처리함
            except Exception as parse_err:
                logger.error(f"슬래시 커맨드 재파싱 중 오류: {parse_err}")
                return {"success": False, "error": f"Command parsing error: {parse_err}"}
        else:
            logger.error("parse_command_parameters 함수 사용 불가. 슬래시 커맨드 처리 실패.")
            return {"success": False, "error": "Command parser not available"}

    # 액션 유형에 따른 분기
    # 1. 슬래시 커맨드 처리
    if action_type == 'slash_command':
        return process_slash_command(action_data) # initial_message_ts is in action_data

    # 2. 직접 실행 액션 (버튼 클릭 등에서 파생)
    direct_actions = ['snapshot_and_delete', 'snapshot_only', 'resize', 'change_type']
    if action_type in direct_actions:
        # Pass initial_message_ts here if needed, though execute usually updates the message directly
        return process_execute_action(parameters, requested_by, channel_id, response_url, thread_ts)

    # 3. 기타 액션 타입 (예: 'analyze', 'execute' - 슬래시 커맨드 하위 명령에서 파생된 경우)
    # 이 부분은 슬래시 커맨드 처리(`process_slash_command`) 내부에서 호출되므로,
    # 직접 `process_action`으로 들어오는 경우는 흔치 않을 수 있음.
    # 하지만 명시적으로 처리 경로를 두는 것이 안전할 수 있음.
    elif action_type == 'analyze':
        logger.warning("process_action 에서 'analyze' 직접 처리. process_slash_command 를 통해야 함.")
        # Pass initial_message_ts to analyze action
        return process_analyze_action(parameters, requested_by, channel_id, thread_ts, initial_message_ts)

    elif action_type == 'execute':
         logger.warning("process_action 에서 'execute' 직접 처리. process_slash_command 를 통해야 함.")
         # process_execute_action 은 실제 실행 타입(resize 등)으로 호출되어야 함
         # 여기서 'execute' 타입은 추가 분기가 필요함
         actual_action = parameters.get('action_type_param') or parameters.get('action_type') # 실제 실행할 액션 찾기
         if actual_action and actual_action != 'execute':
              parameters['action_type'] = actual_action # 실제 액션 타입으로 설정
              return process_execute_action(parameters, requested_by, channel_id, response_url, thread_ts)
         else:
              err_msg = f"'execute' 액션 타입으로 호출되었으나 실제 실행할 하위 액션({actual_action})을 결정할 수 없습니다."
              logger.error(err_msg)
              return {"success": False, "error": err_msg}

    # 4. 이전 방식의 액션 타입 (idle_volume_action, overprovisioned_volume_action) - 하위 호환성
    elif action_type and (action_type.startswith('idle_volume_') or action_type.startswith('overprovisioned_volume_')):
         logger.warning(f"Legacy action type detected: {action_type}. Using process_volume_action.")
         return process_volume_action(action_type, parameters, requested_by, channel_id, response_url, thread_ts)

    # 5. 지원되지 않는 액션
    else:
        error_msg = f"지원되지 않는 액션 유형입니다: {action_type}"
        logger.error(error_msg)
        # 알림 전송 (필요 시)
        if channel_id and thread_ts and send_slack_message and SLACK_BOT_TOKEN:
             send_slack_message(channel_id, f"오류: {error_msg}", SLACK_BOT_TOKEN, thread_ts)
        return {"success": False, "error": error_msg}


def process_slash_command(action_data):
    """
    슬래시 커맨드 데이터를 받아 analyze 또는 execute 액션으로 분기합니다.
    """
    parameters = action_data.get('parameters', {})
    requested_by = action_data.get('requested_by', 'unknown')
    channel_id = action_data.get('channel_id')
    thread_ts = action_data.get('thread_ts')
    initial_message_ts = action_data.get('initial_message_ts') # Extract initial ts

    sub_command = parameters.get("sub_command")
    logger.info(f"슬래시 커맨드 처리 시작: sub_command='{sub_command}', params={parameters}, thread_ts={thread_ts}, initial_ts={initial_message_ts}")

    if not sub_command:
        # Corrected help_text definition
        help_text = (
            "EBS 볼륨 최적화 도구 사용법:\n"
            "• `/ebs-optimize analyze` - 모든 리전의 볼륨 분석\n"
            "• `/ebs-optimize analyze vol-xxxx [--region=ap-northeast-2] [--detailed]` - 특정 볼륨 분석\n"
            "• `/ebs-optimize execute vol-xxxx <action> [--region=ap-northeast-2]` - 특정 볼륨에 조치 실행\n"
            "   (예: `/ebs-optimize execute vol-123 snapshot_and_delete`)\n\n"
            "* 가능한 조치:* `snapshot_and_delete`, `snapshot_only`, `change_type`, `resize`"
        )
        if channel_id and send_slack_message and SLACK_BOT_TOKEN:
            send_slack_message(channel_id, help_text, SLACK_BOT_TOKEN, thread_ts=None)
            logger.info("도움말 메시지 전송됨.")
        return {"success": True, "message": "Help message displayed"}

    if sub_command == "analyze":
        return process_analyze_action(parameters, requested_by, channel_id, thread_ts, initial_message_ts)

    elif sub_command == "execute":
        execute_action_type = parameters.get('action_type_param')
        if not execute_action_type:
             err_msg = "`execute` 명령어에 실행할 액션 타입이 지정되지 않았습니다. (예: snapshot_and_delete)"
             logger.error(err_msg)
             if channel_id and thread_ts and send_slack_message and SLACK_BOT_TOKEN:
                 send_slack_message(channel_id, f"명령어 오류: {err_msg}", SLACK_BOT_TOKEN, thread_ts)
             return {"success": False, "error": err_msg}
        parameters['action_type'] = execute_action_type
        return process_execute_action(parameters, requested_by, channel_id, None, thread_ts)

    else:
        err_msg = f"알 수 없는 명령어입니다: `{sub_command}`. 사용 가능한 명령어: `analyze`, `execute`"
        logger.warning(f"알 수 없는 슬래시 하위 명령어: {sub_command}")
        if channel_id and thread_ts and send_slack_message and SLACK_BOT_TOKEN:
            send_slack_message(channel_id, err_msg, SLACK_BOT_TOKEN, thread_ts)
        return {"success": False, "error": err_msg}


def process_analyze_action(parameters, requested_by, channel_id, thread_ts=None, initial_message_ts=None):
    """
    볼륨 분석 액션을 처리하고 결과를 Slack 스레드에 알립니다.

    :param parameters: 분석 파라미터 (volume_id, region, detailed_report 등 포함)
    :param requested_by: 요청자 ID
    :param channel_id: Slack 채널 ID
    :param thread_ts: 응답을 보낼 스레드 타임스탬프
    :param initial_message_ts: 초기 메시지 타임스탬프
    :return: 처리 결과 딕셔너리
    """
    if analyze_specific_volume is None or analyze_all_regions is None or save_result_to_s3 is None:
         logger.error("EBS 서비스 함수가 제대로 임포트되지 않았습니다.")
         return {"success": False, "error": "EBS service not available"}
    if send_slack_message is None or send_analysis_summary_to_slack is None:
        logger.warning("Slack 메시징 함수 일부가 임포트되지 않았습니다.")
        # 진행은 가능하나 알림이 실패할 수 있음

    volume_id = parameters.get('volume_id')
    region = parameters.get('region') # 특정 볼륨 분석 시 사용
    detailed_report = parameters.get('detailed_report', True) # 상세 보고서 기본 활성화

    logger.info(f"볼륨 분석 액션 시작: volume_id={volume_id}, region={region}, detailed={detailed_report}, thread={thread_ts}, initial_ts={initial_message_ts}")

    analysis_result = None
    error_occurred = False

    try:
        # Notify start (in thread)
        if channel_id and thread_ts and send_slack_message and SLACK_BOT_TOKEN:
            analysis_target = f"볼륨 `{volume_id}`" if volume_id else "전체 EBS 볼륨"
            send_slack_message(channel_id, f"{analysis_target} 분석을 시작합니다...", SLACK_BOT_TOKEN, thread_ts)

        # 실제 분석 실행 (ebs_service 함수 호출) - Pass initial_message_ts
        if volume_id:
            # Pass initial_message_ts and slack info if specific volume analysis needs updates (less likely)
            analysis_result = analyze_specific_volume(volume_id, region, detailed_report)
        else:
            # Pass initial_message_ts and slack info for progress updates
            analysis_result = analyze_all_regions(detailed_report=detailed_report, 
                                              initial_message_ts=initial_message_ts,
                                              channel_id=channel_id, 
                                              bot_token=SLACK_BOT_TOKEN)

        # 분석 결과 확인
        if not analysis_result or 'error' in analysis_result:
             error_message = analysis_result.get('error', '알 수 없는 분석 오류') if analysis_result else '분석 결과 없음'
             logger.error(f"볼륨 분석 중 오류 발생: {error_message}")
             error_occurred = True
             # 오류 메시지를 스레드에 표시
             if channel_id and thread_ts and send_slack_message and SLACK_BOT_TOKEN:
                 send_slack_message(channel_id, f"분석 오류: {error_message}", SLACK_BOT_TOKEN, thread_ts)
             return {"success": False, "error": error_message}

        # 분석 성공 시 결과 처리
        logger.info(f"분석 완료. 결과: {analysis_result.get('summary') if not volume_id else analysis_result}")
        s3_location = "N/A"
        if not volume_id: # 전체 분석 결과만 S3 저장
             s3_location = save_result_to_s3(analysis_result)
             logger.info(f"전체 분석 결과 S3 저장 위치: {s3_location}")


        # Slack 결과 알림 - Use initial_message_ts for update
        if channel_id and SLACK_BOT_TOKEN and send_analysis_summary_to_slack:
             send_success, final_ts = send_analysis_summary_to_slack(
                 analysis_result=analysis_result,
                 channel_id=channel_id,
                 bot_token=SLACK_BOT_TOKEN,
                 message_ts_to_update=initial_message_ts # Use initial_ts for update
             )
             if send_success:
                 logger.info(f"Analysis result sent/updated to Slack. Final TS (or updated TS): {final_ts}")
             else:
                 logger.error("Failed to send/update analysis result to Slack.")
        elif not send_analysis_summary_to_slack:
             logger.warning("send_analysis_summary_to_slack function not available. Cannot send Slack notification.")

        return {
            "success": True,
            "message": "Volume analysis completed successfully.",
            "result_summary": analysis_result.get('summary') if not volume_id else analysis_result,
            "s3_location": s3_location if not volume_id else None
        }

    except Exception as e:
        logger.error(f"볼륨 분석 처리 중 예외 발생: {str(e)}", exc_info=True)
        # 오류 발생 시 Slack 알림 (스레드)
        if channel_id and thread_ts and send_slack_message and SLACK_BOT_TOKEN:
            send_slack_message(channel_id, f"분석 처리 중 오류가 발생했습니다: {str(e)}", SLACK_BOT_TOKEN, thread_ts)
        return {"success": False, "error": str(e)}

def format_single_volume_details(result):
    """단일 볼륨 분석 결과에서 상세 정보를 포맷팅합니다."""
    volume_id = result.get('volume_id', 'N/A')
    details_text = f"*볼륨 `{volume_id}` 세부 정보:*\n" # Start with newline
    volume_data = result.get('details') if isinstance(result.get('details'), dict) else result

    details_text += f"• 상태: {volume_data.get('state', '?')} | 유형: {volume_data.get('volume_type', '?')} | 크기: {volume_data.get('size', '?')} GB | 월비용: ${volume_data.get('monthly_cost', 0):.2f}\n"

    metrics = volume_data.get('metrics', {})
    if metrics:
        details_text += "*최근 메트릭:*\n"
        idle_pct = metrics.get('VolumeIdleTime_percent')
        if idle_pct is not None: details_text += f"  • 유휴 시간: {idle_pct:.2f}%\n"
        rops = metrics.get('VolumeReadOps')
        if rops is not None: details_text += f"  • 읽기 IOPS: {rops:.2f}\n"
        wops = metrics.get('VolumeWriteOps')
        if wops is not None: details_text += f"  • 쓰기 IOPS: {wops:.2f}\n"
        qlen = metrics.get('VolumeQueueLength')
        if qlen is not None: details_text += f"  • 대기열 길이: {qlen:.2f}\n"
        tpct = metrics.get('VolumeThroughputPercentage')
        if tpct is not None: details_text += f"  • 처리량 사용률: {tpct:.2f}%\n"
        burst = metrics.get('BurstBalance')
        if burst is not None: details_text += f"  • 버스트 밸런스: {burst:.2f}%\n"
    else:
        details_text += "*최근 메트릭 정보 없음*\n"

    idle_diag = result.get('idle_diagnosis') or volume_data.get('idle_check_details', {})
    if idle_diag:
        details_text += "*유휴 진단:*\n"
        details_text += f"  • 결과: {'유휴' if idle_diag.get('result') else '활성'}\n"
        if idle_diag.get('reason'): details_text += f"  • 근거: {idle_diag['reason']}\n"

    over_diag = result.get('overprovisioned_diagnosis') or volume_data.get('overprovisioned_check_details', {})
    if over_diag:
        details_text += "*과대 프로비저닝 진단:*\n"
        details_text += f"  • 결과: {'과대' if over_diag.get('result') else '적정'}\n"
        if over_diag.get('reason'): details_text += f"  • 근거: {over_diag['reason']}\n"

    return details_text


def process_execute_action(parameters, requested_by, channel_id, response_url=None, thread_ts=None):
    """
    볼륨에 대한 조치 실행 액션을 처리하고 결과를 Slack으로 알립니다.

    :param parameters: 실행 파라미터 (volume_id, region, action_type, message_ts 등 포함)
    :param requested_by: 요청자 ID
    :param channel_id: Slack 채널 ID
    :param response_url: Slack 응답 URL (현재 사용 안 함)
    :param thread_ts: 스레드 타임스탬프 (원본 메시지 업데이트 실패 시 사용)
    :return: 처리 결과 딕셔너리
    """
    if RecommendationExecutor is None:
        logger.error("RecommendationExecutor가 임포트되지 않았습니다.")
        return {"success": False, "error": "Executor not available"}
    if send_slack_message is None or send_execution_result_to_slack is None:
         logger.warning("Slack 메시징 함수 일부가 임포트되지 않았습니다.")
         # 진행은 가능하나 알림이 실패할 수 있음

    volume_id = parameters.get('volume_id')
    action_type = parameters.get('action_type') # 예: 'snapshot_and_delete', 'resize'
    region = parameters.get('region')
    message_ts = parameters.get('message_ts') # 업데이트할 원본 메시지 타임스탬프

    logger.info(f"볼륨 액션 실행 시작: vol={volume_id}, region={region}, action={action_type}, msg_ts={message_ts}")

    # 필수 파라미터 검증
    if not all([volume_id, action_type, region]):
        err_msg = f"볼륨 ID({volume_id}), 액션 유형({action_type}), 리전({region}) 정보가 모두 필요합니다."
        logger.error(err_msg)
        # 사용자에게 알림 (스레드)
        if channel_id and thread_ts and send_slack_message and SLACK_BOT_TOKEN:
            send_slack_message(channel_id, f"액션 실행 오류: {err_msg}", SLACK_BOT_TOKEN, thread_ts)
        return {"success": False, "error": err_msg}

    try:
        # Notify start (in thread)
        if channel_id and thread_ts and send_slack_message and SLACK_BOT_TOKEN:
            send_slack_message(
                channel_id,
                f"Starting execution of `{action_type}` for volume `{volume_id}`...",
                SLACK_BOT_TOKEN,
                thread_ts
            )

        # 실행 전 볼륨 정보 확인 (ebs_service 사용)
        # analyze_specific_volume을 호출하여 최신 상태와 상세 정보 확인
        if analyze_specific_volume is None: raise ImportError("analyze_specific_volume not available")
        volume_info = analyze_specific_volume(volume_id, region, detailed_report=True)

        if not volume_info or 'error' in volume_info:
            error_msg = volume_info.get('error', '볼륨 정보를 가져올 수 없습니다.')
            logger.error(f"액션 실행 전 볼륨 정보 확인 실패: {error_msg}")
            result_payload = {'success': False, 'error': error_msg, 'status_message': f'오류 (사전 확인): {error_msg}'}
            # 오류 알림 (원본 메시지 업데이트 또는 스레드 메시지)
            if channel_id and SLACK_BOT_TOKEN:
                # Use send_execution_result_to_slack for updates as well
                if message_ts and send_execution_result_to_slack:
                    send_execution_result_to_slack(
                        result=result_payload, 
                        volume_id=volume_id, 
                        action_type=action_type, 
                        channel_id=channel_id,
                        requested_by=requested_by, 
                        bot_token=SLACK_BOT_TOKEN, 
                        message_ts_to_update=message_ts # Pass message_ts for update
                    )
                elif thread_ts and send_slack_message:
                    send_slack_message(channel_id, f"액션 오류(사전 확인): {error_msg}", SLACK_BOT_TOKEN, thread_ts)
            return result_payload


        # 권장 조치 실행기 초기화
        try:
            executor = RecommendationExecutor(region)
        except Exception as exec_init_err:
            err_msg = f"RecommendationExecutor 초기화 실패 ({region}): {exec_init_err}"
            logger.error(err_msg, exc_info=True)
            if channel_id and thread_ts and send_slack_message and SLACK_BOT_TOKEN:
                 send_slack_message(channel_id, f"액션 오류(초기화): {err_msg}", SLACK_BOT_TOKEN, thread_ts)
            return {"success": False, "error": err_msg}

        # 액션 실행
        execution_result = None
        logger.info(f"RecommendationExecutor 호출: action_type={action_type}")

        # executor 함수가 volume_info (분석 결과)와 action_type을 받도록 가정
        idle_actions = ['snapshot_and_delete', 'snapshot_only', 'change_type'] # 유휴 관련 액션
        over_actions = ['resize'] # 과대 프로비저닝 관련 액션

        if action_type in idle_actions:
             execution_result = executor.execute_idle_volume_recommendation(volume_info, action_type)
        elif action_type in over_actions:
             execution_result = executor.execute_overprovisioned_volume_recommendation(volume_info, action_type)
        else:
            err_msg = f"Executor가 지원하지 않는 액션 유형입니다: {action_type}"
            logger.error(err_msg)
            execution_result = {'success': False, 'error': err_msg, 'status_message': f'오류: {err_msg}'}

        logger.info(f"액션 실행 결과: {execution_result}")

        # 결과 알림 (원본 메시지 업데이트 또는 스레드 결과 표시)
        if channel_id and SLACK_BOT_TOKEN:
            # Use send_execution_result_to_slack for updates
            if message_ts and send_execution_result_to_slack:
                # 원본 메시지 업데이트 시도
                update_success = send_execution_result_to_slack(
                    result=execution_result, 
                    volume_id=volume_id, 
                    action_type=action_type, 
                    channel_id=channel_id,
                    requested_by=requested_by, 
                    bot_token=SLACK_BOT_TOKEN, 
                    message_ts_to_update=message_ts # Pass message_ts for update
                )
                logger.info(f"Slack 원본 메시지 업데이트 시도 결과: {update_success}")
                # 업데이트 실패 시 스레드에 결과 전송 (이미 send_execution_result_to_slack 에서 처리할 수 있음, 로깅만 남김)
                if not update_success:
                     logger.warning(f"원본 메시지({message_ts}) 업데이트 실패 또는 응답 없음")
            elif thread_ts and send_execution_result_to_slack:
                 # message_ts가 없거나 업데이트 함수가 없으면 스레드에 결과 전송
                 send_execution_result_to_slack(
                     execution_result, volume_id, action_type, channel_id,
                     requested_by, SLACK_BOT_TOKEN, thread_ts
                 )

        # 최종 결과 반환
        return {
            "success": execution_result.get('success', False),
            "message": execution_result.get('status_message', f"Volume {volume_id} action ({action_type}) processed."),
            "result": execution_result # 상세 결과 포함
        }

    except ImportError as imp_err:
         logger.error(f"필수 모듈 임포트 실패: {imp_err}")
         return {"success": False, "error": f"Import error: {imp_err}"}
    except Exception as e:
        logger.error(f"볼륨 액션 실행 중 예외 발생: vol={volume_id}, action={action_type}, error={str(e)}", exc_info=True)
        error_payload = {'success': False, 'error': str(e), 'status_message': f'예외 발생: {str(e)}'}
        # 오류 발생 시 Slack 알림 (원본 메시지 업데이트 또는 스레드)
        if channel_id and SLACK_BOT_TOKEN:
             message_ts = parameters.get('message_ts')
             # Use send_execution_result_to_slack for updates
             if message_ts and send_execution_result_to_slack:
                 send_execution_result_to_slack(
                     result=error_payload, 
                     volume_id=volume_id, 
                     action_type=action_type, 
                     channel_id=channel_id,
                     requested_by=requested_by, 
                     bot_token=SLACK_BOT_TOKEN, 
                     message_ts_to_update=message_ts # Pass message_ts for update
                 )
             elif thread_ts and send_slack_message:
                 send_slack_message(
                     channel_id,
                     f"볼륨 `{volume_id}` 액션 (`{action_type}`) 실행 중 오류: {str(e)}",
                     SLACK_BOT_TOKEN,
                     thread_ts
                 )
        return error_payload


def process_volume_action(action_type, parameters, requested_by, channel_id, response_url=None, thread_ts=None):
    """
    이전 방식의 볼륨 액션 타입 (예: idle_volume_action)을 처리합니다.
    내부적으로 실제 실행 액션 타입 (예: snapshot_and_delete)을 결정하고
    process_execute_action을 호출합니다.

    :param action_type: 레거시 액션 유형 (e.g., 'idle_volume_action')
    :param parameters: 액션 파라미터 (volume_id, region, message_ts 등 포함)
    :param requested_by: 요청자 ID
    :param channel_id: Slack 채널 ID
    :param response_url: Slack 응답 URL
    :param thread_ts: 스레드 타임스탬프
    :return: 처리 결과 딕셔너리
    """
    logger.warning(f"레거시 액션 타입 처리: {action_type}")

    volume_id = parameters.get('volume_id')
    region = parameters.get('region')
    # 레거시 타입에서 실제 실행 액션 결정 필요
    # 'idle_volume_action' -> 'snapshot_and_delete' 또는 'change_type' (기본값은 snapshot_and_delete?)
    # 'overprovisioned_volume_action' -> 'resize'
    actual_action_type = None
    if action_type == 'idle_volume_action':
         # 기본적으로 snapshot_and_delete 로 가정. 더 정교한 로직이 필요하면 추가.
         actual_action_type = parameters.get('action_type', 'snapshot_and_delete')
    elif action_type == 'overprovisioned_volume_action':
         actual_action_type = parameters.get('action_type', 'resize')

    if not actual_action_type:
         err_msg = f"레거시 액션 타입 '{action_type}'에서 실제 실행할 액션을 결정할 수 없습니다."
         logger.error(err_msg)
         return {"success": False, "error": err_msg}

    # process_execute_action 호출을 위해 파라미터 업데이트
    updated_parameters = parameters.copy()
    updated_parameters['action_type'] = actual_action_type # 실제 실행 타입으로 설정

    logger.info(f"레거시 타입 {action_type} -> 실제 실행 타입 {actual_action_type}으로 변환하여 처리")
    return process_execute_action(updated_parameters, requested_by, channel_id, response_url, thread_ts)

# --- End of moved functions --- 