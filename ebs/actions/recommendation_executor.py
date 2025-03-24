import logging
import json
import time
import boto3
from datetime import datetime
from actions.ebs_actions import EBSActionExecutor

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
            'details': {}
        }
        
        # 유효한 작업 유형 확인
        valid_idle_actions = ['snapshot_and_delete', 'snapshot_only', 'change_type', 'change_type_and_resize']
        if action_type not in valid_idle_actions:
            error_msg = f"유휴 볼륨에 지원되지 않는 작업 유형: {action_type}. 유효한 작업: {valid_idle_actions}"
            logger.error(error_msg)
            result['details']['error'] = error_msg
            return result
            
        logger.info(f"볼륨 {volume_id}에 대한 '{action_type}' 작업 실행 중...")
        
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
                result['details']['error'] = "스냅샷 생성 실패"
                return result
            
            result['details']['snapshot_id'] = snapshot_id
            logger.info(f"볼륨 {volume_id}의 스냅샷 {snapshot_id} 생성 완료")
            
            # 2. 선택한 작업 유형에 따라 실행
            if action_type == 'snapshot_and_delete':
                # 볼륨에 연결된 경우 분리
                if volume_info.get('attached_instances'):
                    for attachment in volume_info['attached_instances']:
                        instance_id = attachment['instance_id']
                        logger.info(f"볼륨 {volume_id}를 인스턴스 {instance_id}에서 분리합니다.")
                        
                        detach_success = self.ebs_action_executor.detach_volume(volume_id)
                        
                        if not detach_success:
                            result['details']['error'] = f"인스턴스 {instance_id}에서 볼륨 분리 실패"
                            return result
                
                # 볼륨 삭제
                delete_success = self.ebs_action_executor.delete_volume(volume_id)
                
                if not delete_success:
                    result['details']['error'] = "볼륨 삭제 실패"
                    return result
                
                result['details']['action'] = "스냅샷 생성 후 볼륨 삭제 완료"
                result['success'] = True
                
            elif action_type == 'snapshot_only':
                result['details']['action'] = "스냅샷 생성 완료"
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
                
                logger.info(f"볼륨 {volume_id} 상세 정보: {volume_detail}")
                
                # 볼륨이 사용 중인지 확인
                if volume_detail.get('State') != 'available' and volume_detail.get('Attachments'):
                    logger.warning(f"볼륨 {volume_id}이(가) 인스턴스에 연결된 상태입니다. 연결된 상태에서도 타입 변경을 진행합니다.")
                
                # 볼륨 타입 변경 실행
                change_success = self._change_volume_type(volume_id, target_type)
                
                if not change_success:
                    result['details']['error'] = f"볼륨 타입을 {current_type}에서 {target_type}으로 변경하지 못했습니다."
                    return result
                
                result['details']['action'] = f"볼륨 타입을 {current_type}에서 {target_type}으로 변경했습니다."
                result['details']['previous_type'] = current_type
                result['details']['new_type'] = target_type
                result['success'] = True
                
            elif action_type == 'change_type_and_resize':
                # 볼륨 타입 변경 및 크기 조정 로직 구현
                current_type = volume_info.get('volume_type', '')
                current_size = volume_info.get('size', 0)
                target_type = self._determine_target_volume_type(current_type)
                
                # 유휴 볼륨의 경우 최소 크기로 축소
                target_size = max(1, current_size // 2)  # 최소 1GB, 또는 현재 크기의 절반
                
                modification_success = self._modify_volume(volume_id, target_type, target_size)
                if not modification_success:
                    result['details']['error'] = f"볼륨 속성 변경 실패 (타입: {current_type}->{target_type}, 크기: {current_size}->{target_size}GB)"
                    return result
                
                result['details']['action'] = f"볼륨 속성 변경 완료 (타입: {current_type}->{target_type}, 크기: {current_size}->{target_size}GB)"
                result['details']['previous_type'] = current_type
                result['details']['previous_size'] = current_size
                result['details']['new_type'] = target_type
                result['details']['new_size'] = target_size
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
            'details': {}
        }
        
        # 유효한 작업 유형 확인
        valid_overprovisioned_actions = ['resize', 'change_type', 'change_type_and_resize']
        if action_type not in valid_overprovisioned_actions:
            error_msg = f"과대 프로비저닝 볼륨에 지원되지 않는 작업 유형: {action_type}. 유효한 작업: {valid_overprovisioned_actions}"
            logger.error(error_msg)
            result['details']['error'] = error_msg
            return result
            
        logger.info(f"볼륨 {volume_id}에 대한 '{action_type}' 작업 실행 중...")
        
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
                
                # 볼륨 크기 조정
                resize_success = self._resize_volume(volume_id, target_size)
                
                if not resize_success:
                    result['details']['error'] = f"볼륨 크기 조정 실패 ({current_size}GB -> {target_size}GB)"
                    return result
                
                result['details']['action'] = f"볼륨 크기를 {current_size}GB에서 {target_size}GB로 조정했습니다."
                result['details']['previous_size'] = current_size
                result['details']['new_size'] = target_size
                result['success'] = True
                
            elif action_type == 'change_type':
                # 권장 볼륨 유형 결정
                target_type = self._determine_target_volume_type(current_type)
                
                if current_type == target_type:
                    result['details']['message'] = f"볼륨 유형 {current_type}에서 변경이 필요하지 않습니다."
                    result['success'] = True
                    return result
                
                # 볼륨 타입 변경
                change_success = self._change_volume_type(volume_id, target_type)
                
                if not change_success:
                    result['details']['error'] = f"볼륨 타입 변경 실패 ({current_type} -> {target_type})"
                    return result
                
                result['details']['action'] = f"볼륨 타입을 {current_type}에서 {target_type}으로 변경했습니다."
                result['details']['previous_type'] = current_type
                result['details']['new_type'] = target_type
                result['success'] = True
                
            elif action_type == 'change_type_and_resize':
                # 권장 볼륨 유형 및 크기 결정
                target_type = self._determine_target_volume_type(current_type)
                target_size = self._calculate_recommended_size(volume_info)
                
                # 볼륨 속성 변경
                modification_success = self._modify_volume(volume_id, target_type, target_size)
                
                if not modification_success:
                    result['details']['error'] = f"볼륨 속성 변경 실패 (타입: {current_type}->{target_type}, 크기: {current_size}->{target_size}GB)"
                    return result
                
                result['details']['action'] = f"볼륨 속성 변경 완료 (타입: {current_type}->{target_type}, 크기: {current_size}->{target_size}GB)"
                result['details']['previous_type'] = current_type
                result['details']['previous_size'] = current_size
                result['details']['new_type'] = target_type
                result['details']['new_size'] = target_size
                result['success'] = True
                
        except Exception as e:
            logger.error(f"권장 조치 실행 중 오류 발생: {str(e)}", exc_info=True)
            result['details']['error'] = str(e)
        
        # 실행 기록 저장
        self.execution_history.append(result)
        
        return result
    
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
    
    def _resize_volume(self, volume_id, target_size):
        """
        EBS 볼륨의 크기를 조정합니다.
        크기 감소는 불가능하므로 스냅샷을 생성하고 새 볼륨을 만드는 방식으로 처리합니다.
        
        :param volume_id: 볼륨 ID
        :param target_size: 조정할 크기(GB)
        :return: 성공 여부
        """
        try:
            logger.info(f"볼륨 {volume_id}의 크기를 {target_size}GB로 조정합니다.")
            
            # 원본 볼륨 정보 가져오기
            original_volume = self._get_volume_info(volume_id)
            if not original_volume:
                logger.error(f"볼륨 {volume_id} 정보를 가져올 수 없습니다.")
                return False
                
            current_size = original_volume.get('Size')
            
            # 볼륨 크기 증가인 경우 기존 API 사용
            if target_size >= current_size:
                logger.info(f"볼륨 크기 증가 요청 (현재: {current_size}GB -> 목표: {target_size}GB)")
                response = self.ec2_client.modify_volume(
                    VolumeId=volume_id,
                    Size=target_size
                )
                
                # 볼륨 크기 조정 상태 확인
                modification_state = response['VolumeModification']['ModificationState']
                logger.info(f"볼륨 크기 조정 요청 상태: {modification_state}")
                
                # 볼륨 수정이 완료될 때까지 대기 (최대 60초)
                for _ in range(30):
                    response = self.ec2_client.describe_volumes_modifications(
                        VolumeIds=[volume_id]
                    )
                    
                    if not response['VolumesModifications']:
                        logger.warning(f"볼륨 {volume_id}의 수정 정보를 찾을 수 없습니다.")
                        break
                    
                    current_state = response['VolumesModifications'][0]['ModificationState']
                    logger.info(f"볼륨 크기 조정 진행 상태: {current_state}")
                    
                    if current_state == 'completed' or current_state == 'optimizing':
                        return True
                    
                    if current_state == 'failed':
                        logger.error(f"볼륨 크기 조정 실패: {response['VolumesModifications'][0].get('StatusMessage', '알 수 없는 오류')}")
                        return False
                    
                    time.sleep(2)  # 2초 대기 후 다시 확인
                    
                return True
                
            # 볼륨 크기 감소의 경우: 스냅샷 생성 후 새 볼륨 생성
            logger.info(f"볼륨 크기 감소를 위해 스냅샷 생성 후 새 볼륨 생성 (현재: {current_size}GB -> 목표: {target_size}GB)")
            
            # 1. 스냅샷 생성
            snapshot_description = f"Snapshot for downsizing volume {volume_id} from {current_size}GB to {target_size}GB"
            snapshot_tags = {
                'Name': f"Resize-{volume_id}",
                'Purpose': 'Volume-Downsizing',
                'OriginalVolumeId': volume_id,
                'AutoCreated': 'true',
                'Source': 'EBS-Optimizer'
            }
            
            # 원본 볼륨의 태그 복사
            if 'Tags' in original_volume:
                for tag in original_volume['Tags']:
                    if tag['Key'] != 'Name' and tag['Key'] not in snapshot_tags:
                        snapshot_tags[tag['Key']] = tag['Value']
            
            snapshot_id = self._create_snapshot(volume_id, snapshot_description, snapshot_tags)
            if not snapshot_id:
                logger.error(f"볼륨 {volume_id}의 스냅샷 생성 실패")
                return False
                
            logger.info(f"볼륨 {volume_id}의 스냅샷 {snapshot_id} 생성 완료")
            
            # 스냅샷 완료 대기
            if not self._wait_for_snapshot_completion(snapshot_id):
                logger.error(f"스냅샷 {snapshot_id}이 제한 시간 내에 완료되지 않았습니다.")
                return False
            
            # 2. 새 볼륨 생성
            # 원본 볼륨의 속성 가져오기
            volume_type = original_volume.get('VolumeType', 'gp3')
            az = original_volume.get('AvailabilityZone')
            encrypted = original_volume.get('Encrypted', False)
            
            # 원본 볼륨의 태그 복사를 위한 준비
            volume_tags = []
            if 'Tags' in original_volume:
                for tag in original_volume['Tags']:
                    # AWS 시스템 태그는 제외
                    if not tag['Key'].startswith('aws:'):
                        volume_tags.append({
                            'Key': tag['Key'],
                            'Value': tag['Value']
                        })
            
            # 새 볼륨용 태그 추가
            volume_tags.append({'Key': 'Name', 'Value': f"Resized-{volume_id}"})
            volume_tags.append({'Key': 'OriginalVolumeId', 'Value': volume_id})
            volume_tags.append({'Key': 'ResizeTime', 'Value': datetime.now().isoformat()})
            
            # 볼륨 유형에 따른 추가 파라미터 설정
            create_args = {
                'AvailabilityZone': az,
                'SnapshotId': snapshot_id,
                'VolumeType': volume_type,
                'Size': target_size,
                'Encrypted': encrypted,
                'TagSpecifications': [
                    {
                        'ResourceType': 'volume',
                        'Tags': volume_tags
                    }
                ]
            }
            
            # IOPS가 필요한 볼륨 유형인 경우 설정
            if volume_type in ['io1', 'io2', 'gp3']:
                iops = original_volume.get('Iops', 3000)  # gp3의 경우 기본값
                create_args['Iops'] = iops
                
                # Throughput은 gp3에만 있음
                if volume_type == 'gp3' and 'Throughput' in original_volume:
                    create_args['Throughput'] = original_volume['Throughput']
            
            # 새 볼륨 생성
            new_volume_id = None
            try:
                response = self.ec2_client.create_volume(**create_args)
                new_volume_id = response['VolumeId']
                logger.info(f"새 볼륨 {new_volume_id} 생성 완료 (크기: {target_size}GB)")
            except Exception as e:
                logger.error(f"새 볼륨 생성 중 오류 발생: {str(e)}")
                return False
            
            # 볼륨이 사용 가능해질 때까지 대기
            if not self._wait_for_volume_available(new_volume_id):
                logger.error(f"새 볼륨 {new_volume_id}이 제한 시간 내에 사용 가능 상태가 되지 않았습니다.")
                return False
            
            # 3. 볼륨 연결 여부 확인 및 처리
            attachments = original_volume.get('Attachments', [])
            if attachments:
                for attachment in attachments:
                    instance_id = attachment.get('InstanceId')
                    device_name = attachment.get('Device')
                    
                    if not instance_id or not device_name:
                        continue
                    
                    # 루트 볼륨인지 확인
                    is_root = self._is_root_volume(instance_id, device_name)
                    
                    logger.info(f"볼륨 {volume_id}는 인스턴스 {instance_id}에 {device_name}으로 연결됨. 루트 볼륨: {is_root}")
                    
                    # 루트 볼륨인 경우 특별 처리 필요
                    if is_root:
                        logger.warning(f"볼륨 {volume_id}는 인스턴스 {instance_id}의 루트 볼륨입니다. 루트 볼륨 크기 조정은 지원하지 않습니다.")
                        # 새 볼륨 삭제
                        self.ec2_client.delete_volume(VolumeId=new_volume_id)
                        return False
                    
                    # 기존 볼륨 분리
                    logger.info(f"볼륨 {volume_id}를 인스턴스 {instance_id}에서 분리합니다.")
                    self.ec2_client.detach_volume(
                        VolumeId=volume_id,
                        InstanceId=instance_id,
                        Device=device_name,
                        Force=True  # 강제 분리 사용
                    )
                    
                    # 볼륨 분리 대기
                    if not self._wait_for_volume_detachment(volume_id):
                        logger.error(f"볼륨 {volume_id} 분리 대기 중 타임아웃")
                        return False
                    
                    # 새 볼륨 연결
                    logger.info(f"새 볼륨 {new_volume_id}를 인스턴스 {instance_id}의 {device_name}에 연결합니다.")
                    try:
                        self.ec2_client.attach_volume(
                            VolumeId=new_volume_id,
                            InstanceId=instance_id,
                            Device=device_name
                        )
                        
                        # 볼륨 연결 대기
                        if not self._wait_for_volume_attachment(new_volume_id, instance_id):
                            logger.error(f"새 볼륨 {new_volume_id} 연결 대기 중 타임아웃")
                            return False
                            
                        logger.info(f"새 볼륨 {new_volume_id}를 인스턴스 {instance_id}에 연결 완료")
                    except Exception as e:
                        logger.error(f"새 볼륨 연결 중 오류 발생: {str(e)}")
                        return False
            
            # 4. 원본 볼륨 삭제 (선택적)
            try:
                # attachments가 있었다면 이미 분리되었음
                logger.info(f"원본 볼륨 {volume_id}를 삭제합니다.")
                self.ec2_client.delete_volume(VolumeId=volume_id)
                logger.info(f"원본 볼륨 {volume_id} 삭제 완료")
            except Exception as e:
                logger.warning(f"원본 볼륨 {volume_id} 삭제 중 오류 발생: {str(e)}")
                # 원본 볼륨 삭제 실패는 전체 프로세스 실패로 간주하지 않음
            
            return True
            
        except Exception as e:
            logger.error(f"볼륨 크기 조정 중 오류 발생: {str(e)}")
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
    
    def _wait_for_snapshot_completion(self, snapshot_id, timeout_seconds=600):
        """
        스냅샷이 완료될 때까지 기다립니다.
        
        :param snapshot_id: 스냅샷 ID
        :param timeout_seconds: 타임아웃(초)
        :return: 성공 여부
        """
        start_time = time.time()
        interval = 10  # 초
        
        while (time.time() - start_time) < timeout_seconds:
            try:
                response = self.ec2_client.describe_snapshots(SnapshotIds=[snapshot_id])
                snapshot = response['Snapshots'][0]
                state = snapshot['State']
                
                logger.info(f"스냅샷 {snapshot_id} 상태: {state}")
                
                if state == 'completed':
                    return True
                elif state == 'error':
                    logger.error(f"스냅샷 {snapshot_id} 생성 중 오류 발생")
                    return False
                
                # 진행률 로깅
                progress = snapshot.get('Progress', '0%')
                logger.info(f"스냅샷 {snapshot_id} 진행률: {progress}")
                
                time.sleep(interval)
            except Exception as e:
                logger.error(f"스냅샷 상태 확인 중 오류: {str(e)}")
                time.sleep(interval)
        
        logger.warning(f"스냅샷 {snapshot_id} 완료 대기 시간 초과 ({timeout_seconds}초)")
        return False
    
    def _wait_for_volume_available(self, volume_id, timeout_seconds=120):
        """
        볼륨이 'available' 상태가 될 때까지 기다립니다.
        
        :param volume_id: 볼륨 ID
        :param timeout_seconds: 타임아웃(초)
        :return: 성공 여부
        """
        start_time = time.time()
        interval = 5  # 초
        
        while (time.time() - start_time) < timeout_seconds:
            try:
                response = self.ec2_client.describe_volumes(VolumeIds=[volume_id])
                volume = response['Volumes'][0]
                state = volume['State']
                
                logger.info(f"볼륨 {volume_id} 상태: {state}")
                
                if state == 'available':
                    return True
                elif state == 'error':
                    logger.error(f"볼륨 {volume_id}가 오류 상태입니다")
                    return False
                
                time.sleep(interval)
            except Exception as e:
                logger.error(f"볼륨 상태 확인 중 오류: {str(e)}")
                time.sleep(interval)
        
        logger.warning(f"볼륨 {volume_id} 'available' 상태 대기 시간 초과 ({timeout_seconds}초)")
        return False
    
    def _wait_for_volume_attachment(self, volume_id, instance_id, timeout_seconds=120):
        """
        볼륨이 인스턴스에 연결될 때까지 기다립니다.
        
        :param volume_id: 볼륨 ID
        :param instance_id: 인스턴스 ID
        :param timeout_seconds: 타임아웃(초)
        :return: 성공 여부
        """
        start_time = time.time()
        interval = 5  # 초
        
        while (time.time() - start_time) < timeout_seconds:
            try:
                response = self.ec2_client.describe_volumes(VolumeIds=[volume_id])
                volume = response['Volumes'][0]
                attachments = volume.get('Attachments', [])
                
                for attachment in attachments:
                    if (attachment.get('InstanceId') == instance_id and 
                        attachment.get('State') == 'attached'):
                        return True
                
                time.sleep(interval)
            except Exception as e:
                logger.error(f"볼륨 연결 상태 확인 중 오류: {str(e)}")
                time.sleep(interval)
        
        logger.warning(f"볼륨 {volume_id}의 인스턴스 {instance_id} 연결 대기 시간 초과 ({timeout_seconds}초)")
        return False
    
    def _wait_for_volume_detachment(self, volume_id, timeout_seconds=120):
        """
        볼륨이 분리될 때까지 기다립니다.
        
        :param volume_id: 볼륨 ID
        :param timeout_seconds: 타임아웃(초)
        :return: 성공 여부
        """
        start_time = time.time()
        interval = 5  # 초
        
        while (time.time() - start_time) < timeout_seconds:
            try:
                response = self.ec2_client.describe_volumes(VolumeIds=[volume_id])
                volume = response['Volumes'][0]
                state = volume['State']
                attachments = volume.get('Attachments', [])
                
                if not attachments and state == 'available':
                    return True
                
                time.sleep(interval)
            except Exception as e:
                logger.error(f"볼륨 분리 상태 확인 중 오류: {str(e)}")
                time.sleep(interval)
        
        logger.warning(f"볼륨 {volume_id} 분리 대기 시간 초과 ({timeout_seconds}초)")
        return False
    
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
    
    def _change_volume_type(self, volume_id, target_type):
        """
        EBS 볼륨의 유형을 변경합니다.
        
        :param volume_id: 볼륨 ID
        :param target_type: 변경할 볼륨 유형(gp2, gp3, io1, io2 등)
        :return: 성공 여부
        """
        try:
            # 현재 볼륨 정보 가져오기
            volume_info = self._get_volume_info(volume_id)
            if not volume_info:
                logger.error(f"볼륨 {volume_id} 정보를 가져올 수 없습니다.")
                return False
                
            current_type = volume_info.get('VolumeType')
            current_size = volume_info.get('Size')
            
            logger.info(f"볼륨 {volume_id}의 유형을 {current_type}에서 {target_type}으로 변경 시도 중...")
            
            # 이미 원하는 타입인 경우 성공으로 처리
            if current_type == target_type:
                logger.info(f"볼륨 {volume_id}은(는) 이미 요청한 타입({target_type})입니다.")
                return True
            
            kwargs = {
                'VolumeId': volume_id,
                'VolumeType': target_type
            }
            
            # gp3, io1, io2의 경우 추가 파라미터 설정
            if target_type == 'gp3':
                # gp3는 기본 IOPS와 Throughput 파라미터 필요
                kwargs['Iops'] = 3000  # gp3의 기본 IOPS
                kwargs['Throughput'] = 125  # gp3의 기본 Throughput (MB/s)
                logger.info(f"gp3 유형에 필요한 기본 파라미터 추가: IOPS=3000, Throughput=125MB/s")
                
            elif target_type in ['io1', 'io2']:
                # io1/io2는 최소 100 IOPS 필요
                # 현재 IOPS가 있으면 그것을 유지, 아니면 최소값 사용
                if 'Iops' in volume_info and volume_info['Iops']:
                    kwargs['Iops'] = max(100, volume_info['Iops'])
                else:
                    kwargs['Iops'] = 100
                logger.info(f"{target_type} 유형에 필요한 IOPS 파라미터 추가: {kwargs['Iops']}")
            
            # 변경 시도
            logger.info(f"볼륨 수정 API 호출 시작: {kwargs}")
            response = self.ec2_client.modify_volume(**kwargs)
            
            # 응답 기록
            logger.info(f"볼륨 수정 API 응답: {response}")
            
            # 볼륨 유형 변경 상태 확인
            modification_state = response['VolumeModification']['ModificationState']
            logger.info(f"볼륨 유형 변경 초기 상태: {modification_state}")
            
            # 볼륨 수정이 완료될 때까지 대기 (최대 60초)
            for i in range(30):
                try:
                    response = self.ec2_client.describe_volumes_modifications(
                        VolumeIds=[volume_id]
                    )
                    
                    if not response['VolumesModifications']:
                        logger.warning(f"볼륨 {volume_id}의 수정 정보를 찾을 수 없습니다.")
                        break
                    
                    current_state = response['VolumesModifications'][0]['ModificationState']
                    logger.info(f"볼륨 유형 변경 진행 상태 ({i+1}/30): {current_state}")
                    
                    if current_state == 'completed':
                        # 볼륨이 실제로 변경되었는지 확인
                        updated_volume = self._get_volume_info(volume_id)
                        actual_type = updated_volume.get('VolumeType') if updated_volume else None
                        
                        if actual_type == target_type:
                            logger.info(f"볼륨 {volume_id}의 유형이 성공적으로 {target_type}으로 변경되었습니다.")
                            return True
                        else:
                            logger.error(f"볼륨 타입 변경 검증 실패: 기대한 타입 {target_type}, 실제 타입 {actual_type}")
                            return False
                    
                    if current_state == 'optimizing':
                        logger.info(f"볼륨 유형 변경이 진행 중입니다. 최적화 단계에서 성공으로 간주합니다.")
                        return True
                    
                    if current_state == 'failed':
                        error_msg = response['VolumesModifications'][0].get('StatusMessage', '알 수 없는 오류')
                        logger.error(f"볼륨 유형 변경 실패: {error_msg}")
                        return False
                    
                    time.sleep(2)  # 2초 대기 후 다시 확인
                
                except Exception as check_error:
                    logger.error(f"볼륨 수정 상태 확인 중 오류: {str(check_error)}")
                    time.sleep(2)
            
            # 시간 초과 후 최종 상태 확인
            try:
                updated_volume = self._get_volume_info(volume_id)
                actual_type = updated_volume.get('VolumeType') if updated_volume else None
                
                if actual_type == target_type:
                    logger.info(f"시간 초과 후 확인: 볼륨 {volume_id}의 유형이 성공적으로 {target_type}으로 변경되었습니다.")
                    return True
                else:
                    logger.warning(f"시간 초과 후 확인: 볼륨 타입 변경이 완료되지 않았습니다. 현재 타입: {actual_type}")
            except Exception as final_check_error:
                logger.error(f"최종 상태 확인 중 오류: {str(final_check_error)}")
            
            return False
            
        except Exception as e:
            logger.error(f"볼륨 유형 변경 중 오류 발생: {str(e)}", exc_info=True)
            return False
    
    def _modify_volume(self, volume_id, target_type, target_size):
        """
        EBS 볼륨의 유형과 크기를 동시에 변경합니다.
        크기 감소는 스냅샷을 통한 새 볼륨 생성 방식으로 처리되나, AWS 제한으로 실제 크기 감소가 되지 않을 수 있습니다.
        
        :param volume_id: 볼륨 ID
        :param target_type: 변경할 볼륨 유형(gp2, gp3, io1, io2 등)
        :param target_size: 조정할 크기(GB)
        :return: 성공 여부
        """
        try:
            logger.info(f"볼륨 {volume_id}의 유형을 {target_type}로, 크기를 {target_size}GB로 변경합니다.")
            
            # 원본 볼륨 정보 가져오기
            original_volume = self._get_volume_info(volume_id)
            if not original_volume:
                logger.error(f"볼륨 {volume_id} 정보를 가져올 수 없습니다.")
                return False
                
            current_size = original_volume.get('Size')
            current_type = original_volume.get('VolumeType')
            
            # 볼륨 크기 감소 시도하는 경우 경고 표시
            if target_size < current_size:
                logger.warning(f"주의: AWS EBS 제한으로 인해 스냅샷을 사용한 볼륨 크기 축소가 불가능합니다.")
                logger.warning(f"요청한 크기 {target_size}GB는 무시되고, 원본 크기 {current_size}GB가 유지됩니다.")
                logger.warning(f"대안 1: 볼륨 유형만 변경하여 비용을 절감하세요 (예: gp2→gp3)")
                logger.warning(f"대안 2: 고급 데이터 마이그레이션 기법을 사용하세요 (EC2 인스턴스에서 수동 작업 필요)")
                
                # 사용자에게 명확한 정보 제공을 위해 요청 크기를 원본 크기로 조정
                target_size = current_size
            
            # 볼륨 크기 증가인 경우에만 기존 API 사용
            # 크기가 줄어들거나 같아도 유형이 변경되는 경우는 스냅샷 방식 사용
            if target_size >= current_size and target_type == current_type:
                logger.info(f"볼륨 크기만 증가하는 경우, AWS API 사용 (현재: {current_size}GB -> 목표: {target_size}GB)")
                kwargs = {
                    'VolumeId': volume_id,
                    'Size': target_size
                }
                
                response = self.ec2_client.modify_volume(**kwargs)
                
                # 볼륨 수정 상태 확인
                modification_state = response['VolumeModification']['ModificationState']
                logger.info(f"볼륨 크기 조정 요청 상태: {modification_state}")
                
                # 볼륨 수정이 완료될 때까지 대기 (최대 60초)
                for _ in range(30):
                    response = self.ec2_client.describe_volumes_modifications(
                        VolumeIds=[volume_id]
                    )
                    
                    if not response['VolumesModifications']:
                        logger.warning(f"볼륨 {volume_id}의 수정 정보를 찾을 수 없습니다.")
                        break
                    
                    current_state = response['VolumesModifications'][0]['ModificationState']
                    logger.info(f"볼륨 크기 조정 진행 상태: {current_state}")
                    
                    if current_state == 'completed' or current_state == 'optimizing':
                        return True
                    
                    if current_state == 'failed':
                        logger.error(f"볼륨 크기 조정 실패: {response['VolumesModifications'][0].get('StatusMessage', '알 수 없는 오류')}")
                        return False
                    
                    time.sleep(2)
                    
                return True
            
            # 볼륨 크기를 줄이거나 유형을 변경하는 경우: 스냅샷 생성 후 새 볼륨 생성
            logger.info(f"볼륨 속성 변경을 위해 스냅샷 생성 후 새 볼륨 생성 (크기: {current_size}GB->{target_size}GB, 유형: {current_type}->{target_type})")
            
            # 1. 스냅샷 생성
            snapshot_description = f"Snapshot for modifying volume {volume_id} (size: {current_size}GB->{target_size}GB, type: {current_type}->{target_type})"
            snapshot_tags = {
                'Name': f"Modify-{volume_id}",
                'Purpose': 'Volume-Modification',
                'OriginalVolumeId': volume_id,
                'AutoCreated': 'true',
                'Source': 'EBS-Optimizer'
            }
            
            # 원본 볼륨의 태그 복사
            if 'Tags' in original_volume:
                for tag in original_volume['Tags']:
                    if tag['Key'] != 'Name' and tag['Key'] not in snapshot_tags:
                        snapshot_tags[tag['Key']] = tag['Value']
            
            snapshot_id = self._create_snapshot(volume_id, snapshot_description, snapshot_tags)
            if not snapshot_id:
                logger.error(f"볼륨 {volume_id}의 스냅샷 생성 실패")
                return False
                
            logger.info(f"볼륨 {volume_id}의 스냅샷 {snapshot_id} 생성 완료")
            
            # 스냅샷 완료 대기
            if not self._wait_for_snapshot_completion(snapshot_id):
                logger.error(f"스냅샷 {snapshot_id}이 제한 시간 내에 완료되지 않았습니다.")
                return False
            
            # 2. 새 볼륨 생성
            # 원본 볼륨의 속성 가져오기
            az = original_volume.get('AvailabilityZone')
            encrypted = original_volume.get('Encrypted', False)
            
            # 원본 볼륨의 태그 복사를 위한 준비
            volume_tags = []
            if 'Tags' in original_volume:
                for tag in original_volume['Tags']:
                    # AWS 시스템 태그는 제외
                    if not tag['Key'].startswith('aws:'):
                        volume_tags.append({
                            'Key': tag['Key'],
                            'Value': tag['Value']
                        })
            
            # 새 볼륨용 태그 추가
            volume_tags.append({'Key': 'Name', 'Value': f"Modified-{volume_id}"})
            volume_tags.append({'Key': 'OriginalVolumeId', 'Value': volume_id})
            volume_tags.append({'Key': 'ModificationTime', 'Value': datetime.now().isoformat()})
            volume_tags.append({'Key': 'PreviousType', 'Value': current_type})
            volume_tags.append({'Key': 'NewType', 'Value': target_type})
            
            # 볼륨 유형에 따른 추가 파라미터 설정
            create_args = {
                'AvailabilityZone': az,
                'SnapshotId': snapshot_id,
                'VolumeType': target_type,
                'Size': target_size,
                'Encrypted': encrypted,
                'TagSpecifications': [
                    {
                        'ResourceType': 'volume',
                        'Tags': volume_tags
                    }
                ]
            }
            
            # IOPS가 필요한 볼륨 유형인 경우 설정
            if target_type in ['io1', 'io2', 'gp3']:
                # 원본에 IOPS 설정이 있으면 사용, 없으면 기본값
                if target_type == 'gp3':
                    iops = original_volume.get('Iops', 3000)  # gp3의 경우 기본값 3000
                    create_args['Iops'] = iops
                    
                    # Throughput은 gp3에만 있음
                    if 'Throughput' in original_volume:
                        create_args['Throughput'] = original_volume['Throughput']
                    else:
                        create_args['Throughput'] = 125  # gp3의 기본 값
                else:  # io1, io2
                    # 최소 IOPS 값 설정
                    min_iops = 100
                    iops = max(min_iops, original_volume.get('Iops', min_iops))
                    create_args['Iops'] = iops
            
            # 새 볼륨 생성
            new_volume_id = None
            try:
                response = self.ec2_client.create_volume(**create_args)
                new_volume_id = response['VolumeId']
                logger.info(f"새 볼륨 {new_volume_id} 생성 완료 (요청 크기: {target_size}GB, 유형: {target_type})")
                
                # 잠시 대기 후 생성된 볼륨의 실제 크기 확인
                if not self._wait_for_volume_available(new_volume_id):
                    logger.error(f"새 볼륨 {new_volume_id}이 제한 시간 내에 사용 가능 상태가 되지 않았습니다.")
                    return False
                    
                # 생성된 볼륨의 실제 크기 확인
                new_volume_info = self._get_volume_info(new_volume_id)
                actual_size = new_volume_info.get('Size') if new_volume_info else None
                
                if (actual_size and actual_size != target_size):
                    logger.warning(f"주의: 새 볼륨이 요청한 크기({target_size}GB)가 아닌 {actual_size}GB로 생성되었습니다. "
                                  f"이는 AWS EBS가 스냅샷 크기보다 작은 볼륨 생성을 허용하지 않기 때문입니다.")
            except Exception as e:
                logger.error(f"새 볼륨 생성 중 오류 발생: {str(e)}")
                return False
            
            # 3. 볼륨 연결 여부 확인 및 처리
            attachments = original_volume.get('Attachments', [])
            if attachments:
                for attachment in attachments:
                    instance_id = attachment.get('InstanceId')
                    device_name = attachment.get('Device')
                    
                    if not instance_id or not device_name:
                        continue
                    
                    # 루트 볼륨인지 확인
                    is_root = self._is_root_volume(instance_id, device_name)
                    
                    logger.info(f"볼륨 {volume_id}는 인스턴스 {instance_id}에 {device_name}으로 연결됨. 루트 볼륨: {is_root}")
                    
                    # 루트 볼륨인 경우 특별 처리 필요
                    if is_root:
                        logger.warning(f"볼륨 {volume_id}는 인스턴스 {instance_id}의 루트 볼륨입니다. 루트 볼륨 변경은 지원하지 않습니다.")
                        # 새 볼륨 삭제
                        self.ec2_client.delete_volume(VolumeId=new_volume_id)
                        return False
                    
                    # 기존 볼륨 분리
                    logger.info(f"볼륨 {volume_id}를 인스턴스 {instance_id}에서 분리합니다.")
                    self.ec2_client.detach_volume(
                        VolumeId=volume_id,
                        InstanceId=instance_id,
                        Device=device_name,
                        Force=True  # 강제 분리 사용
                    )
                    
                    # 볼륨 분리 대기
                    if not self._wait_for_volume_detachment(volume_id):
                        logger.error(f"볼륨 {volume_id} 분리 대기 중 타임아웃")
                        return False
                    
                    # 새 볼륨 연결
                    logger.info(f"새 볼륨 {new_volume_id}를 인스턴스 {instance_id}의 {device_name}에 연결합니다.")
                    try:
                        self.ec2_client.attach_volume(
                            VolumeId=new_volume_id,
                            InstanceId=instance_id,
                            Device=device_name
                        )
                        
                        # 볼륨 연결 대기
                        if not self._wait_for_volume_attachment(new_volume_id, instance_id):
                            logger.error(f"새 볼륨 {new_volume_id} 연결 대기 중 타임아웃")
                            return False
                            
                        logger.info(f"새 볼륨 {new_volume_id}를 인스턴스 {instance_id}에 연결 완료")
                    except Exception as e:
                        logger.error(f"새 볼륨 연결 중 오류 발생: {str(e)}")
                        return False
            
            # 4. 원본 볼륨 삭제 (선택적)
            try:
                # attachments가 있었다면 이미 분리되었음
                logger.info(f"원본 볼륨 {volume_id}를 삭제합니다.")
                self.ec2_client.delete_volume(VolumeId=volume_id)
                logger.info(f"원본 볼륨 {volume_id} 삭제 완료")
                
                # 성공 결과에 실제 크기 정보 추가
                if actual_size and actual_size != target_size:
                    return {
                        'success': True,
                        'warning': f"요청한 크기 {target_size}GB가 아닌, AWS 제한으로 인해 {actual_size}GB 볼륨이 생성되었습니다.",
                        'requested_size': target_size,
                        'actual_size': actual_size
                    }
                
                return True
                
            except Exception as e:
                logger.warning(f"원본 볼륨 {volume_id} 삭제 중 오류 발생: {str(e)}")
                # 원본 볼륨 삭제 실패는 전체 프로세스 실패로 간주하지 않음
            
            return True
            
        except Exception as e:
            logger.error(f"볼륨 속성 변경 중 오류 발생: {str(e)}", exc_info=True)
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
