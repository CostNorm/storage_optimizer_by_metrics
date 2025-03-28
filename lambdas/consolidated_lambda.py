import os
import json
import boto3
import logging
import requests
import sys
import time
import hmac
import hashlib
import base64
from pathlib import Path
from datetime import datetime
from urllib.parse import parse_qs

# 루트 디렉토리를 Python 경로에 추가
root_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(root_dir))

from dotenv import load_dotenv
# 수정된 import 경로들 - 프로젝트 구조에 맞게 조정
from utils.utils import calculate_monthly_cost, get_tags_as_dict, format_bytes
from ebs.analyzer.ebs_analyzer import EBSAnalyzer
from config.config import REGIONS, S3_BUCKET_NAME
from integrations.slack.slack_messenger import (
    send_slack_blocks,
    send_slack_error,
    send_slack_message,
    send_analysis_result_to_slack,
    send_execution_result_to_slack,
)
from utils.sqs_helper import enqueue_action
from ebs.actions.recommendation_executor import RecommendationExecutor

# 환경 변수 로드
load_dotenv()

# 로깅 설정
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 환경 변수에서 필요한 값 가져오기
SLACK_WEBHOOK_URL = os.environ.get('SLACK_WEBHOOK_URL')
SLACK_SIGNING_SECRET = os.environ.get('SLACK_SIGNING_SECRET')
SQS_QUEUE_URL = os.environ.get('SQS_QUEUE_URL')
SLACK_VERIFICATION_TOKEN = os.environ.get('SLACK_VERIFICATION_TOKEN')  # 이전 방식(선택적)
SLACK_BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN')  # Slack Bot 토큰 추가
LAMBDA_FUNCTION_NAME = os.environ.get('AWS_LAMBDA_FUNCTION_NAME')  # 현재 람다 함수 이름

# Lambda 클라이언트 초기화 (전역 변수로 재사용)
try:
    LAMBDA_CLIENT = boto3.client('lambda')
except Exception as e:
    logger.error(f"Lambda 클라이언트 초기화 중 오류 발생: {str(e)}")
    LAMBDA_CLIENT = None

def lambda_handler(event, context):
    """
    통합 Lambda 핸들러 - 이벤트 유형에 따라 적절한 처리 로직으로 분기
    
    :param event: Lambda 이벤트
    :param context: Lambda 컨텍스트
    :return: 적절한 응답 객체
    """
    logger.info("EBS 스토리지 최적화 Lambda 함수 시작")
    
    try:
        # 비동기 처리를 위한 내부 호출 여부 확인
        is_async_processing = event.get('__async_processing', False)
        
        # 이벤트 유형 결정
        event_type = determine_event_type(event)
        logger.info(f"이벤트 유형: {event_type}")
        
        # 비동기 호출이면 실제 처리 수행
        if is_async_processing:
            # 비동기 처리 모드에서 원래 이벤트 타입에 따른 처리
            if event_type == "analyze_request":
                return handle_analyze_request(event, context)
            elif event_type == "slack_request":
                return process_slack_request_async(event, context)
            elif event_type == "sqs_message":
                return handle_sqs_message(event, context)
            else:
                logger.error(f"지원되지 않는 이벤트 타입: {event_type}")
                return {
                    "statusCode": 400,
                    "body": json.dumps({
                        "error": f"지원되지 않는 이벤트 타입: {event_type}"
                    })
                }
        
        # 일반 호출 처리
        if event_type == "analyze_request":
            # 분석 요청은 기존과 동일하게 처리 (이미 동기 처리가 필요)
            return handle_analyze_request(event, context)
        elif event_type == "slack_request":
            # Slack 요청은 즉시 응답 후 비동기 처리
            return handle_slack_request_initial(event, context)
        elif event_type == "sqs_message":
            # SQS 메시지는 기존과 동일하게 처리 (이미 큐에서 가져온 요청 처리)
            return handle_sqs_message(event, context)
        else:
            logger.error(f"지원되지 않는 이벤트 타입: {event_type}")
            return {
                "statusCode": 400,
                "body": json.dumps({
                    "error": f"지원되지 않는 이벤트 타입: {event_type}"
                })
            }
    
    except Exception as e:
        logger.error(f"Lambda 실행 중 오류 발생: {str(e)}", exc_info=True)
        
        # 오류 발생 시에도 Slack에 알림
        try:
            if SLACK_WEBHOOK_URL:
                send_slack_error(SLACK_WEBHOOK_URL, str(e))
        except:
            pass
        
        return {
            "statusCode": 500,
            "body": json.dumps({
                "message": "처리 중 오류가 발생했습니다.",
                "error": str(e)
            })
        }

def determine_event_type(event):
    """
    Lambda 이벤트의 유형 결정
    
    :param event: Lambda 이벤트
    :return: 이벤트 유형 문자열
    """
    # SQS 이벤트 확인
    if 'Records' in event and any(record.get('eventSource') == 'aws:sqs' for record in event['Records']):
        return "sqs_message"
    
    # API Gateway를 통한 Slack 요청 확인
    if 'body' in event and ('headers' in event or event.get('isBase64Encoded', False)):
        return "slack_request"
    
    # 그 외의 경우는 분석 요청으로 취급 (직접 호출 또는 CloudWatch Events)
    return "analyze_request"

# 기존 Lambda 함수들의 기능을 각 핸들러 함수로 통합
def handle_analyze_request(event, context):
    """
    EBS 볼륨 분석 요청 처리 - analyze_and_notify_lambda의 기능 구현
    
    :param event: Lambda 이벤트
    :param context: Lambda 컨텍스트
    :return: 처리 결과
    """
    logger.info("EBS 스토리지 최적화 분석 시작")
    
    # 이벤트에서 파라미터 추출
    volume_id = event.get('volume_id')
    specific_region = event.get('region')
    output_format = event.get('output_format', 'both')  # 기본값은 'both'
    detailed_report = event.get('detailed_report', False)
    channel_id = event.get('channel_id')  # Slack 채널 ID (있는 경우)
    
    # 특정 볼륨 ID가 제공된 경우 처리 로직
    if volume_id:
        result = analyze_specific_volume(volume_id, specific_region, detailed_report)
    else:
        # 기존 로직 - 전체 리전 분석
        result = analyze_all_regions(event, detailed_report)
    
    # 분석 결과를 S3에 저장
    s3_location = save_result_to_s3(result)
    
    # 출력 형식에 따라 응답 생성
    if output_format in ['slack', 'both']:
        # Slack 알림 전송
        if channel_id:
            # 특정 채널로 전송
            send_analysis_result_to_slack(result, channel_id, SLACK_BOT_TOKEN)
        else:
            # 기본 웹훅으로 전송
            slack_response = send_to_slack(result)
            logger.info(f"Slack 알림 전송 결과: {slack_response}")
    
    # 응답 반환
    response = {
        "statusCode": 200,
        "body": json.dumps({
            "message": "EBS 볼륨 최적화 분석이 성공적으로 완료되었습니다.",
            "result_location": s3_location,
            "summary": result["summary"] if "summary" in result else {}
        })
    }
    
    return response

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
    # 상세 검증은 실제 처리 단계에서 수행
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

def process_slack_request_async(event, context):
    """
    비동기 모드에서 Slack 요청을 실제로 처리합니다.
    
    :param event: Lambda 이벤트 (비동기 처리 플래그 포함)
    :param context: Lambda 컨텍스트
    :return: 처리 결과
    """
    logger.info("비동기 모드에서 Slack 요청 처리 시작")
    
    try:
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
                action_data["thread_ts"] = payload.get('container', {}).get('thread_ts')  # 스레드 ID 추가
        
        elif "command" in parse_qs(body):
            # 슬래시 커맨드
            params = parse_qs(body)
            command = params.get('command', [''])[0]
            text = params.get('text', [''])[0]
            
            logger.info(f"슬래시 커맨드 감지: {command}, 텍스트: '{text}'")
            
            action_data["interaction_type"] = "slash_command"
            action_data["command"] = command
            action_data["text"] = text
            action_data["action_type"] = "slash_command"  # 새로 추가
            action_data["ActionType"] = "slash_command"  # 기존 필드 유지
            
            # 사용자 및 채널 정보 추가
            requested_by = params.get('user_id', ['unknown'])[0]
            channel_id = params.get('channel_id', [''])[0]
            action_data["requested_by"] = requested_by
            action_data["channel_id"] = channel_id
            action_data["response_url"] = params.get('response_url', [''])[0]
            
            # 명령어 파싱 - 파라미터 추출하고 로그
            parameters = parse_command_parameters(text)
            action_data["parameters"] = parameters
            logger.info(f"추출된 명령어 파라미터: {parameters}")
            
            # 원본 명령어를 채팅에 표시하고 스레드 생성
            if channel_id and SLACK_BOT_TOKEN:
                from integrations.slack.slack_messenger import send_original_command
                success, thread_ts = send_original_command(
                    channel_id,
                    command,
                    text,
                    SLACK_BOT_TOKEN,
                    requested_by
                )
                
                if success and thread_ts:
                    # 생성된 스레드 ID 저장 (이후 응답을 위해)
                    action_data["thread_ts"] = thread_ts
                    logger.info(f"원본 명령어 메시지 전송 및 스레드 생성 완료: {thread_ts}")
        
        # 4. SQS에 메시지 전송
        logger.info(f"SQS에 전송할 액션 데이터: {action_data}")
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

def handle_sqs_message(event, context):
    """
    SQS 메시지 처리 - action_executor_lambda의 기능 구현
    
    :param event: Lambda SQS 이벤트
    :param context: Lambda 컨텍스트
    :return: 처리 결과
    """
    logger.info("SQS 메시지 처리 시작")
    
    results = {
        "processed": 0,
        "succeeded": 0,
        "failed": 0,
        "details": []
    }
    
    # SQS 이벤트에서 메시지 처리
    if 'Records' not in event:
        logger.error("SQS 레코드가 이벤트에 없습니다.")
        return {
            "statusCode": 400,
            "body": "No SQS records in event"
        }
        
    for record in event['Records']:
        logger.info(f"메시지 처리 중: {record['messageId']}")
        
        try:
            # 메시지 본문에서 액션 데이터 추출
            action_data = json.loads(record['body'])
            
            # Slack 재시도 메시지인 경우 건너뜀 (중복 처리 방지)
            print("action_data: ", action_data)
            if is_retry_request(action_data):
                logger.info(f"Slack 재시도 요청 감지, 메시지 건너뜀: {record['messageId']}")
                results["details"].append({
                    "message_id": record['messageId'],
                    "status": "skipped",
                    "reason": "slack_retry"
                })
                continue
            
            # action_type과 ActionType 중 하나가 있으면 일관성을 위해 둘 다 설정
            if 'ActionType' in action_data and 'action_type' not in action_data:
                action_data['action_type'] = action_data['ActionType']
            elif 'action_type' in action_data and 'ActionType' not in action_data:
                action_data['ActionType'] = action_data['action_type']
            
            logger.info(f"액션 처리: action_type={action_data.get('action_type')}, ActionType={action_data.get('ActionType')}")
            
            # 액션 처리
            result = process_action(action_data)
            
            # 처리 결과 저장
            results["processed"] += 1
            if result.get('success', False):
                results["succeeded"] += 1
            else:
                results["failed"] += 1
            
            results["details"].append({
                "message_id": record['messageId'],
                "action_type": action_data.get('action_type'),
                "result": result
            })
            
        except Exception as e:
            logger.error(f"메시지 처리 중 오류 발생: {str(e)}", exc_info=True)
            results["processed"] += 1
            results["failed"] += 1
            results["details"].append({
                "message_id": record['messageId'] if 'messageId' in record else 'unknown',
                "error": str(e)
            })
    
    return {
        "statusCode": 200,
        "body": json.dumps(results)
    }

# 기존 함수들을 통합 Lambda에서 사용할 수 있도록 추가
# analyze_and_notify_lambda.py에서 가져온 함수들
def analyze_specific_volume(volume_id, region=None, detailed_report=False):
    """
    특정 볼륨 ID에 대한 분석을 수행합니다.
    
    :param volume_id: 분석할 볼륨 ID
    :param region: 볼륨이 위치한 리전 (없으면 첫 번째 리전 사용)
    :param detailed_report: 상세 보고서 여부
    :return: 분석 결과
    """
    # 리전이 지정되지 않은 경우 첫 번째 리전 사용
    target_region = region if region else REGIONS[0]
    
    logger.info(f"특정 볼륨 분석 시작 - 볼륨 ID: {volume_id}, 리전: {target_region}")
    
    # EBS 분석기 초기화
    analyzer = EBSAnalyzer(target_region)
    
    try:
        # 특정 볼륨 분석
        volume_result = analyzer.analyze_specific_volume(volume_id)
        
        # 오류 확인
        if 'error' in volume_result:
            logger.error(f"볼륨 {volume_id} 분석 중 오류: {volume_result['error']}")
            return {
                "timestamp": datetime.now().isoformat(),
                "volume_id": volume_id,
                "region": target_region,
                "error": volume_result['error'],
                "status": "오류 발생",
                "recommendation": "볼륨 정보를 가져올 수 없습니다. 볼륨 ID가 올바른지, 해당 리전에 존재하는지 확인하세요."
            }
        
        # 분석 결과 포맷팅
        formatted_result = {
            "timestamp": datetime.now().isoformat(),
            "volume_id": volume_id,
            "region": target_region,
            "is_idle": volume_result.get('is_idle', False),
            "is_overprovisioned": volume_result.get('is_overprovisioned', False),
            "recommendation": volume_result.get('recommendation', '해당 없음'),
            "status": volume_result.get('status', '알 수 없음'),
            "details": volume_result if detailed_report else {}
        }
        
        # 디버깅을 위한 분석 상세 정보
        if 'idle_check_details' in volume_result:
            formatted_result['idle_diagnosis'] = volume_result['idle_check_details']
        
        if 'overprovisioned_check_details' in volume_result:
            formatted_result['overprovisioned_diagnosis'] = volume_result['overprovisioned_check_details']
        
        # 권장 조치에 따라 작업 정의
        if volume_result.get('is_idle', False):
            formatted_result["suggested_action"] = "idle_volume_action"
            formatted_result["action_params"] = {
                "volume_id": volume_id,
                "region": target_region,
                "action_type": "snapshot_and_delete" if "스냅샷 생성 후 볼륨 삭제" in volume_result.get('recommendation', '') else "change_type"
            }
        elif volume_result.get('is_overprovisioned', False):
            formatted_result["suggested_action"] = "overprovisioned_volume_action"
            formatted_result["action_params"] = {
                "volume_id": volume_id,
                "region": target_region,
                "action_type": "resize"
            }
        else:
            formatted_result["suggested_action"] = "none"
            formatted_result["action_params"] = {}
        
        return formatted_result
    except Exception as e:
        logger.error(f"볼륨 {volume_id} 분석 중 예외 발생: {str(e)}", exc_info=True)
        return {
            "timestamp": datetime.now().isoformat(),
            "volume_id": volume_id,
            "region": target_region,
            "error": str(e),
            "status": "예외 발생",
            "recommendation": "볼륨 분석 중 예기치 않은 오류가 발생했습니다."
        }

def analyze_all_regions(event, detailed_report=False):
    """
    모든 리전의 모든 볼륨을 분석합니다.
    
    :param event: Lambda 이벤트 객체
    :param detailed_report: 상세 보고서 여부
    :return: 분석 결과
    """
    # 전체 분석 결과를 저장할 딕셔너리
    all_results = {
        "timestamp": datetime.now().isoformat(),
        "regions": {},
        "summary": {
            "total_idle_volumes": 0,
            "total_overprovisioned_volumes": 0,
            "total_estimated_savings": 0,
            "suggested_actions": []
        }
    }
    
    # 설정된 각 리전에 대해 분석 실행
    for region in REGIONS:
        logger.info(f"{region} 리전에 대한 EBS 볼륨 분석 시작")
        
        # EBS 분석기 초기화
        analyzer = EBSAnalyzer(region)
        
        # 분석 실행
        region_results = analyzer.analyze_volumes()
        
        # 유휴 볼륨 및 과대 프로비저닝 볼륨 개수 저장
        idle_count = len(region_results.get('idle_volumes', []))
        over_count = len(region_results.get('overprovisioned_volumes', []))
        
        # 리전별 요약 정보
        all_results["regions"][region] = {
            "idle_volumes_count": idle_count,
            "overprovisioned_volumes_count": over_count,
            "total_volumes": region_results.get('total_volumes', 0)
        }
        
        # 상세 보고서가 요청된 경우 상세 정보 추가
        if detailed_report:
            all_results["regions"][region]["details"] = region_results
        
        # 요약 정보에 추가
        all_results["summary"]["total_idle_volumes"] += idle_count
        all_results["summary"]["total_overprovisioned_volumes"] += over_count
        
        # 권장 조치 추가
        for volume in region_results.get('idle_volumes', []):
            # 예상 절감액 계산
            monthly_cost = volume.get('monthly_cost', 0)
            all_results["summary"]["total_estimated_savings"] += monthly_cost
            
            # 조치 추가
            all_results["summary"]["suggested_actions"].append({
                "volume_id": volume.get('volume_id'),
                "region": region,
                "action_type": "idle_volume_action",
                "action_params": {
                    "volume_id": volume.get('volume_id'),
                    "region": region,
                    "action_type": "snapshot_and_delete" if "스냅샷 생성 후 볼륨 삭제" in volume.get('recommendation', '') else "change_type"
                },
                "estimated_savings": monthly_cost,
                "recommendation": volume.get('recommendation', '')
            })
        
        for volume in region_results.get('overprovisioned_volumes', []):
            # 예상 절감액 계산
            estimated_savings = volume.get('estimated_savings', 0)
            all_results["summary"]["total_estimated_savings"] += estimated_savings
            
            # 조치 추가
            all_results["summary"]["suggested_actions"].append({
                "volume_id": volume.get('volume_id'),
                "region": region,
                "action_type": "overprovisioned_volume_action",
                "action_params": {
                    "volume_id": volume.get('volume_id'),
                    "region": region,
                    "action_type": "resize"
                },
                "estimated_savings": estimated_savings,
                "recommendation": volume.get('recommendation', '')
            })
    
    # 예상 절감액 소수점 두 자리로 반올림
    all_results["summary"]["total_estimated_savings"] = round(all_results["summary"]["total_estimated_savings"], 2)
    
    return all_results

def save_result_to_s3(result):
    """
    분석 결과를 S3에 저장합니다.
    
    :param result: 분석 결과
    :return: S3 위치
    """
    # 현재 분석 날짜/시간
    current_time = datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
    
    try:
        # S3 클라이언트 생성
        s3_client = boto3.client('s3')
        
        # 주요 결과를 JSON 파일로 저장
        s3_key = f"ebs-analysis-results-{current_time}.json"
        
        s3_client.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=s3_key,
            Body=json.dumps(result, indent=2),
            ContentType='application/json'
        )
        
        logger.info(f"분석 결과가 S3에 저장되었습니다: s3://{S3_BUCKET_NAME}/{s3_key}")
        
        return f"s3://{S3_BUCKET_NAME}/{s3_key}"
    
    except Exception as e:
        logger.error(f"결과를 S3에 저장하는 중 오류 발생: {str(e)}")
        return "S3 저장 실패"

def send_to_slack(result, channel_id=None):
    """
    분석 결과를 Slack으로 전송합니다.
    
    :param result: 분석 결과
    :param channel_id: Slack 채널 ID (있는 경우)
    :return: Slack API 응답
    """
    if not SLACK_WEBHOOK_URL and not (channel_id and SLACK_BOT_TOKEN):
        logger.warning("Slack 웹훅 URL이나 봇 토큰이 설정되지 않았습니다. Slack 알림을 건너뜁니다.")
        return {"skipped": True, "reason": "No webhook URL or bot token configured"}
    
    try:
        # 요약 정보 추출
        summary = result.get("summary", {})
        idle_volumes = summary.get("total_idle_volumes", 0)
        over_volumes = summary.get("total_overprovisioned_volumes", 0)
        savings = summary.get("total_estimated_savings", 0)
        
        # Slack Block Kit 메시지 생성
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "EBS 볼륨 최적화 분석 결과",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*분석 완료 시간:* {result.get('timestamp')}"
                }
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*유휴 볼륨:* {idle_volumes}개"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*과대 프로비저닝 볼륨:* {over_volumes}개"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*예상 월간 절감액:* ${savings}"
                    }
                ]
            },
            {
                "type": "divider"
            }
        ]
        
        # 권장 조치가 있는 경우 추가
        actions = summary.get("suggested_actions", [])
        if actions:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*권장 조치*"
                }
            })
            
            # 최대 10개의 권장 조치만 표시
            for i, action in enumerate(actions[:10]):
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*{i+1}.* 볼륨 `{action['volume_id']}` ({action['region']})\n"
                                f"• 조치: {action['action_type']}\n"
                                f"• 추천: {action['recommendation']}\n"
                                f"• 예상 절감액: ${action['estimated_savings']:.2f}/월"
                    },
                    "accessory": {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "조치 실행",
                            "emoji": True
                        },
                        "value": json.dumps(action['action_params']),
                        "action_id": f"execute_{action['action_params']['action_type']}"
                    }
                })
            
            # 표시되지 않은 조치가 있는 경우 안내
            if len(actions) > 10:
                blocks.append({
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"*추가 {len(actions) - 10}개의 권장 조치가 있습니다. 상세 보고서를 확인하세요.*"
                        }
                    ]
                })
        
        # 메시지 전송
        text = f"EBS 볼륨 최적화 분석 결과: 유휴 {idle_volumes}개, 과대 프로비저닝 {over_volumes}개, 예상 절감액 ${savings}/월"
        
        if channel_id and SLACK_BOT_TOKEN:
            # Bot 토큰을 사용하여 특정 채널에 메시지 전송
            response = requests.post(
                "https://slack.com/api/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                    "Content-Type": "application/json"
                },
                json={
                    "channel": channel_id,
                    "blocks": blocks,
                    "text": text
                }
            )
            
            if response.status_code != 200 or not response.json().get('ok', False):
                logger.error(f"Slack API로 메시지 전송 실패: {response.status_code} {response.text}")
                return {"success": False, "status_code": response.status_code, "response": response.text}
        else:
            # Webhook URL을 사용하여 메시지 전송
            payload = {
                "blocks": blocks,
                "text": text
            }
            
            response = requests.post(
                SLACK_WEBHOOK_URL,
                json=payload,
                headers={"Content-Type": "application/json"}
            )
            
            if response.status_code != 200:
                logger.error(f"Slack으로 메시지 전송 실패: {response.status_code} {response.text}")
                return {"success": False, "status_code": response.status_code, "response": response.text}
        
        return {"success": True}
    
    except Exception as e:
        logger.error(f"Slack 알림 전송 중 오류 발생: {str(e)}", exc_info=True)
        return {"success": False, "error": str(e)}

# action_request_lambda.py에서 가져온 함수들
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

def parse_slack_request(body):
    """
    Slack 요청을 파싱하여 액션 데이터로 변환합니다.
    
    :param body: Slack 요청 본문
    :return: 액션 데이터 딕셔너리
    """
    action_data = {
        "event_type": "slack_interaction",
        "timestamp": int(time.time())
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
                action_data["action_type"] = action_id.replace('execute_', '')
                
                # 액션 파라미터 추출
                action_data["parameters"] = json.loads(payload['actions'][0].get('value', '{}'))
                
                # 사용자 및 채널 정보 추가
                action_data["requested_by"] = payload.get('user', {}).get('id', 'unknown')
                action_data["channel_id"] = payload.get('channel', {}).get('id')
                action_data["response_url"] = payload.get('response_url')
                
                # 스레드 정보 추출 - 메시지가 스레드의 일부인 경우
                # 메인 메시지의 ts는 container > message_ts에 있고
                # 스레드 메시지는 container > thread_ts에도 있음
                container = payload.get('container', {})
                message_ts = container.get('message_ts')
                thread_ts = payload.get('message', {}).get('thread_ts') or container.get('thread_ts')
                
                # 메시지가 이미 스레드인 경우 thread_ts를 사용하고, 
                # 그렇지 않은 경우 message_ts를 스레드 시작점으로 사용
                action_data["thread_ts"] = thread_ts or message_ts
                
                logger.info(f"스레드 정보 추출: thread_ts={action_data.get('thread_ts')}")
    
    elif "command" in parse_qs(body):
        # 슬래시 커맨드
        params = parse_qs(body)
        command = params.get('command', [''])[0]
        text = params.get('text', [''])[0]
        
        action_data["interaction_type"] = "slash_command"
        action_data["command"] = command
        action_data["text"] = text
        action_data["action_type"] = "slash_command"
        
        # 사용자 및 채널 정보 추가
        action_data["requested_by"] = params.get('user_id', ['unknown'])[0]
        action_data["channel_id"] = params.get('channel_id', [''])[0]
        action_data["response_url"] = params.get('response_url', [''])[0]
        
        # 명령어 파싱
        action_data["parameters"] = parse_command_parameters(text)
    
    return action_data

def parse_command_parameters(text):
    """
    슬래시 커맨드 텍스트를 파라미터로 파싱합니다.
    
    :param text: 슬래시 커맨드 텍스트
    :return: 파라미터 딕셔너리
    """
    params = {}
    words = text.split()
    
    # 디버그 로그 추가
    logger.info(f"슬래시 커맨드 파싱 시작. 텍스트: '{text}', 단어 갯수: {len(words)}")
    
    if not words:
        logger.warning("파싱할 커맨드 텍스트가 없습니다.")
        return params
    
    # 첫 번째 단어는 하위 명령어로 취급
    command = words[0].lower()
    logger.info(f"감지된 명령어: {command}")
    
    if command == "analyze":
        params["action_type"] = "analyze"
        
        # 두 번째 단어가 있으면 볼륨 ID로 취급
        if len(words) > 1 and words[1].startswith("vol-"):
            params["volume_id"] = words[1]
            logger.info(f"볼륨 ID 감지: {params['volume_id']}")
        
        # 리전 파라미터 확인 (--region=xxx 형식)
        for word in words[2:]:
            if word.startswith("--region="):
                params["region"] = word.split("=")[1]
                logger.info(f"리전 감지: {params['region']}")
            elif word == "--detailed":
                params["detailed_report"] = True
                logger.info("상세 보고서 옵션 활성화됨")
    
    elif command == "execute":
        params["action_type"] = "execute"
        
        # 볼륨 ID와 액션 타입 필요
        if len(words) >= 3:
            # 두 번째 단어가 볼륨 ID인지 확인
            if words[1].startswith("vol-"):
                params["volume_id"] = words[1]
                logger.info(f"볼륨 ID 감지: {params['volume_id']}")
                # 세 번째 단어는 액션 타입
                params["action_type"] = words[2]
                logger.info(f"액션 타입 감지: {params['action_type']}")
        
        # 리전 파라미터 확인
        for word in words[3:]:
            if word.startswith("--region="):
                params["region"] = word.split("=")[1]
                logger.info(f"리전 감지: {params['region']}")
    
    logger.info(f"파싱된 파라미터: {params}")
    return params

# action_executor_lambda.py에서 가져온 함수들
def is_retry_request(action_data):
    """
    Slack의 재시도 요청인지 확인합니다.
    
    :param action_data: 액션 데이터
    :return: 재시도 여부
    """
    if 'raw_event' not in action_data:
        return False
        
    headers = action_data.get('raw_event', {}).get('headers', {})
    return 'x-slack-retry-num' in headers or 'X-Slack-Retry-Num' in headers

def process_action(action_data):
    """
    액션 데이터를 처리합니다.
    
    :param action_data: 액션 데이터
    :return: 처리 결과
    """
    action_type = action_data.get('action_type')
    parameters = action_data.get('parameters', {})
    text = action_data.get('text', '')
    requested_by = action_data.get('requested_by', 'unknown')
    channel_id = action_data.get('channel_id')
    response_url = action_data.get('response_url')
    thread_ts = action_data.get('thread_ts')  # 스레드 타임스탬프 추출
    
    logger.info(f"액션 처리 중: {action_type}, 파라미터: {parameters}, 스레드 TS: {thread_ts}")
    
    # 파라미터가 비어 있고, slash_command 타입이면 텍스트에서 다시 파싱 시도
    if action_type == 'slash_command' and not parameters and text:
        logger.info(f"파라미터가 비어 있어 텍스트에서 다시 파싱 시도: '{text}'")
        parameters = parse_command_parameters(text)
        logger.info(f"재파싱된 파라미터: {parameters}")
    
    # 직접적인 액션 타입 처리 (snapshot_and_delete, snapshot_only, resize, change_type)
    direct_actions = ['snapshot_and_delete', 'snapshot_only', 'resize', 'change_type']
    if action_type in direct_actions:
        # 파라미터가 없는 경우 처리
        if not parameters:
            parameters = {
                'volume_id': action_data.get('volume_id'),
                'region': action_data.get('region', 'ap-northeast-2'),  # 기본값 설정
                'action_type': action_type
            }
        # 직접 실행 함수 호출
        return process_execute_action(parameters, requested_by, channel_id, response_url, thread_ts)
    
    # 기존 액션 유형에 따른 처리
    if action_type == 'analyze':
        return process_analyze_action(parameters, requested_by, channel_id, thread_ts)
    elif action_type == 'execute':
        return process_execute_action(parameters, requested_by, channel_id, response_url, thread_ts)
    elif action_type == 'slash_command':
        return process_slash_command(action_data)
    elif action_type and (action_type.startswith('idle_volume_') or action_type.startswith('overprovisioned_volume_')):
        return process_volume_action(action_type, parameters, requested_by, channel_id, response_url, thread_ts)
    else:
        logger.error(f"지원되지 않는 액션 유형: {action_type}")
        return {
            "success": False,
            "error": f"지원되지 않는 액션 유형: {action_type}"
        }

def process_analyze_action(parameters, requested_by, channel_id, thread_ts=None):
    """
    볼륨 분석 액션을 처리합니다.
    
    :param parameters: 분석 파라미터
    :param requested_by: 요청자 ID
    :param channel_id: Slack 채널 ID
    :param thread_ts: 스레드 타임스탬프
    :return: 처리 결과
    """
    try:
        logger.info(f"볼륨 분석 시작, 파라미터: {parameters}")
        
        volume_id = parameters.get('volume_id')
        region = parameters.get('region')
        detailed_report = parameters.get('detailed_report', True)
        
        # 분석 시작 알림을 스레드에 표시
        if channel_id and thread_ts:
            if volume_id:
                send_slack_message(
                    channel_id, 
                    f"볼륨 `{volume_id}` 분석이 진행 중입니다...", 
                    SLACK_BOT_TOKEN, 
                    thread_ts
                )
            else:
                send_slack_message(
                    channel_id, 
                    f"전체 EBS 볼륨 분석이 진행 중입니다...", 
                    SLACK_BOT_TOKEN, 
                    thread_ts
                )
        
        # 여기서 실제 분석 실행
        if volume_id:
            result = analyze_specific_volume(volume_id, region, detailed_report)
            
            # 분석 중 오류가 발생한 경우
            if 'error' in result:
                error_message = f"볼륨 {volume_id} 분석 중 오류가 발생했습니다: {result['error']}"
                logger.error(error_message)
                
                # 오류 메시지를 스레드에 표시
                if channel_id and thread_ts:
                    send_slack_message(
                        channel_id,
                        error_message,
                        SLACK_BOT_TOKEN,
                        thread_ts
                    )
                
                return {
                    "success": False,
                    "error": result['error'],
                    "message": error_message
                }
            
            # Slack 채널에 메시지 전송
            if channel_id:
                # 분석 결과를 메인 채팅에 표시 (use_thread=False로 설정)
                from integrations.slack.slack_messenger import send_analysis_result_to_slack
                success, msg_ts = send_analysis_result_to_slack(
                    result, 
                    channel_id, 
                    SLACK_BOT_TOKEN, 
                    thread_ts=None,  # 메인 채팅에 표시
                    use_thread=False
                )
                
                # 세부 분석 정보는 스레드에 표시
                if thread_ts:
                    # 세부 정보 텍스트 구성
                    details_text = f"*볼륨 {volume_id}의 세부 분석 정보:*\n\n"
                    
                    # 볼륨 기본 정보 추가
                    volume_details = result.get('details', {})
                    
                    # 볼륨 상태 정보 추가
                    details_text += f"*볼륨 상태:* {volume_details.get('state', '알 수 없음')}\n"
                    details_text += f"*볼륨 유형:* {volume_details.get('volume_type', '알 수 없음')}\n"
                    details_text += f"*크기:* {volume_details.get('size', 0)} GB\n"
                    details_text += f"*월 비용:* ${volume_details.get('monthly_cost', 0):.2f}\n\n"
                    
                    # 메트릭 정보가 있으면 추가
                    if 'metrics' in volume_details:
                        metrics = volume_details.get('metrics', {})
                        details_text += "*주요 메트릭:*\n"
                        
                        # 유휴 시간 정보 표시
                        if 'VolumeIdleTime_percent' in metrics:
                            idle_percent = metrics['VolumeIdleTime_percent']
                            details_text += f"• 볼륨 유휴 시간: {idle_percent:.2f}%\n"
                        elif 'VolumeIdleTime' in metrics:
                            idle_time = metrics['VolumeIdleTime']
                            details_text += f"• 볼륨 유휴 시간: {idle_time:.2f}분/시간 ({idle_time/60*100:.2f}%)\n"
                        
                        # 읽기/쓰기 작업 정보 표시
                        if 'VolumeReadOps' in metrics:
                            details_text += f"• 읽기 작업: {metrics['VolumeReadOps']:.2f} ops/s\n"
                        if 'VolumeWriteOps' in metrics:
                            details_text += f"• 쓰기 작업: {metrics['VolumeWriteOps']:.2f} ops/s\n"
                        
                        # 추가 메트릭 정보 표시
                        if 'VolumeQueueLength' in metrics:
                            details_text += f"• 대기열 길이: {metrics['VolumeQueueLength']:.2f}\n"
                        if 'VolumeThroughputPercentage' in metrics:
                            details_text += f"• 처리량: {metrics['VolumeThroughputPercentage']:.2f}%\n"
                        if 'BurstBalance' in metrics:
                            details_text += f"• 버스트 밸런스: {metrics['BurstBalance']:.2f}%\n"
                    else:
                        details_text += "*메트릭 정보가 없습니다.*\n\n"
                    
                    # 유휴 상태 분석 결과 표시
                    if 'idle_check_details' in volume_details:
                        idle_details = volume_details['idle_check_details']
                        details_text += "\n*유휴 상태 분석:*\n"
                        details_text += f"• 유휴 상태: {'예' if idle_details.get('result', False) else '아니오'}\n"
                        details_text += f"• 이유: {idle_details.get('reason', '해당 없음')}\n"
                    
                    # 과대 프로비저닝 분석 결과 표시
                    if 'overprovisioned_check_details' in volume_details:
                        over_details = volume_details['overprovisioned_check_details']
                        details_text += "\n*과대 프로비저닝 분석:*\n"
                        details_text += f"• 과대 프로비저닝: {'예' if over_details.get('result', False) else '아니오'}\n"
                        details_text += f"• 이유: {over_details.get('reason', '해당 없음')}\n"
                    
                    # 권장 사항 표시
                    recommendation = result.get('recommendation')
                    if recommendation:
                        details_text += f"\n*권장 조치:* {recommendation}\n"
                    
                    # 오류가 있으면 표시
                    if 'error' in result:
                        details_text += f"\n*오류:* {result['error']}\n"
                        details_text += "\n볼륨 상태가 정상적으로 표시되지 않습니다."
                    
                    # 스레드에 세부 정보 메시지 전송
                    send_slack_message(channel_id, details_text, SLACK_BOT_TOKEN, thread_ts)
        else:
            # 전체 볼륨 분석
            result = analyze_all_regions({"detailed_report": detailed_report}, detailed_report)
            
            # 결과를 S3에 저장
            s3_location = save_result_to_s3(result)
            
            # 메인 채팅에 분석 결과 표시
            if channel_id:
                # 요약 결과를 메인 채팅에 표시
                from integrations.slack.slack_messenger import send_all_regions_analysis_result_to_slack
                success, msg_ts = send_all_regions_analysis_result_to_slack(
                    result, 
                    channel_id, 
                    SLACK_BOT_TOKEN,
                    thread_ts=None,  # 메인 채팅에 표시
                    use_thread=False
                )
                
                # S3 저장 정보는 스레드에 표시
                if thread_ts:
                    send_slack_message(
                        channel_id,
                        f"전체 분석 결과가 S3에 저장되었습니다: {s3_location}",
                        SLACK_BOT_TOKEN,
                        thread_ts
                    )
        
        return {
            "success": True,
            "message": f"볼륨 분석이 완료되었습니다.",
            "result": "완료",
            "thread_ts": thread_ts
        }
    
    except Exception as e:
        logger.error(f"볼륨 분석 중 오류 발생: {str(e)}", exc_info=True)
        
        # 오류 발생 시 Slack 알림
        if channel_id and thread_ts:
            send_slack_message(
                channel_id, 
                f"볼륨 분석 중 오류가 발생했습니다: {str(e)}", 
                SLACK_BOT_TOKEN,
                thread_ts
            )
        
        return {
            "success": False,
            "error": str(e)
        }

def process_execute_action(parameters, requested_by, channel_id, response_url=None, thread_ts=None):
    """
    볼륨에 대한 조치 실행 액션을 처리합니다.
    
    :param parameters: 실행 파라미터
    :param requested_by: 요청자 ID
    :param channel_id: Slack 채널 ID
    :param response_url: Slack 응답 URL
    :param thread_ts: 스레드 타임스탬프
    :return: 처리 결과
    """
    try:
        volume_id = parameters.get('volume_id')
        action_type = parameters.get('action_type')
        region = parameters.get('region')
        
        if not volume_id or not action_type:
            return {
                "success": False,
                "error": "볼륨 ID와 액션 유형이 필요합니다."
            }
        
        logger.info(f"볼륨 {volume_id}에 {action_type} 액션 실행 시작")
        
        # 실행 진행 알림은 스레드에 표시
        if channel_id and thread_ts:
            send_slack_message(
                channel_id,
                f"볼륨 `{volume_id}`에 대한 `{action_type}` 액션 실행이 진행 중입니다...",
                SLACK_BOT_TOKEN,
                thread_ts
            )
        
        # 먼저 볼륨 상태 확인
        analyzer = EBSAnalyzer(region)
        volume_info = analyzer.analyze_specific_volume(volume_id)
        
        if not volume_info or 'error' in volume_info:
            error_msg = volume_info.get('error', '볼륨 정보를 가져올 수 없습니다.')
            
            # 오류 알림은 스레드에 표시
            if channel_id and thread_ts:
                send_slack_message(channel_id, f"오류: {error_msg}", SLACK_BOT_TOKEN, thread_ts)
            
            return {
                "success": False,
                "error": error_msg
            }
        
        # 권장 조치 실행기 초기화
        executor = RecommendationExecutor(region)
        
        # 볼륨 상태에 따른 적절한 액션 선택 및 실행
        if 'delete' in action_type or action_type == 'snapshot_only':
            result = executor.execute_idle_volume_recommendation(volume_info, action_type)
        else:
            result = executor.execute_overprovisioned_volume_recommendation(volume_info, action_type)
        
        # 결과 알림 - 실행 결과는 메인 채팅에 표시하고 세부 정보는 스레드에 표시
        if channel_id:
            # 기본 실행 결과는 메인 채팅에 표시
            action_name = action_type.replace('_', ' ').title()
            success = result.get('success', False)
            
            # 메인 채팅에 결과 메시지 표시
            message = f"볼륨 `{volume_id}`에 대한 `{action_name}` 액션이 {('성공적으로 완료' if success else '실패')}되었습니다."
            send_slack_message(channel_id, message, SLACK_BOT_TOKEN)
            
            # 세부 결과는 스레드에 표시
            if thread_ts:
                send_execution_result_to_slack(result, volume_id, action_type, channel_id, requested_by, SLACK_BOT_TOKEN, thread_ts)
        
        return {
            "success": True,
            "message": f"볼륨 {volume_id}에 대한 {action_type} 액션이 완료되었습니다.",
            "result": result
        }
    
    except Exception as e:
        logger.error(f"볼륨 액션 실행 중 오류 발생: {str(e)}", exc_info=True)
        
        # 오류 발생 시 Slack 알림
        if channel_id and thread_ts:
            send_slack_message(
                channel_id,
                f"볼륨 `{parameters.get('volume_id', 'unknown')}`에 대한 `{parameters.get('action_type', 'unknown')}` "
                f"액션 실행 중 오류가 발생했습니다: {str(e)}",
                SLACK_BOT_TOKEN,
                thread_ts
            )
        
        return {
            "success": False,
            "error": str(e)
        }

def process_slash_command(action_data):
    """
    슬래시 커맨드를 처리합니다.
    
    :param action_data: 슬래시 커맨드 데이터
    :return: 처리 결과
    """
    text = action_data.get('text', '')
    parameters = action_data.get('parameters', {})
    requested_by = action_data.get('requested_by', 'unknown')
    channel_id = action_data.get('channel_id')
    thread_ts = action_data.get('thread_ts')  # 스레드 ID 추출
    
    # 커맨드 처리 전 로그 출력
    logger.info(f"슬래시 커맨드 처리 시작. 텍스트: '{text}', 파라미터: {parameters}, 스레드 TS: {thread_ts}")
    
    # 파라미터가 비어 있으면 텍스트에서 다시 파싱 시도
    if not parameters and text:
        logger.info(f"파라미터가 비어 있어 텍스트에서 다시 파싱 시도: '{text}'")
        parameters = parse_command_parameters(text)
        logger.info(f"재파싱된 파라미터: {parameters}")
    
    words = text.split()
    if not words:
        # 도움말 표시는 메인 채팅에 표시
        if channel_id:
            send_slack_message(channel_id, 
                "EBS 볼륨 최적화 도구 사용법:\n"
                "• `/ebs-optimize analyze` - 모든 볼륨 분석\n"
                "• `/ebs-optimize analyze vol-1234abcd [--region=us-east-1] [--detailed]` - 특정 볼륨 분석\n"
                "• `/ebs-optimize execute vol-1234abcd snapshot_and_delete [--region=us-east-1]` - 특정 볼륨에 조치 실행\n\n"
                "가능한 조치: snapshot_and_delete, snapshot_only, change_type, resize",
                SLACK_BOT_TOKEN
            )
        return {"success": True, "message": "도움말 표시됨"}
    
    command = words[0].lower()
    logger.info(f"슬래시 커맨드 감지된 명령어: {command}")
    
    if command == "analyze":
        logger.info(f"analyze 명령 처리 시작, 파라미터: {parameters}, 스레드 TS: {thread_ts}")
        return process_analyze_action(parameters, requested_by, channel_id, thread_ts)
    elif command == "execute":
        logger.info(f"execute 명령 처리 시작, 파라미터: {parameters}, 스레드 TS: {thread_ts}")
        return process_execute_action(parameters, requested_by, channel_id, None, thread_ts)
    else:
        # 알 수 없는 명령어 응답은 메인 채팅에 표시
        if channel_id:
            send_slack_message(channel_id, f"알 수 없는 명령어: {command}. 사용 가능한 명령어: analyze, execute", SLACK_BOT_TOKEN)
        return {"success": False, "error": f"알 수 없는 명령어: {command}"}

def process_volume_action(action_type, parameters, requested_by, channel_id, response_url=None, thread_ts=None):
    """
    볼륨에 대한 직접 조치를 처리합니다.
    
    :param action_type: 액션 유형
    :param parameters: 액션 파라미터
    :param requested_by: 요청자 ID
    :param channel_id: Slack 채널 ID
    :param response_url: Slack 응답 URL
    :param thread_ts: 스레드 타임스탬프
    :return: 처리 결과
    """
    # 이 함수는 process_execute_action과 유사하게 동작하지만,
    # 직접 볼륨 조치(idle_volume_action, overprovisioned_volume_action)를 처리합니다.
    
    volume_id = parameters.get('volume_id')
    region = parameters.get('region')
    action_subtype = parameters.get('action_type')  # 'snapshot_and_delete', 'resize' 등
    
    # process_execute_action과 유사한 로직 사용
    return process_execute_action({
        'volume_id': volume_id,
        'region': region,
        'action_type': action_subtype
    }, requested_by, channel_id, response_url, thread_ts)

# Lambda 함수 진입점
if __name__ == "__main__":
    # 로컬 테스트용
    test_event = {
        # 분석 요청 테스트
        "volume_id": "vol-12345678",
        "region": "us-east-1",
        "output_format": "both",
        "detailed_report": True
    }
    
    result = lambda_handler(test_event, None)
    print(json.dumps(result, indent=2))