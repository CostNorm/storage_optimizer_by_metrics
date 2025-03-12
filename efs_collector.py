import boto3
import json
import datetime
from botocore.exceptions import ClientError

def collect_efs_metrics():
    """EFS 파일 시스템의 메트릭 수집"""
    efs_client = boto3.client('efs')
    cloudwatch = boto3.client('cloudwatch')
    metrics_data = []
    
    try:
        # 모든 EFS 파일 시스템 가져오기
        file_systems = efs_client.describe_file_systems()
        
        for fs in file_systems['FileSystems']:
            fs_id = fs['FileSystemId']
            fs_metrics = {
                'file_system_id': fs_id,
                'creation_time': fs['CreationTime'].isoformat(),
                'file_system_size': fs['SizeInBytes']['Value'],
                'performance_mode': fs['PerformanceMode'],
                'throughput_mode': fs['ThroughputMode'],
                'metrics': {}
            }
            
            # 스토리지 클래스별 사용량 수집
            storage_classes = ['Standard', 'IA']
            
            for storage_class in storage_classes:
                try:
                    response = cloudwatch.get_metric_statistics(
                        Namespace='AWS/EFS',
                        MetricName='StorageBytes',
                        Dimensions=[
                            {'Name': 'FileSystemId', 'Value': fs_id},
                            {'Name': 'StorageClass', 'Value': storage_class}
                        ],
                        StartTime=datetime.datetime.now() - datetime.timedelta(days=1),
                        EndTime=datetime.datetime.now(),
                        Period=86400,  # 1일
                        Statistics=['Average']
                    )
                    
                    if response['Datapoints']:
                        fs_metrics['metrics'][f'{storage_class}_StorageBytes'] = response['Datapoints'][0]['Average']
                    
                except ClientError as e:
                    fs_metrics['metrics'][f'{storage_class}_StorageBytes_error'] = str(e)
            
            # 성능 및 처리량 메트릭 수집
            efs_metrics = ['TotalIOBytes', 'DataReadIOBytes', 'DataWriteIOBytes', 'MetadataIOBytes',
                          'ClientConnections', 'PercentIOLimit', 'BurstCreditBalance', 'PermittedThroughput']
            
            for metric_name in efs_metrics:
                try:
                    response = cloudwatch.get_metric_statistics(
                        Namespace='AWS/EFS',
                        MetricName=metric_name,
                        Dimensions=[
                            {'Name': 'FileSystemId', 'Value': fs_id}
                        ],
                        StartTime=datetime.datetime.now() - datetime.timedelta(hours=24),
                        EndTime=datetime.datetime.now(),
                        Period=3600,  # 1시간
                        Statistics=['Average']
                    )
                    
                    if response['Datapoints']:
                        # 데이터 포인트를 시간순으로 정렬
                        datapoints = sorted(response['Datapoints'], key=lambda x: x['Timestamp'])
                        fs_metrics['metrics'][metric_name] = {
                            'latest': datapoints[-1]['Average'],
                            'average_24h': sum(d['Average'] for d in datapoints) / len(datapoints) if datapoints else 0
                        }
                    else:
                        fs_metrics['metrics'][metric_name] = {'latest': None, 'average_24h': None}
                        
                except ClientError as e:
                    fs_metrics['metrics'][f'{metric_name}_error'] = str(e)
            
            metrics_data.append(fs_metrics)
            
    except Exception as e:
        metrics_data.append({'error': str(e)})
    
    return metrics_data