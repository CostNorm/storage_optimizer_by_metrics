import boto3
import time
import logging
import subprocess
import os
from datetime import datetime

logger = logging.getLogger()
logger.setLevel(logging.INFO)

class VolumeDataMigrator:
    """
    EBS 볼륨의 데이터를 더 작은 크기의 볼륨으로 마이그레이션
    """
    
    def __init__(self, region):
        self.region = region
        self.ec2_client = boto3.client('ec2', region_name=region)
        self.ec2_resource = boto3.resource('ec2', region_name=region)
        
    def migrate_volume_data(self, source_volume_id, target_size_gb, helper_instance_id=None):
        """
        소스 볼륨의 데이터를 더 작은 타겟 볼륨으로 마이그레이션합니다.
        
        :param source_volume_id: 소스 볼륨 ID
        :param target_size_gb: 타겟 볼륨 크기(GB)
        :param helper_instance_id: 마이그레이션에 사용할 헬퍼 인스턴스 ID (없으면 임시 인스턴스 생성)
        :return: 마이그레이션 결과 정보
        """
        logger.info(f"볼륨 데이터 마이그레이션 시작: {source_volume_id} -> 크기 {target_size_gb}GB")
        
        # 소스 볼륨 정보 가져오기
        source_volume = self._get_volume_info(source_volume_id)
        if not source_volume:
            return {
                'success': False,
                'error': f"소스 볼륨 {source_volume_id} 정보를 가져올 수 없습니다."
            }
        
        source_size = source_volume.get('Size')
        if source_size <= target_size_gb:
            return {
                'success': False,
                'error': f"타겟 크기({target_size_gb}GB)가 소스 볼륨 크기({source_size}GB)보다 크거나 같습니다."
            }
        
        # 임시 헬퍼 인스턴스 생성 필요 여부 확인
        terminate_instance = False
        if not helper_instance_id:
            helper_instance_id = self._launch_helper_instance(source_volume.get('AvailabilityZone'))
            if not helper_instance_id:
                return {'success': False, 'error': '데이터 마이그레이션 헬퍼 인스턴스 생성 실패'}
            terminate_instance = True
        
        try:
            # 1. 소스 볼륨 연결
            source_device = '/dev/xvdf'
            if not self._attach_volume(source_volume_id, helper_instance_id, source_device):
                raise Exception(f"소스 볼륨 {source_volume_id} 연결 실패")
            
            # 2. 타겟 볼륨 생성
            target_volume_id = self._create_empty_volume(
                target_size_gb, 
                source_volume.get('AvailabilityZone'),
                source_volume.get('VolumeType', 'gp3'),
                source_volume.get('Iops'),
                source_volume.get('Throughput'),
                source_volume.get('Encrypted', False),
                f"Migrated-{source_volume_id}"
            )
            
            if not target_volume_id:
                raise Exception("타겟 볼륨 생성 실패")
            
            # 3. 타겟 볼륨 연결
            target_device = '/dev/xvdg'
            if not self._attach_volume(target_volume_id, helper_instance_id, target_device):
                raise Exception(f"타겟 볼륨 {target_volume_id} 연결 실패")
            
            # 4. 데이터 마이그레이션 스크립트 실행
            migration_script = self._generate_migration_script(source_device, target_device)
            migration_result = self._run_script_on_instance(helper_instance_id, migration_script)
            
            if not migration_result.get('success'):
                raise Exception(f"데이터 마이그레이션 스크립트 실패: {migration_result.get('error')}")
            
            # 5. 볼륨 분리
            self._detach_volume(source_volume_id)
            self._detach_volume(target_volume_id)
            
            # 6. 볼륨 태그 복사
            self._copy_tags(source_volume_id, target_volume_id)
            
            # 7. 결과 반환
            result = {
                'success': True,
                'source_volume_id': source_volume_id,
                'target_volume_id': target_volume_id,
                'original_size': source_size,
                'new_size': target_size_gb,
                'migration_log': migration_result.get('output', '')
            }
            
            return result
            
        except Exception as e:
            logger.error(f"볼륨 데이터 마이그레이션 중 오류 발생: {str(e)}", exc_info=True)
            return {'success': False, 'error': str(e)}
            
        finally:
            # 마지막에 임시 인스턴스 종료 (필요한 경우)
            if terminate_instance and helper_instance_id:
                try:
                    self.ec2_client.terminate_instances(InstanceIds=[helper_instance_id])
                    logger.info(f"헬퍼 인스턴스 {helper_instance_id} 종료")
                except Exception as e:
                    logger.error(f"헬퍼 인스턴스 종료 중 오류: {str(e)}")

    # 인스턴스 생성, 볼륨 연결 등의 헬퍼 메서드는 여기에 구현...
    
    def _generate_migration_script(self, source_device, target_device):
        """
        데이터 마이그레이션을 위한 셸 스크립트 생성
        """
        return f"""#!/bin/bash
set -e

# 디바이스 확인
if [ ! -b {source_device} ] || [ ! -b {target_device} ]; then
    echo "디바이스를 찾을 수 없습니다: {source_device} 또는 {target_device}"
    exit 1
fi

# 파일시스템 타입 확인
FS_TYPE=$(blkid -o value -s TYPE {source_device})
echo "파일시스템 타입: $FS_TYPE"

if [ "$FS_TYPE" == "xfs" ]; then
    # XFS 파일시스템 처리
    echo "XFS 파일시스템 감지됨"
    mkdir -p /mnt/source /mnt/target
    mount {source_device} /mnt/source
    mkfs.xfs {target_device}
    mount {target_device} /mnt/target
    xfsdump -J - /mnt/source | xfsrestore -J - /mnt/target
    umount /mnt/source
    umount /mnt/target
elif [ "$FS_TYPE" == "ext4" ] || [ "$FS_TYPE" == "ext3" ] || [ "$FS_TYPE" == "ext2" ]; then
    # EXT 파일시스템 처리
    echo "EXT 파일시스템 감지됨"
    mkdir -p /mnt/source /mnt/target
    mount {source_device} /mnt/source
    mkfs.ext4 {target_device}
    mount {target_device} /mnt/target
    rsync -aAXv /mnt/source/ /mnt/target/
    umount /mnt/source
    umount /mnt/target
else
    # 기타 파일시스템은 직접 데이터 복사
    echo "미지원 또는 인식할 수 없는 파일시스템. dd 명령으로 데이터 복사"
    dd if={source_device} of={target_device} bs=8M status=progress conv=sparse
fi

echo "데이터 마이그레이션 완료"
"""
