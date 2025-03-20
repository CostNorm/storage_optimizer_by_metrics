import boto3
import logging
import time
from datetime import datetime
from botocore.exceptions import ClientError

logger = logging.getLogger()

class EBSActionExecutor:
    """
    EBS 볼륨에 대한 실제 조치(액션)를 수행하는 클래스
    분석 결과에 따른 권장 조치를 실행합니다.
    """
    
    def __init__(self, region):
        """
        :param region: AWS 리전
        """
        self.region = region
        self.ec2_client = boto3.client('ec2', region_name=region)
    
    def create_snapshot(self, volume_id, description=None, tags=None):
        """
        EBS 볼륨의 스냅샷을 생성합니다.
        
        :param volume_id: 스냅샷을 생성할 볼륨 ID
        :param description: 스냅샷 설명 (기본값: None)
        :param tags: 스냅샷에 적용할 태그 딕셔너리 (기본값: None)
        :return: 생성된 스냅샷 ID 또는 None (실패 시)
        """
        try:
            timestamp = datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
            
            if not description:
                description = f"Auto-created snapshot for volume {volume_id} before optimization - {timestamp}"
            
            # 스냅샷 생성
            response = self.ec2_client.create_snapshot(
                VolumeId=volume_id,
                Description=description
            )
            
            snapshot_id = response['SnapshotId']
            logger.info(f"볼륨 {volume_id}의 스냅샷 {snapshot_id}를 생성했습니다.")
            
            # 태그 적용 (지정된 경우)
            if tags and isinstance(tags, dict):
                tag_list = [{'Key': k, 'Value': v} for k, v in tags.items()]
                self.ec2_client.create_tags(
                    Resources=[snapshot_id],
                    Tags=tag_list
                )
                logger.info(f"스냅샷 {snapshot_id}에 태그를 적용했습니다.")
            
            # 스냅샷 생성 완료 대기 (선택적)
            self.wait_for_snapshot_completion(snapshot_id)
            
            return snapshot_id
            
        except ClientError as e:
            logger.error(f"스냅샷 생성 중 오류 발생: {str(e)}")
            return None
    
    def wait_for_snapshot_completion(self, snapshot_id, timeout_seconds=300, check_interval=15):
        """
        스냅샷 생성 완료를 대기합니다.
        
        :param snapshot_id: 스냅샷 ID
        :param timeout_seconds: 최대 대기 시간 (초)
        :param check_interval: 상태 확인 간격 (초)
        :return: 성공 여부 (boolean)
        """
        logger.info(f"스냅샷 {snapshot_id} 생성 완료 대기 중...")
        
        start_time = time.time()
        
        while time.time() - start_time < timeout_seconds:
            try:
                response = self.ec2_client.describe_snapshots(SnapshotIds=[snapshot_id])
                if response['Snapshots']:
                    state = response['Snapshots'][0]['State']
                    progress = response['Snapshots'][0].get('Progress', 'N/A')
                    
                    if state == 'completed':
                        logger.info(f"스냅샷 {snapshot_id} 생성이 완료되었습니다.")
                        return True
                    
                    logger.info(f"스냅샷 상태: {state}, 진행률: {progress}")
                    
                time.sleep(check_interval)
                
            except ClientError as e:
                logger.error(f"스냅샷 상태 확인 중 오류 발생: {str(e)}")
                return False
        
        logger.warning(f"스냅샷 {snapshot_id} 생성 대기 시간이 초과되었습니다.")
        return False
    
    def detach_volume(self, volume_id, force=False):
        """
        EBS 볼륨을 인스턴스에서 분리합니다.
        
        :param volume_id: 분리할 볼륨 ID
        :param force: 강제 분리 여부 (기본값: False)
        :return: 성공 여부 (boolean)
        """
        try:
            # 볼륨 정보 조회
            response = self.ec2_client.describe_volumes(VolumeIds=[volume_id])
            
            if not response['Volumes'] or not response['Volumes'][0]['Attachments']:
                logger.warning(f"볼륨 {volume_id}는 현재 어떤 인스턴스에도 연결되어 있지 않습니다.")
                return True
            
            attachment = response['Volumes'][0]['Attachments'][0]
            instance_id = attachment['InstanceId']
            device = attachment['Device']
            
            logger.info(f"볼륨 {volume_id}를 인스턴스 {instance_id}에서 분리합니다. (디바이스: {device})")
            
            # 볼륨 상태 확인 (최소한의 안전 장치)
            if not self._is_volume_safe_to_detach(volume_id, instance_id):
                logger.error(f"볼륨 {volume_id}는 현재 분리하기에 안전하지 않습니다.")
                return False
            
            # 볼륨 분리
            self.ec2_client.detach_volume(
                VolumeId=volume_id,
                InstanceId=instance_id,
                Device=device,
                Force=force
            )
            
            # 볼륨 분리 완료 대기
            return self.wait_for_volume_detachment(volume_id)
            
        except ClientError as e:
            logger.error(f"볼륨 분리 중 오류 발생: {str(e)}")
            return False
    
    def wait_for_volume_detachment(self, volume_id, timeout_seconds=300, check_interval=5):
        """
        볼륨 분리 완료를 대기합니다.
        
        :param volume_id: 볼륨 ID
        :param timeout_seconds: 최대 대기 시간 (초)
        :param check_interval: 상태 확인 간격 (초)
        :return: 성공 여부 (boolean)
        """
        logger.info(f"볼륨 {volume_id} 분리 완료 대기 중...")
        
        start_time = time.time()
        
        while time.time() - start_time < timeout_seconds:
            try:
                response = self.ec2_client.describe_volumes(VolumeIds=[volume_id])
                
                if response['Volumes']:
                    state = response['Volumes'][0]['State']
                    has_attachments = bool(response['Volumes'][0]['Attachments'])
                    
                    if state == 'available' and not has_attachments:
                        logger.info(f"볼륨 {volume_id} 분리가 완료되었습니다.")
                        return True
                    
                    logger.info(f"볼륨 상태: {state}, 연결 여부: {has_attachments}")
                
                time.sleep(check_interval)
                
            except ClientError as e:
                logger.error(f"볼륨 상태 확인 중 오류 발생: {str(e)}")
                return False
        
        logger.warning(f"볼륨 {volume_id} 분리 대기 시간이 초과되었습니다.")
        return False
    
    def attach_volume(self, volume_id, instance_id, device):
        """
        EBS 볼륨을 인스턴스에 연결합니다.
        
        :param volume_id: 연결할 볼륨 ID
        :param instance_id: 인스턴스 ID
        :param device: 디바이스 이름
        :return: 성공 여부 (boolean)
        """
        try:
            # 볼륨 상태 확인
            response = self.ec2_client.describe_volumes(VolumeIds=[volume_id])
            
            if not response['Volumes']:
                logger.error(f"볼륨 {volume_id}을 찾을 수 없습니다.")
                return False
            
            if response['Volumes'][0]['State'] != 'available':
                logger.error(f"볼륨 {volume_id}의 상태가 'available'이 아닙니다: {response['Volumes'][0]['State']}")
                return False
            
            # 인스턴스 상태 확인
            instance_response = self.ec2_client.describe_instances(InstanceIds=[instance_id])
            
            if not instance_response['Reservations'] or not instance_response['Reservations'][0]['Instances']:
                logger.error(f"인스턴스 {instance_id}를 찾을 수 없습니다.")
                return False
            
            instance_state = instance_response['Reservations'][0]['Instances'][0]['State']['Name']
            
            if instance_state != 'running':
                logger.error(f"인스턴스 {instance_id}가 'running' 상태가 아닙니다: {instance_state}")
                return False
            
            # 볼륨 연결
            logger.info(f"볼륨 {volume_id}를 인스턴스 {instance_id}에 연결합니다. (디바이스: {device})")
            
            self.ec2_client.attach_volume(
                VolumeId=volume_id,
                InstanceId=instance_id,
                Device=device
            )
            
            # 볼륨 연결 완료 대기
            return self.wait_for_volume_attachment(volume_id, instance_id)
            
        except ClientError as e:
            logger.error(f"볼륨 연결 중 오류 발생: {str(e)}")
            return False
    
    def wait_for_volume_attachment(self, volume_id, instance_id, timeout_seconds=300, check_interval=5):
        """
        볼륨 연결 완료를 대기합니다.
        
        :param volume_id: 볼륨 ID
        :param instance_id: 인스턴스 ID
        :param timeout_seconds: 최대 대기 시간 (초)
        :param check_interval: 상태 확인 간격 (초)
        :return: 성공 여부 (boolean)
        """
        logger.info(f"볼륨 {volume_id}의 인스턴스 {instance_id} 연결 완료 대기 중...")
        
        start_time = time.time()
        
        while time.time() - start_time < timeout_seconds:
            try:
                response = self.ec2_client.describe_volumes(VolumeIds=[volume_id])
                
                if response['Volumes'] and response['Volumes'][0]['Attachments']:
                    attachment = response['Volumes'][0]['Attachments'][0]
                    
                    if attachment['InstanceId'] == instance_id and attachment['State'] == 'attached':
                        logger.info(f"볼륨 {volume_id}가 인스턴스 {instance_id}에 성공적으로 연결되었습니다.")
                        return True
                    
                    logger.info(f"볼륨 연결 상태: {attachment['State']}")
                
                time.sleep(check_interval)
                
            except ClientError as e:
                logger.error(f"볼륨 상태 확인 중 오류 발생: {str(e)}")
                return False
        
        logger.warning(f"볼륨 {volume_id} 연결 대기 시간이 초과되었습니다.")
        return False
    
    def delete_volume(self, volume_id):
        """
        EBS 볼륨을 삭제합니다.
        
        :param volume_id: 삭제할 볼륨 ID
        :return: 성공 여부 (boolean)
        """
        try:
            # 볼륨 상태 확인
            response = self.ec2_client.describe_volumes(VolumeIds=[volume_id])
            
            if not response['Volumes']:
                logger.error(f"볼륨 {volume_id}을 찾을 수 없습니다.")
                return False
            
            if response['Volumes'][0]['State'] != 'available':
                logger.error(f"볼륨 {volume_id}의 상태가 'available'이 아닙니다: {response['Volumes'][0]['State']}")
                return False
            
            if response['Volumes'][0]['Attachments']:
                logger.error(f"볼륨 {volume_id}가 아직 인스턴스에 연결되어 있습니다. 먼저 분리해야 합니다.")
                return False
            
            # 볼륨 삭제
            logger.info(f"볼륨 {volume_id}를 삭제합니다.")
            self.ec2_client.delete_volume(VolumeId=volume_id)
            
            # 볼륨 삭제 완료 대기
            return self.wait_for_volume_deletion(volume_id)
            
        except ClientError as e:
            logger.error(f"볼륨 삭제 중 오류 발생: {str(e)}")
            return False
    
    def wait_for_volume_deletion(self, volume_id, timeout_seconds=180, check_interval=5):
        """
        볼륨 삭제 완료를 대기합니다.
        
        :param volume_id: 볼륨 ID
        :param timeout_seconds: 최대 대기 시간 (초)
        :param check_interval: 상태 확인 간격 (초)
        :return: 성공 여부 (boolean)
        """
        logger.info(f"볼륨 {volume_id} 삭제 완료 대기 중...")
        
        start_time = time.time()
        
        while time.time() - start_time < timeout_seconds:
            try:
                response = self.ec2_client.describe_volumes(VolumeIds=[volume_id])
                logger.info(f"볼륨 {volume_id}가 아직 삭제되지 않았습니다.")
                time.sleep(check_interval)
                
            except ClientError as e:
                if 'InvalidVolume.NotFound' in str(e):
                    logger.info(f"볼륨 {volume_id}가 성공적으로 삭제되었습니다.")
                    return True
                else:
                    logger.error(f"볼륨 상태 확인 중 오류 발생: {str(e)}")
                    return False
        
        logger.warning(f"볼륨 {volume_id} 삭제 대기 시간이 초과되었습니다.")
        return False
    
    def _is_volume_safe_to_detach(self, volume_id, instance_id):
        """
        볼륨이 안전하게 분리될 수 있는지 확인합니다.
        특히 루트 볼륨인 경우 분리하면 안 됩니다.
        
        :param volume_id: 볼륨 ID
        :param instance_id: 인스턴스 ID
        :return: 안전 여부 (boolean)
        """
        try:
            # 인스턴스 정보 조회
            response = self.ec2_client.describe_instances(InstanceIds=[instance_id])
            
            if not response['Reservations'] or not response['Reservations'][0]['Instances']:
                logger.error(f"인스턴스 {instance_id}를 찾을 수 없습니다.")
                return False
            
            instance = response['Reservations'][0]['Instances'][0]
            
            # 루트 디바이스 확인
            root_device_name = instance['RootDeviceName']  # 예: /dev/sda1 또는 /dev/xvda
            
            # 볼륨 연결 정보 조회
            volume_response = self.ec2_client.describe_volumes(VolumeIds=[volume_id])
            
            if not volume_response['Volumes'] or not volume_response['Volumes'][0]['Attachments']:
                logger.error(f"볼륨 {volume_id}의 연결 정보를 찾을 수 없습니다.")
                return False
            
            device_name = volume_response['Volumes'][0]['Attachments'][0]['Device']
            
            # 루트 볼륨인지 확인
            if device_name == root_device_name:
                logger.error(f"볼륨 {volume_id}는 인스턴스 {instance_id}의 루트 볼륨입니다. 분리할 수 없습니다.")
                return False
            
            return True
            
        except ClientError as e:
            logger.error(f"볼륨 안전성 확인 중 오류 발생: {str(e)}")
            return False
