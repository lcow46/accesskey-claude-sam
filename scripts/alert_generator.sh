#!/bin/bash
# macOS 기본 bash(3.2)는 set -u 상태에서 빈 배열을 "${arr[@]}"로 참조하면
# "unbound variable" 오류를 냅니다(4.4+에서 수정된 버그) - 그래서 -u는 빼둡니다.
set -o pipefail

# 첫 번째 인자로 프로필명을 넘기면 그 프로필을, 안 넘기면 기본(default) 프로필을 씁니다.
# 어느 쪽이든 반드시 장기 IAM 사용자 Access Key(AKIA로 시작)여야 합니다.
# SSO/AssumedRole 세션(ASIA로 시작)으로 실행하면 ref-table-processor가 전부 걸러내서
# DynamoDB에 아예 적재되지 않습니다.
PROFILE="${1:-}"
PROFILE_FLAG=()
[ -n "$PROFILE" ] && PROFILE_FLAG=(--profile "$PROFILE")

echo "사용 프로필: ${PROFILE:-<기본(default) 프로필>}"
CALLER_ARN=$(aws sts get-caller-identity "${PROFILE_FLAG[@]}" --query 'Arn' --output text)
echo "호출 Identity: $CALLER_ARN"
echo "$CALLER_ARN" | grep -q ':user/' \
  || echo "경고: IAM 사용자(AKIA)가 아닌 것 같습니다. Access Key가 AKIA로 시작하는지 확인하세요."
echo ""

echo "=== 시나리오 1, 2, 4, 5 테스트 ==="
echo ""

# 시나리오 1: 정찰 API 호출
# 주의: ALLOWED_COUNTRIES(기본 "KR")에 포함된 국가의 IP로 호출하면 시나리오 1/2는
# is_foreign_ip 조건이 거짓이 되어 DynamoDB 적재는 되어도 Slack 알림은 오지 않습니다.
# 알림까지 확인하려면 해외 리전 프록시/VPN을 쓰거나, 테스트 동안만 ALLOWED_COUNTRIES를
# 실제 발신 국가와 다른 값으로 바꿔서 재배포하세요.
echo "[시나리오 1] 정찰 API 호출..."
aws sts get-caller-identity "${PROFILE_FLAG[@]}"
USERNAME=$(aws iam get-user "${PROFILE_FLAG[@]}" --query 'User.UserName' --output text 2>/dev/null)
aws iam list-user-policies --user-name "$USERNAME" "${PROFILE_FLAG[@]}" 2>/dev/null || true
aws iam list-attached-user-policies --user-name "$USERNAME" "${PROFILE_FLAG[@]}" 2>/dev/null || true
echo "[시나리오 1] 완료"
echo ""

# 시나리오 2: 권한 상승 API 호출 (위와 같은 이유로 국가 조건 적용됨)
echo "[시나리오 2] 권한 상승 API 호출..."
aws iam create-user --user-name test-backdoor-user "${PROFILE_FLAG[@]}" 2>/dev/null || true
aws iam create-access-key --user-name test-backdoor-user "${PROFILE_FLAG[@]}" 2>/dev/null || true
aws iam attach-user-policy --user-name test-backdoor-user --policy-arn arn:aws:iam::aws:policy/AdministratorAccess "${PROFILE_FLAG[@]}" 2>/dev/null || true
aws iam put-user-policy --user-name test-backdoor-user --policy-name test-policy --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"*","Resource":"*"}]}' "${PROFILE_FLAG[@]}" 2>/dev/null || true
aws iam add-user-to-group --user-name test-backdoor-user --group-name test-group "${PROFILE_FLAG[@]}" 2>/dev/null || true
echo "[시나리오 2] 완료"
echo ""

# 시나리오 4: 비정상 리전에서 리소스 생성 API 호출 (국가와 무관, 리전만 확인)
echo "[시나리오 4] 오사카 리전에서 리소스 생성 API 호출..."
aws ec2 run-instances \
    --image-id ami-12345678 \
    --instance-type t2.micro \
    --region ap-northeast-3 \
    "${PROFILE_FLAG[@]}" 2>/dev/null || true
aws lambda create-function \
    --function-name test-backdoor \
    --runtime python3.12 \
    --role arn:aws:iam::123456789012:role/test \
    --handler index.handler \
    --zip-file fileb:///dev/null \
    --region ap-northeast-3 \
    "${PROFILE_FLAG[@]}" 2>/dev/null || true
echo "[시나리오 4] 완료"
echo ""

# 시나리오 5: 알려진 공격 도구 시그니처 UserAgent로 API 호출 (국가와 무관)
echo "[시나리오 5] 공격 도구 시그니처 UserAgent로 API 호출..."
AKIA_PROFILE="$PROFILE" python3 - << 'PYEOF'
import os
import boto3
from botocore.config import Config

profile = os.environ.get("AKIA_PROFILE") or None
session = boto3.Session(profile_name=profile)
client = session.client(
    "sts",
    region_name="ap-northeast-2",
    config=Config(user_agent_extra="pacu/1.0"),
)

try:
    client.get_caller_identity()
    print("GetCallerIdentity 호출 완료 (pacu UserAgent)")
except Exception as e:
    print(f"에러: {e}")
PYEOF
echo "[시나리오 5] 완료"
echo ""

echo "=== 테스트 완료 ==="
echo "CloudTrail 배치 전송(~5분) + 폴링 주기(기본 5분)가 지난 뒤에 확인하세요."
echo "빨리 확인하려면 ref-table-processor를 강제로 한 번 invoke 하세요:"
echo "  aws lambda invoke --function-name ref-table-processor-<Stage값> --payload '{}' --cli-binary-format raw-in-base64-out /tmp/poll-output.json"
