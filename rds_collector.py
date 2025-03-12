import boto3
import json
import datetime
from botocore.exceptions import ClientError

def collect_rds_metrics():
    """RDS 인스턴스의 메트릭 수집"""
    rds_client = boto3.client('rds')
    cloudwatch = boto3.client('cloudwatch')
    metrics_data = []
    
    try:
        # 모든 RDS 인스턴스 가져오기
        instances = rds_client.describe_db_instances()
        
        for instance in instances['DBInstances']:
            instance_id = instance['DBInstanceIdentifier']
            instance_metrics = {
                'instance_id': instance_id,
                'instance_class': instance['DBInstanceClass'],
                'engine': instance['Engine'],
                'engine_version': instance['EngineVersion'],
                'allocated_storage': instance['AllocatedStorage'],
                'multi_az': instance['MultiAZ'],
                'creation_time': instance['InstanceCreateTime'].isoformat(),
                'status': instance['DBInstanceStatus'],
                'metrics': {}
            }
            
            # RDS 메트릭 수집 - 기본 메트릭만 포함
            rds_metrics = [
                # 사용률 메트릭
                'CPUUtilization', 'FreeableMemory', 'FreeStorageSpace',
                'DatabaseConnections', 'NetworkReceiveThroughput', 'NetworkTransmitThroughput',
                # I/O 메트릭
                'ReadIOPS', 'WriteIOPS', 'ReadLatency', 'WriteLatency', 
                'ReadThroughput', 'WriteThroughput'
            ]
            
            # 엔진별 특수 메트릭 추가
            if instance['Engine'].startswith('mysql') or instance['Engine'].startswith('mariadb'):
                rds_metrics.extend(['BinLogDiskUsage'])
            
            # 읽기 전용 복제본일 경우 추가 메트릭
            if 'ReadReplicaSourceDBInstanceIdentifier' in instance:
                rds_metrics.append('ReplicaLag')
            
            for metric_name in rds_metrics:
                try:
                    response = cloudwatch.get_metric_statistics(
                        Namespace='AWS/RDS',
                        MetricName=metric_name,
                        Dimensions=[
                            {'Name': 'DBInstanceIdentifier', 'Value': instance_id}
                        ],
                        StartTime=datetime.datetime.now() - datetime.timedelta(hours=24),
                        EndTime=datetime.datetime.now(),
                        Period=3600,  # 1시간
                        Statistics=['Average']
                    )
                    
                    if response['Datapoints']:
                        # 데이터 포인트를 시간순으로 정렬
                        datapoints = sorted(response['Datapoints'], key=lambda x: x['Timestamp'])
                        instance_metrics['metrics'][metric_name] = {
                            'latest': datapoints[-1]['Average'],
                            'average_24h': sum(d['Average'] for d in datapoints) / len(datapoints) if datapoints else 0
                        }
                    else:
                        instance_metrics['metrics'][metric_name] = {'latest': None, 'average_24h': None}
                        
                except ClientError as e:
                    instance_metrics['metrics'][f'{metric_name}_error'] = str(e)
            
            metrics_data.append(instance_metrics)
            
    except Exception as e:
        metrics_data.append({'error': str(e)})
    
    return metrics_data