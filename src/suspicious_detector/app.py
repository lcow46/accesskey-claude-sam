import boto3
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from botocore.exceptions import ClientError

import urllib.request
import urllib.error

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")

ip_country_table  = dynamodb.Table(os.environ["IP_COUNTRY_TABLE"])
region_table      = dynamodb.Table(os.environ["REGION_TABLE"])
user_agent_table  = dynamodb.Table(os.environ["USER_AGENT_TABLE"])
error_event_table = dynamodb.Table(os.environ["ERROR_EVENT_TABLE"])
aws_api_table     = dynamodb.Table(os.environ["AWS_API_TABLE"])

def get_slack_token() -> str:
    secret_name = os.environ["SLACK_SECRET_NAME"]
    client = boto3.client(service_name="secretsmanager")
    try:
        response = client.get_secret_value(SecretId=secret_name)
        secret_data = json.loads(response["SecretString"])
        return secret_data.get("slack_bot_token")
    except ClientError as e:
        logger.error(f"Secrets Manager 에러 발생: {e}")
        raise e


SLACK_BOT_TOKEN   = get_slack_token()
SLACK_CHANNEL_ID  = os.environ["SLACK_CHANNEL_ID"]
ALLOWED_COUNTRIES = set(os.environ.get("ALLOWED_COUNTRIES", "KR").split(","))
ALLOWED_REGIONS   = set(os.environ.get("ALLOWED_REGIONS", "ap-northeast-2").split(","))
ERROR_THRESHOLD   = int(os.environ.get("ERROR_THRESHOLD", "5"))
ERROR_WINDOW_MIN  = int(os.environ.get("ERROR_WINDOW_MIN", "5"))

# 정찰 API (시나리오 1)
RECONNAISSANCE_APIS = {
    "GetCallerIdentity",
    "ListUserPolicies",
    "ListAttachedUserPolicies",
}

# 고위험 API (시나리오 2)
PRIVILEGE_ESCALATION_APIS = {
    "CreateAccessKey", "AttachUserPolicy", "AttachRolePolicy",
    "PutUserPolicy", "AddUserToGroup", "CreateUser", "PutRolePolicy",
}

# 리소스 생성 API (시나리오 4)
RESOURCE_CREATE_APIS = {"RunInstances", "CreateFunction"}

# 알려진 공격 도구 시그니처 (시나리오 5)
ATTACK_TOOL_SIGNATURES = {
    "pacu", "aws_pwn", "weirdaal", "nimbostratus",
    "cloud_enum", "enumerate-iam", "aws-recon"
}


# 메인 핸들러

def lambda_handler(event, context):
    for record in event.get("Records", []):
        logger.info(f"[디버그] record eventName: {record.get('eventName')} / eventSourceARN: {record.get('eventSourceARN', '')}")

        if record.get("eventName") != "INSERT":
            continue

        new_image = record.get("dynamodb", {}).get("NewImage", {})
        logger.info(f"[디버그] new_image: {new_image}")

        if not new_image:
            continue

        source_table = record.get("eventSourceARN", "")

        try:
            if "ref_aws_api" in source_table:
                handle_aws_api_event(new_image)
            elif "ref_error_event" in source_table:
                handle_error_event(new_image)
        except Exception as e:
            logger.error(f"이벤트 처리 실패: {e}")


# ref_aws_api 스트림 핸들러

def handle_aws_api_event(image: dict):
    access_key_id = get_str(image, "accessKeyId")
    sk            = get_str(image, "eventTime#eventId")
    event_name    = get_str(image, "eventName")
    event_source  = get_str(image, "eventSource")

    if not access_key_id or not sk:
        return

    event_time, event_id = sk.split("#", 1)

    # ip_country는 여러 시나리오 공통에서 사용하니까 미리 조회
    ip_info      = get_ip_country(access_key_id, sk)
    country_code = ip_info.get("countryCode", "")
    source_ip    = ip_info.get("sourceIPAddress", "")
    is_foreign_ip = country_code and country_code not in ALLOWED_COUNTRIES
    is_night_time = check_night_time(event_time)

    # region, user_agent는 시나리오 블록 안에서 필요할 때 조회
    aws_region = ""
    ua_type    = ""
    user_agent = ""

    alerts = []

    # 시나리오 1: 정찰 API + 외부 IP
    if event_name in RECONNAISSANCE_APIS and is_foreign_ip:
        region_info = get_region(access_key_id, sk)
        aws_region  = region_info.get("awsRegion", "")
        alerts.append({
            "scenario": "시나리오 1",
            "title": "공격자 초기 정찰 의심",
            "detail": f"{event_name}가 외부 IP에서 호출됨",
        })

    # 시나리오 2: 권한 상승 API + 외부 IP
    if event_name in PRIVILEGE_ESCALATION_APIS and is_foreign_ip:
        region_info = get_region(access_key_id, sk)
        aws_region  = region_info.get("awsRegion", "")
        alerts.append({
            "scenario": "시나리오 2",
            "title": "권한 상승 시도 의심",
            "detail": f"{event_name} API가 외부 IP에서 호출됨",
        })

    # 시나리오 4: 비정상 리전 + 리소스 생성 API
    if event_name in RESOURCE_CREATE_APIS:
        region_info       = get_region(access_key_id, sk)
        aws_region        = region_info.get("awsRegion", "")
        is_foreign_region = aws_region and aws_region not in ALLOWED_REGIONS
        if is_foreign_region:
            alerts.append({
                "scenario": "시나리오 4",
                "title": "비정상 리전 리소스 생성 의심",
                "detail": f"{event_name} API가 {aws_region} 리전에서 호출됨",
            })

    # 시나리오 5: 알려진 공격 도구 시그니처 탐지
    ua_info    = get_user_agent(access_key_id, sk)
    user_agent = ua_info.get("userAgent", "")

    if any(sig in user_agent.lower() for sig in ATTACK_TOOL_SIGNATURES):
        region_info = get_region(access_key_id, sk)
        aws_region  = region_info.get("awsRegion", "")
        # 공격 도구 시그니처 탐지 시 userAgentType은 표시하는 의미 없으니까 표시안함
        ua_type = ""
        alerts.append({
            "scenario": "시나리오 5",
            "title": "알려진 공격 도구 사용 의심",
            "detail": f"공격 도구 시그니처 감지 - {user_agent}",
        })

    for alert in alerts:
        send_slack_alert(
            access_key_id=access_key_id,
            event_name=event_name,
            event_source=event_source,
            event_time=event_time,
            source_ip=source_ip,
            country_code=country_code,
            aws_region=aws_region,
            ua_type=ua_type,
            alert=alert,
        )


# ref_error_event 스트림 핸들러 (시나리오 3)

def handle_error_event(image: dict):
    access_key_id = get_str(image, "accessKeyId")
    sk            = get_str(image, "eventTime#eventId")

    if not access_key_id or not sk:
        return

    event_time, _ = sk.split("#", 1)

    window_start = (
        datetime.fromisoformat(event_time.replace("Z", "+00:00"))
        - timedelta(minutes=ERROR_WINDOW_MIN)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    response = error_event_table.query(
        KeyConditionExpression=(
            boto3.dynamodb.conditions.Key("accessKeyId").eq(access_key_id) &
            boto3.dynamodb.conditions.Key("eventTime#eventId").between(
                window_start, sk
            )
        )
    )

    error_count = len(response.get("Items", []))

    if error_count >= ERROR_THRESHOLD:
        api_response = aws_api_table.query(
            KeyConditionExpression=(
                boto3.dynamodb.conditions.Key("accessKeyId").eq(access_key_id) &
                boto3.dynamodb.conditions.Key("eventTime#eventId").between(
                    window_start, sk
                )
            ),
            ScanIndexForward=False,
            Limit=5
        )
        recent_apis = [item.get("eventName", "") for item in api_response.get("Items", [])]

        send_slack_alert_scenario3(
            access_key_id=access_key_id,
            event_time=event_time,
            error_count=error_count,
            recent_apis=recent_apis,
        )


# DynamoDB 조회 헬퍼

def get_ip_country(access_key_id: str, sk: str) -> dict:
    try:
        res = ip_country_table.get_item(Key={
            "accessKeyId": access_key_id,
            "eventTime#eventId": sk,
        })
        return res.get("Item", {})
    except Exception as e:
        logger.warning(f"ip_country 조회 실패: {e}")
        return {}


def get_region(access_key_id: str, sk: str) -> dict:
    try:
        res = region_table.get_item(Key={
            "accessKeyId": access_key_id,
            "eventTime#eventId": sk,
        })
        return res.get("Item", {})
    except Exception as e:
        logger.warning(f"region 조회 실패: {e}")
        return {}


def get_user_agent(access_key_id: str, sk: str) -> dict:
    try:
        res = user_agent_table.get_item(Key={
            "accessKeyId": access_key_id,
            "eventTime#eventId": sk,
        })
        return res.get("Item", {})
    except Exception as e:
        logger.warning(f"user_agent 조회 실패: {e}")
        return {}


# 유틸

def get_str(image: dict, key: str) -> str:
    return image.get(key, {}).get("S", "")


def check_night_time(event_time: str) -> bool:
    """KST 기준 00:00 ~ 06:00 여부"""
    try:
        utc_dt = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
        kst_dt = utc_dt.astimezone(timezone(timedelta(hours=9)))
        return 0 <= kst_dt.hour < 6
    except Exception:
        return False


# Slack 알림

def send_slack_alert(
    access_key_id: str,
    event_name: str,
    event_source: str,
    event_time: str,
    source_ip: str,
    country_code: str,
    aws_region: str,
    ua_type: str,
    alert: dict,
):
    try:
        dt_utc = datetime.strptime(event_time, "%Y-%m-%dT%H:%M:%SZ")
        event_time_kst = (dt_utc + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        event_time_kst = event_time

    body = (
        f"*액세스 키 ID:* {access_key_id}\n"
        f"*이벤트명:* {event_name} ({event_source})\n"
        f"*발생 시각:* {event_time_kst} (KST)\n"
        f"*출발지 IP:* {source_ip} ({country_code})\n"
    )
    if aws_region:
        body += f"*리전:* {aws_region}\n"
    if ua_type:
        body += f"*UserAgent 타입:* {ua_type}\n"
    body += f"*상세:* {alert['detail']}"

    payload = {
        "channel": SLACK_CHANNEL_ID,
        "text": f"*:warning: [{alert['scenario']}] {alert['title']}*",
        "attachments": [
            {
                "color": "#FFD700",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": body,
                        }
                    }
                ]
            }
        ]
    }
    _post_slack(payload)


def send_slack_alert_scenario3(
    access_key_id: str,
    event_time: str,
    error_count: int,
    recent_apis: list,
):
    try:
        dt_utc = datetime.strptime(event_time, "%Y-%m-%dT%H:%M:%SZ")
        event_time_kst = (dt_utc + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        event_time_kst = event_time

    recent_apis_str = "\n".join([f" - {api}" for api in recent_apis]) if recent_apis else "없음"

    payload = {
        "channel": SLACK_CHANNEL_ID,
        "text": "*:warning: [시나리오 3] 짧은 시간 내 다수 AccessDenied 발생*",
        "attachments": [
            {
                "color": "#FFD700",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f"*액세스 키 ID:* {access_key_id}\n"
                                f"*발생 시각:* {event_time_kst} (KST)\n"
                                f"*상세:* {ERROR_WINDOW_MIN}분 내 AccessDenied {error_count}회 발생\n"
                                f"*최근 호출 API:*\n{recent_apis_str}"
                            )
                        }
                    }
                ]
            }
        ]
    }
    _post_slack(payload)


def _post_slack(payload: dict):
    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url="https://slack.com/api/chat.postMessage",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            body = json.loads(res.read().decode("utf-8"))
            if not body.get("ok"):
                logger.error(f"Slack 전송 실패: {body.get('error')}")
    except urllib.error.URLError as e:
        logger.error(f"Slack 요청 실패: {e}")
