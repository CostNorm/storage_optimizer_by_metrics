import json
import boto3
import logging
from datetime import datetime
import os

from ebs_analyzer import EBSAnalyzer
from config import REGIONS, S3_BUCKET_NAME

# 로깅 설정
logger = logging.getLogger()
logger.setLevel(logging.INFO)

def lambda_handler(event, context):
    """
    Lambda 핸들러 함수 - EBS 볼륨 분석을 실행하고 결과를 S3에 저장
    
    이벤트 파라미터:
    - volume_id (선택): 특정 볼륨 ID를 제공하면 해당 볼륨만 분석
    - region (선택): 볼륨 ID와 함께 특정 리전 지정, 없으면 첫번째 리전 사용
    - optimize_results_size (선택): 결과 크기 최적화 여부 (기본값: True)
    """
    logger.info("EBS 스토리지 최적화 분석 시작")
    
    try:
        # 이벤트에서 파라미터 추출
        volume_id = event.get('volume_id')
        specific_region = event.get('region')
        optimize_results_size = event.get('optimize_results_size', True)
        
        # 특정 볼륨 ID가 제공된 경우 처리 로직
        if volume_id:
            return analyze_specific_volume(volume_id, specific_region, optimize_results_size)
        else:
            # 기존 로직 - 전체 리전 분석
            return analyze_all_regions(event, optimize_results_size)
    
    except Exception as e:
        logger.error(f"EBS 볼륨 분석 중 오류 발생: {str(e)}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps({
                "message": "EBS 볼륨 분석 중 오류가 발생했습니다.",
                "error": str(e)
            })
        }

def analyze_specific_volume(volume_id, region=None, optimize_results_size=True):
    """
    특정 볼륨 ID에 대한 분석을 수행합니다.
    
    :param volume_id: 분석할 볼륨 ID
    :param region: 볼륨이 위치한 리전 (없으면 첫 번째 리전 사용)
    :param optimize_results_size: 결과 크기 최적화 여부
    :return: 분석 결과
    """
    # 리전이 지정되지 않은 경우 첫 번째 리전 사용
    target_region = region if region else REGIONS[0]
    
    logger.info(f"특정 볼륨 분석 시작 - 볼륨 ID: {volume_id}, 리전: {target_region}")
    
    # 현재 분석 날짜/시간
    current_time = datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
    
    # EBS 분석기 초기화
    analyzer = EBSAnalyzer(target_region)
    
    # 특정 볼륨 분석
    volume_result = analyzer.analyze_specific_volume(volume_id)
    
    # 결과를 S3에 저장
    s3_client = boto3.client('s3')
    s3_key = f"volume-analysis-{volume_id}-{current_time}.json"
    
    s3_client.put_object(
        Bucket=S3_BUCKET_NAME,
        Key=s3_key,
        Body=json.dumps(volume_result, indent=2),
        ContentType='application/json'
    )
    
    logger.info(f"볼륨 {volume_id} 분석 결과가 S3에 저장되었습니다: s3://{S3_BUCKET_NAME}/{s3_key}")
    
    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": f"볼륨 {volume_id} 분석이 성공적으로 완료되었습니다.",
            "result_location": f"s3://{S3_BUCKET_NAME}/{s3_key}",
            "is_idle": volume_result.get('is_idle', False),
            "is_overprovisioned": volume_result.get('is_overprovisioned', False),
            "recommendation": volume_result.get('recommendation', '해당 없음')
        })
    }

def analyze_all_regions(event, optimize_results_size=True):
    """
    모든 리전의 모든 볼륨을 분석합니다 (기존 로직).
    
    :param event: Lambda 이벤트 객체
    :param optimize_results_size: 결과 크기 최적화 여부
    :return: 분석 결과
    """
    # 현재 분석 날짜/시간
    current_time = datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
    
    # 전체 분석 결과를 저장할 딕셔너리
    all_results = {
        "timestamp": current_time,
        "regions": {}
    }
    
    # 설정된 각 리전에 대해 분석 실행
    for region in REGIONS:
        logger.info(f"{region} 리전에 대한 EBS 볼륨 분석 시작")
        
        # EBS 분석기 초기화
        analyzer = EBSAnalyzer(region)
        
        # 분석 실행
        region_results = analyzer.analyze_volumes()
        
        # 결과 용량 최적화
        if optimize_results_size and 'all_volumes' in region_results:
            for volume in region_results['all_volumes']:
                # 전체 볼륨 리스트에서는 메트릭 정보를 간소화
                if volume.get('metrics', {}) and len(volume['metrics']) > 3:
                    volume['metrics'] = {k: v for k, v in volume['metrics'].items()
                                        if k in ['VolumeIdleTime', 'VolumeReadOps', 'VolumeWriteOps']}
        
        all_results["regions"][region] = region_results
        
        logger.info(f"{region} 리전 분석 완료: {len(region_results['idle_volumes'])}개 유휴 볼륨, "
                    f"{len(region_results['overprovisioned_volumes'])}개 과대 프로비저닝 볼륨 감지")
    
    # 결과를 S3에 저장
    s3_client = boto3.client('s3')
    s3_key = f"analysis-results-{current_time}.json"
    
    s3_client.put_object(
        Bucket=S3_BUCKET_NAME,
        Key=s3_key,
        Body=json.dumps(all_results, indent=2),
        ContentType='application/json'
    )
    
    # 간략화된 요약 결과도 별도로 저장
    summary_results = {
        "timestamp": current_time,
        "summary": {
            "total_idle_volumes": sum(len(results['idle_volumes']) for results in all_results["regions"].values()),
            "total_overprovisioned_volumes": sum(len(results['overprovisioned_volumes']) for results in all_results["regions"].values()),
            "regions": {}
        }
    }
    
    # 리전별 요약 정보 추가
    for region, results in all_results["regions"].items():
        summary_results["summary"]["regions"][region] = {
            "idle_volumes_count": len(results['idle_volumes']),
            "overprovisioned_volumes_count": len(results['overprovisioned_volumes']),
            "total_volumes": results.get('total_volumes', 0)
        }
    
    # 요약 결과를 S3에 저장
    s3_summary_key = f"analysis-summary-{current_time}.json"
    s3_client.put_object(
        Bucket=S3_BUCKET_NAME,
        Key=s3_summary_key,
        Body=json.dumps(summary_results, indent=2),
        ContentType='application/json'
    )
    
    logger.info(f"분석 결과가 S3에 저장되었습니다: s3://{S3_BUCKET_NAME}/{s3_key}")
    logger.info(f"요약 결과가 S3에 저장되었습니다: s3://{S3_BUCKET_NAME}/{s3_summary_key}")
    
    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "EBS 볼륨 최적화 분석이 성공적으로 완료되었습니다.",
            "result_location": f"s3://{S3_BUCKET_NAME}/{s3_key}",
            "summary_location": f"s3://{S3_BUCKET_NAME}/{s3_summary_key}",
            "summary": {
                "total_idle_volumes": summary_results["summary"]["total_idle_volumes"],
                "total_overprovisioned_volumes": summary_results["summary"]["total_overprovisioned_volumes"]
            }
        })
    }
