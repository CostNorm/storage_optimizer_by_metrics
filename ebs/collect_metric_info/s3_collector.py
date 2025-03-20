import boto3
import json
import datetime
from botocore.exceptions import ClientError

def collect_s3_metrics():
    """S3 버킷의 모든 메트릭 수집"""
    s3_client = boto3.client('s3')
    cloudwatch = boto3.client('cloudwatch')
    metrics_data = []
    
    try:
        # 모든 S3 버킷 리스트 가져오기
        buckets = s3_client.list_buckets()
        
        for bucket in buckets['Buckets']:
            bucket_name = bucket['Name']
            bucket_metrics = {
                'bucket_name': bucket_name,
                'creation_date': bucket['CreationDate'].isoformat(),
                'metrics': {}
            }
            
            # 스토리지 클래스별 사용량 및 객체 수 가져오기
            storage_types = ['StandardStorage', 'IntelligentTieringFAStorage', 'IntelligentTieringIAStorage', 
                             'StandardIAStorage', 'OneZoneIAStorage', 'ReducedRedundancyStorage', 'GlacierStorage',
                             'DeepArchiveStorage', 'GlacierIRStorage']
            
            for storage_type in storage_types:
                try:
                    # 버킷 크기 메트릭
                    size_response = cloudwatch.get_metric_statistics(
                        Namespace='AWS/S3',
                        MetricName='BucketSizeBytes',
                        Dimensions=[
                            {'Name': 'BucketName', 'Value': bucket_name},
                            {'Name': 'StorageType', 'Value': storage_type}
                        ],
                        StartTime=datetime.datetime.now() - datetime.timedelta(days=1),
                        EndTime=datetime.datetime.now(),
                        Period=86400,  # 1일
                        Statistics=['Average']
                    )
                    
                    # 객체 수 메트릭
                    object_count_response = cloudwatch.get_metric_statistics(
                        Namespace='AWS/S3',
                        MetricName='NumberOfObjects',
                        Dimensions=[
                            {'Name': 'BucketName', 'Value': bucket_name},
                            {'Name': 'StorageType', 'Value': storage_type}
                        ],
                        StartTime=datetime.datetime.now() - datetime.timedelta(days=1),
                        EndTime=datetime.datetime.now(),
                        Period=86400,  # 1일
                        Statistics=['Average']
                    )
                    
                    # 메트릭 결과 저장
                    if size_response['Datapoints']:
                        bucket_metrics['metrics'][f'{storage_type}_Size'] = size_response['Datapoints'][0]['Average']
                    
                    if object_count_response['Datapoints']:
                        bucket_metrics['metrics'][f'{storage_type}_ObjectCount'] = object_count_response['Datapoints'][0]['Average']
                        
                except ClientError as e:
                    bucket_metrics['metrics'][f'{storage_type}_error'] = str(e)
            
            # 요청 관련 메트릭 수집
            request_metrics = ['AllRequests', 'GetRequests', 'PutRequests', 'DeleteRequests', 
                              'BytesDownloaded', 'BytesUploaded', '4xxErrors', '5xxErrors', 'FirstByteLatency']
            
            for metric_name in request_metrics:
                try:
                    response = cloudwatch.get_metric_statistics(
                        Namespace='AWS/S3',
                        MetricName=metric_name,
                        Dimensions=[
                            {'Name': 'BucketName', 'Value': bucket_name},
                            {'Name': 'FilterId', 'Value': 'EntireBucket'}
                        ],
                        StartTime=datetime.datetime.now() - datetime.timedelta(days=1),
                        EndTime=datetime.datetime.now(),
                        Period=86400,  # 1일
                        Statistics=['Sum'] if metric_name not in ['FirstByteLatency'] else ['Average']
                    )
                    
                    if response['Datapoints']:
                        stat_key = 'Sum' if metric_name not in ['FirstByteLatency'] else 'Average'
                        bucket_metrics['metrics'][metric_name] = response['Datapoints'][0][stat_key]
                        
                except ClientError as e:
                    bucket_metrics['metrics'][f'{metric_name}_error'] = str(e)
            
            # 객체의 최종 수정 시간 (Last Changed) - S3 API를 통한 추가 정보
            try:
                # 최신 객체 몇 개만 샘플링 (전체 분석은 시간이 많이 소요됨)
                response = s3_client.list_objects_v2(Bucket=bucket_name, MaxKeys=10)
                if 'Contents' in response:
                    newest_object = max(response['Contents'], key=lambda x: x['LastModified'])
                    oldest_object = min(response['Contents'], key=lambda x: x['LastModified'])
                    
                    bucket_metrics['newest_object_last_modified'] = newest_object['LastModified'].isoformat()
                    bucket_metrics['oldest_object_last_modified'] = oldest_object['LastModified'].isoformat()
            except ClientError as e:
                bucket_metrics['object_last_modified_error'] = str(e)
                
            metrics_data.append(bucket_metrics)
            
    except Exception as e:
        metrics_data.append({'error': str(e)})
    
    return metrics_data