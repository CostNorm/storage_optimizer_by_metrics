import json
import logging
import requests
from datetime import datetime

logger = logging.getLogger()

def send_slack_blocks(webhook_url, blocks, text="EBS 볼륨 최적화 시스템 알림"):
    """
    Slack Block Kit 형식의 메시지를 전송합니다.
    
    :param webhook_url: Slack 웹훅 URL
    :param blocks: Block Kit 블록 리스트
    :param text: 대체 텍스트
    :return: 응답 정보
    """
    if not webhook_url:
        logger.warning("Slack 웹훅 URL이 설정되지 않았습니다. Slack 알림을 건너뜁니다.")
        return {"skipped": True, "reason": "No webhook URL configured"}
    
    try:
        # 메시지 전송
        payload = {
            "blocks": blocks,
            "text": text
        }
        
        response = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json"}
        )
        
        if response.status_code != 200:
            logger.error(f"Slack으로 메시지 전송 실패: {response.status_code} {response.text}")
            return {"success": False, "status_code": response.status_code, "response": response.text}
        
        return {"success": True}
    
    except Exception as e:
        logger.error(f"Slack 알림 전송 중 오류 발생: {str(e)}", exc_info=True)
        return {"success": False, "error": str(e)}

def send_slack_error(webhook_url, error_message):
    """
    오류 메시지를 Slack으로 전송합니다.
    
    :param webhook_url: Slack 웹훅 URL
    :param error_message: 오류 메시지
    :return: 응답 정보
    """
    if not webhook_url:
        return {"skipped": True, "reason": "No webhook URL configured"}
    
    try:
        # 오류 메시지용 Block Kit 생성
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "⚠️ EBS 볼륨 최적화 분석 오류",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*오류 발생 시간:* {datetime.now().isoformat()}"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*오류 메시지:*\n```{error_message}```"
                }
            }
        ]
        
        return send_slack_blocks(webhook_url, blocks, f"EBS 볼륨 최적화 분석 중 오류 발생: {error_message[:50]}...")
    
    except Exception as e:
        logger.error(f"오류 메시지 Slack 전송 중 실패: {str(e)}")
        return {"success": False, "error": str(e)}

def send_analysis_result_to_slack(result, channel_id, bot_token):
    """
    분석 결과를 Slack 채널로 전송합니다.
    
    :param result: 분석 결과
    :param channel_id: Slack 채널 ID
    :param bot_token: Slack Bot 토큰
    :return: 전송 성공 여부
    """
    if not bot_token:
        logger.warning("SLACK_BOT_TOKEN이 설정되지 않았습니다. Slack 메시지를 전송할 수 없습니다.")
        return False
    
    try:
        # 메시지 구성
        volume_id = result.get('volume_id', 'unknown')
        is_idle = result.get('is_idle', False)
        is_overprovisioned = result.get('is_overprovisioned', False)
        recommendation = result.get('recommendation', '해당 없음')
        
        status_text = []
        if is_idle:
            status_text.append("유휴 상태")
        if is_overprovisioned:
            status_text.append("과대 프로비저닝")
        
        status = ", ".join(status_text) if status_text else "최적 상태"
        
        # Block Kit 메시지 구성
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"볼륨 {volume_id} 분석 결과",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*볼륨 ID:*\n{volume_id}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*상태:*\n{status}"
                    }
                ]
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*권장 조치:*\n{recommendation}"
                }
            }
        ]
        
        # 상태에 따른 액션 버튼 추가
        actions = []
        
        if is_idle:
            actions.append({
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "스냅샷 생성 후 삭제",
                    "emoji": True
                },
                "style": "danger",
                "value": json.dumps({
                    "volume_id": volume_id,
                    "region": result.get('region'),
                    "action_type": "snapshot_and_delete"
                }),
                "action_id": "execute_snapshot_and_delete"  # Changed from execute_idle_volume_action to be unique
            })
            
            actions.append({
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "스냅샷만 생성",
                    "emoji": True
                },
                "value": json.dumps({
                    "volume_id": volume_id,
                    "region": result.get('region'),
                    "action_type": "snapshot_only"
                }),
                "action_id": "execute_snapshot_only"  # Changed from execute_idle_volume_action to be unique
            })
        
        if is_overprovisioned:
            actions.append({
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "볼륨 크기 조정",
                    "emoji": True
                },
                "value": json.dumps({
                    "volume_id": volume_id,
                    "region": result.get('region'),
                    "action_type": "resize"
                }),
                "action_id": "execute_resize"  # Changed from execute_overprovisioned_volume_action for consistency
            })
        
        # 액션 버튼이 있는 경우 추가
        if actions:
            blocks.append({
                "type": "actions",
                "elements": actions
            })
        
        # Slack API로 메시지 전송
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json"
            },
            json={
                "channel": channel_id,
                "blocks": blocks,
                "text": f"볼륨 {volume_id} 분석 결과: {status}"
            }
        )
        
        if response.status_code != 200 or not response.json().get('ok', False):
            logger.error(f"Slack 메시지 전송 실패: {response.status_code} {response.text}")
            return False
        
        return True
    
    except Exception as e:
        logger.error(f"Slack 메시지 전송 중 오류 발생: {str(e)}", exc_info=True)
        return False

def send_execution_result_to_slack(result, volume_id, action_type, channel_id, requested_by, bot_token):
    """
    실행 결과를 Slack 채널로 전송합니다.
    """
    # 기존 코드 로직을 이곳으로 이동
    if not bot_token:
        logger.warning("SLACK_BOT_TOKEN이 설정되지 않았습니다. Slack 메시지를 전송할 수 없습니다.")
        return False
    
    try:
        success = result.get('success', False)
        details = result.get('details', {})
        
        # 성공 여부에 따른 아이콘 선택
        icon = ":white_check_mark:" if success else ":x:"
        
        # 액션 타입 이름 형식화
        action_name = action_type.replace('_', ' ').title()
        
        # Block Kit 메시지 구성
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{icon} 볼륨 {volume_id} {action_name} 결과",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*볼륨 ID:*\n{volume_id}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*액션:*\n{action_name}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*요청자:*\n<@{requested_by}>"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*결과:*\n{'성공' if success else '실패'}"
                    }
                ]
            }
        ]
        
        # 세부 정보 추가 로직...
        
        # Slack API로 메시지 전송
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json"
            },
            json={
                "channel": channel_id,
                "blocks": blocks,
                "text": f"볼륨 {volume_id}에 대한 {action_name} 액션이 {('성공적으로 완료' if success else '실패')}되었습니다."
            }
        )
        
        if response.status_code != 200 or not response.json().get('ok', False):
            logger.error(f"Slack 메시지 전송 실패: {response.status_code} {response.text}")
            return False
        
        return True
    
    except Exception as e:
        logger.error(f"Slack 메시지 전송 중 오류 발생: {str(e)}", exc_info=True)
        return False

def send_slack_message(channel_id, message, bot_token):
    """
    간단한 텍스트 메시지를 Slack 채널로 전송합니다.
    
    :param channel_id: Slack 채널 ID
    :param message: 전송할 메시지
    :param bot_token: Slack Bot 토큰
    :return: 전송 성공 여부
    """
    if not bot_token:
        logger.warning("SLACK_BOT_TOKEN이 설정되지 않았습니다. Slack 메시지를 전송할 수 없습니다.")
        return False
    
    try:
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json"
            },
            json={
                "channel": channel_id,
                "text": message
            }
        )
        
        if response.status_code != 200 or not response.json().get('ok', False):
            logger.error(f"Slack 메시지 전송 실패: {response.status_code} {response.text}")
            return False
        
        return True
    
    except Exception as e:
        logger.error(f"Slack 메시지 전송 중 오류 발생: {str(e)}", exc_info=True)
        return False
