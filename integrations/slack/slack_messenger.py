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

def send_analysis_result_to_slack(result, channel_id, bot_token, thread_ts=None, use_thread=True):
    """
    분석 결과를 Slack 채널로 전송합니다.
    
    :param result: 분석 결과
    :param channel_id: Slack 채널 ID
    :param bot_token: Slack Bot 토큰
    :param thread_ts: 스레드 타임스탬프 (스레드에 응답할 경우)
    :param use_thread: 결과를 스레드에 표시할지 여부
    :return: 전송 성공 여부와 thread_ts (스레드로 사용할 타임스탬프)
    """
    if not bot_token:
        logger.warning("SLACK_BOT_TOKEN이 설정되지 않았습니다. Slack 메시지를 전송할 수 없습니다.")
        return False, None
    
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
                    "action_type": "snapshot_and_delete",
                    "message_ts": "" # 이 값은 나중에 response.json().get('ts')로 채워질 것입니다
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
                    "action_type": "snapshot_only",
                    "message_ts": "" # 이 값은 나중에 response.json().get('ts')로 채워질 것입니다
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
                    "action_type": "resize",
                    "message_ts": "" # 이 값은 나중에 response.json().get('ts')로 채워질 것입니다
                }),
                "action_id": "execute_resize"  # Changed from execute_overprovisioned_volume_action for consistency
            })
        
        # 액션 버튼이 있는 경우 추가
        if actions:
            blocks.append({
                "type": "actions",
                "elements": actions
            })
        
        # 메시지 JSON 구성
        message_json = {
            "channel": channel_id,
            "blocks": blocks,
            "text": f"볼륨 {volume_id} 분석 결과: {status}"
        }
        
        # 스레드에 응답하는 경우
        if use_thread and thread_ts:
            message_json["thread_ts"] = thread_ts
        
        # Slack API로 메시지 전송
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json"
            },
            json=message_json
        )
        
        if response.status_code != 200 or not response.json().get('ok', False):
            logger.error(f"Slack 메시지 전송 실패: {response.status_code} {response.text}")
            return False, None
        
        # 메시지 타임스탬프 저장
        ts = response.json().get('ts')
        
        # 버튼 값에 메시지 타임스탬프 포함 - 이 부분이 중요합니다!
        try:
            if ts and len(blocks) > 0:
                for block in blocks:
                    if block.get('type') == 'actions':
                        for element in block.get('elements', []):
                            if element.get('type') == 'button' and element.get('action_id', '').startswith('execute_'):
                                # 버튼 값에서 메시지 타임스탬프 업데이트
                                button_value = json.loads(element.get('value', '{}'))
                                button_value['message_ts'] = ts
                                element['value'] = json.dumps(button_value)
                                
                # 업데이트된 블록으로 메시지 업데이트
                updated_message_json = {
                    "channel": channel_id,
                    "ts": ts,
                    "blocks": blocks,
                    "text": message_json.get('text', '')
                }
                
                update_response = requests.post(
                    "https://slack.com/api/chat.update",
                    headers={
                        "Authorization": f"Bearer {bot_token}",
                        "Content-Type": "application/json"
                    },
                    json=updated_message_json
                )
                
                if not update_response.json().get('ok', False):
                    logger.warning(f"버튼 값 업데이트 실패: {update_response.text}")
        except Exception as e:
            logger.warning(f"버튼 값 업데이트 중 오류 발생: {str(e)}")
        
        return True, ts
    
    except Exception as e:
        logger.error(f"Slack 메시지 전송 중 오류 발생: {str(e)}", exc_info=True)
        return False, None

def send_execution_result_to_slack(result, volume_id, action_type, channel_id, requested_by, bot_token, thread_ts=None):
    """
    실행 결과를 Slack 채널로 전송합니다.
    
    :param result: 실행 결과
    :param volume_id: 볼륨 ID
    :param action_type: 액션 유형
    :param channel_id: Slack 채널 ID 
    :param requested_by: 요청자 ID
    :param bot_token: Slack Bot 토큰
    :param thread_ts: 스레드 타임스탬프 (스레드에 응답할 경우)
    :return: 전송 성공 여부
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
        
        # 메시지 JSON 구성
        message_json = {
            "channel": channel_id,
            "blocks": blocks,
            "text": f"볼륨 {volume_id}에 대한 {action_name} 액션이 {('성공적으로 완료' if success else '실패')}되었습니다."
        }
        
        # 스레드에 응답하는 경우
        if thread_ts:
            message_json["thread_ts"] = thread_ts
        
        # Slack API로 메시지 전송
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json"
            },
            json=message_json
        )
        
        if response.status_code != 200 or not response.json().get('ok', False):
            logger.error(f"Slack 메시지 전송 실패: {response.status_code} {response.text}")
            return False
        
        return True
    
    except Exception as e:
        logger.error(f"Slack 메시지 전송 중 오류 발생: {str(e)}", exc_info=True)
        return False

def send_slack_message(channel_id, message, bot_token, thread_ts=None):
    """
    간단한 텍스트 메시지를 Slack 채널로 전송합니다.
    
    :param channel_id: Slack 채널 ID
    :param message: 전송할 메시지
    :param bot_token: Slack Bot 토큰
    :param thread_ts: 스레드 타임스탬프 (스레드에 응답할 경우)
    :return: 전송 성공 여부와 메시지 타임스탬프
    """
    if not bot_token:
        logger.warning("SLACK_BOT_TOKEN이 설정되지 않았습니다. Slack 메시지를 전송할 수 없습니다.")
        return False, None
    
    try:
        # 메시지 JSON 구성
        message_json = {
            "channel": channel_id,
            "text": message
        }
        
        # 스레드에 응답하는 경우
        if thread_ts:
            message_json["thread_ts"] = thread_ts
        
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json"
            },
            json=message_json
        )
        
        if response.status_code != 200 or not response.json().get('ok', False):
            logger.error(f"Slack 메시지 전송 실패: {response.status_code} {response.text}")
            return False, None
        
        # 메시지 타임스탬프 반환 (스레드 식별용)
        ts = response.json().get('ts')
        return True, ts
    
    except Exception as e:
        logger.error(f"Slack 메시지 전송 중 오류 발생: {str(e)}", exc_info=True)
        return False, None

def send_original_command(channel_id, command, text, bot_token, user_id):
    """
    사용자가 입력한 원본 명령어를 채팅에 표시하고 스레드를 생성합니다.
    
    :param channel_id: Slack 채널 ID
    :param command: 사용자가 입력한 슬래시 명령어 (예: /ebs-optimize)
    :param text: 명령어 뒤에 붙은 텍스트 (예: analyze vol-123)
    :param bot_token: Slack Bot 토큰
    :param user_id: 명령을 실행한 사용자 ID
    :return: 전송 성공 여부와 스레드 타임스탬프
    """
    if not bot_token:
        logger.warning("SLACK_BOT_TOKEN이 설정되지 않았습니다. Slack 메시지를 전송할 수 없습니다.")
        return False, None
    
    try:
        # 명령어를 표시하는 메시지 구성
        formatted_command = f"{command} {text}"
        message_text = f"<@{user_id}>님이 실행한 명령어: `{formatted_command}`"
        
        # Slack API로 메시지 전송
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json"
            },
            json={
                "channel": channel_id,
                "text": message_text,
                "mrkdwn": True
            }
        )
        
        if response.status_code != 200 or not response.json().get('ok', False):
            logger.error(f"원본 명령어 메시지 전송 실패: {response.status_code} {response.text}")
            return False, None
        
        # 스레드 식별자 반환
        ts = response.json().get('ts')
        return True, ts
    
    except Exception as e:
        logger.error(f"원본 명령어 메시지 전송 중 오류 발생: {str(e)}", exc_info=True)
        return False, None

def send_all_regions_analysis_result_to_slack(result, channel_id, bot_token, thread_ts=None, use_thread=True):
    """
    전체 리전 분석 결과를 Slack 채널로 전송합니다.
    
    :param result: 분석 결과
    :param channel_id: Slack 채널 ID
    :param bot_token: Slack Bot 토큰
    :param thread_ts: 스레드 타임스탬프 (스레드에 응답할 경우)
    :param use_thread: 결과를 스레드에 표시할지 여부
    :return: 전송 성공 여부와 thread_ts (스레드로 사용할 타임스탬프)
    """
    if not bot_token:
        logger.warning("SLACK_BOT_TOKEN이 설정되지 않았습니다. Slack 메시지를 전송할 수 없습니다.")
        return False, None
    
    try:
        # 요약 정보 추출
        summary = result.get("summary", {})
        idle_volumes = summary.get("total_idle_volumes", 0)
        over_volumes = summary.get("total_overprovisioned_volumes", 0)
        savings = summary.get("total_estimated_savings", 0)
        
        # Block Kit 메시지 생성
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "EBS 볼륨 최적화 분석 결과",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*분석 완료 시간:* {result.get('timestamp')}"
                }
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*유휴 볼륨:* {idle_volumes}개"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*과대 프로비저닝 볼륨:* {over_volumes}개"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*예상 월간 절감액:* ${savings}"
                    }
                ]
            }
        ]
        
        # 메시지 JSON 구성
        message_json = {
            "channel": channel_id,
            "blocks": blocks,
            "text": f"EBS 볼륨 최적화 분석 결과: 유휴 {idle_volumes}개, 과대 프로비저닝 {over_volumes}개, 예상 절감액 ${savings}/월"
        }
        
        # 스레드에 응답하는 경우
        if use_thread and thread_ts:
            message_json["thread_ts"] = thread_ts
        
        # Slack API로 메시지 전송
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json"
            },
            json=message_json
        )
        
        if response.status_code != 200 or not response.json().get('ok', False):
            logger.error(f"Slack 메시지 전송 실패: {response.status_code} {response.text}")
            return False, None
        
        # 스레드 식별자 반환 (후속 응답을 스레드로 유지하기 위함)
        ts = response.json().get('ts')
        
        # 권장 조치가 있으면 스레드에 추가
        actions = summary.get("suggested_actions", [])
        if actions and ts:
            # 최대 10개만 표시
            send_actions_to_thread(actions[:10], channel_id, bot_token, ts)
            
            # 표시되지 않은 조치가 있는 경우 안내
            if len(actions) > 10:
                send_slack_message(
                    channel_id,
                    f"*추가 {len(actions) - 10}개의 권장 조치가 있습니다. 상세 보고서를 확인하세요.*",
                    bot_token,
                    ts
                )
        
        return True, ts
    
    except Exception as e:
        logger.error(f"Slack 메시지 전송 중 오류 발생: {str(e)}", exc_info=True)
        return False, None

def send_actions_to_thread(actions, channel_id, bot_token, thread_ts):
    """
    권장 조치 목록을 스레드에 전송합니다.
    
    :param actions: 권장 조치 목록
    :param channel_id: Slack 채널 ID
    :param bot_token: Slack Bot 토큰
    :param thread_ts: 스레드 타임스탬프
    """
    try:
        actions_text = "*권장 조치 목록:*\n\n"
        
        for i, action in enumerate(actions):
            volume_id = action.get('volume_id', 'unknown')
            region = action.get('region', 'unknown')
            action_type = action.get('action_type', 'unknown')
            recommendation = action.get('recommendation', '해당 없음')
            savings = action.get('estimated_savings', 0)
            
            actions_text += f"*{i+1}.* 볼륨 `{volume_id}` ({region})\n"
            actions_text += f"• 조치: {action_type}\n"
            actions_text += f"• 추천: {recommendation}\n"
            actions_text += f"• 예상 절감액: ${savings:.2f}/월\n\n"
        
        # 스레드에 메시지 전송
        send_slack_message(channel_id, actions_text, bot_token, thread_ts)
    except Exception as e:
        logger.error(f"권장 조치 스레드 메시지 전송 중 오류 발생: {str(e)}", exc_info=True)

def update_analysis_result_message(result, volume_id, action_type, channel_id, requested_by, bot_token, message_ts=None):
    """
    기존 분석 결과 메시지를 액션 실행 결과로 업데이트합니다.
    
    :param result: 실행 결과
    :param volume_id: 볼륨 ID
    :param action_type: 액션 유형
    :param channel_id: Slack 채널 ID 
    :param requested_by: 요청자 ID
    :param bot_token: Slack Bot 토큰
    :param message_ts: 업데이트할 메시지의 타임스탬프
    :return: 업데이트 성공 여부
    """
    # 로그 추가
    logger.info(f"메시지 업데이트 시작: channel_id={channel_id}, message_ts={message_ts}, action_type={action_type}, volume_id={volume_id}")
    
    if not bot_token:
        logger.warning("SLACK_BOT_TOKEN이 설정되지 않았습니다. Slack 메시지를 업데이트할 수 없습니다.")
        return False
    
    if not message_ts:
        logger.warning("메시지 타임스탬프가 제공되지 않아 업데이트할 수 없습니다.")
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
        
        # 실행 세부 정보가 있으면 추가
        if isinstance(details, dict) and details:
            detail_text = ""
            for key, value in details.items():
                # 스냅샷 ID나 특정 중요 정보는 강조 표시
                if key.lower() in ['snapshot_id', 'snapshot', 'id']:
                    detail_text += f"*{key}:* `{value}`\n"
                else:
                    detail_text += f"*{key}:* {value}\n"
            
            if detail_text:
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*세부 정보:*\n{detail_text}"
                    }
                })
        
        # 실행 결과 메시지에는 추가 안내 메시지 추가
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "액션이 완료되었습니다. 추가 조치가 필요한 경우 새 분석을 요청하세요."
                }
            ]
        })
        
        # 메시지 JSON 구성
        message_json = {
            "channel": channel_id,
            "ts": message_ts,  # 업데이트할 메시지 타임스탬프
            "blocks": blocks,
            "text": f"볼륨 {volume_id}에 대한 {action_name} 액션이 {('성공적으로 완료' if success else '실패')}되었습니다."
        }
        
        # Slack API로 메시지 업데이트
        response = requests.post(
            "https://slack.com/api/chat.update",  # update API 사용
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json"
            },
            json=message_json
        )
        
        if response.status_code != 200 or not response.json().get('ok', False):
            logger.error(f"Slack 메시지 업데이트 실패: {response.status_code} {response.text}")
            return False
        
        logger.info(f"Slack 메시지 업데이트 성공: channel_id={channel_id}, message_ts={message_ts}")
        return True
    
    except Exception as e:
        logger.error(f"Slack 메시지 업데이트 중 오류 발생: {str(e)}", exc_info=True)
        return False
