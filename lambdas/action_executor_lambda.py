import os
import json
import boto3
import logging
import requests
from datetime import datetime
import sys
from pathlib import Path

# 루트 디렉토리를 Python 경로에 추가
root_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(root_dir))

from dotenv import load_dotenv

from actions.recommendation_executor import RecommendationExecutor
from utils.ebs_analyzer import EBSAnalyzer
from integrations.slack.slack_messenger import send_slack_message, send_analysis_result_to_slack, send_execution_result_to_slack

# 환경 변수 로드
load_dotenv()

# 로깅 설정
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 환경 변수 가져오기
SQS_QUEUE_URL = os.environ.get('SQS_QUEUE_URL')
SLACK_WEBHOOK_URL = os.environ.get('SLACK_WEBHOOK_URL')
SLACK_BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN')

def lambda_handler(event, context):
    """
    SQS 큐에서 액션을 가져와 처리하는 Lambda 함수
    
    :param event: SQS 이벤트
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
    
    try:
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
                if is_retry_request(action_data):
                    logger.info(f"Slack 재시도 요청 감지, 메시지 건너뜀: {record['messageId']}")
                    results["details"].append({
                        "message_id": record['messageId'],
                        "status": "skipped",
                        "reason": "slack_retry"
                    })
                    continue
                
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
                    "action_type": action_data.get('ActionType'),
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
    
    except Exception as e:
        logger.error(f"SQS 이벤트 처리 중 오류 발생: {str(e)}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": str(e),
                "results": results
            })
        }

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
    requested_by = action_data.get('requested_by', 'unknown')
    channel_id = action_data.get('channel_id')
    response_url = action_data.get('response_url')
    thread_ts = action_data.get('thread_ts')  # 스레드 타임스탬프 추가
    
    logger.info(f"액션 처리 중: {action_type}, 파라미터: {parameters}")
    
    # 액션 유형에 따른 처리
    if action_type == 'analyze':
        return process_analyze_action(parameters, requested_by, channel_id, thread_ts)
    elif action_type == 'execute':
        return process_execute_action(parameters, requested_by, channel_id, response_url, thread_ts)
    elif action_type.startswith('idle_volume_') or action_type.startswith('overprovisioned_volume_'):
        # 직접적인 볼륨 액션 처리
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
    :param thread_ts: 스레드 타임스탬프 (이미 존재하는 스레드에 응답하는 경우)
    :return: 처리 결과
    """
    try:
        logger.info(f"볼륨 분석 시작, 파라미터: {parameters}")
        
        volume_id = parameters.get('volume_id')
        region = parameters.get('region')
        detailed_report = parameters.get('detailed_report', True)
        
        # 특정 볼륨 또는 전체 분석
        if volume_id:
            # EBS 분석기 초기화
            analyzer = EBSAnalyzer(region)
            
            # 특정 볼륨 분석
            result = analyzer.analyze_specific_volume(volume_id)
            
            # 결과 포맷팅
            formatted_result = {
                "timestamp": datetime.now().isoformat(),
                "volume_id": volume_id,
                "region": region,
                "is_idle": result.get('is_idle', False),
                "is_overprovisioned": result.get('is_overprovisioned', False),
                "recommendation": result.get('recommendation', '해당 없음'),
                "details": result if detailed_report else {}
            }
            
            # Slack 채널에 메시지 전송하고 thread_ts 받기
            if channel_id:
                success, new_thread_ts = send_analysis_result_to_slack(formatted_result, channel_id, SLACK_BOT_TOKEN)
                # 분석 결과의 ts를 향후 액션에 사용할 수 있도록 반환값에 포함
                formatted_result["thread_ts"] = new_thread_ts
            
            return {
                "success": True,
                "message": f"볼륨 {volume_id} 분석이 완료되었습니다.",
                "result": formatted_result,
                "thread_ts": formatted_result.get("thread_ts")  # thread_ts를 반환값에 포함
            }
        else:
            # 전체 볼륨 분석은 많은 시간이 소요될 수 있으므로 분석 시작 알림
            if channel_id:
                # 기존 스레드가 없으면 새 메시지 시작, 있으면 스레드에 응답
                success, new_thread_ts = send_slack_message(
                    channel_id, 
                    f"전체 EBS 볼륨 분석이 시작되었습니다. 요청자: <@{requested_by}>", 
                    SLACK_BOT_TOKEN,
                    thread_ts
                )
                # 새로운 스레드 시작이면 thread_ts 저장
                if not thread_ts and new_thread_ts:
                    thread_ts = new_thread_ts
            
            # 직접 분석 실행 (Lambda 호출하는 대신 내부 함수 호출)
            from analyze_and_notify_lambda import analyze_all_regions
            
            # 분석 파라미터
            analysis_params = {
                'output_format': 'slack',
                'detailed_report': detailed_report,
                'channel_id': channel_id,
                'thread_ts': thread_ts  # 스레드 정보 전달
            }
            
            # 분석 실행 (이 함수를 비동기로 실행하거나, 백그라운드 태스크로 실행하는 것이 좋음)
            # 여기서는 예시로 간단히 직접 호출
            analysis_result = analyze_all_regions(analysis_params, detailed_report)
            
            # 결과를 S3에 저장하고 Slack으로 전송하는 코드도 직접 호출 가능
            # 여기서는 이미 analyze_all_regions 내부에서 처리된다고 가정
            
            return {
                "success": True,
                "message": "전체 볼륨 분석 요청이 처리되었습니다.",
                "result": "처리 중",
                "thread_ts": thread_ts  # thread_ts를 반환값에 포함
            }
    
    except Exception as e:
        logger.error(f"볼륨 분석 중 오류 발생: {str(e)}", exc_info=True)
        
        # 오류 발생 시 Slack 알림
        if channel_id:
            send_slack_message(
                channel_id, 
                f"볼륨 분석 중 오류가 발생했습니다: {str(e)}", 
                SLACK_BOT_TOKEN,
                thread_ts  # 스레드가 있는 경우 같은 스레드에 오류 메시지 전송
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
        
        # 실행 전 Slack 알림
        if channel_id:
            # 스레드에 알림 메시지 전송
            send_slack_message(
                channel_id,
                f"볼륨 `{volume_id}`에 대한 `{action_type}` 액션 실행이 시작되었습니다. 요청자: <@{requested_by}>",
                SLACK_BOT_TOKEN,
                thread_ts  # 스레드가 있는 경우 스레드에 메시지 전송
            )
        
        # 먼저 볼륨 상태 확인
        analyzer = EBSAnalyzer(region)
        volume_info = analyzer.analyze_specific_volume(volume_id)
        
        if not volume_info or 'error' in volume_info:
            error_msg = volume_info.get('error', '볼륨 정보를 가져올 수 없습니다.')
            
            # 오류 알림
            if channel_id:
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
        
        # 결과 알림
        if channel_id:
            # 실행 결과를 포맷팅하여 Slack으로 전송 (같은 스레드에)
            send_execution_result_to_slack(result, volume_id, action_type, channel_id, requested_by, SLACK_BOT_TOKEN, thread_ts)
        
        return {
            "success": True,
            "message": f"볼륨 {volume_id}에 대한 {action_type} 액션이 완료되었습니다.",
            "result": result,
            "thread_ts": thread_ts  # thread_ts를 반환값에 포함
        }
    
    except Exception as e:
        logger.error(f"볼륨 액션 실행 중 오류 발생: {str(e)}", exc_info=True)
        
        # 오류 발생 시 Slack 알림
        if channel_id:
            send_slack_message(
                channel_id,
                f"볼륨 `{parameters.get('volume_id', 'unknown')}`에 대한 `{parameters.get('action_type', 'unknown')}` "
                f"액션 실행 중 오류가 발생했습니다: {str(e)}",
                SLACK_BOT_TOKEN,
                thread_ts  # 스레드가 있는 경우 같은 스레드에 오류 메시지 전송
            )
        
        return {
            "success": False,
            "error": str(e)
        }

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