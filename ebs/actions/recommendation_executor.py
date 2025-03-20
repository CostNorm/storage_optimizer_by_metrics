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
                
                if current_type == target_type:
                    result['details']['message'] = f"볼륨 유형 {current_type}에서 변경이 필요하지 않습니다."
                    result['success'] = True
                    return result
                
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
        
        :param volume_id: 볼륨 ID
        :param target_size: 조정할 크기(GB)
        :return: 성공 여부
        """
        try:
            logger.info(f"볼륨 {volume_id}의 크기를 {target_size}GB로 조정합니다.")
            
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
            
        except Exception as e:
            logger.error(f"볼륨 크기 조정 중 오류 발생: {str(e)}")
            return False
    
    def _change_volume_type(self, volume_id, target_type):
        """
        EBS 볼륨의 유형을 변경합니다.
        
        :param volume_id: 볼륨 ID
        :param target_type: 변경할 볼륨 유형(gp2, gp3, io1, io2 등)
        :return: 성공 여부
        """
        try:
            logger.info(f"볼륨 {volume_id}의 유형을 {target_type}로 변경합니다.")
            
            kwargs = {
                'VolumeId': volume_id,
                'VolumeType': target_type
            }
            
            # gp3, io1, io2의 경우 추가 파라미터 설정
            if target_type == 'gp3':
                kwargs['Iops'] = 3000  # gp3의 기본 IOPS
                
            elif target_type in ['io1', 'io2']:
                # io1/io2는 최소 100 IOPS 필요
                kwargs['Iops'] = 100
            
            response = self.ec2_client.modify_volume(**kwargs)
            
            # 볼륨 유형 변경 상태 확인
            modification_state = response['VolumeModification']['ModificationState']
            logger.info(f"볼륨 유형 변경 요청 상태: {modification_state}")
            
            # 볼륨 수정이 완료될 때까지 대기 (최대 60초)
            for _ in range(30):
                response = self.ec2_client.describe_volumes_modifications(
                    VolumeIds=[volume_id]
                )
                
                if not response['VolumesModifications']:
                    logger.warning(f"볼륨 {volume_id}의 수정 정보를 찾을 수 없습니다.")
                    break
                
                current_state = response['VolumesModifications'][0]['ModificationState']
                logger.info(f"볼륨 유형 변경 진행 상태: {current_state}")
                
                if current_state == 'completed' or current_state == 'optimizing':
                    return True
                
                if current_state == 'failed':
                    logger.error(f"볼륨 유형 변경 실패: {response['VolumesModifications'][0].get('StatusMessage', '알 수 없는 오류')}")
                    return False
                
                time.sleep(2)  # 2초 대기 후 다시 확인
                
            return True
            
        except Exception as e:
            logger.error(f"볼륨 유형 변경 중 오류 발생: {str(e)}")
            return False
    
    def _modify_volume(self, volume_id, target_type, target_size):
        """
        EBS 볼륨의 유형과 크기를 동시에 변경합니다.
        
        :param volume_id: 볼륨 ID
        :param target_type: 변경할 볼륨 유형(gp2, gp3, io1, io2 등)
        :param target_size: 조정할 크기(GB)
        :return: 성공 여부
        """
        try:
            logger.info(f"볼륨 {volume_id}의 유형을 {target_type}로, 크기를 {target_size}GB로 변경합니다.")
            
            kwargs = {
                'VolumeId': volume_id,
                'VolumeType': target_type,
                'Size': target_size
            }
            
            # gp3, io1, io2의 경우 추가 파라미터 설정
            if target_type == 'gp3':
                kwargs['Iops'] = 3000  # gp3의 기본 IOPS
                
            elif target_type in ['io1', 'io2']:
                # io1/io2는 최소 100 IOPS 필요
                kwargs['Iops'] = 100
            
            response = self.ec2_client.modify_volume(**kwargs)
            
            # 볼륨 수정 상태 확인
            modification_state = response['VolumeModification']['ModificationState']
            logger.info(f"볼륨 속성 변경 요청 상태: {modification_state}")
            
            # 볼륨 수정이 완료될 때까지 대기 (최대 60초)
            for _ in range(30):
                response = self.ec2_client.describe_volumes_modifications(
                    VolumeIds=[volume_id]
                )
                
                if not response['VolumesModifications']:
                    logger.warning(f"볼륨 {volume_id}의 수정 정보를 찾을 수 없습니다.")
                    break
                
                current_state = response['VolumesModifications'][0]['ModificationState']
                logger.info(f"볼륨 속성 변경 진행 상태: {current_state}")
                
                if current_state == 'completed' or current_state == 'optimizing':
                    return True
                
                if current_state == 'failed':
                    logger.error(f"볼륨 속성 변경 실패: {response['VolumesModifications'][0].get('StatusMessage', '알 수 없는 오류')}")
                    return False
                
                time.sleep(2)  # 2초 대기 후 다시 확인
                
            return True
            
        except Exception as e:
            logger.error(f"볼륨 속성 변경 중 오류 발생: {str(e)}")
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
