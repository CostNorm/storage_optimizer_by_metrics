import boto3
import logging
import argparse
import time
import json
from datetime import datetime

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()

def test_volume_type_change(volume_id, target_type, region='ap-northeast-2'):
    """
    볼륨 타입 변경 기능을 테스트합니다.
    
    :param volume_id: 테스트할 볼륨 ID
    :param target_type: 변경할 볼륨 타입 (gp2, gp3, io1, io2 등)
    :param region: AWS 리전
    """
    ec2_client = boto3.client('ec2', region_name=region)
    
    try:
        # 1. 현재 볼륨 정보 확인
        logger.info(f"볼륨 {volume_id} 정보 조회 중...")
        try:
            response = ec2_client.describe_volumes(VolumeIds=[volume_id])
            volume = response['Volumes'][0]
            
            current_type = volume['VolumeType']
            size = volume['Size']
            state = volume['State']
            
            logger.info(f"볼륨 정보: ID={volume_id}, 유형={current_type}, 크기={size}GB, 상태={state}")
            
            # 기존 볼륨이 이미 요청한 타입인 경우 종료
            if current_type == target_type:
                logger.info(f"볼륨이 이미 요청한 타입({target_type})입니다. 다른 타입으로 변경해보세요.")
                return
                
        except Exception as e:
            logger.error(f"볼륨 정보 조회 실패: {str(e)}")
            return
            
        # 2. 볼륨 타입 변경을 위한 파라미터 설정
        modify_args = {
            'VolumeId': volume_id,
            'VolumeType': target_type
        }
        
        # 볼륨 타입에 따라 추가 파라미터 설정
        if target_type == 'gp3':
            # gp3의 기본값 설정
            modify_args['Iops'] = 3000
            modify_args['Throughput'] = 125
            logger.info("gp3 볼륨을 위한 기본값 설정: IOPS=3000, Throughput=125MB/s")
            
        elif target_type in ['io1', 'io2']:
            # 현재 IOPS가 있으면 유지, 없으면 최소값 설정
            if 'Iops' in volume:
                modify_args['Iops'] = max(100, volume['Iops'])
            else:
                modify_args['Iops'] = 100
            logger.info(f"{target_type} 볼륨을 위한 IOPS 값 설정: {modify_args['Iops']}")
        
        # 3. 볼륨 타입 변경 요청
        logger.info(f"볼륨 타입 변경 요청: {current_type} -> {target_type}, 파라미터: {modify_args}")
        
        try:
            response = ec2_client.modify_volume(**modify_args)
            logger.info(f"볼륨 타입 변경 요청 응답: {json.dumps(response, default=str)}")
            
            # 변경 상태 추적
            modification = response['VolumeModification']
            modification_id = modification['ModificationId']
            start_state = modification['ModificationState']
            
            logger.info(f"볼륨 타입 변경 ID: {modification_id}, 초기 상태: {start_state}")
            
        except Exception as e:
            logger.error(f"볼륨 타입 변경 요청 실패: {str(e)}")
            return
            
        # 4. 볼륨 타입 변경 상태 모니터링
        logger.info("볼륨 타입 변경 상태 모니터링 중...")
        
        for i in range(30):  # 최대 5분 동안 모니터링 (10초 간격)
            try:
                response = ec2_client.describe_volumes_modifications(VolumeIds=[volume_id])
                
                if not response['VolumesModifications']:
                    logger.warning("볼륨 수정 정보를 찾을 수 없습니다.")
                    break
                    
                modification = response['VolumesModifications'][0]
                state = modification['ModificationState']
                
                logger.info(f"[{i+1}/30] 볼륨 타입 변경 상태: {state}")
                
                # 상태에 따른 처리
                if state == 'completed':
                    logger.info("볼륨 타입 변경이 완료되었습니다!")
                    break
                    
                elif state == 'optimizing':
                    logger.info("볼륨 타입 변경이 최적화 중입니다. 곧 완료됩니다.")
                    
                elif state == 'failed':
                    error_message = modification.get('StatusMessage', '알 수 없는 오류')
                    logger.error(f"볼륨 타입 변경이 실패했습니다: {error_message}")
                    break
                    
                # 10초 대기 후 다시 확인
                time.sleep(10)
                
            except Exception as e:
                logger.error(f"볼륨 수정 상태 확인 중 오류 발생: {str(e)}")
                time.sleep(10)
        
        # 5. 최종 볼륨 상태 확인
        try:
            response = ec2_client.describe_volumes(VolumeIds=[volume_id])
            volume = response['Volumes'][0]
            
            final_type = volume['VolumeType']
            
            logger.info(f"최종 볼륨 타입: {final_type}")
            
            if final_type == target_type:
                logger.info(f"성공! 볼륨 타입이 {current_type}에서 {target_type}으로 변경되었습니다.")
            else:
                logger.warning(f"볼륨 타입 변경이 반영되지 않았습니다. 현재 타입: {final_type}")
                
        except Exception as e:
            logger.error(f"최종 볼륨 상태 확인 중 오류 발생: {str(e)}")
    
    except Exception as e:
        logger.error(f"테스트 실행 중 예상치 못한 오류 발생: {str(e)}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EBS 볼륨 타입 변경 테스트")
    parser.add_argument("volume_id", help="테스트할 EBS 볼륨 ID")
    parser.add_argument("target_type", help="변경할 볼륨 타입 (gp2, gp3, io1, io2 등)")
    parser.add_argument("--region", default="ap-northeast-2", help="AWS 리전 (기본값: ap-northeast-2)")
    
    args = parser.parse_args()
    
    test_volume_type_change(args.volume_id, args.target_type, args.region)
