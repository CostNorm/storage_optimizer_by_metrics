import logging
import json
import time
import boto3
from datetime import datetime
from .ebs_actions import EBSActionExecutor

logger = logging.getLogger()

class RecommendationExecutor:
    """
    분석 결과의 권장 조치를 실행하는 클래스
    """
    
    def __init__(self, region):
        """
        :param region: AWS 리전
        """
        self.region = region
        self.ebs_action_executor = EBSActionExecutor(region)
        self.execution_history = []
        self.ec2_client = boto3.client('ec2', region_name=region)
    
    def execute_idle_volume_recommendation(self, volume_info, action_type):
        """
        유휴 볼륨에 대한 권장 조치를 실행합니다.
        비동기적으로 처리하여 슬랙 액션이 오래 기다리지 않도록 합니다.
        
        :param volume_info: 볼륨 정보 딕셔너리
        :param action_type: 실행할 조치 유형 ('snapshot_and_delete', 'snapshot_only', 'change_type')
        :return: 결과 딕셔너리
        """
        volume_id = volume_info['volume_id']
        result = {
            'volume_id': volume_id,
            'action_type': action_type,
            'success': False,
            'timestamp': datetime.now().isoformat(),
            'details': {},
            'status': 'initiated' # 작업이 시작되었음을 표시
        }
        
        # 유효한 작업 유형 확인
        valid_idle_actions = ['snapshot_and_delete', 'snapshot_only', 'change_type', 'change_type_and_resize']
        if action_type not in valid_idle_actions:
            error_msg = f"유휴 볼륨에 지원되지 않는 작업 유형: {action_type}. 유효한 작업: {valid_idle_actions}"
            logger.error(error_msg)
            result['details']['error'] = error_msg
            return result
            
        logger.info(f"볼륨 {volume_id}에 대한 '{action_type}' 작업 시작 중...")
        
        try:
            # 1. 스냅샷 생성 (모든 작업에 공통)
            tags = {'Name': f"Idle-{volume_id}", 'AutoCreated': 'true', 'Source': 'EBS-Optimizer'}
            
            if 'name' in volume_info:
                tags['SourceName'] = volume_info['name']
            
            snapshot_id = self.ebs_action_executor.create_snapshot(
                volume_id,
                description=f"Idle volume snapshot before {action_type} - {datetime.now().strftime('%Y-%m-%d')}",
                tags=tags
            )
            
            if not snapshot_id:
                result['details']['error'] = "스냅샷 생성 요청 실패"
                return result
            
            result['details']['snapshot_id'] = snapshot_id
            logger.info(f"볼륨 {volume_id}의 스냅샷 {snapshot_id} 생성 요청 완료. 스냅샷 생성은 백그라운드에서 계속됩니다.")
            
            # 2. 선택한 작업 유형에 따라 실행
            if action_type == 'snapshot_and_delete':
                # 볼륨에 연결된 경우 분리
                if volume_info.get('attached_instances'):
                    for attachment in volume_info['attached_instances']:
                        instance_id = attachment['instance_id']
                        logger.info(f"볼륨 {volume_id}를 인스턴스 {instance_id}에서 분리합니다.")
                        
                        detach_success = self.ebs_action_executor.detach_volume(volume_id)
                        
                        if not detach_success:
                            result['details']['error'] = f"인스턴스 {instance_id}에서 볼륨 분리 요청 실패"
                            return result
                
                # 볼륨 삭제
                delete_success = self.ebs_action_executor.delete_volume(volume_id)
                
                if not delete_success:
                    result['details']['error'] = "볼륨 삭제 요청 실패"
                    return result
                
                result['details']['action'] = "스냅샷 생성 및 볼륨 삭제 요청 완료"
                result['details']['note'] = "작업은 백그라운드에서 계속 진행됩니다. 완료까지 몇 분 소요될 수 있습니다."
                result['success'] = True
                
            elif action_type == 'snapshot_only':
                result['details']['action'] = "스냅샷 생성 요청 완료"
                result['details']['note'] = "스냅샷 생성은 백그라운드에서 계속 진행됩니다. 완료까지 몇 분 소요될 수 있습니다."
                result['success'] = True
                
            elif action_type == 'change_type':
                # 볼륨 타입 변경 로직 구현
                current_type = volume_info.get('volume_type', '')
                target_type = self._determine_target_volume_type(current_type)
                
                logger.info(f"볼륨 {volume_id}의 타입 변경: {current_type} -> {target_type}")
                
                if current_type == target_type:
                    logger.info(f"볼륨 {volume_id}는 이미 최적의 타입({target_type})입니다.")
                    result['details']['message'] = f"볼륨 유형 {current_type}에서 변경이 필요하지 않습니다."
                    result['success'] = True
                    return result
                
                # 현재 볼륨 상태 확인
                volume_detail = self._get_volume_info(volume_id)
                if not volume_detail:
                    result['details']['error'] = "볼륨 정보를 가져올 수 없어 타입 변경을 진행할 수 없습니다."
                    return result
                
                logger.info(f"볼륨 {volume_id} 상세 정보 확인 완료")
                
                # 볼륨이 사용 중인지 확인
                if volume_detail.get('State') != 'available' and volume_detail.get('Attachments'):
                    logger.warning(f"볼륨 {volume_id}이(가) 인스턴스에 연결된 상태입니다. 연결된 상태에서도 타입 변경을 진행합니다.")
                
                # 볼륨 타입 변경 요청
                change_initiated = self._initiate_volume_type_change(volume_id, target_type)
                
                if not change_initiated:
                    result['details']['error'] = f"볼륨 타입을 {current_type}에서 {target_type}으로 변경 요청 실패"
                    return result
                
                result['details']['action'] = f"볼륨 타입을 {current_type}에서 {target_type}으로 변경 요청 완료"
                result['details']['note'] = "볼륨 타입 변경은 백그라운드에서 계속 진행됩니다. 완료까지 몇 분 소요될 수 있습니다."
                result['details']['previous_type'] = current_type
                result['details']['target_type'] = target_type
                result['success'] = True
                
            elif action_type == 'change_type_and_resize':
                # 볼륨 타입 변경 및 크기 조정 로직 구현
                current_type = volume_info.get('volume_type', '')
                current_size = volume_info.get('size', 0)
                target_type = self._determine_target_volume_type(current_type)
                
                # 유휴 볼륨의 경우 최소 크기로 축소
                target_size = max(1, current_size // 2)  # 최소 1GB, 또는 현재 크기의 절반
                
                modification_initiated = self._initiate_volume_modification(volume_id, target_type, target_size)
                if not modification_initiated:
                    result['details']['error'] = f"볼륨 속성 변경 요청 실패 (타입: {current_type}->{target_type}, 크기: {current_size}->{target_size}GB)"
                    return result
                
                result['details']['action'] = f"볼륨 속성 변경 요청 완료 (타입: {current_type}->{target_type}, 크기: {current_size}->{target_size}GB)"
                result['details']['note'] = "볼륨 속성 변경은 백그라운드에서 계속 진행됩니다. 완료까지 몇 분 소요될 수 있습니다."
                result['details']['previous_type'] = current_type
                result['details']['previous_size'] = current_size
                result['details']['target_type'] = target_type
                result['details']['target_size'] = target_size
                result['success'] = True
                
            else:
                result['details']['error'] = f"지원되지 않는 작업 유형: {action_type}"
                
        except Exception as e:
            logger.error(f"권장 조치 실행 중 오류 발생: {str(e)}", exc_info=True)
            result['details']['error'] = str(e)
        
        # 실행 기록 저장
        self.execution_history.append(result)
        
        return result
    
    def execute_overprovisioned_volume_recommendation(self, volume_info, action_type):
        """
        과대 프로비저닝된 볼륨에 대한 권장 조치를 실행합니다.
        비동기적으로 처리하여 슬랙 액션이 오래 기다리지 않도록 합니다.
        
        :param volume_info: 볼륨 정보 딕셔너리
        :param action_type: 실행할 조치 유형 ('resize', 'change_type', 'change_type_and_resize')
        :return: 결과 딕셔너리
        """
        volume_id = volume_info['volume_id']
        result = {
            'volume_id': volume_id,
            'action_type': action_type,
            'success': False,
            'timestamp': datetime.now().isoformat(),
            'details': {},
            'status': 'initiated' # 작업이 시작되었음을 표시
        }
        
        # 유효한 작업 유형 확인
        valid_overprovisioned_actions = ['resize', 'change_type', 'change_type_and_resize']
        if action_type not in valid_overprovisioned_actions:
            error_msg = f"과대 프로비저닝 볼륨에 지원되지 않는 작업 유형: {action_type}. 유효한 작업: {valid_overprovisioned_actions}"
            logger.error(error_msg)
            result['details']['error'] = error_msg
            return result
            
        logger.info(f"볼륨 {volume_id}에 대한 '{action_type}' 작업 시작 중...")
        
        try:
            # 스냅샷 생성 (안전을 위해)
            tags = {'Name': f"Overprovisioned-{volume_id}", 'AutoCreated': 'true', 'Source': 'EBS-Optimizer'}
            
            if 'name' in volume_info:
                tags['SourceName'] = volume_info['name']
            
            snapshot_id = self.ebs_action_executor.create_snapshot(
                volume_id,
                description=f"Overprovisioned volume snapshot before {action_type} - {datetime.now().strftime('%Y-%m-%d')}",
                tags=tags
            )
            
            if snapshot_id:
                result['details']['snapshot_id'] = snapshot_id
                logger.info(f"볼륨 {volume_id}의 스냅샷 {snapshot_id} 생성 요청 완료. 스냅샷 생성은 백그라운드에서 계속됩니다.")
            
            # 현재 볼륨 정보 가져오기
            current_type = volume_info.get('volume_type', '')
            current_size = volume_info.get('size', 0)
            
            # 작업 유형에 따른 처리
            if action_type == 'resize':
                # 권장 크기 계산
                target_size = self._calculate_recommended_size(volume_info)
                if target_size >= current_size:
                    result['details']['message'] = f"볼륨 크기를 줄일 필요가 없습니다 (현재: {current_size}GB, 권장: {target_size}GB)"
                    result['success'] = True
                    return result
                
                # 볼륨 크기 조정 요청
                resize_initiated = self._initiate_resize_volume(volume_id, target_size)
                
                if not resize_initiated:
                    result['details']['error'] = f"볼륨 크기 조정 요청 실패 ({current_size}GB -> {target_size}GB)"
                    return result
                
                result['details']['action'] = f"볼륨 크기 조정 요청 완료 ({current_size}GB -> {target_size}GB)"
                result['details']['note'] = "볼륨 크기 조정은 백그라운드에서 계속 진행됩니다. 완료까지 몇 분 소요될 수 있습니다."
                result['details']['previous_size'] = current_size
                result['details']['target_size'] = target_size
                result['success'] = True
                
            elif action_type == 'change_type':
                # 권장 볼륨 유형 결정
                target_type = self._determine_target_volume_type(current_type)
                
                if current_type == target_type:
                    result['details']['message'] = f"볼륨 유형 {current_type}에서 변경이 필요하지 않습니다."
                    result['success'] = True
                    return result
                
                # 볼륨 타입 변경 요청
                change_initiated = self._initiate_volume_type_change(volume_id, target_type)
                
                if not change_initiated:
                    result['details']['error'] = f"볼륨 타입 변경 요청 실패 ({current_type} -> {target_type})"
                    return result
                
                result['details']['action'] = f"볼륨 타입 변경 요청 완료 ({current_type} -> {target_type})"
                result['details']['note'] = "볼륨 타입 변경은 백그라운드에서 계속 진행됩니다. 완료까지 몇 분 소요될 수 있습니다."
                result['details']['previous_type'] = current_type
                result['details']['target_type'] = target_type
                result['success'] = True
                
            elif action_type == 'change_type_and_resize':
                # 권장 볼륨 유형 및 크기 결정
                target_type = self._determine_target_volume_type(current_type)
                target_size = self._calculate_recommended_size(volume_info)
                
                # 볼륨 속성 변경 요청
                modification_initiated = self._initiate_volume_modification(volume_id, target_type, target_size)
                
                if not modification_initiated:
                    result['details']['error'] = f"볼륨 속성 변경 요청 실패 (타입: {current_type}->{target_type}, 크기: {current_size}->{target_size}GB)"
                    return result
                
                result['details']['action'] = f"볼륨 속성 변경 요청 완료 (타입: {current_type}->{target_type}, 크기: {current_size}->{target_size}GB)"
                result['details']['note'] = "볼륨 속성 변경은 백그라운드에서 계속 진행됩니다. 완료까지 몇 분 소요될 수 있습니다."
                result['details']['previous_type'] = current_type
                result['details']['previous_size'] = current_size
                result['details']['target_type'] = target_type
                result['details']['target_size'] = target_size
                result['success'] = True
                
        except Exception as e:
            logger.error(f"권장 조치 실행 중 오류 발생: {str(e)}", exc_info=True)
            result['details']['error'] = str(e)
        
        # 실행 기록 저장
        self.execution_history.append(result)
        
        return result

    # 비동기 작업을 위한 새로운 메서드들
    def _initiate_volume_type_change(self, volume_id, target_type):
        """
        볼륨 유형 변경을 요청합니다. 작업 완료를 기다리지 않습니다.
        
        :param volume_id: 볼륨 ID
        :param target_type: 변경할 볼륨 유형
        :return: 요청 성공 여부
        """
        try:
            # 현재 볼륨 정보 가져오기
            volume_info = self._get_volume_info(volume_id)
            if not volume_info:
                logger.error(f"볼륨 {volume_id} 정보를 가져올 수 없습니다.")
                return False
            
            # 변경 매개변수 설정
            kwargs = {
                'VolumeId': volume_id,
                'VolumeType': target_type
            }
            
            # 필요한 추가 매개변수 설정
            if target_type == 'gp3':
                kwargs['Iops'] = 3000
                kwargs['Throughput'] = 125
            elif target_type in ['io1', 'io2']:
                if 'Iops' in volume_info and volume_info['Iops']:
                    kwargs['Iops'] = max(100, volume_info['Iops'])
                else:
                    kwargs['Iops'] = 100
            
            # 볼륨 유형 변경 요청
            logger.info(f"볼륨 {volume_id}의 유형을 {target_type}으로 변경 요청")
            response = self.ec2_client.modify_volume(**kwargs)
            
            # 요청 확인
            modification_state = response['VolumeModification']['ModificationState']
            logger.info(f"볼륨 유형 변경 초기 상태: {modification_state}")
            
            return True
            
        except Exception as e:
            logger.error(f"볼륨 유형 변경 요청 중 오류 발생: {str(e)}")
            return False
    
    def _initiate_resize_volume(self, volume_id, target_size):
        """
        볼륨 크기 조정을 요청합니다. 작업 완료를 기다리지 않습니다.
        
        :param volume_id: 볼륨 ID
        :param target_size: 대상 크기(GB)
        :return: 요청 성공 여부
        """
        try:
            # 크기 조정 요청
            logger.info(f"볼륨 {volume_id}의 크기를 {target_size}GB로 조정 요청")
            response = self.ec2_client.modify_volume(
                VolumeId=volume_id,
                Size=target_size
            )
            
            # 요청 확인
            modification_state = response['VolumeModification']['ModificationState']
            logger.info(f"볼륨 크기 조정 초기 상태: {modification_state}")
            
            return True
            
        except Exception as e:
            logger.error(f"볼륨 크기 조정 요청 중 오류 발생: {str(e)}")
            return False
    
    def _initiate_volume_modification(self, volume_id, target_type, target_size):
        """
        볼륨 유형 및 크기 동시 변경을 요청합니다. 작업 완료를 기다리지 않습니다.
        
        :param volume_id: 볼륨 ID
        :param target_type: 변경할 볼륨 유형
        :param target_size: 대상 크기(GB)
        :return: 요청 성공 여부
        """
        try:
            # 현재 볼륨 정보 가져오기
            volume_info = self._get_volume_info(volume_id)
            if not volume_info:
                logger.error(f"볼륨 {volume_id} 정보를 가져올 수 없습니다.")
                return False
            
            # 변경 매개변수 설정
            kwargs = {
                'VolumeId': volume_id,
                'VolumeType': target_type,
                'Size': target_size
            }
            
            # 필요한 추가 매개변수 설정
            if target_type == 'gp3':
                kwargs['Iops'] = 3000
                kwargs['Throughput'] = 125
            elif target_type in ['io1', 'io2']:
                if 'Iops' in volume_info and volume_info['Iops']:
                    kwargs['Iops'] = max(100, volume_info['Iops'])
                else:
                    kwargs['Iops'] = 100
            
            # 볼륨 속성 변경 요청
            logger.info(f"볼륨 {volume_id}의 유형을 {target_type}으로, 크기를 {target_size}GB로 변경 요청")
            response = self.ec2_client.modify_volume(**kwargs)
            
            # 요청 확인
            modification_state = response['VolumeModification']['ModificationState']
            logger.info(f"볼륨 속성 변경 초기 상태: {modification_state}")
            
            return True
            
        except Exception as e:
            logger.error(f"볼륨 속성 변경 요청 중 오류 발생: {str(e)}")
            return False
    
    def get_execution_history(self):
        """
        실행 기록을 반환합니다.
        
        :return: 실행 기록 리스트
        """
        return self.execution_history
    
    def save_execution_history(self, filepath):
        """
        실행 기록을 파일로 저장합니다.
        
        :param filepath: 저장할 파일 경로
        :return: 저장 성공 여부
        """
        try:
            with open(filepath, 'w') as f:
                json.dump(self.execution_history, f, indent=2)
            return True
        except Exception as e:
            logger.error(f"실행 기록 저장 중 오류 발생: {str(e)}")
            return False
    
    def _get_volume_info(self, volume_id):
        """
        EBS 볼륨 정보를 가져옵니다.
        
        :param volume_id: 볼륨 ID
        :return: 볼륨 정보 또는 None
        """
        try:
            response = self.ec2_client.describe_volumes(VolumeIds=[volume_id])
            volumes = response.get('Volumes', [])
            return volumes[0] if volumes else None
        except Exception as e:
            logger.error(f"볼륨 {volume_id} 정보 조회 중 오류: {str(e)}")
            return None
    
    def _create_snapshot(self, volume_id, description, tags):
        """
        EBS 볼륨의 스냅샷을 생성합니다.
        
        :param volume_id: 볼륨 ID
        :param description: 스냅샷 설명
        :param tags: 스냅샷에 적용할 태그 딕셔너리
        :return: 스냅샷 ID 또는 None
        """
        try:
            # 태그 형식 변환
            tag_specs = [{
                'ResourceType': 'snapshot',
                'Tags': [{'Key': k, 'Value': v} for k, v in tags.items()]
            }]
            
            response = self.ec2_client.create_snapshot(
                VolumeId=volume_id,
                Description=description,
                TagSpecifications=tag_specs
            )
            
            return response.get('SnapshotId')
        except Exception as e:
            logger.error(f"스냅샷 생성 중 오류: {str(e)}")
            return None
    
    def check_snapshot_status(self, snapshot_id):
        """
        스냅샷 상태를 확인합니다.
        
        :param snapshot_id: 스냅샷 ID
        :return: 상태 정보 딕셔너리
        """
        try:
            response = self.ec2_client.describe_snapshots(SnapshotIds=[snapshot_id])
            if response['Snapshots']:
                snapshot = response['Snapshots'][0]
                return {
                    'state': snapshot['State'],
                    'progress': snapshot.get('Progress', 'N/A'),
                    'start_time': snapshot.get('StartTime', 'N/A').isoformat() if hasattr(snapshot.get('StartTime', 'N/A'), 'isoformat') else snapshot.get('StartTime', 'N/A'),
                    'volume_id': snapshot.get('VolumeId', 'N/A'),
                    'volume_size': snapshot.get('VolumeSize', 'N/A')
                }
            return {'error': '스냅샷을 찾을 수 없습니다.'}
        except Exception as e:
            logger.error(f"스냅샷 상태 확인 중 오류: {str(e)}")
            return {'error': f"스냅샷 상태 확인 중 오류: {str(e)}"}
    
    def check_volume_status(self, volume_id):
        """
        볼륨 상태를 확인합니다.
        
        :param volume_id: 볼륨 ID
        :return: 상태 정보 딕셔너리
        """
        try:
            response = self.ec2_client.describe_volumes(VolumeIds=[volume_id])
            if response['Volumes']:
                volume = response['Volumes'][0]
                attachments = volume.get('Attachments', [])
                attach_info = []
                
                for attachment in attachments:
                    attach_info.append({
                        'instance_id': attachment.get('InstanceId'),
                        'device': attachment.get('Device'),
                        'state': attachment.get('State')
                    })
                
                return {
                    'state': volume['State'],
                    'attachments': attach_info,
                    'volume_type': volume.get('VolumeType'),
                    'size': volume.get('Size'),
                    'iops': volume.get('Iops', 'N/A'),
                    'throughput': volume.get('Throughput', 'N/A')
                }
            return {'error': '볼륨을 찾을 수 없습니다.'}
        except Exception as e:
            logger.error(f"볼륨 상태 확인 중 오류: {str(e)}")
            return {'error': f"볼륨 상태 확인 중 오류: {str(e)}"}
    
    def check_volume_modification_status(self, volume_id):
        """
        볼륨 수정 상태를 확인합니다.
        
        :param volume_id: 볼륨 ID
        :return: 수정 상태 정보 딕셔너리
        """
        try:
            response = self.ec2_client.describe_volumes_modifications(VolumeIds=[volume_id])
            if response['VolumesModifications']:
                mod = response['VolumesModifications'][0]
                return {
                    'state': mod.get('ModificationState'),
                    'progress': mod.get('Progress', 0),
                    'start_time': mod.get('StartTime', 'N/A').isoformat() if hasattr(mod.get('StartTime', 'N/A'), 'isoformat') else mod.get('StartTime', 'N/A'),
                    'original_type': mod.get('OriginalVolumeType'),
                    'target_type': mod.get('TargetVolumeType'),
                    'original_size': mod.get('OriginalSize'),
                    'target_size': mod.get('TargetSize'),
                    'original_iops': mod.get('OriginalIops', 'N/A'),
                    'target_iops': mod.get('TargetIops', 'N/A')
                }
            return {'error': '볼륨 수정 정보를 찾을 수 없습니다.'}
        except Exception as e:
            logger.error(f"볼륨 수정 상태 확인 중 오류: {str(e)}")
            return {'error': f"볼륨 수정 상태 확인 중 오류: {str(e)}"}
    
    def _is_root_volume(self, instance_id, device_name):
        """
        지정된 볼륨이 인스턴스의 루트 볼륨인지 확인합니다.
        
        :param instance_id: 인스턴스 ID
        :param device_name: 디바이스 이름
        :return: 루트 볼륨 여부
        """
        try:
            response = self.ec2_client.describe_instances(InstanceIds=[instance_id])
            for reservation in response.get('Reservations', []):
                for instance in reservation.get('Instances', []):
                    root_device_name = instance.get('RootDeviceName')
                    if root_device_name == device_name:
                        return True
            return False
        except Exception as e:
            logger.error(f"인스턴스 {instance_id}의 루트 볼륨 확인 중 오류: {str(e)}")
            return False
    
    def _determine_target_volume_type(self, current_type):
        """
        현재 볼륨 유형에 따라 권장되는 볼륨 유형을 결정합니다.
        
        :param current_type: 현재 볼륨 유형
        :return: 권장 볼륨 유형
        """
        # 볼륨 유형 최적화 로직
        type_map = {
            'standard': 'gp3',  # 마그네틱 → gp3
            'gp2': 'gp3',      # gp2 → gp3 (비용 효율적)
            'io1': 'gp3',      # io1 → gp3 (대부분의 경우 gp3로 충분)
            'st1': 'st1',      # st1은 그대로 유지 (처리량 최적화)
            'sc1': 'sc1'       # sc1은 그대로 유지 (콜드 HDD)
        }
        
        # 기본적으로 gp3로 변환하며, 특수 유형은 예외 처리
        return type_map.get(current_type, 'gp3')
    
    def _calculate_recommended_size(self, volume_info):
        """
        볼륨 사용량 정보를 기반으로 권장 크기를 계산합니다.
        
        :param volume_info: 볼륨 정보
        :return: 권장 볼륨 크기(GB)
        """
        current_size = volume_info.get('size', 0)
        
        # 분석된 데이터가 있는 경우 사용
        if volume_info.get('recommendation'):
            if 'recommended_size' in volume_info['recommendation']:
                return max(1, int(volume_info['recommendation']['recommended_size']))
        
        # 메트릭 데이터가 있으면 사용
        if volume_info.get('metrics') and 'VolumeConsumedReadWriteOps' in volume_info['metrics']:
            # 간단한 예: 사용량이 낮다면 크기 축소
            ops_data = volume_info['metrics']['VolumeConsumedReadWriteOps']
            avg_ops = sum(dp['Average'] for dp in ops_data['Datapoints']) / len(ops_data['Datapoints']) if ops_data['Datapoints'] else 0
            
            if avg_ops < 100:  # 매우 낮은 사용량
                return max(1, current_size // 2)  # 현재 크기의 절반
            elif avg_ops < 500:  # 낮은 사용량
                return max(1, int(current_size * 0.7))  # 현재 크기의 70%
        
        # 기본값: 최소 크기와 현재 크기의 75% 중 큰 값
        return max(1, int(current_size * 0.75))
