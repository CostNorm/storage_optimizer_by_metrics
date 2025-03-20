import boto3
import json
import datetime
from botocore.exceptions import ClientError
from s3_collector import collect_s3_metrics
from ebs_collector import collect_ebs_metrics
from efs_collector import collect_efs_metrics
from rds_collector import collect_rds_metrics
from cloudwatch_logs_collector import collect_logs_metrics

def lambda_handler(event, context):
    """
    AWS 스토리지 관련 모든 메트릭을 수집하여 S3에 저장하는 Lambda 함수
    """
    # 결과를 저장할 딕셔너리
    metrics_data = {
        'collected_at': datetime.datetime.now().isoformat(),
        's3_metrics': collect_s3_metrics(),
        'ebs_metrics': collect_ebs_metrics(),
        #'efs_metrics': collect_efs_metrics(),
        #'rds_metrics': collect_rds_metrics(),
        'cloudwatch_logs_metrics': collect_logs_metrics()
    }

    print(metrics_data)
    
    # S3에 저장
    #store_to_s3(metrics_data)
    
    return {
        'statusCode': 200,
        'body': 'Metrics collection completed successfully'
    }

