import boto3
import datetime
import os
import json
import zipfile
import logging
import tarfile
import requests

log = logging.getLogger()
log.setLevel(logging.INFO)

logging.getLogger('urllib3.connectionpool').setLevel(logging.WARNING)
logging.getLogger('requests.packages.urllib3.connectionpool').setLevel(logging.WARNING)

SECRET_NAME = os.environ["SECRET_NAME"]
LAYER_NAME = os.environ.get("LAYER_NAME", "geoip-mmdb")
PROCESSOR_FUNCTION_NAME = os.environ["PROCESSOR_FUNCTION_NAME"]

EDITION_ID = "GeoLite2-City"
LOCAL_TMP  = "/tmp/"
CHUNK_SIZE = 1024 * 1024  # 1MB

lambda_client  = boto3.client("lambda")
secrets_client = boto3.client("secretsmanager")

# 콜드스타트 시 1회만 조회 (컨테이너 재사용 시 캐싱)
_license_key: str | None = None

def get_license_key() -> str:
    global _license_key
    if _license_key is None:
        response = secrets_client.get_secret_value(SecretId=SECRET_NAME)
        secret = json.loads(response["SecretString"])
        _license_key = secret["MAXMIND_LICENSE_KEY"]
        log.info("Secrets Manager에서 라이선스 키 로드 완료")
    return _license_key


def get_latest_layer_hash() -> str | None:
    try:
        response = lambda_client.list_layer_versions(LayerName=LAYER_NAME)
        versions = response.get("LayerVersions", [])
        if not versions:
            return None
        latest = versions[0]
        description = latest.get("Description", "")
        # Description 형식: "hash=<sha256>"
        if description.startswith("hash="):
            return description.split("=", 1)[1]
        return None
    except lambda_client.exceptions.ResourceNotFoundException:
        return None


def download_mmdb(tar_path: str, mmdb_path: str):
    """MaxMind tar.gz 다운로드 후 mmdb 파일만 추출"""
    license_key  = get_license_key()
    download_url = (
        f"https://download.maxmind.com/app/geoip_download"
        f"?edition_id={EDITION_ID}&license_key={license_key}&suffix=tar.gz"
    )

    log.info("mmdb 다운로드 시작")
    with open(tar_path, "wb") as f:
        with requests.get(download_url, stream=True) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    f.write(chunk)

    log.info("mmdb 압축 해제 시작")
    with tarfile.open(tar_path, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name.endswith(".mmdb"):
                member.name = os.path.basename(member.name)
                tar.extract(member, LOCAL_TMP)
                break

    log.info(f"mmdb 추출 완료: {mmdb_path}")


def build_zip(mmdb_path: str, zip_path: str):
    """mmdb → zip 패키징"""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(mmdb_path, os.path.basename(mmdb_path))
    size_mb = os.path.getsize(zip_path) / 1024 / 1024
    log.info(f"zip 패키징 완료: {zip_path} ({size_mb:.1f} MB)")


def publish_layer(zip_path: str, db_hash: str) -> str:
    """zip 파일을 직접 읽어 Layer 새 버전 발행"""
    with open(zip_path, "rb") as f:
        zip_bytes = f.read()

    size_mb = len(zip_bytes) / 1024 / 1024
    log.info(f"Layer 발행 시작 ({size_mb:.1f} MB)")

    if size_mb > 50:
        raise ValueError(f"zip 크기({size_mb:.1f}MB)가 직접 업로드 한도(50MB)를 초과합니다.")

    response = lambda_client.publish_layer_version(
        LayerName=LAYER_NAME,
        Description=f"hash={db_hash}",
        Content={"ZipFile": zip_bytes},
        CompatibleRuntimes=["python3.13", "python3.14"],
        CompatibleArchitectures=["x86_64", "arm64"],
    )

    layer_arn = response["LayerVersionArn"]
    log.info(f"Layer 발행 완료: {layer_arn}")
    return layer_arn


def lambda_handler(event, context):
    start = datetime.datetime.now()
    log.info("루틴 시작")

    # 1. MaxMind 최신 해시 확인
    license_key = get_license_key()
    sha256_url  = (
        f"https://download.maxmind.com/app/geoip_download"
        f"?edition_id={EDITION_ID}&license_key={license_key}&suffix=tar.gz.sha256"
    )
    db_hash = requests.get(sha256_url).content.decode("utf-8").split()[0]
    log.info(f"MaxMind 최신 hash: {db_hash}")

    # 2. 현재 Layer 해시와 비교
    current_hash = get_latest_layer_hash()
    log.info(f"현재 Layer hash: {current_hash}")

    if db_hash == current_hash:
        log.info("동일한 버전이 이미 Layer에 존재합니다. 종료.")
        return {"status": "skipped", "hash": db_hash}

    # 3. 다운로드 및 추출
    tar_path  = LOCAL_TMP + f"{EDITION_ID}.tar.gz"
    mmdb_path = LOCAL_TMP + f"{EDITION_ID}.mmdb"
    zip_path  = LOCAL_TMP + f"{EDITION_ID}.zip"

    download_mmdb(tar_path, mmdb_path)

    # 4. zip 패키징
    build_zip(mmdb_path, zip_path)

    # 5. Layer 발행
    layer_arn = publish_layer(zip_path, db_hash)

    current = lambda_client.get_function_configuration(
        FunctionName=PROCESSOR_FUNCTION_NAME
    )
    existing_layers = [
        l["Arn"] for l in current.get("Layers", [])
        if LAYER_NAME not in l["Arn"]  # geoip 레이어 빼고
    ]

    lambda_client.update_function_configuration(
        FunctionName=PROCESSOR_FUNCTION_NAME,
        Layers=existing_layers + [layer_arn]
    )

    elapsed_ms = (datetime.datetime.now() - start).total_seconds() * 1000
    log.info(f"경과 시간: {elapsed_ms:.0f}ms")
    if context:
        log.info(f"남은 시간: {context.get_remaining_time_in_millis()}ms")

    return {"status": "updated", "layer_arn": layer_arn, "hash": db_hash}
