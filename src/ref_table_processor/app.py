import boto3
import gzip
import json
import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 중앙 CloudTrail 버킷이 다른 계정(Log Archive 등)에 있고, 그 계정의 버킷 정책을
# 직접 편집할 수 없는 환경(Control Tower SCP로 보호되는 경우 등)을 위한 옵션.
# 지정하면 이 역할을 assume해서 S3를 읽으므로, 버킷 정책 수정이 전혀 필요 없다.
CROSS_ACCOUNT_S3_ROLE_ARN = os.environ.get("CROSS_ACCOUNT_S3_ROLE_ARN", "")

# 폴링 모드: S3 이벤트 알림 없이, 이 함수가 주기 실행(EventBridge Schedule)될 때마다
# 스스로 버킷을 스캔해서 새 로그 파일을 찾는다. POLL_BUCKET_NAME이 설정된 경우에만 동작.
POLL_BUCKET_NAME = os.environ.get("POLL_BUCKET_NAME", "")
POLL_CURSOR_TABLE_NAME = os.environ.get("POLL_CURSOR_TABLE", "")

_default_s3_client = boto3.client("s3")
_cross_account_s3_client = None
_cross_account_s3_client_expiry = None

dynamodb = boto3.resource("dynamodb")


# DynamoDB Reference 테이블 5종
error_event_table = dynamodb.Table(os.environ["ERROR_EVENT_TABLE"])
ip_country_table = dynamodb.Table(os.environ["IP_COUNTRY_TABLE"])
aws_api_table = dynamodb.Table(os.environ["AWS_API_TABLE"])
region_table = dynamodb.Table(os.environ["REGION_TABLE"])
user_agent_table = dynamodb.Table(os.environ["USER_AGENT_TABLE"])

# 폴링 커서 저장용 (버킷 이벤트 알림을 쓰지 않는 폴링 모드에서만 사용)
poll_cursor_table = dynamodb.Table(POLL_CURSOR_TABLE_NAME) if POLL_CURSOR_TABLE_NAME else None

try:
    import geoip2.database
    geo_reader = geoip2.database.Reader("/opt/GeoLite2-City.mmdb")
except Exception as e:
    geo_reader = None
    logger.warning(f"GeoIP reader 초기화 실패: {e}")


# -----------------------------------------------------------------------
# 메인 핸들러 및 테이블 적재 로직
# -----------------------------------------------------------------------
def lambda_handler(event, context):
    # S3 이벤트 알림으로 호출된 경우 (데모 모드, 또는 버킷 정책이 허용되는 환경)
    if event.get("Records"):
        for record in event["Records"]:
            bucket = record["s3"]["bucket"]["name"]
            key = record["s3"]["object"]["key"]

            try:
                process_cloudtrail_file(bucket, key)
            except Exception as e:
                logger.error(f"파일 처리 실패 - bucket: {bucket}, key: {key}, error: {e}")
        return

    # EventBridge Schedule로 호출된 경우 (폴링 모드: S3 이벤트 알림을 쓰지 않음)
    if POLL_BUCKET_NAME:
        poll_bucket_for_new_logs()


# -----------------------------------------------------------------------
# 폴링 모드: S3 이벤트 알림 없이 버킷을 직접 스캔
# -----------------------------------------------------------------------
def poll_bucket_for_new_logs():
    """
    Organization Trail 버킷 키 구조(AWSLogs/<OrgId>/<AccountId>/CloudTrail/<Region>/...)를
    delimiter 기반으로 얕게 탐색해 (계정, 리전) 조합을 자동으로 찾아내고, 각 조합별로
    마지막으로 처리한 키 이후의 신규 객체만 순서대로 가져와 처리한다. 계정/리전 목록을
    미리 설정해둘 필요가 없어 조직에 계정이 추가되어도 별도 설정 변경이 필요 없다.
    """
    s3 = get_s3_client()

    for org_prefix in list_common_prefixes(s3, "AWSLogs/"):
        for account_prefix in list_common_prefixes(s3, org_prefix):
            cloudtrail_prefix = f"{account_prefix}CloudTrail/"
            for region_prefix in list_common_prefixes(s3, cloudtrail_prefix):
                poll_prefix(s3, region_prefix)


def list_common_prefixes(s3, prefix: str) -> list:
    """prefix 바로 아래 단계의 '폴더'만 나열한다 (객체 내용은 가져오지 않아 가볍다)."""
    prefixes = []
    kwargs = {"Bucket": POLL_BUCKET_NAME, "Prefix": prefix, "Delimiter": "/"}
    while True:
        resp = s3.list_objects_v2(**kwargs)
        prefixes.extend(p["Prefix"] for p in resp.get("CommonPrefixes", []))
        if not resp.get("IsTruncated"):
            break
        kwargs["ContinuationToken"] = resp["NextContinuationToken"]
    return prefixes


def poll_prefix(s3, prefix: str):
    """
    이 prefix(계정+리전 단위) 안에서 마지막으로 처리한 키 이후의 .json.gz 객체를 순서대로
    가져와 처리한다. 같은 계정+리전 안에서는 키에 포함된 연/월/일/타임스탬프가 문자열
    정렬 순서와 일치하므로, StartAfter만으로 안전하게 "신규 파일만" 가져올 수 있다.
    """
    cursor = get_poll_cursor(prefix)
    last_seen_key = cursor
    continuation_token = None

    while True:
        kwargs = {"Bucket": POLL_BUCKET_NAME, "Prefix": prefix}
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        elif cursor:
            kwargs["StartAfter"] = cursor

        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".json.gz"):
                continue
            try:
                process_cloudtrail_file(POLL_BUCKET_NAME, key)
            except Exception as e:
                logger.error(f"파일 처리 실패 - key: {key}, error: {e}")
            last_seen_key = key

        if not resp.get("IsTruncated"):
            break
        continuation_token = resp["NextContinuationToken"]

    if last_seen_key != cursor:
        save_poll_cursor(prefix, last_seen_key)


def get_poll_cursor(prefix: str) -> str:
    if not poll_cursor_table:
        return ""
    try:
        res = poll_cursor_table.get_item(Key={"prefix": prefix})
        return res.get("Item", {}).get("lastKey", "")
    except Exception as e:
        logger.warning(f"폴링 커서 조회 실패 - prefix: {prefix}, error: {e}")
        return ""


def save_poll_cursor(prefix: str, last_key: str):
    if not poll_cursor_table or not last_key:
        return
    poll_cursor_table.put_item(Item={"prefix": prefix, "lastKey": last_key})


def get_s3_client():
    """
    CROSS_ACCOUNT_S3_ROLE_ARN이 설정되어 있으면 그 역할을 assume해서 발급받은 임시
    자격증명으로 S3 클라이언트를 만든다 (중앙 버킷을 소유한 계정의 IAM 역할이므로,
    버킷 정책 수정 없이 같은 계정 접근처럼 동작). 설정되어 있지 않으면 기존과 동일하게
    이 함수 자신의 실행 역할로 S3에 접근한다 (같은 계정 버킷 또는 버킷 정책으로 이미
    크로스 계정 접근이 허용된 경우).
    """
    if not CROSS_ACCOUNT_S3_ROLE_ARN:
        return _default_s3_client

    global _cross_account_s3_client, _cross_account_s3_client_expiry
    now = datetime.now(timezone.utc)
    needs_refresh = (
        _cross_account_s3_client is None
        or _cross_account_s3_client_expiry is None
        or now >= _cross_account_s3_client_expiry - timedelta(minutes=5)
    )

    if needs_refresh:
        sts_client = boto3.client("sts")
        assumed = sts_client.assume_role(
            RoleArn=CROSS_ACCOUNT_S3_ROLE_ARN,
            RoleSessionName="ref-table-processor",
        )
        creds = assumed["Credentials"]
        _cross_account_s3_client = boto3.client(
            "s3",
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )
        _cross_account_s3_client_expiry = creds["Expiration"]

    return _cross_account_s3_client


def process_cloudtrail_file(bucket: str, key: str):
    response = get_s3_client().get_object(Bucket=bucket, Key=key)
    compressed = response["Body"].read()

    with gzip.GzipFile(fileobj=__import__("io").BytesIO(compressed)) as f:
        log_data = json.loads(f.read().decode("utf-8"))

    records = log_data.get("Records", [])
    logger.info(f"총 {len(records)}개 이벤트 파싱 시작")

    for record in records:
        access_key_id = extract_access_key_id(record)

        # AKIA로 시작하는 IAM 사용자 액세스키만 처리
        if not access_key_id or not access_key_id.startswith("AKIA"):
            continue

        try:
            process_event(record, access_key_id)
        except Exception as e:
            logger.error(f"이벤트 처리 실패 - eventId: {record.get('eventID')}, error: {e}")


def process_event(record: dict, access_key_id: str):
    event_id = record.get("eventID", "")
    event_time = record.get("eventTime", "")
    sk = f"{event_time}#{event_id}"

    ttl = int((datetime.now(timezone.utc) + timedelta(days=7)).timestamp())

    write_region(access_key_id, sk, record, ttl)
    write_aws_api(access_key_id, sk, record, ttl)
    write_user_agent(access_key_id, sk, record, ttl)

    # errorCode가 있는 이벤트만, ref_error_event에 적재
    if record.get("errorCode"):
        write_error_event(access_key_id, sk, record, ttl)

    # sourceIPAddress가 AWS 서비스 도메인이 아닌 경우만, GeoIP 조회
    source_ip = record.get("sourceIPAddress", "")
    if source_ip and not source_ip.endswith(".amazonaws.com"):
        write_ip_country(access_key_id, sk, source_ip, ttl)


# 테이블별 적재 함수 모음

def write_error_event(access_key_id: str, sk: str, record: dict, ttl: int):
    item = {
        "accessKeyId": access_key_id,
        "eventTime#eventId": sk,
        "eventName": record.get("eventName", ""),
        "errorCode": record.get("errorCode", ""),
        "errorMessage": record.get("errorMessage", ""),
        "ttl": ttl,
    }
    error_event_table.put_item(Item=item)


def write_ip_country(access_key_id: str, sk: str, source_ip: str, ttl: int):
    country_code = ""
    city = ""

    if geo_reader:
        try:
            geo = geo_reader.city(source_ip)
            country_code = geo.country.iso_code or ""
            city = geo.city.name or ""
        except Exception as e:
            logger.warning(f"GeoIP 조회 실패 - IP: {source_ip}, error: {e}")

    item = {
        "accessKeyId": access_key_id,
        "eventTime#eventId": sk,
        "sourceIPAddress": source_ip,
        "countryCode": country_code,
        "city": city,
        "ttl":ttl,
    }
    ip_country_table.put_item(Item=item)


def write_aws_api(access_key_id: str, sk: str, record: dict, ttl: int):
    item = {
        "accessKeyId": access_key_id,
        "eventTime#eventId": sk,
        "eventName": record.get("eventName", ""),
        "eventSource": record.get("eventSource", ""),
        "ttl": ttl,
    }
    aws_api_table.put_item(Item=item)


def write_region(access_key_id: str, sk: str, record: dict, ttl: int):
    item = {
        "accessKeyId": access_key_id,
        "eventTime#eventId": sk,
        "awsRegion": record.get("awsRegion", ""),
        "ttl": ttl,
    }
    region_table.put_item(Item=item)


def write_user_agent(access_key_id: str, sk: str, record: dict, ttl: int):
    user_agent = record.get("userAgent", "")
    item = {
        "accessKeyId": access_key_id,
        "eventTime#eventId": sk,
        "userAgent": user_agent,
        "userAgentType": classify_user_agent(user_agent),
        "ttl": ttl,
    }
    user_agent_table.put_item(Item=item)


# 헬퍼 함수 1: userIdentity에서 accessKeyId 추출
def extract_access_key_id(record: dict) -> str:
    user_identity = record.get("userIdentity", {})
    return user_identity.get("accessKeyId", "")

# 헬퍼 함수 2: userAgent 문자열 분류
def classify_user_agent(user_agent: str) -> str:
    ua = user_agent.lower()

    if not ua:
        return "Unknown"
    if "aws-cli" in ua:
        return "CLI"
    if any(sdk in ua for sdk in ["boto3", "botocore", "aws-sdk", "aws-java-sdk", "aws-go-sdk"]):
        return "SDK"
    if any(svc in ua for svc in ["aws-internal", "signin.amazonaws.com", "console.amazonaws.com"]):
        return "Service"
    if any(browser in ua for browser in ["mozilla", "chrome", "safari", "firefox"]):
        return "Browser"

    return "Unknown"
