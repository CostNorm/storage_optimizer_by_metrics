import logging
import json
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
                # 향후 확장: 볼륨 타입 변경 로직 구현
                result['details']['error'] = "볼륨 타입 변경 기능은 아직 구현되지 않았습니다."
            
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
        :param action_type: 실행할 조치 유형 ('resize', 'change_type_and_resize')
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
        
        logger.info(f"볼륨 {volume_id}에 대한 '{action_type}' 작업 실행 중...")
        
        # 현재는 구현되지 않음 - 향후 확장 예정
        result['details']['error'] = "과대 프로비저닝된 볼륨 최적화 기능은 아직 구현되지 않았습니다."
        
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
