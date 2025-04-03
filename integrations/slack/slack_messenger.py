import json
import logging
import requests
from datetime import datetime
import os
import textwrap

logger = logging.getLogger()

# --- Helper Functions ---

def format_value(value, default="N/A"):
    """Helper function to format values, handling None or empty cases."""
    if value is None or value == '':
        return default
    # Check if value is already a formatted string like 'N/A'
    if isinstance(value, str) and not value.replace('.', '', 1).isdigit():
        return value
    try:
        if isinstance(value, (int, float)):
            # Format floats to 2 decimal places
            if isinstance(value, float):
                 return f"{value:.2f}"
            return str(value) # Return int as string
    except ValueError:
        pass # Fallback to string conversion if direct check fails
    return str(value)

def _build_action_buttons(volume_id, region, is_idle, is_over, is_attached, is_root, current_size, recommended_size, recommended_type, current_iops, current_throughput, recommended_iops, recommended_throughput, message_ts=""):
    """Generates a list of action buttons based on the provided conditions."""
    action_elements = []
    # Ensure base types are correct for JSON serialization
    button_value_base = {
        "volume_id": str(volume_id) if volume_id else None,
        "region": str(region) if region else None,
        "message_ts": str(message_ts) if message_ts else ""
    }
    # Filter out None values from base
    button_value_base = {k: v for k, v in button_value_base.items() if v is not None}


    if is_idle:
        snap_value = button_value_base.copy(); snap_value["action_type"] = "snapshot_only"
        action_elements.append({"type": "button", "text": {"type": "plain_text", "text": "Snapshot Only", "emoji": True}, "value": json.dumps(snap_value), "action_id": "execute_snapshot_only"})
        # Explicitly prevent delete button for root volumes, even if somehow detached
        if not is_root:
             # Original condition also checked for attachment, keep for clarity unless problematic
             # if not is_attached and not is_root:
             delete_value = button_value_base.copy(); delete_value["action_type"] = "snapshot_and_delete"
             action_elements.append({"type": "button", "text": {"type": "plain_text", "text": "Snapshot & Delete", "emoji": True}, "style": "danger", "value": json.dumps(delete_value), "action_id": "execute_snapshot_and_delete"})
    elif is_over:
        primary_action, action_text = None, "Execute Optimization"
        current_type = None # Passed from caller
        # This part needs the current_type to be passed correctly
        # Placeholder for current_type if available:
        # current_type = kwargs.get('current_type')

        change_type_needed = recommended_type is not None and recommended_type != current_type
        resize_needed = recommended_size is not None and current_size is not None
        perf_adjust_needed = (recommended_iops is not None and recommended_iops != current_iops) or \
                             (recommended_throughput is not None and recommended_throughput != current_throughput)

        try:
            rc_size = float(recommended_size) if recommended_size is not None else None
            cr_size = float(current_size) if current_size is not None else None
            is_increase = resize_needed and rc_size is not None and cr_size is not None and rc_size > cr_size
            is_decrease = resize_needed and rc_size is not None and cr_size is not None and rc_size < cr_size
        except (TypeError, ValueError):
            is_increase = False
            is_decrease = False

        if change_type_needed and is_increase:
            primary_action, action_text = "change_type_and_resize", "Change Type & Increase Size"
        elif change_type_needed:
            primary_action, action_text = "change_type", "Change Type/Performance"
        elif is_increase:
            primary_action, action_text = "resize", "Increase Size"
        # Only allow resize down if not root
        elif is_decrease and not is_root:
             primary_action, action_text = "resize", "Decrease Size (Manual)"
        elif perf_adjust_needed:
             # Assuming perf adjustment implies change_type (e.g., for gp3)
             primary_action, action_text = "change_type", "Adjust Performance"

        if primary_action:
             # Disable button for root volume decrease size (manual action needed)
             disable_button = is_root and is_decrease

             if not disable_button:
                 action_value = button_value_base.copy(); action_value["action_type"] = primary_action
                 action_value = {k: str(v) if v is not None else None for k, v in action_value.items()}
                 action_value = {k: v for k, v in action_value.items() if v is not None}
                 action_elements.append({"type": "button", "text": {"type": "plain_text", "text": action_text, "emoji": True}, "value": json.dumps(action_value), "action_id": f"execute_{primary_action}"})

    return action_elements


def _build_single_volume_blocks(analysis_result):
    """Creates a list of Slack Block Kit blocks for a single volume analysis result."""
    volume_id = analysis_result.get('volume_id', 'N/A')
    region = analysis_result.get('region', 'N/A')
    details = analysis_result.get('details', {})

    az = details.get('availability_zone', 'N/A')
    name_tag = 'N/A' # Name Tag needs to be added in analyzer
    current_type = details.get('volume_type', 'N/A')
    current_size = details.get('size')
    prov_iops = details.get('iops')
    prov_tp = details.get('throughput')

    is_root = False
    attachments = details.get('attached_instances', [])
    if attachments:
        device_name = attachments[0].get('device')
        root_device_patterns = ['/dev/xvda', '/dev/sda1', '/dev/sda', '/dev/vda']
        if device_name and any(device_name.startswith(pattern) for pattern in root_device_patterns):
            is_root = True

    is_idle = analysis_result.get('is_idle', False)
    is_over = analysis_result.get('is_overprovisioned', False)

    reason = 'Analysis data unavailable'
    if is_idle:
        reason = analysis_result.get('idle_diagnosis', {}).get('reason', reason)
    elif is_over:
        reason = analysis_result.get('overprovisioned_diagnosis', {}).get('reason', reason)

    recommendation_text = analysis_result.get('recommendation', 'None')

    over_diag = analysis_result.get('overprovisioned_diagnosis', {})
    over_diag_data = over_diag.get('additional_data', {}) if isinstance(over_diag.get('additional_data'), dict) else {}

    recommended_size = over_diag_data.get('recommended_size')
    recommended_iops = over_diag_data.get('recommended_iops')
    recommended_throughput = over_diag_data.get('recommended_throughput')
    recommended_type = over_diag_data.get('recommended_type')
    savings = over_diag_data.get('estimated_savings', 0)

    is_attached = bool(attachments)
    timestamp = analysis_result.get("timestamp", datetime.now().isoformat())

    status_icon = ":large_green_circle:" # Optimal
    status_desc = "Optimal"
    if is_idle: status_icon, status_desc = ":large_yellow_circle:", "Idle"
    elif is_over: status_icon, status_desc = ":large_orange_circle:", "Over-provisioned"

    summary_text = f"Volume {volume_id} Detailed Analysis: {status_desc}"

    blocks = []
    # Header & Context
    blocks.append({"type": "header", "text": {"type": "plain_text", "text": f"{status_icon} Volume {volume_id} Detailed Analysis", "emoji": True}})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"Analysis Time: {timestamp}"}]})

    # Basic Info
    fields = [
        {"type": "mrkdwn", "text": f"*Volume ID:*\n`{volume_id}`"}, {"type": "mrkdwn", "text": f"*Name Tag:*\n{name_tag}"},
        {"type": "mrkdwn", "text": f"*Region/AZ:*\n{region} / {az}"}, {"type": "mrkdwn", "text": f"*Root Volume:*\n{'Yes' if is_root else 'No'}"},
        {"type": "mrkdwn", "text": f"*Current Type:*\n{current_type}"}, {"type": "mrkdwn", "text": f"*Current Size:*\n{format_value(current_size)} GB"},
    ]
    if prov_iops is not None: fields.append({"type": "mrkdwn", "text": f"*Current IOPS:*\n{format_value(prov_iops)}"})
    if prov_tp is not None: fields.append({"type": "mrkdwn", "text": f"*Current Throughput:*\n{format_value(prov_tp)} MB/s"})
    blocks.append({"type": "section", "fields": fields})
    blocks.append({"type": "divider"})

    # Analysis Results
    analysis_fields = [{"type": "mrkdwn", "text": f"*Status:*\n*{status_desc}*"}, {"type": "mrkdwn", "text": f"*Basis for Status:*\n{reason}"}]

    disk_usage = details.get('disk_usage_data', {})
    perf_data = details.get('performance_data', {})

    if disk_usage:
         avg_usage = disk_usage.get('average_usage_percent')
         max_usage = disk_usage.get('max_usage_percent')
         usage_text = f"Avg: {format_value(avg_usage)}%" + (f", Max: {format_value(max_usage)}%" if max_usage is not None else "")
         analysis_fields.append({"type": "mrkdwn", "text": f"*Disk Usage:*\n{usage_text}"})
    if perf_data:
         max_ops = perf_data.get('max_total_ops_in_period')
         max_bytes = perf_data.get('max_total_bytes_in_period')
         perf_text = (f"Max Observed IOPS (period): {format_value(max_ops)}\n" if max_ops is not None else "") + \
                     (f"Max Observed Throughput (period): {format_value(max_bytes / (1024*1024), 'N/A')} MB" if max_bytes is not None else "")
         if perf_text: analysis_fields.append({"type": "mrkdwn", "text": f"*Performance Metrics (Period Max):*\n{perf_text}"})

    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*📊 Analysis Results*"}})
    blocks.append({"type": "section", "fields": analysis_fields})
    blocks.append({"type": "divider"})

    # Recommendation
    reco_fields = [{"type": "mrkdwn", "text": f"*Recommendation:*\n{recommendation_text}"}, {"type": "mrkdwn", "text": f"*Estimated Monthly Savings:*\n*${format_value(savings, '0.00')}*"} ]
    reco_spec = []
    if recommended_type: reco_spec.append(f"Type: {recommended_type}")
    try:
        rc_size = float(recommended_size) if recommended_size is not None else None
        cr_size = float(current_size) if current_size is not None else None
        if rc_size is not None and cr_size is not None and rc_size != cr_size: reco_spec.append(f"Size: {format_value(recommended_size)} GB")
    except (TypeError, ValueError): pass
    try:
        rc_iops = int(recommended_iops) if recommended_iops is not None else None
        pv_iops = int(prov_iops) if prov_iops is not None else None
        if rc_iops is not None and pv_iops is not None and rc_iops != pv_iops: reco_spec.append(f"IOPS: {format_value(recommended_iops)}")
    except (TypeError, ValueError): pass
    try:
        rc_tp = int(recommended_throughput) if recommended_throughput is not None else None
        pv_tp = int(prov_tp) if prov_tp is not None else None
        if rc_tp is not None and pv_tp is not None and rc_tp != pv_tp: reco_spec.append(f"Throughput: {format_value(recommended_throughput)} MB/s")
    except (TypeError, ValueError): pass

    if reco_spec: reco_fields.append({"type": "mrkdwn", "text": f"*Recommended Specs:*\n" + ", ".join(reco_spec)})
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*💡 Recommendation*"}})
    blocks.append({"type": "section", "fields": reco_fields})

    # Warnings
    warnings = []
    if is_root: warnings.append("This is a root volume. Automated actions (delete, resize down) may be restricted. Manual changes require extra caution.")
    try:
        rc_size = float(recommended_size) if recommended_size is not None else None
        cr_size = float(current_size) if current_size is not None else None
        if rc_size is not None and cr_size is not None:
            if rc_size < cr_size: warnings.append("Reducing volume size is not supported automatically. Manual steps like creating a new volume and migrating data are required.")
            elif rc_size > cr_size: warnings.append("After increasing volume size, you might need to extend the file system at the OS level.")
    except (TypeError, ValueError): pass
    if recommended_type and recommended_type != current_type: warnings.append(f"Changing type from {current_type} to {recommended_type} might alter performance characteristics. Review workload impact.")
    warnings.append("A snapshot will be automatically created for safety before executing any action.")
    if warnings:
         blocks.append({"type": "divider"})
         blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*⚠️ Warnings*"}})
         blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "- " + "\n- ".join(warnings)}})

    # Rollback Info
    blocks.append({"type": "divider"})
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*🔙 Rollback Information*"}})
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "If issues occur after an action, you can recover using the automatically created snapshot. Check the execution result message for the Snapshot ID."}})

    return blocks, summary_text


def _build_summary_blocks(analysis_result):
    """Creates a list of Slack Block Kit blocks for an overall region analysis summary."""
    summary_data = analysis_result.get("summary", {})
    timestamp = analysis_result.get("timestamp", datetime.now().isoformat())
    idle_volumes = summary_data.get("total_idle_volumes", 0)
    over_volumes = summary_data.get("total_overprovisioned_volumes", 0)
    savings = summary_data.get("total_estimated_savings", 0)
    actions = summary_data.get("suggested_actions", [])
    summary_text = f"EBS Analysis Summary: Idle {idle_volumes}, Over-provisioned {over_volumes}, Savings ${format_value(savings, '0.00')}/month"

    blocks = []
    blocks.append({"type": "header", "text": {"type": "plain_text", "text": "EBS Volume Optimization Analysis Summary", "emoji": True}})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"Analysis Time: {timestamp}"}]})
    blocks.append({"type": "section", "fields": [
         {"type": "mrkdwn", "text": f"*Total Idle Volumes:* {idle_volumes}"},
         {"type": "mrkdwn", "text": f"*Total Over-provisioned Volumes:* {over_volumes}"},
         {"type": "mrkdwn", "text": f"*Total Estimated Monthly Savings:* ${format_value(savings, '0.00')}"}
    ]})

    if actions:
         blocks.append({"type": "divider"})
         blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Top Recommended Actions (Summary)*"}})
         action_texts = []
         for action in actions[:5]: # Top 5
              vol_id = action.get('volume_id','N/A')
              region = action.get('region', 'N/A')
              reco = action.get('recommendation','N/A')
              sav = action.get('estimated_savings',0)
              action_texts.append(f"- `{vol_id}` ({region}): {reco} (Est. Savings: ${format_value(sav, '0.00')})")
         blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(action_texts)}})
         if len(actions) > 5:
             blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"*... and {len(actions) - 5} more actions suggested. See full report or individual analysis.*"}]})

    return blocks, summary_text

def _build_execution_result_blocks(result, volume_id, action_type, requested_by):
     """Creates a list of Slack Block Kit blocks for an action execution result."""
     success = result.get('success', False)
     status = result.get('status', 'unknown')
     details = result.get('details', {})
     snapshot_id = details.get('snapshot_id')
     error_msg = details.get('error')
     warning_msg = details.get('warning')

     icon = ":white_check_mark:" if success else (":warning:" if status.startswith("skipped") else ":x:")
     action_name = action_type.replace('_', ' ').title()
     result_text = "Success"
     if not success: result_text = "Skipped" if status.startswith("skipped") else "Failed"

     blocks = []
     blocks.append({"type": "header", "text": {"type": "plain_text", "text": f"{icon} Volume {volume_id} {action_name} Result", "emoji": True}})
     blocks.append({"type": "section", "fields": [
         {"type": "mrkdwn", "text": f"*Volume ID:*\n`{volume_id}`"}, {"type": "mrkdwn", "text": f"*Action:* {action_name}"},
         {"type": "mrkdwn", "text": f"*Requested By:* <@{requested_by}>"}, {"type": "mrkdwn", "text": f"*Result:* {result_text}"}
     ]})

     detail_items = []
     if error_msg: detail_items.append(f"*Error:* {error_msg}")
     if warning_msg: detail_items.append(f"*Warning:* {warning_msg}")
     if details.get('message'): detail_items.append(f"*Message:* {details['message']}")
     if details.get('action'): detail_items.append(f"*Details:* {details['action']}")
     if details.get('note'): detail_items.append(f"*Note:* {details['note']}")
     if snapshot_id: detail_items.append(f"*Snapshot Created:* `{format_value(snapshot_id)}`")
     if detail_items:
         blocks.append({"type": "divider"})
         blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Details:*\n" + "\n".join([f"- {item}" for item in detail_items])}})

     if snapshot_id:
         blocks.append({"type": "divider"})
         blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*🔙 Rollback Information*"}})
         snap_id_formatted = format_value(snapshot_id)
         blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"If issues occur, you can restore from the created snapshot ID (`{snap_id_formatted}`) via AWS Console or CLI by creating a new volume and attaching it to the instance."}})

     blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"Request Processed Time: {result.get('timestamp', datetime.now().isoformat())}"}]})

     summary_text = f"Volume {volume_id} {action_name} Result: {result_text}"
     return blocks, summary_text


# --- Slack API Call Functions ---

def _post_slack_message(channel_id, bot_token, blocks, text, thread_ts=None):
    """Slack API(chat.postMessage)를 사용하여 새 메시지를 전송합니다."""
    if not bot_token: return {"success": False, "error": "Slack Bot Token이 없습니다."}
    if not channel_id: return {"success": False, "error": "Slack Channel ID가 없습니다."}


    payload = {"channel": channel_id, "blocks": blocks, "text": text}
    if thread_ts: payload["thread_ts"] = thread_ts

    try:
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
            json=payload,
            timeout=30 # Add timeout
        )
        response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
        response_data = response.json()
        if not response_data.get('ok'):
            error_detail = response_data.get('error', 'Unknown error')
            logger.error(f"Slack chat.postMessage 실패: {error_detail}")
            return {"success": False, "error": error_detail}
        logger.info(f"Slack chat.postMessage 성공: channel={channel_id}, ts={response_data.get('ts')}")
        return {"success": True, "response": response_data}
    except requests.exceptions.RequestException as e:
        logger.error(f"Slack chat.postMessage 요청 오류: {e}", exc_info=True)
        return {"success": False, "error": f"Request failed: {e}"}
    except Exception as e:
        logger.error(f"Slack chat.postMessage 중 예외: {e}", exc_info=True)
        return {"success": False, "error": str(e)}

def _update_slack_message(channel_id, bot_token, ts, blocks, text):
    """Slack API(chat.update)를 사용하여 기존 메시지를 업데이트합니다."""
    if not bot_token: return {"success": False, "error": "Slack Bot Token이 없습니다."}
    if not channel_id: return {"success": False, "error": "Slack Channel ID가 없습니다."}
    if not ts: return {"success": False, "error": "업데이트할 메시지 타임스탬프(ts)가 없습니다."}

    payload = {"channel": channel_id, "ts": ts, "blocks": blocks, "text": text}

    try:
        response = requests.post(
            "https://slack.com/api/chat.update",
            headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
            json=payload,
            timeout=30 # Add timeout
        )
        response.raise_for_status()
        response_data = response.json()
        if not response_data.get('ok'):
            error_detail = response_data.get('error', 'Unknown error')
            logger.error(f"Slack chat.update 실패: {error_detail}")
            return {"success": False, "error": error_detail}
        logger.info(f"Slack chat.update 성공: channel={channel_id}, ts={ts}")
        return {"success": True, "response": response_data}
    except requests.exceptions.RequestException as e:
         logger.error(f"Slack chat.update 요청 오류: {e}", exc_info=True)
         return {"success": False, "error": f"Request failed: {e}"}
    except Exception as e:
        logger.error(f"Slack chat.update 중 예외: {e}", exc_info=True)
        return {"success": False, "error": str(e)}

def _send_slack_webhook(webhook_url, blocks, text):
     """Slack Incoming Webhook을 사용하여 메시지를 전송합니다."""
     if not webhook_url: return {"success": False, "error": "Slack Webhook URL이 없습니다."}

     payload = {"blocks": blocks, "text": text}
     try:
          response = requests.post(webhook_url, json=payload, headers={"Content-Type": "application/json"}, timeout=30)
          response.raise_for_status() # Check for HTTP errors
          # Webhook response is typically just 'ok'
          if response.text != "ok":
               logger.warning(f"Slack 웹훅 응답 예상과 다름: {response.text}")
               # Consider it a success if status code was 2xx
          logger.info("Slack 웹훅 전송 성공.")
          return {"success": True}
     except requests.exceptions.RequestException as e:
          logger.error(f"Slack 웹훅 전송 오류: {e}", exc_info=True)
          return {"success": False, "error": f"Request failed: {e}"}
     except Exception as e:
          logger.error(f"Slack 웹훅 전송 중 예외: {e}", exc_info=True)
          return {"success": False, "error": str(e)}


# --- Main Interface Functions (Refactored) ---

def send_analysis_summary_to_slack(analysis_result, channel_id=None, bot_token=None, webhook_url=None, message_ts_to_update=None):
    """
    EBS 분석 결과(단일 또는 전체 요약)를 Slack으로 전송 또는 업데이트합니다. (리팩토링됨)
    """
    target_webhook_url = webhook_url or os.environ.get('SLACK_WEBHOOK_URL')
    target_channel_id = channel_id or os.environ.get('SLACK_CHANNEL_ID')
    target_bot_token = bot_token or os.environ.get('SLACK_BOT_TOKEN')

    if not (target_channel_id and target_bot_token) and not target_webhook_url:
        logger.warning("Slack 전송 대상 정보 부족")
        return {"skipped": True, "reason": "No channel/token or webhook URL configured"}

    try:
        is_single_volume = "volume_id" in analysis_result and "summary" not in analysis_result
        blocks, summary_text = [], "EBS Analysis Result" # Initialize

        if is_single_volume:
            blocks, summary_text = _build_single_volume_blocks(analysis_result)
            # Build action buttons again, passing correct context including is_root
            details = analysis_result.get('details', {})
            attachments = details.get('attached_instances', [])
            is_root_button_check = False
            if attachments:
                device_name = attachments[0].get('device')
                root_device_patterns = ['/dev/xvda', '/dev/sda1', '/dev/sda', '/dev/vda']
                if device_name and any(device_name.startswith(pattern) for pattern in root_device_patterns):
                     is_root_button_check = True

            # Fetch data for button builder, using details where appropriate
            current_size = details.get('size')
            current_iops = details.get('iops')
            current_throughput = details.get('throughput')
            over_diag = analysis_result.get('overprovisioned_diagnosis', {})
            over_diag_data = over_diag.get('additional_data', {}) if isinstance(over_diag.get('additional_data'), dict) else {}
            recommended_size = over_diag_data.get('recommended_size')
            recommended_iops = over_diag_data.get('recommended_iops')
            recommended_throughput = over_diag_data.get('recommended_throughput')
            recommended_type = over_diag_data.get('recommended_type')

            action_elements = _build_action_buttons(
                 analysis_result.get('volume_id'),
                 analysis_result.get('region'),
                 analysis_result.get('is_idle', False),
                 analysis_result.get('is_overprovisioned', False),
                 bool(attachments), # is_attached
                 is_root_button_check, # Correctly calculated is_root
                 current_size,
                 recommended_size,
                 recommended_type,
                 current_iops,
                 current_throughput,
                 recommended_iops,
                 recommended_throughput,
                 message_ts=message_ts_to_update or "" # Pass message_ts if updating
             )
            if action_elements:
                 # 기존 블록 리스트에서 actions 타입 블록 찾기
                 action_block_index = -1
                 for i, block in enumerate(blocks):
                     if block.get("type") == "actions":
                         action_block_index = i
                         break
                 # actions 블록이 있으면 요소 업데이트, 없으면 새로 추가
                 if action_block_index != -1:
                      blocks[action_block_index]["elements"] = action_elements
                 else:
                      # divider가 마지막에 추가되었을 수 있으니 그 앞에 추가
                      has_divider = False
                      if blocks: # Check if blocks list is not empty
                           # Check if the last block is a divider
                           if blocks[-1].get("type") == "divider":
                                has_divider = True
                                # Insert before the last element (divider)
                                blocks.insert(-1, {"type": "actions", "elements": action_elements})
                      # If no divider was found at the end, or list was empty, append normally
                      if not has_divider:
                           blocks.append({"type": "actions", "elements": action_elements})

        elif analysis_result.get("summary"):
            blocks, summary_text = _build_summary_blocks(analysis_result)
        else:
            blocks, summary_text = [{"type": "section", "text": {"type": "mrkdwn", "text": "*오류:* 알 수 없는 분석 결과 형식"}}], "EBS Analysis Summary Generation Failed"

        # 메시지 전송/업데이트 결정
        result = {"success": False, "error": "No valid Slack target or action."} # Default result
        if message_ts_to_update and target_channel_id and target_bot_token:
            logger.info(f"Slack 메시지 업데이트 시도: channel={target_channel_id}, ts={message_ts_to_update}")
            result = _update_slack_message(target_channel_id, target_bot_token, message_ts_to_update, blocks, summary_text)
        elif target_channel_id and target_bot_token:
            logger.info(f"Slack 새 메시지 전송 시도: channel={target_channel_id}")
            result = _post_slack_message(target_channel_id, target_bot_token, blocks, summary_text)
        elif target_webhook_url:
             # 웹훅은 업데이트 불가
             if message_ts_to_update:
                  logger.warning("웹훅은 메시지 업데이트를 지원하지 않습니다. 새 메시지로 전송합니다.")
             logger.info(f"Slack 웹훅 전송 시도: url={target_webhook_url[:30]}...")
             result = _send_slack_webhook(target_webhook_url, blocks, summary_text)
        else:
             logger.error("유효한 Slack 전송 대상 없음")
             result = {"success": False, "error": "유효한 Slack 전송 대상 없음"}


        return result

    except Exception as e:
        logger.error(f"Slack 분석 요약 처리 중 예외 발생: {str(e)}", exc_info=True)
        error_text = f"EBS Analysis Summary Message Generation/Sending Failed: {e}"
        # 오류 발생 시 간단한 오류 메시지 전송 시도
        if target_channel_id and target_bot_token:
            _post_slack_message(target_channel_id, target_bot_token, [{"type": "section", "text": {"type": "mrkdwn", "text": error_text}}], error_text)
        elif target_webhook_url:
            _send_slack_webhook(target_webhook_url, [{"type": "section", "text": {"type": "mrkdwn", "text": error_text}}], error_text)
        return {"success": False, "error": str(e)}


def send_execution_result_to_slack(result, volume_id, action_type, channel_id, requested_by, bot_token, thread_ts=None, message_ts_to_update=None):
    """
    실행 결과를 Slack 채널로 전송하거나 기존 분석 메시지를 업데이트합니다. (리팩토링됨)
    """
    if not bot_token:
        logger.warning("SLACK_BOT_TOKEN이 설정되지 않았습니다.")
        return False
    if not channel_id:
         logger.warning("SLACK_CHANNEL_ID가 설정되지 않았습니다.")
         return False

    try:
        blocks, summary_text = _build_execution_result_blocks(result, volume_id, action_type, requested_by)

        api_result = {"success": False, "error": "No valid Slack action taken."} # Default
        if message_ts_to_update:
             # 원본 분석 메시지를 실행 결과로 업데이트
             logger.info(f"Slack 메시지 업데이트 시도 (실행 결과): channel={channel_id}, ts={message_ts_to_update}")
             api_result = _update_slack_message(channel_id, bot_token, message_ts_to_update, blocks, summary_text)
        else:
             # 새 메시지 또는 스레드 응답으로 전송
             logger.info(f"Slack 메시지 전송 시도 (실행 결과): channel={channel_id}, thread_ts={thread_ts}")
             api_result = _post_slack_message(channel_id, bot_token, blocks, summary_text, thread_ts=thread_ts)

        return api_result.get("success", False)

    except Exception as e:
        logger.error(f"Slack 실행 결과 처리 중 오류 발생: {str(e)}", exc_info=True)
        return False

# --- 기존 유틸성 함수들 --- # 필요 없는 함수는 주석 처리 또는 삭제 가능 # send_analysis_result_to_slack -> send_analysis_summary_to_slack 사용 권장 # update_analysis_result_message -> send_execution_result_to_slack(..., message_ts_to_update=...) 사용 권장 # send_all_regions_analysis_result_to_slack -> send_analysis_summary_to_slack 사용 권장 # send_actions_to_thread -> _build_summary_blocks 내부에 통합 또는 별도 유지

def send_slack_error(webhook_url, error_message):
    # ... (기존 코드 유지 또는 _send_slack_webhook 사용하도록 수정) ...
    if not webhook_url: return {"skipped": True}
    blocks = [
         {"type": "header", "text": {"type": "plain_text", "text": "⚠️ EBS Optimization Error", "emoji": True}},
         {"type": "section", "text": {"type": "mrkdwn", "text": f"*Time:* {datetime.now().isoformat()}"}},
         {"type": "section", "text": {"type": "mrkdwn", "text": f"*Message:*\n```{error_message}```"}}
    ]
    return _send_slack_webhook(webhook_url, blocks, f"EBS Optimization Error: {error_message[:50]}...")


def send_slack_message(channel_id, message, bot_token, thread_ts=None):
    # ... (기존 코드 유지 또는 _post_slack_message 사용하도록 수정) ...
    if not bot_token or not channel_id: return False, None
    # 간단 텍스트는 blocks 없이 전송
    payload = {"channel": channel_id, "text": message}
    if thread_ts: payload["thread_ts"] = thread_ts
    try:
         response = requests.post(
             "https://slack.com/api/chat.postMessage",
             headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
             json=payload, timeout=30
         )
         response_data = response.json()
         if response.status_code == 200 and response_data.get('ok'):
              return True, response_data.get('ts')
         else:
              logger.error(f"Slack 간단 메시지 전송 실패: {response.status_code} {response_data.get('error', response.text)}")
              return False, None
    except Exception as e:
         logger.error(f"Slack 간단 메시지 전송 중 오류: {e}", exc_info=True)
         return False, None


def send_original_command(channel_id, command, text, bot_token, user_id):
    # ... (기존 코드 유지 또는 send_slack_message 사용) ...
    message_text = f"<@{user_id}>님이 실행한 명령어: `{command} {text}`"
    success, ts = send_slack_message(channel_id, message_text, bot_token)
    return success, ts

# 더 이상 사용되지 않을 수 있는 기존 함수들 주석 처리 # def send_analysis_result_to_slack(...): #     pass # def send_all_regions_analysis_result_to_slack(...): #     pass # def send_actions_to_thread(...): #     pass # def update_analysis_result_message(...): #     pass
