import boto3
import json
import datetime
from botocore.exceptions import ClientError

def collect_ebs_metrics():
    """EBS 볼륨의 메트릭 수집"""
    ec2_client = boto3.client('ec2')
    cloudwatch = boto3.client('cloudwatch')
    metrics_data = []
    
    try:
        # 모든 EBS 볼륨 가져오기
        volumes = ec2_client.describe_volumes()
        
        for volume in volumes['Volumes']:
            volume_id = volume['VolumeId']
            volume_metrics = {
                'volume_id': volume_id,
                'volume_type': volume['VolumeType'],
                'volume_size': volume['Size'],
                'create_time': volume['CreateTime'].isoformat(),
                'state': volume['State'],
                'availability_zone': volume['AvailabilityZone'],
                'metrics': {}
            }
            
            # 연결 상태 정보
            if 'Attachments' in volume and volume['Attachments']:
                attachment = volume['Attachments'][0]
                volume_metrics['attached_to'] = attachment['InstanceId']
                volume_metrics['attach_time'] = attachment['AttachTime'].isoformat()
            else:
                volume_metrics['attached_to'] = None
                volume_metrics['attach_time'] = None
            
            # EBS 메트릭 수집
            ebs_metrics = ['VolumeReadBytes', 'VolumeWriteBytes', 'VolumeReadOps', 'VolumeWriteOps',
                          'VolumeTotalReadTime', 'VolumeTotalWriteTime', 'VolumeIdleTime',
                          'VolumeQueueLength', 'BurstBalance', 'VolumeStatusCheckFailed']
            
            for metric_name in ebs_metrics:
                try:
                    response = cloudwatch.get_metric_statistics(
                        Namespace='AWS/EBS',
                        MetricName=metric_name,
                        Dimensions=[
                            {'Name': 'VolumeId', 'Value': volume_id}
                        ],
                        StartTime=datetime.datetime.now() - datetime.timedelta(days=1),
                        EndTime=datetime.datetime.now(),
                        Period=3600,  # 1시간
                        Statistics=['Average']
                    )
                    
                    if response['Datapoints']:
                        # 데이터 포인트를 시간순으로 정렬
                        datapoints = sorted(response['Datapoints'], key=lambda x: x['Timestamp'])
                        volume_metrics['metrics'][metric_name] = {
                            'latest': datapoints[-1]['Average'],
                            'average_24h': sum(d['Average'] for d in datapoints) / len(datapoints) if datapoints else 0
                        }
                    else:
                        volume_metrics['metrics'][metric_name] = {'latest': None, 'average_24h': None}
                        
                except ClientError as e:
                    volume_metrics['metrics'][f'{metric_name}_error'] = str(e)
            
            metrics_data.append(volume_metrics)
            
    except Exception as e:
        metrics_data.append({'error': str(e)})
    
    return metrics_data