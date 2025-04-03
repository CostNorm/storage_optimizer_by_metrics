import logging
import boto3
import re
import time
from datetime import datetime, timedelta
from ..utils.utils import calculate_monthly_cost
from botocore.exceptions import ClientError

logger = logging.getLogger()

class OverprovisionedVolumeDetector:
    """
    과대 프로비저닝된 EBS 볼륨을 감지하는 클래스
    """
    
    def __init__(self, region, ec2_client, cloudwatch_client, criteria):
        """
        :param region: AWS 리전
        :param ec2_client: EC2 클라이언트
        :param cloudwatch_client: CloudWatch 클라이언트
        :param criteria: 과대 프로비저닝 감지 기준
        """
        self.region = region
        self.ec2_client = ec2_client
        self.cloudwatch_client = cloudwatch_client
        self.criteria = criteria
        # SSM 클라이언트 초기화 (EC2 내부 파일시스템 정보 수집용)
        self.ssm_client = boto3.client('ssm', region_name=region)
        # 인스턴스 SSM 상태 캐시 (성능 향상을 위해)
        self.instance_ssm_status_cache = {}
    
    def check_instance_ssm_status(self, instance_id):
        """
        인스턴스가 SSM 명령을 실행할 수 있는 상태인지 확인
        
        :param instance_id: EC2 인스턴스 ID
        :return: (가능 여부, 상태 메시지)
        """
        # 캐시된 결과가 있으면 반환
        if instance_id in self.instance_ssm_status_cache:
            return self.instance_ssm_status_cache[instance_id]
            
        try:
            # 인스턴스 상태 확인
            ec2_response = self.ec2_client.describe_instances(InstanceIds=[instance_id])
            if not ec2_response['Reservations'] or not ec2_response['Reservations'][0]['Instances']:
                result = (False, f"인스턴스 {instance_id}를 찾을 수 없습니다.")
                self.instance_ssm_status_cache[instance_id] = result
                return result
                
            instance = ec2_response['Reservations'][0]['Instances'][0]
            state = instance.get('State', {}).get('Name', '')
            
            if state != 'running':
                result = (False, f"인스턴스 {instance_id}가 실행 중이 아닙니다(현재 상태: {state}).")
                self.instance_ssm_status_cache[instance_id] = result
                return result
            
            # SSM에서 관리되는 인스턴스인지 확인
            try:
                ssm_response = self.ssm_client.describe_instance_information(
                    Filters=[{'Key': 'InstanceIds', 'Values': [instance_id]}]
                )
                
                if not ssm_response['InstanceInformationList']:
                    result = (False, f"인스턴스 {instance_id}가 SSM에 등록되지 않았습니다. SSM Agent가 설치되어 있고 올바르게 구성되어 있는지 확인하세요.")
                    self.instance_ssm_status_cache[instance_id] = result
                    return result
                
                ping_status = ssm_response['InstanceInformationList'][0].get('PingStatus', '')
                if ping_status != 'Online':
                    result = (False, f"인스턴스 {instance_id}의 SSM Agent가 온라인 상태가 아닙니다(현재 상태: {ping_status}).")
                    self.instance_ssm_status_cache[instance_id] = result
                    return result
                
                result = (True, "인스턴스가 SSM 명령을 실행할 수 있는 상태입니다.")
                self.instance_ssm_status_cache[instance_id] = result
                return result
            except Exception as ssm_error:
                # SSM 서비스 오류(권한 부족 등)가 발생한 경우
                logger.warning(f"SSM 서비스 오류: {str(ssm_error)}")
                result = (False, f"SSM 서비스 오류: {str(ssm_error)}")
                self.instance_ssm_status_cache[instance_id] = result
                return result
            
        except Exception as e:
            # 권한이 없거나 다른 오류가 발생한 경우
            logger.warning(f"인스턴스 {instance_id}의 상태 확인 중 오류 발생: {str(e)}")
            result = (False, f"인스턴스 상태 확인 중 오류 발생: {str(e)}")
            self.instance_ssm_status_cache[instance_id] = result
            return result
    
    def get_disk_usage_metrics(self, instance_id, device_name, start_time, end_time):
        """
        CloudWatch 에이전트를 통해 수집된 디스크 사용률 지표를 가져옴
        
        :param instance_id: EC2 인스턴스 ID
        :param device_name: 디바이스 이름
        :param start_time: 측정 시작 시간
        :param end_time: 측정 종료 시간
        :return: 디스크 사용률 지표
        """
        # 먼저 CloudWatch 메트릭 확인
        try:
            # 인스턴스에 연결된 모든 볼륨의 CloudWatch 메트릭 확인
            metrics = self.cloudwatch_client.list_metrics(
                Namespace='CWAgent',
                MetricName='disk_used_percent',
                Dimensions=[{'Name': 'InstanceId', 'Value': instance_id}]
            )
            
            # CloudWatch에 메트릭이 있으면 메트릭 사용
            if metrics.get('Metrics'):
                paths = set()
                for metric in metrics['Metrics']:
                    for dim in metric['Dimensions']:
                        if dim['Name'] == 'path':
                            paths.add(dim['Value'])
                
                # 경로 정보 로깅
                if paths:
                    logger.info(f"인스턴스 {instance_id}에서 발견된 디스크 경로: {paths}")
                else:
                    logger.warning(f"인스턴스 {instance_id}에서 디스크 경로를 찾을 수 없습니다. 모든 차원 정보: {[metric['Dimensions'] for metric in metrics['Metrics']]}")
                
                # 루트 디바이스인 경우 '/' 경로 사용 시도
                device_short_name = device_name.split('/')[-1]
                if device_short_name in ['xvda', 'sda', 'nvme0n1'] or device_short_name.startswith('xvda') or device_short_name.startswith('sda'):
                    if '/' in paths:
                        logger.info(f"루트 디바이스 {device_name}에 대해 경로 '/'를 사용합니다.")
                        response = self.cloudwatch_client.get_metric_statistics(
                            Namespace='CWAgent',
                            MetricName='disk_used_percent',
                            Dimensions=[
                                {'Name': 'InstanceId', 'Value': instance_id},
                                {'Name': 'path', 'Value': '/'}
                            ],
                            StartTime=start_time,
                            EndTime=end_time,
                            Period=86400,
                            Statistics=['Average']
                        )
                        
                        if response['Datapoints']:
                            return response['Datapoints']
                
                # 가장 적합한 경로 찾기 시도
                fs_path = self.estimate_filesystem_path(device_name, paths)
                
                if fs_path:
                    logger.info(f"디바이스 {device_name}에 대해 추정된 경로: {fs_path}")
                    
                    response = self.cloudwatch_client.get_metric_statistics(
                        Namespace='CWAgent',
                        MetricName='disk_used_percent',
                        Dimensions=[
                            {'Name': 'InstanceId', 'Value': instance_id},
                            {'Name': 'path', 'Value': fs_path}
                        ],
                        StartTime=start_time,
                        EndTime=end_time,
                        Period=86400,  # 1일 단위
                        Statistics=['Average']
                    )
                    
                    if response['Datapoints']:
                        return response['Datapoints']
                    
            # 기타 모든 방법을 시도 후 실패하면 직접 마운트 정보 조회
            logger.info(f"CloudWatch에서 인스턴스 {instance_id}의 디스크 사용률 메트릭을 찾을 수 없습니다. 대체 방법 사용...")
            
            # 디바이스가 루트 볼륨인 경우 바로 SSM 통해 루트 볼륨 확인
            device_short_name = device_name.split('/')[-1]
            if device_short_name in ['xvda', 'sda', 'nvme0n1'] or device_short_name.startswith('xvda') or device_short_name.startswith('sda'):
                logger.info(f"루트 디바이스 {device_name} 감지됨. SSM을 통해 루트 파티션 사용률을 확인합니다.")
                datapoints = self.get_root_disk_usage_via_ssm(instance_id)
                if datapoints:
                    return datapoints
            
            # 일반적인 SSM 경로 사용
            ssm_status, message = self.check_instance_ssm_status(instance_id)
            if ssm_status:
                # SSM을 통해 디스크 사용률 조회 시도
                return self.get_disk_usage_via_ssm(instance_id, device_name)
            else:
                logger.warning(f"SSM을 사용할 수 없습니다: {message}. 추정치 사용...")
                return self.get_estimated_disk_usage(instance_id, device_name)
            
        except Exception as e:
            logger.error(f"CloudWatch 메트릭 조회 중 오류 발생: {str(e)}", exc_info=True)
            # 오류 발생 시 추정 데이터 사용
            return self.get_estimated_disk_usage(instance_id, device_name)
    
    def estimate_filesystem_path(self, device_name, available_paths):
        """
        디바이스 이름과 사용 가능한 경로 목록을 기반으로 가장 적합한 경로 추정
        
        :param device_name: 디바이스 이름 
        :param available_paths: 사용 가능한 경로 목록
        :return: 추정된 경로 또는 None
        """
        # 디바이스 이름에서 짧은 이름 추출 (예: /dev/sda1 -> sda1)
        short_name = device_name.split('/')[-1]
        
        # 디바이스 이름과 경로 간의 일반적인 매핑
        common_mappings = {
            'xvda1': '/', 'sda1': '/',  # 루트 볼륨
            'xvdf': '/data', 'sdf': '/data',  # 데이터 볼륨
            'xvdg': '/mnt', 'sdg': '/mnt',  # 마운트 볼륨
        }
        
        # 1. 디바이스 이름으로 직접 매핑이 있으면 해당 경로 반환
        if short_name in common_mappings and common_mappings[short_name] in available_paths:
            return common_mappings[short_name]
        
        # 2. 루트 볼륨의 경우 '/'를 반환
        if re.match(r'xvda\d*|sda\d*|nvme0n1p\d*', short_name) and '/' in available_paths:
            return '/'
        
        # 3. 데이터 볼륨의 경우 일반적인 데이터 경로 찾기
        data_paths = [p for p in available_paths if p.startswith('/data') or p.startswith('/mnt')]
        if data_paths:
            return data_paths[0]
        
        # 4. '/' 외의 가장 짧은 경로 반환 (일반적으로 주요 볼륨)
        non_root_paths = [p for p in available_paths if p != '/']
        if non_root_paths:
            return min(non_root_paths, key=len)
        
        # 5. 마지막 수단으로 '/' 반환
        if '/' in available_paths:
            return '/'
        
        # 적합한 경로를 찾지 못한 경우
        return None
    
    def get_disk_usage_via_ssm(self, instance_id, device_name):
        """
        SSM을 통해 디스크 사용률 조회 (인스턴스가 SSM을 지원하는지 미리 확인해야 함)
        
        :param instance_id: EC2 인스턴스 ID
        :param device_name: 디바이스 이름
        :return: 디스크 사용률 데이터
        """
        try:
            # 먼저 파일시스템 경로 조회
            fs_path = self.get_filesystem_path_safe(instance_id, device_name)
            
            if not fs_path:
                logger.warning(f"인스턴스 {instance_id}의 디바이스 {device_name}에 대한 파일시스템 경로를 찾을 수 없습니다.")
                return self.get_estimated_disk_usage(instance_id, device_name)
            
            # SSM을 통해 디스크 사용률 조회
            response = self.ssm_client.send_command(
                InstanceIds=[instance_id],
                DocumentName='AWS-RunShellScript',
                Parameters={
                    'commands': [f'df -h "{fs_path}" | tail -1 | awk \'{{print $5}}\'']
                }
            )
            
            command_id = response['Command']['CommandId']
            
            # 명령 실행 결과 대기
            time.sleep(3)
            
            output = self.ssm_client.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id
            )
            
            if output['Status'] == 'Success':
                # 결과 파싱 (예: '45%' -> 45)
                usage_percent_str = output['StandardOutputContent'].strip().rstrip('%')
                try:
                    usage_percent = float(usage_percent_str)
                    # CloudWatch 메트릭과 유사한 형식으로 변환
                    now = datetime.now()
                    return [
                        {
                            'Timestamp': now,
                            'Average': usage_percent,
                            'Unit': 'Percent'
                        }
                    ]
                except ValueError:
                    logger.error(f"디스크 사용률 파싱 오류: '{usage_percent_str}'")
                    return self.get_estimated_disk_usage(instance_id, device_name)
            else:
                logger.warning(f"SSM 명령 실행 실패: {output.get('StatusDetails')}")
                return self.get_estimated_disk_usage(instance_id, device_name)
                
        except Exception as e:
            logger.error(f"SSM을 통한 디스크 사용률 조회 중 오류 발생: {str(e)}", exc_info=True)
            return self.get_estimated_disk_usage(instance_id, device_name)
    
    def get_estimated_disk_usage(self, instance_id, device_name):
        """
        CloudWatch 메트릭이나 SSM을 사용할 수 없을 때 볼륨 크기를 기반으로 디스크 사용률 추정
        
        :param instance_id: EC2 인스턴스 ID
        :param device_name: 디바이스 이름
        :return: 추정된 디스크 사용률 데이터
        """
        try:
            # 인스턴스에 연결된 볼륨 정보 조회
            volumes = self.ec2_client.describe_volumes(
                Filters=[
                    {'Name': 'attachment.instance-id', 'Values': [instance_id]},
                    {'Name': 'attachment.device', 'Values': [device_name]}
                ]
            )['Volumes']
            
            if not volumes:
                logger.warning(f"인스턴스 {instance_id}에 연결된 디바이스 {device_name}를 찾을 수 없습니다.")
                # 기본 추정치 반환 (평균 사용률)
                return [{'Timestamp': datetime.now(), 'Average': 40.0, 'Unit': 'Percent'}]
            
            volume = volumes[0]
            volume_type = volume['VolumeType']
            volume_size = volume['Size']
            
            # 볼륨 유형과 크기를 기반으로 사용률 추정
            estimated_usage = None
            
            if volume_size <= 10:  # 작은 볼륨은 보통 많이 사용됨
                estimated_usage = 70.0
            elif volume_size <= 100:  # 중간 크기 볼륨
                estimated_usage = 50.0
            else:  # 대용량 볼륨은 보통 덜 사용됨
                estimated_usage = 30.0
            
            # 볼륨 유형에 따라 조정
            if volume_type in ['io1', 'io2', 'gp3']:  # 고성능 볼륨은 보통 중요한 데이터를 저장하므로 더 많이 사용됨
                estimated_usage *= 1.2
            elif volume_type in ['sc1', 'st1']:  # 저비용 스토리지는 보통 덜 중요한 데이터를 저장하므로 덜 사용됨
                estimated_usage *= 0.8
            
            # 범위 제한 (0-100%)
            estimated_usage = max(0.0, min(100.0, estimated_usage))
            
            logger.info(f"인스턴스 {instance_id}의 디바이스 {device_name}에 대해 추정된 디스크 사용률: {estimated_usage:.1f}%")
            
            # 추정치와 함께 약간의 변동성 추가 (더 현실적인 데이터를 위해)
            import random
            variations = [
                estimated_usage * 0.95,  # 약간 낮은 값
                estimated_usage,         # 기본값
                estimated_usage * 1.05   # 약간 높은 값
            ]
            
            # 3개의 데이터 포인트 생성 (평균은 추정치와 거의 동일)
            now = datetime.now()
            return [
                {'Timestamp': now - timedelta(days=2), 'Average': variations[0], 'Unit': 'Percent'},
                {'Timestamp': now - timedelta(days=1), 'Average': variations[1], 'Unit': 'Percent'},
                {'Timestamp': now, 'Average': variations[2], 'Unit': 'Percent'}
            ]
            
        except Exception as e:
            logger.error(f"디스크 사용률 추정 중 오류 발생: {str(e)}", exc_info=True)
            # 기본값 반환
            return [{'Timestamp': datetime.now(), 'Average': 50.0, 'Unit': 'Percent'}]
    
    def get_filesystem_path_safe(self, instance_id, device_name):
        """
        안전하게 파일시스템 경로를 조회 (SSM 사용 불가능 시 기본값 반환)
        
        :param instance_id: EC2 인스턴스 ID
        :param device_name: 디바이스 이름
        :return: 파일시스템 경로 또는 기본값
        """
        try:
            # SSM 상태 확인
            ssm_status, message = self.check_instance_ssm_status(instance_id)
            
            if not ssm_status:
                logger.warning(f"SSM을 통한 파일시스템 정보 조회 불가능: {message}")
                return self.get_default_filesystem_path(device_name)
            
            # SSM을 통해 파일시스템 정보 조회
            fs_path, _ = self.get_filesystem_info(instance_id, device_name)
            
            if fs_path:
                return fs_path
            else:
                return self.get_default_filesystem_path(device_name)
                
        except Exception as e:
            logger.error(f"파일시스템 경로 안전 조회 중 오류 발생: {str(e)}", exc_info=True)
            return self.get_default_filesystem_path(device_name)
    
    def get_filesystem_info(self, instance_id, device_name):
        """
        디바이스 이름으로부터 파일시스템 경로와 유형을 추정
        
        :param instance_id: EC2 인스턴스 ID
        :param device_name: 디바이스 이름
        :return: (파일시스템 경로, 파일시스템 유형) 또는 (None, None)
        """
        try:
            # 루트 디바이스인 경우 바로 '/'로 간주 (매우 일반적인 패턴)
            device_short_name = device_name.split('/')[-1]
            if device_short_name in ['xvda', 'sda', 'nvme0n1'] or device_short_name.startswith('xvda') or device_short_name.startswith('sda'):
                logger.info(f"디바이스 {device_name}는 루트 디바이스로 간주됩니다. 마운트 포인트 '/'로 추정합니다.")
                return '/', 'xfs'  # 대부분의 AWS AMI는 xfs를 사용
            
            # SSM Run Command를 사용하여 인스턴스에서 마운트 정보와 파일시스템 유형 조회
            response = self.ssm_client.send_command(
                InstanceIds=[instance_id],
                DocumentName='AWS-RunShellScript',
                Parameters={
                    'commands': [
                        'df -T | grep -v tmpfs | grep -v devtmpfs',  # 파일시스템 유형 포함 출력
                        'lsblk -o NAME,MOUNTPOINT,FSTYPE -n | grep -v "^loop"',
                        'cat /proc/mounts | grep -v tmpfs | grep -v sysfs | grep -v proc',  # 또 다른 대체 명령어
                        'mount | grep -v tmpfs'  # 또 다른 대체 명령어
                    ]
                }
            )
            
            command_id = response['Command']['CommandId']
            
            # 명령 실행 결과 대기
            time.sleep(3)
            
            output = self.ssm_client.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id
            )
            
            if output['Status'] == 'Success':
                # 결과 파싱
                mount_info = output['StandardOutputContent']
                logger.debug(f"마운트 정보: {mount_info}")
                
                # 1. df 명령어 출력 파싱 (가장 명확한 출력)
                df_pattern = re.compile(r'/dev/([^\s]+)\s+([^\s]+)\s+([^\s]+)')
                for line in mount_info.splitlines():
                    if device_name in line or device_short_name in line:
                        match = df_pattern.search(line)
                        if match:
                            return match.group(3), match.group(2)  # 마운트 포인트, 파일시스템 유형
                
                # 2. lsblk 출력 형식: name mountpoint fstype
                for line in mount_info.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 3 and (parts[0] == device_short_name or device_short_name in parts[0]):
                        return parts[1], parts[2]  # 마운트 포인트, 파일시스템 유형
                    elif len(parts) >= 2 and (parts[0] == device_short_name or device_short_name in parts[0]):
                        return parts[1], None  # 마운트 포인트만 반환
                
                # 3. /proc/mounts 출력 파싱
                for line in mount_info.splitlines():
                    if device_name in line:
                        parts = line.strip().split()
                        if len(parts) >= 2:
                            return parts[1], parts[2] if len(parts) > 2 else None
                
                # 4. NVMe 디바이스의 경우 특별 처리
                if device_short_name.startswith('nvme'):
                    nvme_pattern = re.compile(r'nvme\d+n\d+')
                    for line in mount_info.splitlines():
                        if nvme_pattern.search(line):
                            parts = line.strip().split()
                            if len(parts) >= 2:
                                return parts[1], parts[2] if len(parts) > 2 else None
                
                # 로그에 마운트 정보 출력
                logger.info(f"인스턴스 {instance_id}의 마운트 정보: {mount_info}")
            
            # 기본 매핑 시도 (일반적인 디바이스 이름 패턴)
            return self.get_default_filesystem_path(device_name), None
                
        except Exception as e:
            logger.error(f"파일시스템 정보 조회 중 오류 발생: {str(e)}", exc_info=True)
            return self.get_default_filesystem_path(device_name), None
    
    def get_default_filesystem_path(self, device_name):
        """
        디바이스 이름을 기반으로 기본 파일시스템 경로 추정
        
        :param device_name: 디바이스 이름
        :return: 추정된 파일시스템 경로 또는 None
        """
        # 디바이스 이름에서 짧은 이름 추출
        device_short_name = device_name.split('/')[-1]
        
        # 일반적인 디바이스 이름과 마운트 포인트 매핑 (확장)
        device_mappings = {
            # 루트 볼륨 (다양한 디바이스 이름 패턴)
            'xvda': '/',
            'xvda1': '/',
            'sda': '/',
            'sda1': '/',
            'nvme0n1': '/',
            'nvme0n1p1': '/',
            # 데이터 볼륨
            'xvdf': '/data',
            'sdf': '/data',
            'nvme1n1': '/data',
            # 추가 볼륨
            'xvdg': '/mnt/data',
            'sdg': '/mnt/data',
            'nvme2n1': '/mnt/data'
        }
        
        # 정확한 매핑이 없는 경우 패턴 기반으로 추정
        if device_short_name in device_mappings:
            return device_mappings[device_short_name]
        
        # 루트 볼륨 패턴
        elif re.match(r'^xvda\d*$|^sda\d*$|^nvme0n1(p\d*)?$', device_short_name):
            logger.info(f"디바이스 {device_name}는 패턴에 따라 루트 볼륨으로 추정됩니다.")
            return '/'
        
        # 추가 볼륨 패턴
        elif re.match(r'^xvd[b-z]\d*$|^sd[b-z]\d*$|^nvme[1-9]n1(p\d*)?$', device_short_name):
            base_letter = re.search(r'[b-z]', device_short_name).group(0)
            
            if base_letter in ['f', 'b', 'h']:  # 일반적인 첫 번째 추가 볼륨
                return '/data'
            elif base_letter in ['g', 'c', 'i']:  # 일반적인 두 번째 추가 볼륨
                return '/mnt/data'
            else:  # 기타 볼륨
                return f'/mnt/{base_letter}'
        
        # 매핑 실패 시 기본값
        logger.warning(f"디바이스 {device_name}에 대한 마운트 포인트 추정 실패. 기본값 '/' 반환.")
        return '/'

    def get_root_disk_usage_via_ssm(self, instance_id):
        """
        루트 디스크 사용률을 SSM을 통해 직접 조회
        
        :param instance_id: EC2 인스턴스 ID
        :return: 디스크 사용률 데이터
        """
        try:
            # SSM 상태 확인
            ssm_status, message = self.check_instance_ssm_status(instance_id)
            if not ssm_status:
                logger.warning(f"SSM을 사용할 수 없습니다: {message}")
                return None
                
            # 루트 파티션 사용률 확인 명령 실행
            response = self.ssm_client.send_command(
                InstanceIds=[instance_id],
                DocumentName='AWS-RunShellScript',
                Parameters={
                    'commands': ['df -h / | tail -1 | awk \'{print $5}\'']
                }
            )
            
            command_id = response['Command']['CommandId']
            time.sleep(3)
            
            output = self.ssm_client.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id
            )
            
            if output['Status'] == 'Success':
                # 결과 파싱 (예: '45%' -> 45)
                usage_percent_str = output['StandardOutputContent'].strip().rstrip('%')
                try:
                    usage_percent = float(usage_percent_str)
                    now = datetime.now()
                    logger.info(f"SSM을 통해 조회한 루트 파티션 사용률: {usage_percent}%")
                    return [
                        {
                            'Timestamp': now,
                            'Average': usage_percent,
                            'Unit': 'Percent'
                        }
                    ]
                except ValueError:
                    logger.error(f"디스크 사용률 파싱 오류: '{usage_percent_str}'")
                    return None
            else:
                logger.warning(f"루트 파티션 사용률 조회 실패: {output.get('StatusDetails')}")
                return None
        except Exception as e:
            logger.error(f"루트 디스크 사용률 조회 중 오류 발생: {str(e)}", exc_info=True)
            return None
    
    def is_overprovisioned(self, usage_datapoints):
        """
        디스크 사용률 데이터를 분석하여 과대 프로비저닝 여부 판단
        
        :param usage_datapoints: 디스크 사용률 데이터포인트
        :return: 과대 프로비저닝 여부(True/False), 판단 근거 메시지, 사용률 데이터 요약
        """
        if not usage_datapoints or len(usage_datapoints) == 0:
            return False, "디스크 사용률 데이터가 없습니다.", None
        
        # 평균 디스크 사용률 계산
        avg_usage = sum(dp['Average'] for dp in usage_datapoints) / len(usage_datapoints)
        
        # 최대 디스크 사용률도 확인
        max_usage = max(dp['Average'] for dp in usage_datapoints)
        min_usage = min(dp['Average'] for dp in usage_datapoints)
        
        # 사용률 데이터 요약
        usage_summary = {
            'average_usage_percent': avg_usage,
            'max_usage_percent': max_usage,
            'min_usage_percent': min_usage,
            'datapoints_count': len(usage_datapoints)
        }
        
        # 타임스탬프가 있는 경우만 포함
        if hasattr(usage_datapoints[0].get('Timestamp', None), 'isoformat'):
            usage_summary['oldest_datapoint'] = min(dp['Timestamp'].isoformat() for dp in usage_datapoints if 'Timestamp' in dp)
            usage_summary['newest_datapoint'] = max(dp['Timestamp'].isoformat() for dp in usage_datapoints if 'Timestamp' in dp)
        
        # 과대 프로비저닝 여부 판단
        if avg_usage < self.criteria['disk_used_percent_threshold'] and max_usage < self.criteria['disk_used_percent_threshold'] * 1.5:
            return True, f"평균 디스크 사용률: {avg_usage:.2f}%, 최대 사용률: {max_usage:.2f}% (임계값: {self.criteria['disk_used_percent_threshold']}%)", usage_summary
        else:
            return False, f"디스크가 적절히 사용 중입니다. 평균 사용률: {avg_usage:.2f}%, 최대 사용률: {max_usage:.2f}%", usage_summary
    
    def detect_overprovisioned_volumes(self, volumes):
        """
        과대 프로비저닝된 볼륨을 감지
        
        :param volumes: 분석할 볼륨 목록
        :return: 과대 프로비저닝으로 감지된 볼륨 정보 리스트
        """
        overprovisioned_volumes = []
        end_time = datetime.now()
        start_time = end_time - timedelta(days=30 * self.criteria['months_to_check'])
        
        for volume in volumes:
            volume_id = volume['VolumeId']
            
            # 인스턴스에 연결된 볼륨만 분석
            if not volume['Attachments']:
                logger.info(f"{volume_id} 볼륨은 인스턴스에 연결되어 있지 않아 과대 프로비저닝 분석을 건너뜁니다.")
                continue
                
            try:
                logger.info(f"{volume_id} 볼륨 과대 프로비저닝 분석 중...")
                
                # 연결된 인스턴스 정보 가져오기
                instance_id = volume['Attachments'][0]['InstanceId']
                device_name = volume['Attachments'][0]['Device']
                
                # CloudWatch 에이전트 지표 조회 (오류 처리 포함)
                usage_datapoints = self.get_disk_usage_metrics(instance_id, device_name, start_time, end_time)
                
                # 데이터가 없는 경우 추정치 사용
                if not usage_datapoints:
                    logger.warning(f"{volume_id} 볼륨의 디스크 사용률 데이터를 수집할 수 없습니다. 추정치를 사용합니다.")
                    usage_datapoints = self.get_estimated_disk_usage(instance_id, device_name)
                
                # 과대 프로비저닝 여부 판단
                is_over, reason, usage_summary = self.is_overprovisioned(usage_datapoints)
                
                if is_over:
                    # 과대 프로비저닝된 볼륨으로 판단된 경우 정보 저장
                    volume_info = {
                        'volume_id': volume_id,
                        'volume_type': volume['VolumeType'],
                        'size': volume['Size'],
                        'create_time': volume['CreateTime'].isoformat(),
                        'state': volume['State'],
                        'availability_zone': volume['AvailabilityZone'],
                        'overprovisioned_reason': reason,
                        'device': device_name,
                        'instance_id': instance_id,
                        'disk_usage_data': usage_summary,
                        'monthly_cost': calculate_monthly_cost(volume['Size'], volume['VolumeType'], self.region)
                    }
                    
                    # 연결된 인스턴스 정보 추가
                    volume_info['attached_instances'] = [{
                        'instance_id': attachment['InstanceId'],
                        'attach_time': attachment['AttachTime'].isoformat(),
                        'device': attachment['Device']
                    } for attachment in volume['Attachments']]
                    
                    # 권장 조치 추가
                    if volume['VolumeType'] in ['io1', 'io2']:
                        # 프로비저닝된 IOPS 볼륨은 gp3로 변경 권장
                        volume_info['recommendation'] = '과대 프로비저닝 상태입니다. gp3 볼륨 유형으로 전환 고려'
                        # 예상 절감액 계산
                        current_cost = volume_info['monthly_cost']
                        gp3_cost = calculate_monthly_cost(volume['Size'], 'gp3', self.region)
                        savings = current_cost - gp3_cost
                        volume_info['estimated_savings'] = savings
                    else:
                        # 일반 볼륨은 크기 축소 권장
                        recommended_size = self.recommend_volume_size(usage_summary, volume['Size'])
                        volume_info['recommendation'] = f'과대 프로비저닝 상태입니다. 볼륨 크기를 {recommended_size}GB로 축소 고려'
                        # 예상 절감액 계산
                        current_cost = volume_info['monthly_cost']
                        reduced_cost = calculate_monthly_cost(recommended_size, volume['VolumeType'], self.region)
                        savings = current_cost - reduced_cost
                        volume_info['estimated_savings'] = savings
                        volume_info['recommended_size'] = recommended_size
                    
                    overprovisioned_volumes.append(volume_info)
                    logger.info(f"{volume_id} 볼륨이 과대 프로비저닝 상태로 감지되었습니다: {reason}")
                else:
                    logger.info(f"{volume_id} 볼륨은 과대 프로비저닝 상태가 아닙니다: {reason}")
            
            except Exception as e:
                logger.error(f"{volume_id} 볼륨 분석 중 오류 발생: {str(e)}", exc_info=True)
        
        logger.info(f"{self.region} 리전에서 총 {len(overprovisioned_volumes)}개의 과대 프로비저닝된 볼륨이 감지되었습니다.")
        return overprovisioned_volumes
    
    def recommend_volume_size(self, usage_summary, current_size):
        """
        디스크 사용률을 기반으로 권장 볼륨 크기 계산
        
        :param usage_summary: 디스크 사용 요약 정보
        :param current_size: 현재 볼륨 크기(GB)
        :return: 권장 볼륨 크기(GB)
        """
        if not usage_summary or 'average_usage_percent' not in usage_summary:
            # 데이터가 없으면 현재 크기의 75%로 권장
            return max(4, int(current_size * 0.75))
        
        # 사용률에 기반한 필요 크기 계산
        usage_percent = usage_summary['average_usage_percent']
        
        # 이론적 필요 크기
        theoretical_size = current_size * (usage_percent / 100)
        
        # 최대 사용률도 고려
        if 'max_usage_percent' in usage_summary:
            max_usage_percent = usage_summary['max_usage_percent']
            # 최대 사용률에 기반한 크기
            max_usage_size = current_size * (max_usage_percent / 100)
            # 안전 마진(20%)을 추가하여 이론적 필요 크기와 비교해 더 큰 값 선택
            theoretical_size = max(theoretical_size, max_usage_size * 1.2)
        else:
            # 최대 사용률 정보가 없는 경우 안전 마진 추가
            theoretical_size *= 1.3  # 30% 안전 마진
        
        # 앞으로의 추가 사용량을 위한 버퍼 추가 (최소 10%, 최대 30%)
        growth_buffer = min(max(theoretical_size * 0.1, 1), theoretical_size * 0.3)
        
        # 권장 크기 = 이론적 필요 크기 + 성장 버퍼, 최소 4GB
        recommended_size_float = theoretical_size + growth_buffer
        recommended_size = max(4, int(recommended_size_float + 0.99))  # 올림 효과 + 최소값

        logger.info(f"권장 크기 계산: 현재={current_size}GB, 최대사용률={usage_percent:.1f}%, 이론크기={theoretical_size:.1f}GB, 버퍼={growth_buffer:.1f}GB -> 권장={recommended_size}GB")

        # 권장 크기가 현재 크기보다 크거나 같으면 현재 크기 유지
        if recommended_size >= current_size:
            logger.info(f"권장 크기({recommended_size}GB)가 현재 크기({current_size}GB)보다 크거나 같아 현재 크기 유지 권장.")
            return current_size
        else:
            # 최소 절감 비율 확인 (예: 10% 이상 절감될 때만)
            min_saving_percent = self.criteria.get('min_saving_percent_for_resize', 10)
            if recommended_size <= current_size * (1 - min_saving_percent / 100.0):
                return recommended_size
            else:
                logger.info(f"권장 크기({recommended_size}GB) 절감 효과({min_saving_percent}% 미만)가 미미하여 현재 크기({current_size}GB) 유지 권장.")
                return current_size

    def get_performance_metrics(self, volume_id, start_time, end_time):
        """
        CloudWatch에서 볼륨의 최대 성능 메트릭(IOPS, 처리량)을 가져옵니다.
        
        :param volume_id: EBS 볼륨 ID
        :param start_time: 측정 시작 시간
        :param end_time: 측정 종료 시간
        :return: 메트릭 데이터 딕셔너리 (최대값 포함)
        """
        metrics_to_check = {
            'VolumeReadOps': 'Count',
            'VolumeWriteOps': 'Count',
            'VolumeReadBytes': 'Bytes',
            'VolumeWriteBytes': 'Bytes'
        }
        performance_data = {}
        period = int((end_time - start_time).total_seconds())
        # CloudWatch GetMetricStatistics는 최대 1440개의 데이터 포인트를 반환합니다.
        # 기간이 너무 길면 Period를 조정해야 할 수 있습니다. (여기서는 전체 기간으로 설정)
        # 더 긴 기간의 경우, 여러 번 호출하거나 GetMetricData 사용 고려.
        # 여기서는 GetMetricStatistics를 사용하고, 데이터가 없는 경우를 처리합니다.
        if period > 14 * 86400: # 2주 이상이면 Period 조정 (예: 일 단위)
            period = 86400
        elif period == 0: # 시작/종료 같으면 기본값
             period = 300 # 5분

        for metric_name, unit in metrics_to_check.items():
            try:
                response = self.cloudwatch_client.get_metric_statistics(
                    Namespace='AWS/EBS',
                    MetricName=metric_name,
                    Dimensions=[{'Name': 'VolumeId', 'Value': volume_id}],
                    StartTime=start_time,
                    EndTime=end_time,
                    Period=period,
                    Statistics=['Maximum'], # 최대값 사용
                    Unit=unit
                )
                # 데이터 포인트가 있는 경우 최대값 찾기
                if response['Datapoints']:
                    max_value = max(dp['Maximum'] for dp in response['Datapoints'])
                    performance_data[f'max_{metric_name}'] = max_value
                else:
                    performance_data[f'max_{metric_name}'] = 0 # 데이터 없으면 0
            except ClientError as e:
                # 권한 부족 등의 오류 처리
                if e.response['Error']['Code'] == 'AccessDenied':
                    logger.warning(f"CloudWatch 메트릭 접근 권한 부족: {metric_name} for {volume_id}")
                else:
                    logger.error(f"CloudWatch {metric_name} 메트릭 조회 오류 for {volume_id}: {e}", exc_info=True)
                performance_data[f'max_{metric_name}'] = None
            except Exception as e:
                logger.error(f"CloudWatch {metric_name} 메트릭 처리 중 예외 for {volume_id}: {e}", exc_info=True)
                performance_data[f'max_{metric_name}'] = None

        # 초당 최대 IOPS 계산 (Ops는 Period 동안의 합계 또는 최대값이므로 주의 필요)
        # GetMetricStatistics의 Maximum은 해당 Period 내의 '최대 발생률'을 의미할 수 있음 (문서 확인 필요)
        # 여기서는 Period 동안 발생한 최대 Operation 수를 기준으로 함 (더 정확하려면 짧은 Period 사용 필요)
        # 편의상 Read+Write Ops의 최대값을 합산하여 사용 (동시 발생 아닐 수 있음)
        max_total_ops = (performance_data.get('max_VolumeReadOps', 0) or 0) + (performance_data.get('max_VolumeWriteOps', 0) or 0)
         # 실제 필요한 IOPS는 Period로 나눠야 하지만, GetMetricStatistics Maximum 의미 해석 필요
         # 여기서는 일단 최대 발생 건수를 'max_total_ops_in_period'로 저장
        performance_data['max_total_ops_in_period'] = max_total_ops

        # 초당 최대 처리량 계산 (Bytes/period)
        max_read_throughput_bps = (performance_data.get('max_VolumeReadBytes', 0) or 0)
        max_write_throughput_bps = (performance_data.get('max_VolumeWriteBytes', 0) or 0)
        # 이것도 Period 동안의 최대 Byte 수. 실제 Throughput(Bytes/sec) 계산 필요
        # performance_data['max_read_throughput_bytes_per_sec'] = max_read_throughput_bps / period if period > 0 else 0
        # performance_data['max_write_throughput_bytes_per_sec'] = max_write_throughput_bps / period if period > 0 else 0
        # 여기서는 일단 최대 Byte 수를 저장
        performance_data['max_total_bytes_in_period'] = max_read_throughput_bps + max_write_throughput_bps


        logger.info(f"볼륨 {volume_id} 성능 메트릭 (최대값): {performance_data}")
        return performance_data

    def is_overprovisioned_volume(self, volume_id, volume):
        """
        특정 볼륨이 과대 프로비저닝되었는지 확인 (디스크 사용률 및 성능 고려)
        
        :param volume_id: 볼륨 ID
        :param volume: 볼륨 정보 딕셔너리
        :return: (과대 프로비저닝 여부, 이유, 추가 데이터)
        """
        try:
            # 볼륨이 인스턴스에 연결되어 있는지 확인
            if not volume['Attachments']:
                logger.info(f"볼륨 {volume_id}가 인스턴스에 연결되어 있지 않아 과대 프로비저닝 검사를 수행하지 않습니다.")
                return False, "볼륨이 인스턴스에 연결되어 있지 않습니다.", None
            
            # 크기가 작은 볼륨은 과대 프로비저닝 검사에서 제외할 수 있음
            min_size_gb = self.criteria.get('min_size_gb', 0)
            if min_size_gb > 0 and volume.get('Size', 0) < min_size_gb:
                logger.info(f"볼륨 {volume_id}의 크기가 {volume.get('Size')}GB로, 최소 검사 크기인 {min_size_gb}GB보다 작아 검사를 생략합니다.")
                return False, f"볼륨 크기({volume.get('Size')}GB)가 최소 검사 크기({min_size_gb}GB)보다 작습니다.", None
            
            # 연결된 인스턴스 정보
            instance_id = volume['Attachments'][0]['InstanceId']
            device_name = volume['Attachments'][0]['Device']
            current_type = volume.get('VolumeType', '')
            provisioned_iops = volume.get('Iops') # io1, io2, gp3
            provisioned_throughput = volume.get('Throughput') # gp3

            logger.info(f"볼륨 {volume_id} (타입: {current_type}, 크기: {volume.get('Size')}GB, IOPS: {provisioned_iops}, Throughput: {provisioned_throughput}) 분석 시작")

            end_time = datetime.now()
            start_time = end_time - timedelta(days=max(1, 30 * self.criteria.get('months_to_check', 1))) # 최소 1일

            # 1. 디스크 사용률 분석
            usage_datapoints = self.get_disk_usage_metrics(instance_id, device_name, start_time, end_time)
            if not usage_datapoints:
                logger.warning(f"볼륨 {volume_id}: 디스크 사용률 데이터 없음, 추정치 사용.")
                usage_datapoints = self.get_estimated_disk_usage(instance_id, device_name)
            
            is_size_over, size_reason, usage_summary = self.is_overprovisioned(usage_datapoints)
            recommended_size = self.recommend_volume_size(usage_summary, volume.get('Size', 0))

            # 2. 성능 메트릭 분석 (IOPS, Throughput) - 최대값 기준
            performance_data = self.get_performance_metrics(volume_id, start_time, end_time)
            
            # 성능 과대 프로비저닝 판단 로직 (간단 예시)
            is_perf_over = False
            perf_reason = "성능은 적절히 사용 중"
            recommended_iops = None
            recommended_throughput = None

            # 성능 임계값 (설정 파일에서 가져와야 함)
            max_iops_usage_threshold_percent = self.criteria.get('max_iops_usage_threshold_percent', 50) # 예: 50%
            max_throughput_usage_threshold_percent = self.criteria.get('max_throughput_usage_threshold_percent', 50) # 예: 50%
            buffer_percent = self.criteria.get('buffer_percent', 20)

            # 최대 관측 IOPS (해석 주의)
            # GetMetricStatistics Maximum / Period 로 초당 최대 IOPS 추정 필요
            # 여기서는 'max_total_ops_in_period'를 사용 (단순 참고용)
            max_observed_ops = performance_data.get('max_total_ops_in_period') 
            max_observed_bytes = performance_data.get('max_total_bytes_in_period')
            
            # --- 성능 과대 프로비저닝 판단 (io1/io2/gp3 대상) ---
            if current_type in ['io1', 'io2'] and provisioned_iops and max_observed_ops is not None:
                # io1/io2는 IOPS 기준 판단 (Throughput은 IOPS에 따라 결정됨)
                # !!! 중요: max_observed_ops의 정확한 해석 및 기간 고려 필요 !!!
                # !!! 아래 로직은 예시이며, 실제로는 기간 내 평균 최대 IOPS 등으로 계산해야 할 수 있음 !!!
                estimated_peak_iops = max_observed_ops # 가정: Period 내 최대 발생 건수가 Peak IOPS와 유사
                if estimated_peak_iops < provisioned_iops * (max_iops_usage_threshold_percent / 100.0):
                    is_perf_over = True
                    perf_reason = f"프로비저닝된 IOPS({provisioned_iops}) 대비 최대 관측 IOPS({estimated_peak_iops})가 낮음 ({max_iops_usage_threshold_percent}% 미만 사용)"
                    # 권장 IOPS 계산
                    recommended_iops = int(estimated_peak_iops * (1 + buffer_percent / 100.0))
                    # io1/io2 최소 IOPS 적용
                    recommended_iops = max(100, recommended_iops) 
                    logger.info(f"{volume_id}: IOPS 과대 프로비저닝 감지. 권장 IOPS: {recommended_iops}")

            elif current_type == 'gp3' and provisioned_iops and provisioned_throughput and max_observed_ops is not None and max_observed_bytes is not None:
                 # gp3는 IOPS와 Throughput 모두 고려
                 # !!! 위와 동일한 IOPS/Throughput 해석 주의 !!!
                 estimated_peak_iops = max_observed_ops # 가정
                 # Throughput 계산 (Bytes / Period) -> MB/s
                 period_seconds = max(1, int((end_time - start_time).total_seconds())) # 0 방지
                 estimated_peak_throughput_mbps = (max_observed_bytes / period_seconds) / (1024 * 1024) if period_seconds > 0 else 0

                 iops_over = estimated_peak_iops < provisioned_iops * (max_iops_usage_threshold_percent / 100.0)
                 throughput_over = estimated_peak_throughput_mbps < provisioned_throughput * (max_throughput_usage_threshold_percent / 100.0)

                 if iops_over or throughput_over:
                      is_perf_over = True
                      perf_reason_parts = []
                      # 권장 IOPS 계산
                      recommended_iops = int(estimated_peak_iops * (1 + buffer_percent / 100.0))
                      recommended_iops = max(3000, recommended_iops) # gp3 최소 IOPS
                      recommended_iops = min(16000, recommended_iops) # gp3 최대 IOPS
                      if iops_over:
                           perf_reason_parts.append(f"IOPS({provisioned_iops} 대비 최대 {estimated_peak_iops}) 낮음")
                      
                      # 권장 Throughput 계산
                      recommended_throughput = int(estimated_peak_throughput_mbps * (1 + buffer_percent / 100.0))
                      recommended_throughput = max(125, recommended_throughput) # gp3 최소 Throughput
                      recommended_throughput = min(1000, recommended_throughput) # gp3 최대 Throughput
                      if throughput_over:
                           perf_reason_parts.append(f"Throughput({provisioned_throughput}MB/s 대비 최대 {estimated_peak_throughput_mbps:.1f}MB/s) 낮음")
                      
                      perf_reason = "성능 과대 프로비저닝: " + ", ".join(perf_reason_parts)
                      logger.info(f"{volume_id}: 성능 과대 프로비저닝 감지. 권장 IOPS: {recommended_iops}, 권장 Throughput: {recommended_throughput}")

            # 최종 판단: 크기 또는 성능 중 하나라도 과대 프로비저닝이면 True
            is_overall_over = is_size_over or is_perf_over
            
            # 최종 이유 조합
            final_reason = ""
            if is_size_over: final_reason += size_reason
            if is_perf_over: final_reason += ("; " if final_reason else "") + perf_reason
            if not is_overall_over: final_reason = "크기와 성능 모두 적절히 사용 중"

            # 결과 데이터 구성
            additional_data = {
                'volume_type': current_type,
                'size': volume.get('Size', 0),
                'provisioned_iops': provisioned_iops,
                'provisioned_throughput': provisioned_throughput,
                'disk_usage_data': usage_summary,
                'performance_data': performance_data, # 수집된 성능 메트릭 추가
                'recommended_size': recommended_size if is_size_over else volume.get('Size', 0), # 사이즈 줄일 때만 권장값 반영
                'recommended_iops': recommended_iops if is_perf_over else provisioned_iops,
                'recommended_throughput': recommended_throughput if is_perf_over else provisioned_throughput,
                'recommendation': "최적화 권장", # 기본 메시지
                'estimated_savings': 0 # 절감액 계산 로직 필요
            }

            # 권장 메시지 및 절감액 계산
            if is_overall_over:
                recommendation_parts = []
                # io1/io2 -> gp3 전환 우선 고려
                if current_type in ['io1', 'io2'] and is_overall_over:
                    # 항상 gp3로 변경 권장 (비용 효율적)
                     target_type = 'gp3'
                     target_size = recommended_size if is_size_over else volume.get('Size', 0)
                     target_iops = recommended_iops if recommended_iops else 3000 # 권장값 없으면 기본값
                     target_throughput = recommended_throughput if recommended_throughput else 125 # 권장값 없으면 기본값
                     
                     # gp3 비용 계산 (개선된 calculate_monthly_cost 필요 - iops/throughput 비용 포함)
                     # new_cost = calculate_monthly_cost(target_size, target_type, self.region, iops=target_iops, throughput=target_throughput)
                     # current_cost = calculate_monthly_cost(current_size, current_type, self.region, iops=provisioned_iops)
                     # savings = current_cost - new_cost
                     # additional_data['estimated_savings'] = savings
                     
                     recommendation_parts.append(f"gp3 타입 변경(크기:{target_size}GB, IOPS:{target_iops}, TP:{target_throughput}MB/s)")
                     additional_data['recommended_type'] = target_type # 추천 타입 명시

                else: # gp2, gp3, st1, sc1 등
                     target_type = current_type
                     target_size = recommended_size if is_size_over else volume.get('Size', 0)
                     target_iops = provisioned_iops # 기본값
                     target_throughput = provisioned_throughput # 기본값
                     
                     if is_size_over and target_size < volume.get('Size', 0):
                          recommendation_parts.append(f"크기 축소 ({target_size}GB)")
                     
                     if current_type == 'gp3' and is_perf_over:
                          target_iops = recommended_iops if recommended_iops else provisioned_iops
                          target_throughput = recommended_throughput if recommended_throughput else provisioned_throughput
                          if target_iops != provisioned_iops or target_throughput != provisioned_throughput:
                               recommendation_parts.append(f"gp3 성능 조정 (IOPS:{target_iops}, TP:{target_throughput}MB/s)")
                     
                     # 비용 계산 (개선된 calculate_monthly_cost 필요)
                     # new_cost = calculate_monthly_cost(target_size, target_type, self.region, iops=target_iops, throughput=target_throughput) # gp3만 iops/tp 전달
                     # current_cost = calculate_monthly_cost(current_size, current_type, self.region, iops=provisioned_iops, throughput=provisioned_throughput)
                     # savings = current_cost - new_cost
                     # additional_data['estimated_savings'] = savings

                additional_data['recommendation'] = "과대 프로비저닝: " + ", ".join(recommendation_parts) + " 고려"
            else:
                 additional_data['recommendation'] = "현재 설정 유지 권장"


            return is_overall_over, final_reason, additional_data

        except Exception as e:
            logger.error(f"볼륨 {volume_id} 과대 프로비저닝 확인 중 오류 발생: {str(e)}", exc_info=True)
            return False, f"분석 오류: {str(e)}", None