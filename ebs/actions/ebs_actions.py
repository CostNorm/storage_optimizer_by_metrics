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
            # 스냅샷 생성 요청 구성
            create_args = {'VolumeId': volume_id}
            
            if description:
                create_args['Description'] = description
                
            # 태그 변환
            if tags:
                tag_specs = [{
                    'ResourceType': 'snapshot',
                    'Tags': [{'Key': k, 'Value': v} for k, v in tags.items()]
                }]
                create_args['TagSpecifications'] = tag_specs
            
            logger.info(f"볼륨 {volume_id}의 스냅샷 생성 시작")
            response = self.ec2_client.create_snapshot(**create_args)
            
            snapshot_id = response.get('SnapshotId')
            logger.info(f"볼륨 {volume_id}의 스냅샷 {snapshot_id} 생성 요청 완료")
            
            # 스냅샷 생성이 진행 중임을 확인
            self._wait_for_snapshot_started(snapshot_id)
            
            return snapshot_id
            
        except ClientError as e:
            logger.error(f"스냅샷 생성 중 오류 발생: {str(e)}")
            return None
    
    def _wait_for_snapshot_started(self, snapshot_id, timeout_seconds=30):
        """
        스냅샷 생성이 시작되었는지 확인합니다.
        
        :param snapshot_id: 스냅샷 ID
        :param timeout_seconds: 타임아웃(초)
        :return: 성공 여부
        """
        start_time = time.time()
        while (time.time() - start_time) < timeout_seconds:
            try:
                response = self.ec2_client.describe_snapshots(SnapshotIds=[snapshot_id])
                if response['Snapshots']:
                    logger.info(f"스냅샷 {snapshot_id} 생성 상태: {response['Snapshots'][0].get('State', '알 수 없음')}, "
                               f"진행률: {response['Snapshots'][0].get('Progress', '0%')}")
                    return True
            except Exception as e:
                logger.warning(f"스냅샷 {snapshot_id} 상태 확인 중 오류: {str(e)}")
            
            time.sleep(5)
        
        logger.warning(f"스냅샷 {snapshot_id} 시작 확인 타임아웃")
        return False
    
    def detach_volume(self, volume_id, force=False):
        """
        EBS 볼륨을 인스턴스에서 분리합니다.
        
        :param volume_id: 분리할 볼륨 ID
        :param force: 강제 분리 여부 (기본값: False)
        :return: 성공 여부 (boolean)
        """
        try:
            # 볼륨 정보 가져오기
            response = self.ec2_client.describe_volumes(VolumeIds=[volume_id])
            
            if not response['Volumes']:
                logger.error(f"볼륨 {volume_id}을 찾을 수 없습니다.")
                return False
                
            volume = response['Volumes'][0]
            attachments = volume.get('Attachments', [])
            
            # 연결된 인스턴스가 없으면 성공으로 처리
            if not attachments:
                logger.info(f"볼륨 {volume_id}이 어떤 인스턴스에도 연결되어 있지 않습니다.")
                return True
            
            # 각 연결에 대해 분리 실행
            for attachment in attachments:
                instance_id = attachment.get('InstanceId')
                device = attachment.get('Device')
                
                logger.info(f"볼륨 {volume_id}을 인스턴스 {instance_id}의 {device}에서 분리 시도")
                
                detach_args = {'VolumeId': volume_id}
                
                if force:
                    detach_args['Force'] = True
                
                self.ec2_client.detach_volume(**detach_args)
                
                # 분리가 완료될 때까지 대기
                self._wait_for_volume_detachment(volume_id, timeout_seconds=120)
            
            return True
            
        except ClientError as e:
            logger.error(f"볼륨 분리 중 오류 발생: {str(e)}")
            return False
    
    def _wait_for_volume_detachment(self, volume_id, timeout_seconds=120):
        """
        볼륨이 완전히 분리될 때까지 기다립니다.
        
        :param volume_id: 볼륨 ID
        :param timeout_seconds: 타임아웃(초)
        :return: 성공 여부
        """
        start_time = time.time()
        logger.info(f"볼륨 {volume_id} 분리 완료 대기 시작")
        
        while (time.time() - start_time) < timeout_seconds:
            try:
                response = self.ec2_client.describe_volumes(VolumeIds=[volume_id])
                
                if not response['Volumes']:
                    logger.warning(f"볼륨 {volume_id}을 찾을 수 없습니다.")
                    return False
                    
                volume = response['Volumes'][0]
                state = volume.get('State')
                attachments = volume.get('Attachments', [])
                
                logger.info(f"볼륨 {volume_id} 상태: {state}, 연결 수: {len(attachments)}")
                
                # 연결된 인스턴스가 없고 상태가 'available'이면 분리 완료
                if not attachments and state == 'available':
                    logger.info(f"볼륨 {volume_id} 분리 완료")
                    return True
                    
                # 특정 상태에서는 더 이상 기다릴 필요 없음
                if state in ['error', 'deleted']:
                    logger.error(f"볼륨 {volume_id}이 예기치 않은 상태({state})입니다.")
                    return False
                    
                time.sleep(5)  # 5초 대기 후 다시 확인
                
            except ClientError as e:
                logger.warning(f"볼륨 상태 확인 중 오류 발생: {str(e)}")
                time.sleep(5)
        
        logger.warning(f"볼륨 {volume_id} 분리 대기 시간 초과 ({timeout_seconds}초)")
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
            logger.info(f"볼륨 {volume_id} 삭제 시작")
            self.ec2_client.delete_volume(VolumeId=volume_id)
            
            # 볼륨이 삭제되었는지 확인
            for _ in range(12):  # 60초 동안 최대 12번 시도
                try:
                    volumes = self.ec2_client.describe_volumes(VolumeIds=[volume_id])
                    state = volumes['Volumes'][0]['State'] if volumes['Volumes'] else None
                    logger.info(f"볼륨 {volume_id} 상태: {state}")
                    
                    # 볼륨이 삭제 중이면 대기
                    if state == 'deleting':
                        logger.info(f"볼륨 {volume_id} 삭제 중...")
                    else:
                        logger.warning(f"볼륨 {volume_id}이 예기치 않은 상태({state})입니다.")
                        
                except Exception as e:
                    # VolumeNotFound 예외는 삭제 성공의 신호
                    if 'VolumeNotFound' in str(e):
                        logger.info(f"볼륨 {volume_id} 삭제 확인됨")
                        return True
                    logger.warning(f"볼륨 {volume_id} 상태 확인 중 오류: {str(e)}")
                
                time.sleep(5)
                
            # 확인 타임아웃이지만 삭제 요청은 성공했으므로 성공으로 처리
            logger.info(f"볼륨 {volume_id} 삭제 요청 성공, 삭제 확인 타임아웃")
            return True
            
        except ClientError as e:
            logger.error(f"볼륨 삭제 중 오류 발생: {str(e)}")
            return False
    
    def modify_volume_type(self, volume_id, target_type, iops=None, throughput=None):
        """
        볼륨 유형을 변경합니다.
        
        :param volume_id: 볼륨 ID
        :param target_type: 대상 볼륨 타입
        :param iops: IOPS 값 (io1, io2, gp3 타입에만 필요)
        :param throughput: 처리량 (gp3 타입에만 필요)
        :return: 성공 여부 및 결과 정보
        """
        try:
            logger.info(f"볼륨 {volume_id}의 타입을 {target_type}으로 변경 시작")
            
            # 현재 볼륨 정보 가져오기
            current_volume = self._get_volume_info(volume_id)
            if not current_volume:
                return {'success': False, 'error': f"볼륨 {volume_id} 정보를 가져올 수 없습니다."}
            
            current_type = current_volume.get('VolumeType')
            
            if current_type == target_type:
                return {'success': True, 'message': f"볼륨 {volume_id}이 이미 요청한 타입({target_type})입니다."}
            
            # 변경 요청 준비
            modify_args = {
                'VolumeId': volume_id,
                'VolumeType': target_type
            }
            
            # 볼륨 타입에 따른 추가 파라미터 설정
            if target_type in ['io1', 'io2']:
                # io1/io2는 IOPS 필요
                modify_args['Iops'] = iops if iops is not None else 100
                
            elif target_type == 'gp3':
                # gp3는 IOPS와 Throughput 필요
                modify_args['Iops'] = iops if iops is not None else 3000
                modify_args['Throughput'] = throughput if throughput is not None else 125
            
            # 변경 요청
            response = self.ec2_client.modify_volume(**modify_args)
            
            # 변경 상태 확인
            modification = response.get('VolumeModification', {})
            start_state = modification.get('ModificationState')
            
            return {
                'success': True,
                'message': f"볼륨 타입 변경 요청 성공: {current_type} -> {target_type}",
                'initial_state': start_state,
                'modification_id': modification.get('ModificationId')
            }
            
        except ClientError as e:
            logger.error(f"볼륨 {volume_id} 타입 변경 중 오류 발생: {str(e)}")
            return {'success': False, 'error': str(e)}
    
    def _get_volume_info(self, volume_id):
        """
        볼륨 정보를 가져옵니다.
        
        :param volume_id: 볼륨 ID
        :return: 볼륨 정보 또는 None
        """
        try:
            response = self.ec2_client.describe_volumes(VolumeIds=[volume_id])
            return response['Volumes'][0] if response['Volumes'] else None
        except ClientError as e:
            logger.error(f"볼륨 {volume_id} 정보 조회 중 오류: {str(e)}")
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
