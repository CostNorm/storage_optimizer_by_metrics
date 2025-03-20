import logging
from datetime import datetime, timedelta
from config import METRIC_PERIOD
from utils import calculate_monthly_cost

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
        
        # 필수 지표가 없는 경우 (지표가 없는 것은 볼륨이 사용되지 않는다는 강한 증거)
        required_metrics = ['VolumeIdleTime', 'VolumeReadOps', 'VolumeWriteOps']
        missing_metrics = [m for m in required_metrics if m not in metrics]
        
        if missing_metrics:
            return False, f"일부 필수 지표({', '.join(missing_metrics)})가 누락되어 분석할 수 없습니다.", None
        
        # 지표 형식 확인 및 처리
        is_new_format = isinstance(metrics.get('VolumeIdleTime'), dict) and 'latest' in metrics.get('VolumeIdleTime', {})
        
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
        
        # IO 작업 수 검사 (일시적으로 조건에서 제외)
        if all(k in metrics for k in ['VolumeReadOps', 'VolumeWriteOps']):
            if is_new_format:
                # 새 형식
                read_ops = metrics['VolumeReadOps'].get('latest', 0)  
                write_ops = metrics['VolumeWriteOps'].get('latest', 0)
                total_ops = read_ops + write_ops
            else:
                # 기존 형식
                avg_read_ops = sum(dp['Sum'] for dp in metrics['VolumeReadOps']) / len(metrics['VolumeReadOps'])
                avg_write_ops = sum(dp['Sum'] for dp in metrics['VolumeWriteOps']) / len(metrics['VolumeWriteOps'])
                total_ops = avg_read_ops + avg_write_ops
            
            logger.info(f"{volume_id} 볼륨의 총 IO 작업: {total_ops:.2f}/일")
            
            metrics_summary['io_operations'] = {
                'read_ops': read_ops if is_new_format else avg_read_ops,
                'write_ops': write_ops if is_new_format else avg_write_ops,
                'total_ops': total_ops,
                'threshold': self.criteria['io_ops_threshold']
            }
            
            # 유휴 상태 판단에서는 IO 작업 수 조건을 일시적으로 제외함
            # 정보 수집 목적으로만 기록
            if total_ops < self.criteria['io_ops_threshold']:
                reasons.append(f"평균 IO 작업 수: {total_ops:.2f}/일 (임계값: {self.criteria['io_ops_threshold']}/일) - 현재 조건에서 제외됨")
            else:
                logger.info(f"{volume_id} 볼륨의 IO 작업 수({total_ops:.2f}/일)가 임계값({self.criteria['io_ops_threshold']}/일)을 초과하지만 유휴 상태 판단에서 일시적으로 제외됨")
                # 기존 코드: 조건 불충족 시 바로 종료
                # return False, f"평균 IO 작업 수({total_ops:.2f}/일)가 임계값({self.criteria['io_ops_threshold']}/일)을 초과합니다.", metrics_summary
        
        # 데이터 처리량 검사 (일시적으로 조건에서 제외)
        if all(k in metrics for k in ['VolumeReadBytes', 'VolumeWriteBytes']):
            if is_new_format:
                # 새 형식
                read_bytes = metrics['VolumeReadBytes'].get('latest', 0)
                write_bytes = metrics['VolumeWriteBytes'].get('latest', 0)
                total_bytes = read_bytes + write_bytes
            else:
                # 기존 형식
                avg_read_bytes = sum(dp['Sum'] for dp in metrics['VolumeReadBytes']) / len(metrics['VolumeReadBytes'])
                avg_write_bytes = sum(dp['Sum'] for dp in metrics['VolumeWriteBytes']) / len(metrics['VolumeWriteBytes'])
                total_bytes = avg_read_bytes + avg_write_bytes
            
            logger.info(f"{volume_id} 볼륨의 총 처리량: {total_bytes/(1024*1024):.2f}MB/일")
            
            metrics_summary['throughput'] = {
                'read_bytes': read_bytes if is_new_format else avg_read_bytes,
                'write_bytes': write_bytes if is_new_format else avg_write_bytes,
                'total_bytes': total_bytes,
                'threshold': self.criteria['throughput_threshold']
            }
            
            # 유휴 상태 판단에서는 처리량 조건을 일시적으로 제외함
            # 정보 수집 목적으로만 기록
            if total_bytes < self.criteria['throughput_threshold']:
                reasons.append(f"평균 처리량: {total_bytes/(1024*1024):.2f}MB/일 (임계값: {self.criteria['throughput_threshold']/(1024*1024)}MB/일) - 현재 조건에서 제외됨")
            else:
                logger.info(f"{volume_id} 볼륨의 처리량({total_bytes/(1024*1024):.2f}MB/일)이 임계값({self.criteria['throughput_threshold']/(1024*1024)}MB/일)을 초과하지만 유휴 상태 판단에서 일시적으로 제외됨")
                # 기존 코드: 조건 불충족 시 바로 종료
                # return False, f"평균 처리량({total_bytes/(1024*1024):.2f}MB/일)이 임계값({self.criteria['throughput_threshold']/(1024*1024)}MB/일)을 초과합니다.", metrics_summary
        
        # BurstBalance 검사 (gp2, st1, sc1 타입에만 해당)
        if 'BurstBalance' in metrics and metrics['BurstBalance']:
            avg_burst_balance = sum(dp['Average'] for dp in metrics['BurstBalance']) / len(metrics['BurstBalance'])
            
            metrics_summary['burst_balance'] = {
                'average': avg_burst_balance,
                'maximum': max(dp['Average'] for dp in metrics['BurstBalance']),
                'minimum': min(dp['Average'] for dp in metrics['BurstBalance']),
                'threshold': self.criteria['burst_balance_threshold']
            }
            
            if avg_burst_balance > self.criteria['burst_balance_threshold']:
                reasons.append(f"평균 버스트 밸런스: {avg_burst_balance:.2f}% (임계값: {self.criteria['burst_balance_threshold']}%)")
        
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
                        volume_info['recommendation'] = '유휴 상태입니다. 스냅샷 생성 후 볼륨 삭제 또는 gp3로 변경 고려'
                    else:
                        volume_info['recommendation'] = '유휴 상태입니다. 스냅샷 생성 후 볼륨 삭제 또는 필요 최소 크기로 축소 고려'
                    
                    idle_volumes.append(volume_info)
                    logger.info(f"{volume_id} 볼륨이 유휴 상태로 감지되었습니다: {reason}")
                else:
                    logger.info(f"{volume_id} 볼륨은 유휴 상태가 아닙니다: {reason}")
            
            except Exception as e:
                logger.error(f"{volume_id} 볼륨 분석 중 오류 발생: {str(e)}", exc_info=True)
        
        logger.info(f"{self.region} 리전에서 총 {len(idle_volumes)}개의 유휴 볼륨이 감지되었습니다.")
        return idle_volumes
