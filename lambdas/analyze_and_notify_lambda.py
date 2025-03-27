import os
import json
import boto3
import logging
from datetime import datetime
import requests
import sys
from pathlib import Path

# 루트 디렉토리를 Python 경로에 추가
root_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(root_dir))

from dotenv import load_dotenv

from utils.ebs_analyzer import EBSAnalyzer
from config.config import REGIONS, S3_BUCKET_NAME
from integrations.slack.slack_messenger import send_slack_blocks, send_slack_error

# 환경 변수 로드
load_dotenv()

# 로깅 설정
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Slack 웹훅 URL 가져오기
SLACK_WEBHOOK_URL = os.environ.get('SLACK_WEBHOOK_URL')

def lambda_handler(event, context):
    """
    Lambda 핸들러 함수 - EBS 볼륨 분석을 실행하고 결과를 Slack 및 S3에 저장
    
    이벤트 파라미터:
    - volume_id (선택): 특정 볼륨 ID를 제공하면 해당 볼륨만 분석
    - region (선택): 볼륨 ID와 함께 특정 리전 지정, 없으면 첫번째 리전 사용
    - output_format (선택): 결과 출력 형식 ('slack', 'json', 'both', 기본값: 'both')
    - detailed_report (선택): 상세 보고서 여부 (기본값: False)
    """
    logger.info("EBS 스토리지 최적화 분석 시작")
    
    try:
        # 이벤트에서 파라미터 추출
        volume_id = event.get('volume_id')
        specific_region = event.get('region')
        output_format = event.get('output_format', 'both')  # 기본값은 'both'
        detailed_report = event.get('detailed_report', False)
        
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
    
    except Exception as e:
        logger.error(f"EBS 볼륨 분석 중 오류 발생: {str(e)}", exc_info=True)
        
        # 오류 발생 시에도 Slack에 알림
        if SLACK_WEBHOOK_URL:
            send_slack_error(SLACK_WEBHOOK_URL, str(e))
        
        return {
            "statusCode": 500,
            "body": json.dumps({
                "message": "EBS 볼륨 분석 중 오류가 발생했습니다.",
                "error": str(e)
            })
        }

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
    
    # 특정 볼륨 분석
    volume_result = analyzer.analyze_specific_volume(volume_id)
    
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

def send_to_slack(result):
    """
    분석 결과를 Slack으로 전송합니다.
    
    :param result: 분석 결과
    :return: Slack API 응답
    """
    if not SLACK_WEBHOOK_URL:
        logger.warning("Slack 웹훅 URL이 설정되지 않았습니다. Slack 알림을 건너뜁니다.")
        return {"skipped": True, "reason": "No webhook URL configured"}
    
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
                        "action_id": f"execute_{action['action_type']}"
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
        payload = {
            "blocks": blocks,
            "text": f"EBS 볼륨 최적화 분석 결과: 유휴 {idle_volumes}개, 과대 프로비저닝 {over_volumes}개, 예상 절감액 ${savings}/월"
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

if __name__ == "__main__":
    # 로컬 테스트용
    test_event = {
        # "volume_id": "vol-12345678",  # 특정 볼륨만 분석하려면 주석 해제
        # "region": "us-east-1",        # 특정 리전을 지정하려면 주석 해제
        "output_format": "both",      # 'slack', 'json', 'both' 중 선택
        "detailed_report": True       # 상세 보고서 여부
    }
    
    result = lambda_handler(test_event, None)
    print(json.dumps(result, indent=2))