import boto3
import gzip
import json
import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3_client = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")


# DynamoDB Reference 테이블 5종
error_event_table = dynamodb.Table(os.environ["ERROR_EVENT_TABLE"])
ip_country_table = dynamodb.Table(os.environ["IP_COUNTRY_TABLE"])
aws_api_table = dynamodb.Table(os.environ["AWS_API_TABLE"])
region_table = dynamodb.Table(os.environ["REGION_TABLE"])
user_agent_table = dynamodb.Table(os.environ["USER_AGENT_TABLE"])

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
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]

        try:
            process_cloudtrail_file(bucket, key)
        except Exception as e:
            logger.error(f"파일 처리 실패 - bucket: {bucket}, key: {key}, error: {e}")


def process_cloudtrail_file(bucket: str, key: str):
    response = s3_client.get_object(Bucket=bucket, Key=key)
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
