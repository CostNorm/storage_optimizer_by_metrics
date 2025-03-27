from datetime import datetime
import json
import logging
import boto3

from ebs.ebs_analyzer import EBSAnalyzer
from ebs.config import REGIONS, S3_BUCKET_NAME
from ebs.actions.recommendation_executor import RecommendationExecutor

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
    - execute_recommendations (선택): 권장 사항 자동 실행 여부 (기본값: False)
    - action_type (선택): 실행할 권장 조치 유형 ('snapshot_and_delete', 'snapshot_only', 'change_type', 'resize', 'change_type_and_resize')
    """
    logger.info("EBS 스토리지 최적화 분석 시작")
    
    try:
        # 이벤트에서 파라미터 추출
        volume_id = event.get('volume_id')
        specific_region = event.get('region')
        optimize_results_size = event.get('optimize_results_size', True)
        execute_recommendations = event.get('execute_recommendations', False)
        action_type = event.get('action_type')
        
        # 특정 볼륨 ID가 제공된 경우 처리 로직
        if volume_id:
            return analyze_specific_volume(volume_id, specific_region, optimize_results_size, execute_recommendations, action_type)
        else:
            # 기존 로직 - 전체 리전 분석
            return analyze_all_regions(event, optimize_results_size, execute_recommendations, action_type)
    
    except Exception as e:
        logger.error(f"EBS 볼륨 분석 중 오류 발생: {str(e)}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps({
                "message": "EBS 볼륨 분석 중 오류가 발생했습니다.",
                "error": str(e)
            })
        }

def analyze_specific_volume(volume_id, region=None, optimize_results_size=True, execute_recommendations=False, action_type=None):
    """
    특정 볼륨 ID에 대한 분석을 수행합니다.
    
    :param volume_id: 분석할 볼륨 ID
    :param region: 볼륨이 위치한 리전 (없으면 첫 번째 리전 사용)
    :param optimize_results_size: 결과 크기 최적화 여부
    :param execute_recommendations: 권장 조치 자동 실행 여부
    :param action_type: 실행할 조치 유형
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
    
    # 권장 조치 자동 실행
    execution_result = None
    if execute_recommendations and action_type:
        logger.info(f"볼륨 {volume_id}에 대한 권장 조치 실행 - 작업 유형: {action_type}")
        executor = RecommendationExecutor(target_region)
        
        # 유효한 액션 유형 검증
        valid_actions = ['snapshot_and_delete', 'snapshot_only', 'change_type', 'resize', 'change_type_and_resize']
        if action_type not in valid_actions:
            logger.warning(f"지정된 액션 유형 '{action_type}'이 유효하지 않습니다. 유효한 액션: {valid_actions}")
            
        # 볼륨 상태에 상관없이 요청된 액션 실행
        if volume_result.get('is_idle', False):
            logger.info(f"볼륨 {volume_id}이(가) 유휴 상태로 감지되어 권장 조치 실행")
            execution_result = executor.execute_idle_volume_recommendation(volume_result, action_type)
        elif volume_result.get('is_overprovisioned', False):
            logger.info(f"볼륨 {volume_id}이(가) 과대 프로비저닝 상태로 감지되어 권장 조치 실행")
            execution_result = executor.execute_overprovisioned_volume_recommendation(volume_result, action_type)
        else:
            logger.info(f"볼륨 {volume_id}는 최적화가 필요한 것으로 감지되지 않았으나, 사용자 요청에 따라 액션 실행")
            # 볼륨 상태에 따라 적절한 액션 선택
            if 'delete' in action_type or action_type == 'snapshot_only':
                execution_result = executor.execute_idle_volume_recommendation(volume_result, action_type)
            else:
                execution_result = executor.execute_overprovisioned_volume_recommendation(volume_result, action_type)
        
        # 실행 결과 저장
        if execution_result:
            s3_execution_key = f"execution-result-{volume_id}-{current_time}.json"
            s3_client.put_object(
                Bucket=S3_BUCKET_NAME,
                Key=s3_execution_key,
                Body=json.dumps(execution_result, indent=2),
                ContentType='application/json'
            )
            logger.info(f"실행 결과가 S3에 저장되었습니다: s3://{S3_BUCKET_NAME}/{s3_execution_key}")
    
    response = {
        "statusCode": 200,
        "body": json.dumps({
            "message": f"볼륨 {volume_id} 분석이 성공적으로 완료되었습니다.",
            "result_location": f"s3://{S3_BUCKET_NAME}/{s3_key}",
            "is_idle": volume_result.get('is_idle', False),
            "is_overprovisioned": volume_result.get('is_overprovisioned', False),
            "recommendation": volume_result.get('recommendation', '해당 없음')
        })
    }
    
    # 실행 결과가 있으면 응답에 추가
    if execution_result:
        response["body"] = json.loads(response["body"])
        response["body"]["action_executed"] = True
        response["body"]["action_result"] = execution_result
        response["body"] = json.dumps(response["body"])
    
    return response

def analyze_all_regions(event, optimize_results_size=True, execute_recommendations=False, action_type=None):
    """
    모든 리전의 모든 볼륨을 분석합니다.
    
    :param event: Lambda 이벤트 객체
    :param optimize_results_size: 결과 크기 최적화 여부
    :param execute_recommendations: 권장 조치 자동 실행 여부
    :param action_type: 실행할 조치 유형
    :return: 분석 결과
    """
    # 현재 분석 날짜/시간
    current_time = datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
    
    # 전체 분석 결과를 저장할 딕셔너리
    all_results = {
        "timestamp": current_time,
        "regions": {}
    }
    
    # 권장 조치 실행 결과를 저장할 딕셔너리
    execution_results = {
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
        
        # 권장 조치 실행 (요청된 경우)
        if execute_recommendations and action_type:
            logger.info(f"권장 조치 실행 요청됨 - 작업 유형: {action_type}")
            
            # 유효한 액션 유형 검증
            valid_actions = ['snapshot_and_delete', 'snapshot_only', 'change_type', 'resize', 'change_type_and_resize']
            if action_type not in valid_actions:
                logger.warning(f"지정된 액션 유형 '{action_type}'이 유효하지 않습니다. 유효한 액션: {valid_actions}")
            
            execution_results["regions"][region] = {
                "idle_volumes": [],
                "overprovisioned_volumes": [],
                "other_volumes": []  # 유휴 또는 과대 프로비저닝이 아닌 볼륨에 대한 조치 결과
            }
                
            # RecommendationExecutor 초기화
            executor = RecommendationExecutor(region)
            
            # 유휴 볼륨에 대한 조치 실행
            for volume in region_results.get('idle_volumes', []):
                logger.info(f"유휴 볼륨 {volume['volume_id']}에 대한 권장 조치 실행 - 작업 유형: {action_type}")
                result = executor.execute_idle_volume_recommendation(volume, action_type)
                execution_results["regions"][region]["idle_volumes"].append(result)
            
            # 과대 프로비저닝된 볼륨에 대한 조치 실행
            for volume in region_results.get('overprovisioned_volumes', []):
                logger.info(f"과대 프로비저닝된 볼륨 {volume['volume_id']}에 대한 권장 조치 실행 - 작업 유형: {action_type}")
                result = executor.execute_overprovisioned_volume_recommendation(volume, action_type)
                execution_results["regions"][region]["overprovisioned_volumes"].append(result)
                
            # 요청이 있고 idle_volumes와 overprovisioned_volumes에 없는 볼륨에 대해서도 작업 실행
            if event.get('force_execution_for_all', False):
                logger.info(f"모든 볼륨에 대한 권장 조치 강제 실행")
                # 이미 처리된 볼륨 ID 목록 생성
                processed_volumes = set([vol['volume_id'] for vol in region_results.get('idle_volumes', [])] +
                                    [vol['volume_id'] for vol in region_results.get('overprovisioned_volumes', [])])
                
                # 나머지 볼륨에 대해 처리
                for volume in region_results.get('all_volumes', []):
                    if volume['volume_id'] not in processed_volumes:
                        logger.info(f"일반 볼륨 {volume['volume_id']}에 대한 요청 조치 실행 - 작업 유형: {action_type}")
                        # 볼륨 상태에 따라 적절한 액션 선택
                        if 'delete' in action_type or action_type == 'snapshot_only':
                            result = executor.execute_idle_volume_recommendation(volume, action_type)
                        else:
                            result = executor.execute_overprovisioned_volume_recommendation(volume, action_type)
                        execution_results["regions"][region]["other_volumes"].append(result)
        
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
    
    # 실행 결과를 S3에 저장 (실행된 경우)
    if execute_recommendations and action_type:
        s3_execution_key = f"execution-results-{current_time}.json"
        s3_client.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=s3_execution_key,
            Body=json.dumps(execution_results, indent=2),
            ContentType='application/json'
        )
        logger.info(f"실행 결과가 S3에 저장되었습니다: s3://{S3_BUCKET_NAME}/{s3_execution_key}")
    
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
    
    response = {
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
    
    # 실행 결과가 있으면 응답에 추가
    if execute_recommendations and action_type:
        response["body"] = json.loads(response["body"])
        response["body"]["actions_executed"] = True
        response["body"]["actions_result_location"] = f"s3://{S3_BUCKET_NAME}/{s3_execution_key}"
        response["body"] = json.dumps(response["body"])
    
    return response