import boto3
import logging
import json
import os
import sys
from pathlib import Path
from datetime import datetime

# 프로젝트 루트 디렉토리를 Python 경로에 추가 (필요에 따라 조정)
# 이 경로는 ebs_service.py 파일의 위치를 기준으로 설정해야 합니다.
try:
    root_dir = Path(__file__).resolve().parent.parent
    sys.path.append(str(root_dir))

    from config.config import REGIONS, S3_BUCKET_NAME
    from ebs.analyzer.ebs_analyzer import EBSAnalyzer
except ImportError as e:
    # 로컬 실행 등 환경에 따라 경로 문제가 발생할 수 있으므로 로깅 추가
    logging.error(f"모듈 임포트 중 오류 발생: {e}. 경로 설정을 확인하세요.")
    # 기본값 또는 다른 방식으로 설정 로드 시도 (선택 사항)
    REGIONS = ['ap-northeast-2'] # 예시 기본값
    S3_BUCKET_NAME = os.environ.get('S3_BUCKET_NAME', 'default-bucket-name') # 환경 변수 또는 기본값
    # EBSAnalyzer 임포트 실패 시 처리는 더 복잡할 수 있음
    EBSAnalyzer = None # 또는 오류 발생 처리

# Import Slack messenger for updates (adjust path if necessary)
try:
    from integrations.slack.slack_messenger import _update_slack_message
except ImportError:
    _update_slack_message = None
    logging.warning("Failed to import _update_slack_message for progress updates.")

# 로깅 설정
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
# 필요시 핸들러 추가 (예: StreamHandler)
# handler = logging.StreamHandler()
# formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# handler.setFormatter(formatter)
# logger.addHandler(handler)


def analyze_specific_volume(volume_id, region=None, detailed_report=False):
    """
    특정 볼륨 ID에 대한 분석을 수행합니다.
    (수정: 결과에 'attachments' 정보 포함)

    :param volume_id: 분석할 볼륨 ID
    :param region: 볼륨이 위치한 리전 (없으면 첫 번째 리전 사용)
    :param detailed_report: 상세 보고서 여부
    :return: 분석 결과
    """
    if EBSAnalyzer is None:
        logger.error("EBSAnalyzer가 제대로 임포트되지 않았습니다.")
        return {"error": "EBSAnalyzer 초기화 실패"}

    # 리전이 지정되지 않은 경우 첫 번째 리전 사용
    target_region = region if region else (REGIONS[0] if REGIONS else 'us-east-1') # REGIONS 비어있을 경우 대비

    logger.info(f"특정 볼륨 분석 시작 - 볼륨 ID: {volume_id}, 리전: {target_region}")

    # EBS 분석기 초기화
    try:
        analyzer = EBSAnalyzer(target_region)
    except Exception as e:
        logger.error(f"EBSAnalyzer({target_region}) 초기화 중 오류: {e}", exc_info=True)
        return {
            "timestamp": datetime.now().isoformat(),
            "volume_id": volume_id,
            "region": target_region,
            "error": f"분석기 초기화 실패: {e}",
            "status": "오류 발생",
            "recommendation": "분석기 초기화 중 오류가 발생했습니다."
        }

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

        # 분석 결과 포맷팅 (attachments 추가)
        formatted_result = {
            "timestamp": datetime.now().isoformat(),
            "volume_id": volume_id,
            "region": target_region,
            "is_idle": volume_result.get('is_idle', False),
            "is_overprovisioned": volume_result.get('is_overprovisioned', False),
            "recommendation": volume_result.get('recommendation', '해당 없음'),
            "status": volume_result.get('status', '알 수 없음'),
            "attachments": volume_result.get('Attachments', []),  # Attachments 정보 추가
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

def analyze_all_regions(detailed_report=False, initial_message_ts=None, channel_id=None, bot_token=None):
    """
    모든 리전의 모든 볼륨을 분석합니다. 진행 상황을 주기적으로 Slack 메시지로 업데이트합니다.

    :param detailed_report: 상세 보고서 여부
    :param initial_message_ts: 업데이트할 초기 Slack 메시지 타임스탬프
    :param channel_id: Slack 채널 ID
    :param bot_token: Slack 봇 토큰
    :return: 분석 결과
    """
    if EBSAnalyzer is None:
        logger.error("EBSAnalyzer가 제대로 임포트되지 않았습니다.")
        return {"error": "EBSAnalyzer 초기화 실패"}
    if not REGIONS:
        logger.warning("분석할 AWS 리전이 설정되지 않았습니다.")
        return {"error": "분석할 리전 없음"}

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
    total_regions = len(REGIONS)
    processed_regions_count = 0

    for region in REGIONS:
        logger.info(f"{region} 리전에 대한 EBS 볼륨 분석 시작 (analyze_specific_volume 재사용)")
        all_results["regions"][region] = {
            "idle_volumes_count": 0,
            "overprovisioned_volumes_count": 0,
            "analyzed_volumes_count": 0,
            "errors_count": 0,
            "details": [] # 상세 보고서용
        }
        region_savings = 0
        region_volumes_analyzed = 0
        region_volumes_total = 0

        try:
            analyzer = EBSAnalyzer(region)
            volumes_in_region = analyzer.get_all_volumes()
            region_volumes_total = len(volumes_in_region)
            logger.info(f"{region} 리전에서 {region_volumes_total}개의 볼륨 발견.")
            all_results["regions"][region]["total_volumes_found"] = region_volumes_total

            for i, volume_data in enumerate(volumes_in_region):
                 volume_id = volume_data.get('VolumeId')
                 if not volume_id:
                     logger.warning(f"{region} 리전에서 VolumeId 없는 볼륨 데이터 발견: {volume_data}")
                     continue

                 # --- Progress Update Logic (Example: Update every 50 volumes or at the end of a region) ---
                 if initial_message_ts and channel_id and bot_token and _update_slack_message and (i % 50 == 0 or i == region_volumes_total - 1):
                     progress_percent = ((processed_regions_count / total_regions) + ( (i + 1) / region_volumes_total) / total_regions ) * 100
                     progress_text = f"Analyzing... Region {processed_regions_count + 1}/{total_regions} ({region}): Volume {i + 1}/{region_volumes_total}. Overall progress: {progress_percent:.1f}%"
                     logger.info(f"Sending progress update: {progress_text}")
                     # Use a simple text block for progress
                     progress_blocks = [{
                         "type": "context",
                         "elements": [{"type": "mrkdwn", "text": progress_text}]
                     }]
                     _update_slack_message(channel_id, bot_token, initial_message_ts, progress_blocks, progress_text)
                 # --- End Progress Update --- 

                 # 각 볼륨에 대해 analyze_specific_volume 함수 호출
                 volume_result = analyze_specific_volume(volume_id, region, detailed_report)
                 all_results["regions"][region]["analyzed_volumes_count"] += 1
                 region_volumes_analyzed += 1

                 # 결과 처리
                 if volume_result and 'error' not in volume_result:
                     is_idle = volume_result.get('is_idle', False)
                     is_over = volume_result.get('is_overprovisioned', False)
                     estimated_savings = 0

                     if is_idle:
                         all_results["regions"][region]["idle_volumes_count"] += 1
                         all_results["summary"]["total_idle_volumes"] += 1
                         # 유휴 볼륨 절감액은 월 비용 전체로 가정 (details 에서 가져와야 함)
                         details = volume_result.get('details', {})
                         monthly_cost = details.get('monthly_cost', 0) if isinstance(details, dict) else 0
                         estimated_savings += monthly_cost

                         # Suggested action 추가 (analyze_specific_volume 에서 생성된 것 사용)
                         if volume_result.get("suggested_action") != "none":
                              action = volume_result.copy() # 필요한 정보만 추릴 수도 있음
                              action['estimated_savings'] = monthly_cost # 절감액 명시
                              all_results["summary"]["suggested_actions"].append(action)

                     elif is_over:
                         all_results["regions"][region]["overprovisioned_volumes_count"] += 1
                         all_results["summary"]["total_overprovisioned_volumes"] += 1
                         # 과대 볼륨 절감액은 overprovisioned_diagnosis 에서 가져와야 함
                         over_diag = volume_result.get('overprovisioned_diagnosis', {})
                         over_diag_data = over_diag.get('additional_data', {}) if isinstance(over_diag.get('additional_data'), dict) else {}
                         savings_from_over = over_diag_data.get('estimated_savings', 0)
                         estimated_savings += savings_from_over

                         # Suggested action 추가 (analyze_specific_volume 에서 생성된 것 사용)
                         if volume_result.get("suggested_action") != "none":
                              action = volume_result.copy()
                              action['estimated_savings'] = savings_from_over # 절감액 명시
                              all_results["summary"]["suggested_actions"].append(action)

                     region_savings += estimated_savings

                     # 상세 보고서 요청 시 결과 저장
                     if detailed_report:
                         all_results["regions"][region]["details"].append(volume_result)
                 else:
                     # 개별 볼륨 분석 오류 처리
                     logger.error(f"{volume_id} ({region}) 분석 오류: {volume_result.get('error', 'Unknown error')}")
                     all_results["regions"][region]["errors_count"] += 1
                     if detailed_report:
                          all_results["regions"][region]["details"].append(volume_result or {'volume_id': volume_id, 'region': region, 'error': 'Analysis failed'})

        except AttributeError as ae:
             logger.error(f"{region} 리전 분석 중 오류: EBSAnalyzer에 필요한 메소드(예: get_all_volumes)가 없을 수 있습니다. {ae}", exc_info=True)
             all_results["regions"][region]["error"] = f"Analyzer method error: {ae}"
        except Exception as e:
             logger.error(f"{region} 리전 분석 중 예외 발생: {str(e)}", exc_info=True)
             all_results["regions"][region]["error"] = f"리전 분석 중 오류: {e}"

        all_results["summary"]["total_estimated_savings"] += region_savings
        processed_regions_count += 1 # Increment after processing a region

    # 최종 절감액 반올림
    all_results["summary"]["total_estimated_savings"] = round(all_results["summary"]["total_estimated_savings"], 2)

    # 권장 조치 목록을 절감액 기준으로 정렬 (선택 사항)
    all_results["summary"]["suggested_actions"].sort(key=lambda x: x.get('estimated_savings', 0), reverse=True)

    # Final update before returning (optional, as sqs_handler will update with full results)
    if initial_message_ts and channel_id and bot_token and _update_slack_message:
         final_progress_text = f"Analysis complete for all {total_regions} regions. Preparing final summary..."
         _update_slack_message(channel_id, bot_token, initial_message_ts,
                              [{
                                "type": "context",
                                "elements": [{"type": "mrkdwn", "text": final_progress_text}]
                              }],
                              final_progress_text)

    return all_results

def save_result_to_s3(result):
    """
    분석 결과를 S3에 저장합니다.

    :param result: 분석 결과
    :return: S3 위치 또는 실패 시 메시지
    """
    if not S3_BUCKET_NAME or S3_BUCKET_NAME == 'default-bucket-name':
        logger.error("S3 버킷 이름이 설정되지 않았습니다.")
        return "S3 저장 실패: 버킷 이름 없음"

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
            Body=json.dumps(result, indent=2, ensure_ascii=False), # ensure_ascii=False 추가 (한글 깨짐 방지)
            ContentType='application/json'
        )

        logger.info(f"분석 결과가 S3에 저장되었습니다: s3://{S3_BUCKET_NAME}/{s3_key}")

        return f"s3://{S3_BUCKET_NAME}/{s3_key}"

    except Exception as e:
        logger.error(f"결과를 S3 버킷 '{S3_BUCKET_NAME}'에 저장하는 중 오류 발생: {str(e)}", exc_info=True)
        return f"S3 저장 실패: {str(e)}" 