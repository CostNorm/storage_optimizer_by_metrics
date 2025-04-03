import logging
from datetime import datetime, timedelta
from ..utils.config import METRIC_PERIOD
from ..utils.utils import calculate_monthly_cost

logger = logging.getLogger()

class IdleVolumeDetector:
    """
    유휴 상태의 EBS 볼륨을 감지하는 클래스
    """
    
    def __init__(self, region, ec2_client, cloudwatch_client, criteria):
        """
        :param region: AWS 리전
        :param ec2_client: EC2 클라이언트
        :param cloudwatch_client: CloudWatch 클라이언트
        :param criteria: 유휴 볼륨 감지 기준
        """
        self.region = region
        self.ec2_client = ec2_client
        self.cloudwatch_client = cloudwatch_client
        self.criteria = criteria
    
    def get_volume_metrics(self, volume_id, start_time, end_time):
        """
        특정 볼륨의 CloudWatch 지표를 수집
        
        :param volume_id: EBS 볼륨 ID
        :param start_time: 수집 시작 시간
        :param end_time: 수집 종료 시간
        :return: 수집된 지표 딕셔너리
        """
        metrics = {}
        
        # 수집할 지표 목록
        metric_names = [
            'VolumeIdleTime',
            'VolumeReadOps',
            'VolumeWriteOps',
            'VolumeReadBytes',
            'VolumeWriteBytes'
        ]
        
        # 볼륨이 gp2, st1, sc1 타입인 경우 BurstBalance도 수집
        volume_info = self.ec2_client.describe_volumes(VolumeIds=[volume_id])
        volume_type = volume_info['Volumes'][0]['VolumeType']
        
        if volume_type in ['gp2', 'st1', 'sc1']:
            metric_names.append('BurstBalance')
        
        # 각 지표 수집
        for metric_name in metric_names:
            response = self.cloudwatch_client.get_metric_statistics(
                Namespace='AWS/EBS',
                MetricName=metric_name,
                Dimensions=[{'Name': 'VolumeId', 'Value': volume_id}],
                StartTime=start_time,
                EndTime=end_time,
                Period=METRIC_PERIOD,
                Statistics=['Average', 'Sum', 'Maximum']
            )
            
            # 수집된 데이터포인트가 있는 경우에만 저장
            if response['Datapoints']:
                metrics[metric_name] = response['Datapoints']
        
        return metrics
    
    def is_idle_volume(self, volume_id, metrics):
        """
        주어진 지표를 기반으로 볼륨이 유휴 상태인지 확인
        
        :param volume_id: EBS 볼륨 ID
        :param metrics: 수집된 지표
        :return: 유휴 상태 여부(True/False), 판단 근거 메시지, 메트릭 요약 데이터
        """
        reasons = []
        metrics_summary = {}
        
        # 볼륨 상태 확인
        try:
            volume_response = self.ec2_client.describe_volumes(VolumeIds=[volume_id])
            volume_state = volume_response['Volumes'][0]['State'] if volume_response['Volumes'] else None
            
            # 'available' 상태는 볼륨이 어떤 인스턴스에도 연결되지 않았음을 의미
            if volume_state == 'available':
                reasons.append(f"볼륨이 'available' 상태로 어떤 인스턴스에도 연결되어 있지 않음")
                metrics_summary['volume_state'] = {'state': 'available'}
                return True, "볼륨이 어떤 인스턴스에도 연결되어 있지 않습니다.", metrics_summary
            
            # 볼륨이 'in-use' 상태인지 확인
            if volume_state == 'in-use':
                metrics_summary['volume_state'] = {'state': 'in-use'}
                
                # 볼륨이 in-use 상태인데 메트릭이 없는 경우 특별 처리
                if not metrics or len(metrics) == 0:
                    # 연결 시간 확인 (최근에 연결된 볼륨은 메트릭이 없을 수 있음)
                    attachments = volume_response['Volumes'][0].get('Attachments', [])
                    if attachments:
                        # 가장 최근 연결 시간 확인
                        from datetime import datetime, timezone
                        now = datetime.now(timezone.utc)
                        
                        for attachment in attachments:
                            attach_time = attachment.get('AttachTime')
                            if attach_time:
                                # 연결된지 24시간 이내면 유휴 상태가 아닌 것으로 판단
                                hours_since_attach = (now - attach_time).total_seconds() / 3600
                                if hours_since_attach < 24:
                                    return False, f"볼륨이 최근({hours_since_attach:.1f}시간 전)에 연결되어 데이터가 충분하지 않습니다.", metrics_summary
                    
                    # 메트릭이 없는 in-use 볼륨은 유휴 상태로 간주하지 않음
                    return False, "볼륨이 'in-use' 상태이지만 CloudWatch 메트릭이 없습니다. 메트릭 수집에 문제가 있을 수 있으니 추가 조사가 필요합니다.", metrics_summary
        except Exception as e:
            logger.warning(f"볼륨 {volume_id}의 상태 확인 중 오류 발생: {str(e)}")
        
        # 필수 지표가 없는 경우 (지표가 없는 것은 볼륨이 사용되지 않는다는 강한 증거)
        required_metrics = ['VolumeIdleTime', 'VolumeReadOps', 'VolumeWriteOps']
        missing_metrics = [m for m in required_metrics if m not in metrics]
        
        if missing_metrics:
            # 메트릭이 없는 것을 유휴 상태의 증거로 취급
            if not metrics or len(metrics) == 0:
                reasons.append("모든 CloudWatch 메트릭 데이터가 없음 (볼륨이 사용되지 않았거나 최근에 생성됨)")
                return True, "모든 CloudWatch 메트릭 데이터가 없어 볼륨이 사용되지 않는 것으로 판단됩니다.", {'missing_metrics': required_metrics}
            elif len(missing_metrics) == len(required_metrics):
                reasons.append(f"모든 필수 메트릭({', '.join(missing_metrics)})이 누락됨 (볼륨이 사용되지 않음)")
                return True, f"모든 필수 메트릭({', '.join(missing_metrics)})이 누락되어 볼륨이 사용되지 않는 것으로 판단됩니다.", {'missing_metrics': missing_metrics}
            else:
                # 일부 메트릭만 누락된 경우 계속 분석 진행
                logger.info(f"볼륨 {volume_id}에서 일부 필수 메트릭({', '.join(missing_metrics)})이 누락되었지만 분석을 계속합니다.")
        
        # 지표 형식 확인 및 처리
        is_new_format = isinstance(next(iter(metrics.values() if metrics else []), {}), dict) and 'latest' in next(iter(metrics.values() if metrics else []), {})
        logger.debug(f"메트릭 형식 감지: {'새 형식' if is_new_format else '기존 형식'}")
        
        # 유휴 시간 비율 검사
        if 'VolumeIdleTime' in metrics:
            # VolumeIdleTime은 "분당 초" 단위로, 최대값은 60초입니다
            # 이것을 퍼센트로 변환해야 합니다 (예: 59.87초 -> 99.78%)
            if is_new_format:
                # 새 형식: metrics[metric_name]이 dictionary임
                idle_time_seconds = metrics['VolumeIdleTime'].get('average', 
                               metrics['VolumeIdleTime'].get('latest', 0))
                # 초 -> 퍼센트 변환
                idle_time_percent = (idle_time_seconds / 60) * 100
            else:
                # 기존 형식: metrics[metric_name]이 datapoints 리스트임
                avg_idle_seconds = sum(dp['Average'] for dp in metrics['VolumeIdleTime']) / len(metrics['VolumeIdleTime'])
                # 초 -> 퍼센트 변환
                idle_time_percent = (avg_idle_seconds / 60) * 100
            
            logger.info(f"{volume_id} 볼륨의 유휴 시간: {idle_time_percent:.2f}% (원시값: {idle_time_seconds if is_new_format else avg_idle_seconds:.2f}초/분)")
            
            metrics_summary['idle_time'] = {
                'value_seconds': idle_time_seconds if is_new_format else avg_idle_seconds,
                'percent': idle_time_percent,
                'threshold': self.criteria['idle_time_threshold']
            }
            
            if idle_time_percent >= self.criteria['idle_time_threshold']:
                reasons.append(f"유휴 시간 비율: {idle_time_percent:.2f}% (임계값: {self.criteria['idle_time_threshold']}%)")
            else:
                return False, f"유휴 시간 비율({idle_time_percent:.2f}%)이 임계값({self.criteria['idle_time_threshold']}%) 미만입니다.", metrics_summary
        else:
            # VolumeIdleTime 측정값이 없는 경우 - 오랜 시간 동안 측정 데이터가 없는 것은 볼륨이 사용되지 않고 있다는 신호일 수 있음
            logger.info(f"볼륨 {volume_id}에 VolumeIdleTime 메트릭이 없습니다. 다른 기준으로 평가합니다.")
        
        # IO 작업 및 처리량 평가 (일시적으로 조건에서 제외되어 있지만 여전히 로깅됨)
        # 실제로 적용하려면 아래 주석 처리된 로직을 활성화해야 함
        
        # 모든 조건을 충족하면 유휴 상태로 판단
        if reasons:
            return True, " / ".join(reasons), metrics_summary
        else:
            return False, "유휴 상태 판단 기준을 충족하지 않습니다.", metrics_summary
    
    def detect_idle_volumes(self, volumes):
        """
        유휴 상태의 볼륨을 감지
        
        :param volumes: 분석할 볼륨 목록
        :return: 유휴 상태로 감지된 볼륨 정보 리스트
        """
        idle_volumes = []
        end_time = datetime.now()
        start_time = end_time - timedelta(days=self.criteria['days_to_check'])
        
        for volume in volumes:
            volume_id = volume['VolumeId']
            
            try:
                logger.info(f"{volume_id} 볼륨 유휴 상태 분석 중...")
                
                # 볼륨이 'available' 상태인지 먼저 확인 (어떤 인스턴스에도 연결되지 않음)
                if volume['State'] == 'available':
                    logger.info(f"{volume_id} 볼륨이 'available' 상태로, 자동으로 유휴 상태로 감지됩니다.")
                    
                    # 유휴 볼륨으로 판단된 경우 정보 저장
                    volume_info = {
                        'volume_id': volume_id,
                        'volume_type': volume['VolumeType'],
                        'size': volume['Size'],
                        'create_time': volume['CreateTime'].isoformat(),
                        'state': volume['State'],
                        'availability_zone': volume['AvailabilityZone'],
                        'idle_reason': "볼륨이 어떤 인스턴스에도 연결되어 있지 않습니다.",
                        'monthly_cost': calculate_monthly_cost(volume['Size'], volume['VolumeType'], self.region),
                        'attached_instances': [],
                        'metrics_summary': {'volume_state': {'state': 'available'}}
                    }
                    
                    # 권장 조치 추가
                    if volume['VolumeType'] in ['io1', 'io2']:
                        volume_info['recommendation'] = 'Idle volume detected. Consider creating a snapshot and deleting the volume or changing type to gp3.'
                    else:
                        volume_info['recommendation'] = 'Idle volume detected. Consider creating a snapshot and deleting the volume or resizing to the minimum required size.'
                    
                    idle_volumes.append(volume_info)
                    continue  # 다음 볼륨으로 넘어감
                
                # CloudWatch 지표 수집
                metrics = self.get_volume_metrics(volume_id, start_time, end_time)
                
                # 유휴 상태 판단
                is_idle, reason, metrics_summary = self.is_idle_volume(volume_id, metrics)
                
                if is_idle:
                    # 유휴 볼륨으로 판단된 경우 정보 저장
                    volume_info = {
                        'volume_id': volume_id,
                        'volume_type': volume['VolumeType'],
                        'size': volume['Size'],
                        'create_time': volume['CreateTime'].isoformat(),
                        'state': volume['State'],
                        'availability_zone': volume['AvailabilityZone'],
                        'idle_reason': reason,
                        'monthly_cost': calculate_monthly_cost(volume['Size'], volume['VolumeType'], self.region),
                        'attached_instances': [],
                        'metrics_summary': metrics_summary  # 메트릭 요약 정보 추가
                    }
                    
                    # 연결된 인스턴스 정보 추가
                    if volume['Attachments']:
                        for attachment in volume['Attachments']:
                            volume_info['attached_instances'].append({
                                'instance_id': attachment['InstanceId'],
                                'attach_time': attachment['AttachTime'].isoformat(),
                                'device': attachment['Device']
                            })
                    
                    # 권장 조치 추가 - 유휴 볼륨에 맞는 적절한 추천으로 변경
                    if volume['VolumeType'] in ['io1', 'io2']:
                        volume_info['recommendation'] = 'Idle volume detected. Consider creating a snapshot and deleting the volume or changing type to gp3.'
                    else:
                        volume_info['recommendation'] = 'Idle volume detected. Consider creating a snapshot and deleting the volume or resizing to the minimum required size.'
                    
                    idle_volumes.append(volume_info)
                    logger.info(f"{volume_id} 볼륨이 유휴 상태로 감지되었습니다: {reason}")
                else:
                    logger.info(f"{volume_id} 볼륨은 유휴 상태가 아닙니다: {reason}")
            
            except Exception as e:
                logger.error(f"{volume_id} 볼륨 분석 중 오류 발생: {str(e)}", exc_info=True)
        
        logger.info(f"{self.region} 리전에서 총 {len(idle_volumes)}개의 유휴 볼륨이 감지되었습니다.")
        return idle_volumes