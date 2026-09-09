# AWS SAM 배포 가이드

이 문서는 [docs/architecture.md](architecture.md)에서 설명한 Access Key 이상탐지 아키텍처 중
**Audit 계정 구성요소**(3개 Lambda, DynamoDB Reference Table 5종, DynamoDB Streams, 알림 연동)를
AWS SAM CLI로 빌드·배포하는 방법을 처음부터 끝까지 안내합니다.

> 알림 채널은 Slack 또는 Microsoft Teams 중 선택할 수 있습니다(`NotificationProvider` 파라미터).
> 이 문서는 두 채널에 공통되는 빌드/배포 절차를 다루고, 채널별로 다른 사전 준비·파라미터 값·
> 트러블슈팅은 [docs/notifications/slack.md](notifications/slack.md) /
> [docs/notifications/teams.md](notifications/teams.md)에 각각 정리했습니다. 3단계(Secrets Manager
> 준비)와 5단계(배포 파라미터)를 진행하기 전에 사용할 채널의 문서를 먼저 읽어주세요.

## 0. 이 SAM 앱이 배포하는 범위

Control Tower / Organization Trail 자체는 조직 전체에 걸친 별도 설정이라 하나의 SAM 스택으로
만들 수 없습니다. 그래서 이 프로젝트는 **Audit 계정에서 관리하는 부분**을 SAM으로 구현합니다.

| 리소스 | 설명 |
|---|---|
| `ref-table-processor` Lambda | S3(CloudTrail 로그) → Reference Table 5종 적재 |
| `ref-suspicious-detector` Lambda | DynamoDB Streams → 탐지 시나리오 평가 → Slack/Teams 알림 |
| `geoip-layer-builder` Lambda | MaxMind mmdb 갱신 확인 → Lambda Layer 재발행 → `ref-table-processor`에 자동 연결 (주기 실행) |
| DynamoDB 테이블 5종 | `ref_ip_country`, `ref_region`, `ref_user_agent`, `ref_error_event`, `ref_aws_api` |
| (선택) 데모용 S3 버킷 + CloudTrail | Organization 환경이 없어도 엔드투엔드로 테스트할 수 있도록 하는 옵션 |

실제 운영 환경(Control Tower + Organization Trail)에서는 CloudTrail 로그가 **다른 계정(Log
Archive)의 S3 버킷**에 쌓이므로, 그 버킷에서 이 스택의 Lambda를 호출하도록 하는 크로스 계정 설정이
별도로 필요합니다. 이 가이드의 "5. 배포 후 수동 설정"에서 다룹니다.

## 1. 사전 준비물

### 1-1. 도구 설치

```bash
# AWS CLI v2 (설치 여부 확인)
aws --version

# SAM CLI (설치 여부 확인)
sam --version
```

- AWS CLI가 없다면 [AWS 공식 설치 가이드](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)를 참고하세요.
- SAM CLI가 없다면 [SAM CLI 설치 가이드](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)를 참고하세요. macOS는 `brew install aws-sam-cli`로도 설치할 수 있습니다.

```bash
aws configure
```

배포를 실행할 IAM 사용자/역할에는 최소한 다음 권한이 필요합니다: CloudFormation, Lambda, DynamoDB,
IAM(역할 생성), S3(SAM 배포용 버킷 및 데모 버킷), CloudTrail(데모 모드 사용 시), EventBridge,
Secrets Manager(읽기), CloudWatch Logs.

### 1-2. Docker 불필요 — 빌드 방식 안내

이 프로젝트는 **Docker를 전혀 사용하지 않습니다.** 사내 정책 등으로 Docker를 쓸 수 없는
환경을 위해, `template.yaml`의 세 Lambda 함수 모두 SAM의 커스텀 빌드 방식
(`Metadata.BuildMethod: makefile`)으로 구성되어 있고, 각 함수 소스 폴더(`src/*/Makefile`)에
빌드 스크립트가 들어있습니다.

일반적으로 `sam build`는 **로컬에 설치된 Python 인터프리터**로 의존성을 설치하기 때문에, 로컬
Python 버전이 Lambda 런타임(`python3.14`)과 다르면 `Binary validation failed ...` 오류가
나거나, 이를 피하려면 `sam build --use-container`(Docker 필요)를 써야 하는 것이 SAM의 기본
동작입니다. 이 프로젝트는 그 대신 각 함수의 Makefile 안에서
`pip install --platform manylinux2014_x86_64 --python-version 3.14 --abi cp314 --only-binary=:all:`
같은 **pip의 크로스 플랫폼 다운로드 옵션**을 직접 사용해, 로컬 Python 버전이 무엇이든 상관없이
Lambda 런타임(Linux x86_64, Python 3.14)에 맞는 wheel을 PyPI에서 바로 받아옵니다. 그 결과
`make`와 `pip`(그리고 PyPI 접속 가능한 네트워크)만 있으면 되고, Docker도, 정확한 버전의 로컬
Python도 필요하지 않습니다.

- macOS/Linux에는 보통 `make`가 기본 설치되어 있습니다. (`make --version`으로 확인)
- 사내 프록시/미러 때문에 `pypi.org`에 직접 접속할 수 없다면, `pip.conf`(또는 `PIP_INDEX_URL`
  환경변수)로 사내 PyPI 미러를 가리키도록 설정하세요. 사내 미러에도 `geoip2`, `maxminddb`,
  `requests`의 `manylinux2014_x86_64` / `cp314` wheel이 미러링되어 있어야 이 빌드 방식이
  그대로 동작합니다.
- 다른 아키텍처(arm64)로 배포하려면 Makefile의 `PLATFORM` 값을 `manylinux2014_aarch64`로,
  `template.yaml`의 `Globals.Function.Architectures`를 `[arm64]`로 함께 바꿔야 합니다.

### 1-3. Lambda 런타임 버전이 지원되지 않는 경우

배포하려는 리전/계정에서 아직 `python3.14` Lambda 런타임을 지원하지 않는다면, `template.yaml`의
`Globals.Function.Runtime`을 `python3.13`으로 낮추고, `src/ref_table_processor/Makefile`과
`src/geoip_layer_builder/Makefile`의 `PY_VERSION`/`PY_ABI` 값도 `3.13`/`cp313`으로 함께
맞춰주세요. (Docker를 안 쓰는 이 빌드 방식에서는 코드의 로컬 Python 버전이 아니라, Makefile에
적힌 `PY_VERSION`/`PY_ABI` 값이 실제로 다운로드되는 wheel의 대상 버전을 결정합니다.)

### 1-4. 알림 채널 준비 (Slack 또는 Teams)

사용할 채널에 맞는 문서를 먼저 진행해서 알림 자격 증명(Slack Bot Token 또는 Teams Webhook
URL)을 확보해두세요.

- Slack을 사용한다면 → [docs/notifications/slack.md](notifications/slack.md)
- Microsoft Teams를 사용한다면 → [docs/notifications/teams.md](notifications/teams.md)

### 1-5. MaxMind 계정 준비

1. [MaxMind](https://www.maxmind.com)에서 계정을 만들고 GeoLite2 라이선스 키를 발급받습니다.
2. 발급된 라이선스 키 문자열을 확보해둡니다.

## 2. 프로젝트 구조

```
accesskey-claude-sam/
├── template.yaml                     # SAM 템플릿 (전체 인프라 정의)
├── src/
│   ├── ref_table_processor/
│   │   ├── app.py
│   │   ├── requirements.txt          # geoip2, maxminddb
│   │   └── Makefile                  # Docker 없이 빌드하기 위한 커스텀 빌드 스크립트
│   ├── suspicious_detector/
│   │   ├── app.py                    # 표준 라이브러리 + boto3만 사용
│   │   └── Makefile                  # 의존성 없음 — 소스 복사만 수행
│   └── geoip_layer_builder/
│       ├── app.py
│       ├── requirements.txt          # requests
│       └── Makefile
├── events/                           # sam local invoke 대체 테스트용 샘플 이벤트
└── docs/
    ├── architecture.md
    ├── sam-deployment-guide.md       # 이 문서
    └── notifications/
        ├── slack.md
        └── teams.md
```

## 3. 배포 전 준비: Secrets Manager 시크릿 생성

알림 자격 증명(Slack Bot Token 또는 Teams Webhook URL)과 MaxMind 라이선스 키는 절대
`template.yaml`이나 파라미터에 평문으로 넣지 않고, **미리 Secrets Manager에 직접 생성**합니다.

알림 채널용 시크릿 생성 명령은 채널마다 시크릿 값의 JSON 형태가 다르므로
[docs/notifications/slack.md](notifications/slack.md) 또는
[docs/notifications/teams.md](notifications/teams.md)의 "Secrets Manager 시크릿 생성" 절을
참고하세요. (시크릿 이름 기본값: `accesskey-detector/notification-credential`)

MaxMind 라이선스 키는 채널과 무관하게 공통으로 아래처럼 생성합니다.

```bash
aws secretsmanager create-secret \
  --name "accesskey-detector/maxmind-license-key" \
  --secret-string '{"MAXMIND_LICENSE_KEY":"여기에-실제-라이선스키"}'
```

시크릿 이름을 위 기본값과 다르게 만들었다면, 배포 시 `NotificationSecretName` /
`MaxMindSecretName` 파라미터로 그 이름을 지정하면 됩니다.

## 4. 빌드

프로젝트 루트(`template.yaml`이 있는 위치)에서 실행합니다. Docker는 필요하지 않습니다.

```bash
sam build
```

`template.yaml`에 이미 각 함수마다 `Metadata: BuildMethod: makefile`이 지정되어 있으므로,
`sam build`는 자동으로 `src/*/Makefile`을 실행해 의존성을 내려받습니다. (1-2절 참고) 출력에
`Running CustomMakeBuilder:MakeBuild`가 보이면 이 방식으로 빌드되고 있는 것입니다.

빌드가 성공하면 `.aws-sam/build/`에 각 함수의 배포 패키지가 생성됩니다. `ref_table_processor`,
`geoip_layer_builder` 아래에 `geoip2`, `maxminddb`, `requests` 등 `requirements.txt`의
의존성이 (Linux x86_64용으로) 함께 패키징된 것을 확인할 수 있습니다.

```bash
file .aws-sam/build/RefTableProcessorFunction/maxminddb/*.so
# ELF 64-bit LSB shared object, x86-64 ... 로 나오면 정상 (macOS/Windows에서 빌드해도 Linux용 바이너리)
```

## 5. 배포

### 5-1. 처음 배포하는 경우

```bash
sam deploy --guided
```

대화형으로 아래 항목들을 물어봅니다.

| 항목 | 권장 값/설명 |
|---|---|
| Stack Name | 예: `accesskey-anomaly-detector` |
| AWS Region | 예: `ap-northeast-2` |
| Parameter Stage | `dev`, `prod` 등 환경 구분자 |
| Parameter NotificationProvider | `slack` 또는 `teams` |
| Parameter SlackChannelId | Slack 채널 ID (예: `C0123456789`). `NotificationProvider=teams`면 비워둠 |
| Parameter NotificationSecretName | 3단계에서 만든 알림 자격 증명 시크릿 이름 (기본값 그대로 써도 됨) |
| Parameter MaxMindSecretName | 3단계에서 만든 시크릿 이름 (기본값 그대로 써도 됨) |
| Parameter AllowedCountries | 허용 국가코드, 콤마 구분 (예: `KR`) |
| Parameter AllowedRegions | 허용 리전, 콤마 구분 (예: `ap-northeast-2`) |
| Parameter ErrorThreshold | 시나리오 3 임계값 (기본 5) |
| Parameter ErrorWindowMinutes | 시나리오 3 시간 윈도우(분) (기본 5) |
| Parameter GeoIpUpdateSchedule | GeoIP DB 갱신 주기 (기본 `rate(7 days)`) |
| Parameter DeployDemoCloudTrail | **처음 테스트해보는 것이라면 `true`** 권장 (아래 6절 참고) |
| Parameter ExistingCloudTrailBucketName / AccountId | `DeployDemoCloudTrail=false`일 때만 입력 |
| Confirm changes before deploy | `Y` 권장 (변경 내용을 보고 승인) |
| Allow SAM CLI IAM role creation | `Y` (Lambda 실행 역할 등을 생성해야 함) |
| Disable rollback | `N` |
| Save arguments to configuration file | `Y` → 다음부터는 `sam deploy`만으로 재배포 가능 |

배포가 끝나면 `Outputs`에 함수 이름/ARN, 테이블 이름, (데모 모드면) 버킷 이름이 출력됩니다.

### 5-2. 이후 재배포

```bash
sam build && sam deploy
```

(`--guided`로 저장된 `samconfig.toml`을 그대로 사용합니다.)

## 6. 배포 모드: 데모 모드 vs 기존 Organization Trail 연동

### 6-1. 데모 모드 (`DeployDemoCloudTrail=true`, 기본값)

이 스택이 자체 S3 버킷과 단일 계정 CloudTrail(멀티 리전)을 함께 만들어서, Organization Trail
없이도 혼자서 전체 파이프라인을 끝까지 테스트해볼 수 있습니다. **처음 이 아키텍처를 구현해보는
용도라면 이 모드로 시작하는 것을 권장합니다.**

데모 모드에서는 S3 이벤트 알림이 같은 계정/같은 스택 안에서 자동으로 연결되므로 별도 수동 설정이
필요 없습니다. (7단계로 바로 진행)

### 6-2. 기존 Organization Trail 연동 모드 (`DeployDemoCloudTrail=false`)

실제 운영 중인 Control Tower/Organization Trail의 Log Archive 계정 버킷과 연동하려면 이 모드를
사용합니다. `ExistingCloudTrailBucketName`(중앙 버킷 이름)과
`ExistingCloudTrailBucketAccountId`(그 버킷을 소유한 계정 ID)를 지정해야 합니다.

이 모드는 버킷이 **다른 AWS 계정**에 있으므로, CloudFormation 한 스택만으로는 양쪽을 다 설정할 수
없습니다. 이 스택은 "Lambda가 그 버킷으로부터의 호출을 허용한다"는 쪽(Lambda 리소스 정책)만
자동으로 만들고, 아래 두 가지는 **Log Archive 계정에서 별도로** 실행해야 합니다.

**(1) Log Archive 계정에서: 버킷 정책에 Audit 계정의 Lambda 실행 역할 읽기 권한 추가**

먼저 이 스택의 `RefTableProcessorFunction` 실행 역할 ARN을 Audit 계정에서 확인합니다.

```bash
aws cloudformation describe-stack-resource \
  --stack-name <스택이름> \
  --logical-resource-id RefTableProcessorFunctionRole \
  --query "StackResourceDetail.PhysicalResourceId" --output text
```

Log Archive 계정에서, 위에서 확인한 역할 ARN에 대해 버킷 정책에 아래와 같은 statement를
추가합니다 (기존 정책에 병합하세요).

```json
{
  "Sid": "AllowAuditAccountRefTableProcessorRead",
  "Effect": "Allow",
  "Principal": {
    "AWS": "arn:aws:iam::<Audit계정ID>:role/<위에서 확인한 역할 이름>"
  },
  "Action": "s3:GetObject",
  "Resource": "arn:aws:s3:::<중앙버킷이름>/*"
}
```

**(2) Log Archive 계정에서: S3 이벤트 알림 등록**

Audit 계정에서 배포된 `RefTableProcessorFunction`의 ARN을 확인한 뒤,

```bash
aws cloudformation describe-stacks --stack-name <스택이름> \
  --query "Stacks[0].Outputs[?OutputKey=='RefTableProcessorFunctionArn'].OutputValue" --output text
```

Log Archive 계정에서 아래처럼 알림을 등록합니다. (기존 NotificationConfiguration이 있다면
`get-bucket-notification-configuration`으로 먼저 받아서 병합한 뒤 put 하세요.)

```bash
aws s3api put-bucket-notification-configuration \
  --bucket <중앙버킷이름> \
  --notification-configuration '{
    "LambdaFunctionConfigurations": [
      {
        "LambdaFunctionArn": "<위에서 확인한 RefTableProcessorFunctionArn>",
        "Events": ["s3:ObjectCreated:Put"],
        "Filter": {
          "Key": {
            "FilterRules": [
              { "Name": "suffix", "Value": ".json.gz" }
            ]
          }
        }
      }
    ]
  }'
```

## 7. 배포 후 필수 수동 단계: GeoIP Layer 최초 생성

`geoip-layer-builder`는 EventBridge 스케줄로 주기 실행되지만, **첫 배포 직후에는 Layer가 아직
없으므로 `ref-table-processor`가 GeoIP 조회 없이(국가/도시 정보 빈 값으로) 동작**합니다. 배포
직후 한 번은 수동으로 실행해서 Layer를 만들고 연결해주세요.

```bash
aws lambda invoke \
  --function-name geoip-layer-builder-<Stage값> \
  --cli-binary-format raw-in-base64-out \
  /tmp/geoip-layer-builder-output.json
cat /tmp/geoip-layer-builder-output.json
```

`{"status": "updated", ...}`가 나오면 성공입니다. 이후 `ref-table-processor`의 설정을 확인해서
Layer가 붙었는지 확인할 수 있습니다.

```bash
aws lambda get-function-configuration \
  --function-name ref-table-processor-<Stage값> \
  --query "Layers"
```

## 8. 동작 확인

### 8-1. 데모 모드: 테스트 이벤트 발생시키기

데모 모드로 배포했다면, 실제로 아무 IAM 사용자의 Access Key로 AWS CLI 명령을 몇 번 호출해보면
(예: `aws sts get-caller-identity`, `aws iam list-users`) 약 5분 내(CloudTrail 배치 주기) 해당
계정의 CloudTrail 로그가 데모 버킷에 쌓이고, `ref-table-processor`가 트리거됩니다.

### 8-2. DynamoDB 테이블 확인

```bash
aws dynamodb scan --table-name ref_aws_api-<Stage값> --max-items 5
```

데이터가 쌓이고 있다면 파이프라인 앞단(S3 → ref-table-processor → DynamoDB)이 정상 동작하는
것입니다.

### 8-3. CloudWatch Logs로 두 Lambda 로그 확인

```bash
sam logs -n ref-table-processor-<Stage값> --stack-name <스택이름> --tail
```

```bash
sam logs -n ref-suspicious-detector-<Stage값> --stack-name <스택이름> --tail
```

### 8-4. 알림 발송 테스트

허용 국가 외부에서 호출한 것처럼 조건을 맞추기는 어려우므로, 가장 쉬운 검증 방법은 `ALLOWED_COUNTRIES`
환경변수를 일부러 실제 발신 국가와 다르게 좁혀서(예: 테스트 동안만 `US`로) `sam deploy`를 다시
실행한 뒤, `GetCallerIdentity`를 호출해보고 시나리오 1 알림이 오는지 확인하는 것입니다. 확인 후에는
반드시 원래 값으로 되돌려서 재배포하세요. 채널별 세부 테스트 방법은
[docs/notifications/slack.md](notifications/slack.md) /
[docs/notifications/teams.md](notifications/teams.md)의 "동작 확인" 절을 참고하세요.

## 9. 로컬 테스트 (Docker 없이)

`sam local invoke`/`sam local start-lambda`는 Lambda 실행 환경을 로컬 컨테이너로 재현하기
때문에 Docker가 반드시 필요합니다. 이 환경에서는 Docker를 쓸 수 없으므로 아래 두 가지
대안으로 테스트합니다.

### 9-1. 배포된 함수에 직접 이벤트를 보내서 테스트 (권장)

가장 확실한 방법은 5단계까지 배포한 뒤, `events/` 디렉터리의 샘플 이벤트를 **실제 배포된
함수**에 `aws lambda invoke`로 직접 보내보는 것입니다. Lambda 실행 환경 자체(Linux, 정확한
런타임 버전, 실제 IAM 권한)에서 돌아가므로 오히려 `sam local invoke`보다 신뢰도가 높습니다.

```bash
# ref-suspicious-detector: DynamoDB Streams INSERT 이벤트를 흉내낸 샘플로 테스트
aws lambda invoke \
  --function-name ref-suspicious-detector-<Stage값> \
  --cli-binary-format raw-in-base64-out \
  --payload file://events/dynamodb-stream-aws-api-insert.json \
  /tmp/detector-output.json
cat /tmp/detector-output.json
```

```bash
# ref-table-processor: events/s3-put-event.json의 bucket/key 값을
# 실제 존재하는 CloudTrail 로그 객체로 바꾼 뒤 테스트
aws lambda invoke \
  --function-name ref-table-processor-<Stage값> \
  --cli-binary-format raw-in-base64-out \
  --payload file://events/s3-put-event.json \
  /tmp/processor-output.json
cat /tmp/processor-output.json
```

테스트 후에는 CloudWatch Logs(8-3절)로 실제 동작을 확인하세요.

### 9-2. 순수 로직만 빠르게 확인 (배포 전, 로컬 venv)

DynamoDB/Secrets Manager 호출 이전의 순수 파싱/판단 로직만 빠르게 확인하고 싶다면, 로컬
가상환경에 의존성을 설치해 핸들러를 직접 import해서 호출할 수 있습니다. 이때는 로컬 머신의
OS/Python 버전 그대로 설치해도 무방합니다 (배포용 빌드가 아니라 로직 확인용이므로 1-2절의
Linux 타깃 제약과 무관합니다).

```bash
python3 -m venv .venv-test
source .venv-test/bin/activate
pip install -r src/ref_table_processor/requirements.txt boto3

python3 - <<'EOF'
import sys, json
sys.path.insert(0, "src/ref_table_processor")
import os
os.environ.update({
    "ERROR_EVENT_TABLE": "ref_error_event-dev",
    "IP_COUNTRY_TABLE": "ref_ip_country-dev",
    "AWS_API_TABLE": "ref_aws_api-dev",
    "REGION_TABLE": "ref_region-dev",
    "USER_AGENT_TABLE": "ref_user_agent-dev",
})
import app
print(app.classify_user_agent("aws-cli/2.15.0"))  # 예: 순수 함수 단위 테스트
EOF
deactivate
```

`app.lambda_handler(event, None)`처럼 핸들러를 직접 호출할 수도 있지만, 그 경우 `boto3`
호출은 실제 AWS로 나가므로(로컬 자격증명이 설정되어 있어야 함) 9-1절과 사실상 같은 효과이며
차이는 Lambda 실행 환경을 흉내내지 않는다는 점뿐입니다.

## 10. 스택 삭제

```bash
sam delete
```

삭제 시 주의할 점:

- DynamoDB 테이블은 삭제되며 **누적된 탐지 데이터도 함께 사라집니다.**
- 데모 모드(`DeployDemoCloudTrail=true`)로 배포했다면, S3 버킷에 CloudTrail 로그 객체가 남아있는
  경우 버킷이 비어있지 않아 삭제가 실패할 수 있습니다. 이 경우 먼저 버킷을 비운 뒤 다시
  `sam delete`를 실행하세요.

  ```bash
  aws s3 rm s3://accesskey-detector-demo-trail-<Stage값>-<계정ID> --recursive
  ```

- `geoip-layer-builder`가 발행한 Lambda Layer(`geoip-mmdb-<Stage값>`)는 CloudFormation이 관리하지
  않으므로(동적으로 발행되었기 때문에) 스택을 삭제해도 남아있습니다. 필요 없다면 별도로 정리하세요.

  ```bash
  aws lambda list-layer-versions --layer-name geoip-mmdb-<Stage값>
  aws lambda delete-layer-version --layer-name geoip-mmdb-<Stage값> --version-number <버전번호>
  ```

## 11. 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| `sam build` 시 `make: pip: command not found` 또는 `python3: command not found` | 빌드 머신에 `make` 또는 `python3`/`pip`이 없음. macOS는 Xcode Command Line Tools(`xcode-select --install`)로 `make`를, Linux는 배포판 패키지 매니저로 `python3`/`python3-pip`을 설치 |
| `sam build` 시 pip이 wheel을 못 받아옴 (타임아웃, `Could not find a version`) | 사내 네트워크에서 `pypi.org` 접속이 막혀있을 가능성. 1-2절의 사내 PyPI 미러 설정(`PIP_INDEX_URL` 등)을 확인하고, 그 미러에 `manylinux2014_x86_64`/`cp314` wheel이 있는지 확인 |
| 배포 시 `Unsupported runtime` 오류 | 해당 리전에 아직 `python3.14` Lambda 런타임이 제공되지 않음. 1-3절대로 `template.yaml`의 Runtime과 각 Makefile의 `PY_VERSION`/`PY_ABI`를 함께 `python3.13`/`3.13`/`cp313`으로 낮춰서 재배포 |
| `ref-table-processor`가 트리거되지 않음 (데모 모드) | S3 버킷 NotificationConfiguration이 실제로 등록됐는지 `aws s3api get-bucket-notification-configuration --bucket <버킷명>`으로 확인 |
| `ref-table-processor`가 트리거되지 않음 (기존 버킷 모드) | 6-2절의 두 수동 단계(버킷 정책, 알림 등록)가 Log Archive 계정에서 실제로 적용됐는지 확인 |
| GeoIP 국가 정보가 계속 빈 값 | `geoip-layer-builder`를 최초 1회 수동 실행했는지, `ref-table-processor`에 Layer가 붙었는지 7단계로 확인 |
| 알림이 안 옴 | 채널별 트러블슈팅 표 참고: [slack.md](notifications/slack.md#6-트러블슈팅) / [teams.md](notifications/teams.md#7-트러블슈팅). 공통적으로 CloudWatch Logs에서 `ref-suspicious-detector`의 에러 로그부터 확인 |
| `AccessDeniedException` (Secrets Manager) | Lambda 실행 역할의 정책 Resource ARN 패턴(`...secret:<시크릿이름>-*`)과 실제 시크릿 이름이 일치하는지 확인 |

## 12. 원본 Lambda 코드 대비 변경 사항

제공된 원본 코드(`geoip-layer-builder.py`, `ref-table-processor.py`, `suspicious-detector.py`)를
재사용 가능한 SAM 템플릿으로 감싸기 위해, 로직은 그대로 두고 **하드코딩된 값만 환경변수로
분리**했습니다.

1. `suspicious-detector.py`: `ref_ip_country` 등 5개 테이블명 하드코딩 → `ref-table-processor.py`와
   동일하게 환경변수(`IP_COUNTRY_TABLE` 등)로 변경
2. `suspicious-detector.py`: Slack 토큰 시크릿 이름 하드코딩(`msu-security-event-app-token`) →
   환경변수 `NOTIFICATION_SECRET_NAME`으로 변경
3. `suspicious-detector.py`: Slack 전용으로 되어있던 알림 발송 로직을 `NOTIFICATION_PROVIDER`
   환경변수(`slack` 기본값 / `teams`)로 분기하도록 리팩터링. 탐지 조건/임계값 로직은 변경 없이
   알림 페이로드 구성과 전송 부분만 provider별로 분리했습니다. 자세한 내용은
   [docs/notifications/slack.md](notifications/slack.md), [docs/notifications/teams.md](notifications/teams.md)
   참고.
4. `suspicious-detector.py`, `geoip-layer-builder.py`: `boto3.client(..., region_name="ap-northeast-2")`
   하드코딩 제거 → Lambda 실행 리전을 자동으로 사용하도록 변경 (다른 리전 배포 가능하게)
5. `geoip-layer-builder.py`: `FunctionName="ref-table-processor"` 하드코딩 → 환경변수
   `PROCESSOR_FUNCTION_NAME`으로 변경
6. `geoip-layer-builder.py`: `LAYER_NAME = "geoip-mmdb"` 상수 → 환경변수 `LAYER_NAME`으로 변경
   (스테이지별로 다른 이름을 써서 dev/prod 동시 배포 시 충돌 방지)
7. `geoip-layer-builder.py`: `publish_layer()`의 `CompatibleRuntimes=["python3.12"]` →
   `["python3.13", "python3.14"]`로 갱신 (함수 런타임과 불일치 시 Layer 연결 실패 가능성 방지)
8. Docker를 쓸 수 없는 환경을 위해, `ref-table-processor`/`geoip-layer-builder`/
   `ref-suspicious-detector` 세 함수 모두 `Metadata: BuildMethod: makefile`로 전환하고
   `src/*/Makefile`을 추가했습니다. 각 Makefile은 `pip install --platform
   manylinux2014_x86_64 --python-version 3.14 --abi cp314 --only-binary=:all:`로 로컬
   Python 버전과 무관하게 Lambda 런타임에 맞는 의존성을 내려받습니다. (1-2절 참고)

**참고로 로직/임계값은 변경하지 않았으므로, 아래 두 가지는 원본 그대로임을 인지하고 있어야 합니다.**

- `ref-table-processor.py`의 TTL은 **7일**로 계산됩니다 (`docs/architecture.md`의 설계 문서에는
  30일로 기술되어 있어 문서와 코드 간 차이가 있습니다. 필요하면 `timedelta(days=7)` 부분을
  직접 조정하세요).
- 원본 설계 문서(`docs/architecture.md`)는 GeoIP Lambda Layer에 `geoip2`/`maxminddb` 라이브러리도
  함께 포함하는 것으로 설명하지만, 실제 `geoip-layer-builder.py` 코드는 `GeoLite2-City.mmdb`
  파일만 Layer로 발행합니다. 이 SAM 구현에서는 `geoip2`/`maxminddb` 파이썬 라이브러리를
  `ref-table-processor`의 `requirements.txt`로 함수 자체 패키지에 포함시키고, mmdb 데이터 파일만
  동적 Lambda Layer로 관리하는 방식으로 동작합니다.
