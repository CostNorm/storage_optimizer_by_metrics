import hmac
import hashlib
import time
import logging
from urllib.parse import parse_qs

logger = logging.getLogger()

def verify_slack_request(event, signing_secret, verification_token=None):
    """
    Slack 요청의 진위를 검증합니다.
    
    :param event: API Gateway로부터 전달된 이벤트 객체
    :param signing_secret: Slack 앱 서명 비밀키
    :param verification_token: Slack 검증 토큰 (이전 방식, 선택적)
    :return: 유효한 요청 여부 (True/False)
    """
    if not signing_secret and not verification_token:
        logger.warning("서명 비밀키와 검증 토큰이 모두 설정되지 않았습니다. 검증을 건너뜁니다.")
        return True
    
    # API Gateway를 통해 들어온 요청의 헤더와 본문 추출
    headers = event.get('headers', {})
    body = event.get('body', '')
    
    # 기본 검증 - Slack Verification Token 방식(이전 방식)
    if verification_token:
        # 본문을 파싱하여 토큰 확인
        if event.get('isBase64Encoded', False):
            import base64
            body_decoded = base64.b64decode(body).decode('utf-8')
        else:
            body_decoded = body
        
        params = parse_qs(body_decoded)
        token = params.get('token', [None])[0]
        
        if token and token == verification_token:
            return True
    
    # 서명 기반 검증(권장 방식)
    if signing_secret:
        slack_signature = headers.get('X-Slack-Signature')
        slack_request_timestamp = headers.get('X-Slack-Request-Timestamp')
        
        if not slack_signature or not slack_request_timestamp:
            logger.warning("Slack 서명 또는 타임스탬프가 누락되었습니다.")
            return False
        
        # 타임스탬프 검증 - 5분 초과 요청은 재생 공격 가능성
        if abs(int(time.time()) - int(slack_request_timestamp)) > 300:
            logger.warning("Slack 요청 타임스탬프가 5분을 초과했습니다.")
            return False
        
        # Base64 인코딩된 본문 디코딩
        if event.get('isBase64Encoded', False):
            import base64
            body = base64.b64decode(body).decode('utf-8')
        
        # 서명 검증
        sig_basestring = f"v0:{slack_request_timestamp}:{body}"
        my_signature = f"v0={hmac.new(signing_secret.encode(), sig_basestring.encode(), hashlib.sha256).hexdigest()}"
        
        return hmac.compare_digest(my_signature, slack_signature)
    
    return False
