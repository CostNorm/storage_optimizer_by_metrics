import boto3
import logging

logger = logging.getLogger()

def calculate_monthly_cost(size_gb, volume_type, region):
    """
    EBS 볼륨의 월별 비용을 계산
    
    :param size_gb: 볼륨 크기 (GB)
    :param volume_type: 볼륨 유형 (gp2, gp3, io1, io2, st1, sc1, standard)
    :param region: AWS 리전
    :return: 월별 예상 비용 (USD)
    """
    # 리전별 EBS 가격 매핑 (예시 - 실제 가격과 다를 수 있음)
    # https://aws.amazon.com/ebs/pricing/
    pricing = {
        'us-east-1': {  # 버지니아
            'gp2': 0.10,
            'gp3': 0.08,
            'io1': 0.125,  # + 추가 프로비저닝된 IOPS 비용
            'io2': 0.125,  # + 추가 프로비저닝된 IOPS 비용
            'st1': 0.045,
            'sc1': 0.025,
            'standard': 0.05
        },
        'ap-northeast-2': {  # 서울
            'gp2': 0.114,
            'gp3': 0.0912,
            'io1': 0.153,  # + 추가 프로비저닝된 IOPS 비용
            'io2': 0.153,  # + 추가 프로비저닝된 IOPS 비용
            'st1': 0.051,
            'sc1': 0.028,
            'standard': 0.08
        }
        # 필요시 다른 리전 추가
    }
    
    # 리전 가격 정보가 없는 경우 기본값 사용
    if region not in pricing:
        logger.warning(f"{region} 리전의 가격 정보가 없습니다. 미국 동부(버지니아) 가격으로 계산합니다.")
        region = 'us-east-1'
    
    # 볼륨 유형 가격 정보가 없는 경우 기본값 사용
    if volume_type not in pricing[region]:
        logger.warning(f"{volume_type} 볼륨 유형의 가격 정보가 없습니다. gp2 가격으로 계산합니다.")
        volume_type = 'gp2'
    
    # 볼륨 비용 계산 (볼륨 크기 * GB당 월별 가격)
    monthly_cost = size_gb * pricing[region][volume_type]
    
    # io1, io2 유형의 경우 IOPS 비용도 고려 필요
    # 여기서는 기본 IOPS만 고려하여 간단하게 계산
    
    return monthly_cost

def get_volume_attachment_history(ec2_client, volume_id):
    """
    EBS 볼륨의 연결/분리 이벤트 이력을 조회
    
    :param ec2_client: EC2 클라이언트
    :param volume_id: EBS 볼륨 ID
    :return: 연결/분리 이벤트 목록
    """
    try:
        # CloudTrail 이벤트 조회 (최근 90일 이내 이벤트만 가능)
        cloudtrail = boto3.client('cloudtrail', region_name=ec2_client.meta.region_name)
        
        # AttachVolume, DetachVolume 이벤트 조회
        attach_events = cloudtrail.lookup_events(
            LookupAttributes=[
                {
                    'AttributeKey': 'ResourceName',
                    'AttributeValue': volume_id
                },
                {
                    'AttributeKey': 'EventName',
                    'AttributeValue': 'AttachVolume'
                }
            ],
            MaxResults=10
        )
        
        detach_events = cloudtrail.lookup_events(
            LookupAttributes=[
                {
                    'AttributeKey': 'ResourceName',
                    'AttributeValue': volume_id
                },
                {
                    'AttributeKey': 'EventName',
                    'AttributeValue': 'DetachVolume'
                }
            ],
            MaxResults=10
        )
        
        # 이벤트 정보 통합
        events = []
        
        for event in attach_events['Events']:
            events.append({
                'event_type': 'attach',
                'event_time': event['EventTime'].isoformat(),
                'username': event['Username'] if 'Username' in event else 'N/A',
                'event_id': event['EventId']
            })
        
        for event in detach_events['Events']:
            events.append({
                'event_type': 'detach',
                'event_time': event['EventTime'].isoformat(),
                'username': event['Username'] if 'Username' in event else 'N/A',
                'event_id': event['EventId']
            })
        
        # 이벤트 시간순 정렬
        events.sort(key=lambda x: x['event_time'], reverse=True)
        
        return events
    
    except Exception as e:
        logger.error(f"볼륨 연결/분리 이력 조회 중 오류 발생: {str(e)}", exc_info=True)
        return []

def get_tags_as_dict(resource_tags):
    """
    AWS 리소스 태그 목록을 딕셔너리로 변환
    
    :param resource_tags: AWS 리소스 태그 목록
    :return: 태그 딕셔너리
    """
    if not resource_tags:
        return {}
    
    return {tag['Key']: tag['Value'] for tag in resource_tags}

def recommend_volume_type(current_type, metrics):
    """
    현재 볼륨 사용 패턴에 따라 최적의 볼륨 유형 추천
    
    :param current_type: 현재 볼륨 유형
    :param metrics: 수집된 볼륨 지표
    :return: 추천 볼륨 유형 및 근거
    """
    if not metrics:
        return current_type, "충분한 지표 데이터가 없어 추천할 수 없습니다."
    
    # 간단한 추천 로직 (실제로는 더 복잡한 분석이 필요)
    if current_type == 'io1' or current_type == 'io2':
        # IOPS 사용량 분석
        if 'VolumeReadOps' in metrics and 'VolumeWriteOps' in metrics:
            read_ops = [dp['Maximum'] for dp in metrics['VolumeReadOps']]
            write_ops = [dp['Maximum'] for dp in metrics['VolumeWriteOps']]
            
            max_iops = max(max(read_ops) if read_ops else 0, max(write_ops) if write_ops else 0)
            
            if max_iops < 16000:  # gp3의 최대 기본 IOPS는 16,000
                return 'gp3', f"최대 IOPS 사용량({max_iops})이 gp3의 기본 IOPS 한도 내에 있습니다."
    
    elif current_type == 'gp2':
        return 'gp3', "gp3는 gp2와 동일한 성능을 제공하면서도 비용이 더 저렴합니다."
    
    return current_type, "현재 볼륨 유형이 적합합니다."

def get_optimal_volume_size(current_size_gb, used_percent, buffer_percent=20):
    """
    현재 사용률을 기반으로 최적의 볼륨 크기를 계산합니다.
    
    :param current_size_gb: 현재 볼륨 크기 (GB)
    :param used_percent: 사용 중인 공간 비율 (%)
    :param buffer_percent: 추가 버퍼 비율 (%)
    :return: 권장 볼륨 크기 (GB)
    """
    # 사용 중인 공간 계산 (GB)
    used_space_gb = current_size_gb * (used_percent / 100)
    
    # 버퍼를 포함한 권장 크기 계산
    recommended_size = used_space_gb * (1 + (buffer_percent / 100))
    
    # 최소 1GB, 정수로 반올림
    return max(1, int(recommended_size + 0.5))
