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
| `ref-table-processor` Lambda | S3(CloudTrail 로그) → Reference Table 5종 적재. S3 이벤트 알림(데모/버킷정책 모드) 또는 EventBridge 폴링(크로스 계정 역할 모드)으로 트리거 |
| `ref-suspicious-detector` Lambda | DynamoDB Streams → 탐지 시나리오 평가 → Slack/Teams 알림 |
| `geoip-layer-builder` Lambda | MaxMind mmdb 갱신 확인 → Lambda Layer 재발행 → `ref-table-processor`에 자동 연결 (주기 실행) |
| DynamoDB 테이블 5종 | `ref_ip_country`, `ref_region`, `ref_user_agent`, `ref_error_event`, `ref_aws_api` |
| DynamoDB `ref_poll_cursor` 테이블 | 폴링 모드에서 (계정+리전)별 마지막 처리 위치를 저장 (다른 모드에서는 비어있음) |
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

### 1-2. 알림 채널 준비 (Slack 또는 Teams)

사용할 채널에 맞는 문서를 먼저 진행해서 알림 자격 증명(Slack Bot Token 또는 Teams Webhook
URL)을 확보해두세요.

- Slack을 사용한다면 → [docs/notifications/slack.md](notifications/slack.md)
- Microsoft Teams를 사용한다면 → [docs/notifications/teams.md](notifications/teams.md)

### 1-3. MaxMind 계정 준비

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
`sam build`는 자동으로 `src/*/Makefile`을 실행해 의존성을 내려받습니다. 출력에
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
| Parameter CrossAccountS3RoleArn | `DeployDemoCloudTrail=false`일 때 사용. 처음 배포할 때는 비워두고, 6-2절에서 역할을 만든 뒤 재배포 시 지정. 지정하면 폴링 모드가 자동으로 켜짐 |
| Parameter PollSchedule | 폴링 모드(위 파라미터 지정 시)의 버킷 스캔 주기 (기본 `rate(5 minutes)`) |
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

버킷이 **다른 AWS 계정**에 있으므로, CloudFormation 한 스택만으로는 양쪽을 다 설정할 수
없습니다. **Log Archive 계정에서 해야 할 일은 크로스 계정 IAM 역할을 하나 만드는 것,
그것뿐입니다.** S3 이벤트 알림은 등록하지 않습니다 — `ref-table-processor`는 이 역할이
지정되면 S3 알림을 기다리는 대신, EventBridge 스케줄로 주기 실행되면서 스스로 버킷을
스캔해 새 로그 파일을 찾아옵니다(폴링). Control Tower SCP가 버킷 알림 등록(`s3:PutBucket
Notification`)까지 막는 경우가 많아 애초에 알림에 의존하지 않는 방식입니다.

읽기 권한은 **크로스 계정 IAM 역할**로 부여합니다. Log Archive 계정의 버킷 정책 자체는
건드리지 않습니다. Log Archive 계정에 IAM 역할을 하나 만들고 `ref-table-processor`가 그
역할을 assume해서 S3에 접근하게 합니다. 역할을 assume한 시점부터는 임시 자격증명이 Log
Archive 계정 소속이 되므로 같은 계정 접근과 동일하게 처리되고, 버킷 정책 수정이 필요
없습니다.

전체 흐름은 이렇습니다.

```
Audit 계정 (EventBridge Schedule)
  → ref-table-processor Lambda
      → AssumeRole
        → Log Archive 계정의 accesskey-detector-cloudtrail-reader 역할
          → S3 ListBucket / GetObject (같은 계정 접근으로 처리됨)
```

**아래 작업은 계정이 서로 다르므로, 어느 계정 콘솔에서 진행하는지 각 단계마다 명시했습니다.
반드시 표시된 계정으로 콘솔 우측 상단에서 전환(스위치 롤/SSO 계정 변경)한 뒤 진행하세요.**

#### Audit 계정에서 (1) — Lambda 실행 역할 확인

**Lambda 콘솔** → 함수 목록에서 `ref-table-processor-<Stage값>` 클릭 → **Configuration(구성)**
탭 → **Permissions(권한)** → **Execution role(실행 역할)** 섹션에 표시된 역할 이름을 클릭하면
IAM 콘솔로 이동합니다. 그 페이지 상단의 **ARN**을 복사해둡니다. (뒤에서 Log Archive 쪽 신뢰
정책에 사용)

#### Log Archive 계정에서 — 크로스 계정 역할 생성

**역할은 반드시 이 계정(버킷을 소유한 계정) 안에 만들어야 합니다.** Audit 계정에 만들면
동작하지 않습니다 — Audit 계정에 만든 역할은 Log Archive 계정 소속이 아니므로, 그 역할을
assume해도 여전히 "다른 계정에서 접근"하는 것이 되어 버킷 정책이 없으면 거부됩니다.

**1) 역할 생성**

**IAM 콘솔** → 왼쪽 메뉴 **Roles(역할)** → **Create role(역할 생성)**

- **Trusted entity type**: **Custom trust policy** 선택
- 아래 JSON 편집창에 기존 내용을 지우고 아래 내용을 붙여넣습니다. `Principal.AWS` 값은
  Audit 계정에서 복사해둔 Lambda 실행 역할 ARN으로 바꾸세요.

  ```json
  {
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Principal": {
          "AWS": "arn:aws:iam::<Audit계정ID>:role/<Lambda 실행 역할 이름>"
        },
        "Action": "sts:AssumeRole"
      }
    ]
  }
  ```

- **Next** → **Add permissions** 화면은 아무것도 선택하지 않고 그대로 **Next**
  (권한은 역할 생성 후 인라인 정책으로 따로 추가합니다)
- **Role name**: `accesskey-detector-cloudtrail-reader` 입력 → **Create role**

**2) 읽기 권한 추가**

폴링을 위해서는 객체를 읽는 `s3:GetObject`뿐 아니라 버킷 목록을 조회하는 `s3:ListBucket`도
필요합니다.

방금 만든 역할 페이지로 이동 → **Permissions(권한)** 탭 → **Add permissions** →
**Create inline policy**

- **JSON** 탭으로 전환 후 아래 내용을 붙여넣습니다. 버킷 이름을 실제 중앙 버킷 이름으로
  바꾸세요.

  ```json
  {
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": "s3:ListBucket",
        "Resource": "arn:aws:s3:::<중앙버킷이름>"
      },
      {
        "Effect": "Allow",
        "Action": "s3:GetObject",
        "Resource": "arn:aws:s3:::<중앙버킷이름>/*"
      }
    ]
  }
  ```

- **Next** → Policy name: `read-cloudtrail-logs` → **Create policy**
- 역할 페이지 상단의 **ARN**을 복사해둡니다 (Audit 계정 재배포에 필요).

이것으로 Log Archive 계정에서 할 일은 끝입니다. 이 버킷에는 그 외 어떤 설정도(버킷 정책,
이벤트 알림 등) 추가하지 않습니다.

#### Audit 계정에서 (2) — 크로스 계정 역할 ARN으로 재배포

**CloudFormation 콘솔** → **Stacks(스택)** → 이 스택 선택 → **Update(업데이트)**

- **Use current template(현재 템플릿 사용)** 선택 → **Next**
- **Parameters** 화면에서 `CrossAccountS3RoleArn` 값에 Log Archive 계정에서 복사해둔 역할
  ARN(`arn:aws:iam::<LogArchive계정ID>:role/accesskey-detector-cloudtrail-reader`)을
  붙여넣기 → **Next**
- Stack options는 그대로 **Next**
- 검토 화면에서 **"I acknowledge that AWS CloudFormation might create IAM resources"**
  체크 → **Update stack(스택 업데이트)**

`CrossAccountS3RoleArn`이 채워진 채로 배포되면, 템플릿이 자동으로 EventBridge Schedule
(`PollSchedule` 파라미터, 기본 5분)을 활성화합니다. `ref-table-processor`는 이 스케줄로
호출될 때마다 이 역할을 assume해서 버킷을 스캔하고, 마지막으로 처리한 위치를
`ref_poll_cursor` 테이블에 기억해뒀다가 다음 폴링에서 신규 파일만 가져옵니다
(`src/ref_table_processor/app.py`의 `get_s3_client()` / `poll_bucket_for_new_logs()` 참고).
별도로 켜거나 등록할 것은 없습니다.

> CLI로 재배포하려면 `sam deploy --guided`를 다시 실행해 전체 파라미터를 한 번에 다시
> 입력하거나, `samconfig.toml`의 `parameter_overrides`에 `CrossAccountS3RoleArn=<위 ARN>`을
> 직접 추가한 뒤 `sam deploy`를 실행하세요. (이전에 저장된 다른 파라미터가 초기화되지 않도록
> `sam deploy --parameter-overrides`만 단독으로 넘기지 않도록 주의하세요.)

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

응답은 세 가지 중 하나입니다.

- `{"status": "updated", "layer_arn": "...", "hash": "..."}` — 성공. 새 Layer 버전을 발행하고
  `ref-table-processor`에 연결까지 마쳤습니다.
- `{"status": "skipped", "hash": "..."}` — MaxMind의 최신 해시가 이미 발행된 Layer의 해시와
  같아서(즉 이미 최신 상태라서) 아무것도 하지 않고 종료했습니다. 정상입니다.
- 그 외 에러 JSON (`errorMessage` 포함) — 실패. 흔한 원인은 아래와 같습니다.

| 에러 | 원인 / 해결 |
|---|---|
| `Secrets Manager` 관련 에러 (`ResourceNotFoundException`, `AccessDenied`) | `MaxMindSecretName`이 가리키는 시크릿이 없거나 값이 `{"MAXMIND_LICENSE_KEY": "..."}` 형식이 아님. 3절대로 시크릿을 다시 생성 |
| `403`/`401` (MaxMind 다운로드 URL 호출 시) | 라이선스 키가 잘못됐거나 만료됨. MaxMind 계정에서 재발급 |
| `NoSuchBucket`, `AccessDenied` (S3 업로드 관련) | `GeoIpLayerBuildBucket`이 스택에 정상적으로 생성됐는지 CloudFormation 콘솔에서 확인 |
| `ValueError: zip 크기(...)가 직접 업로드 한도(50MB)를 초과합니다` | 이전 버전 코드의 잔재입니다. `publish_layer()`는 이제 S3를 경유하도록 바뀌어 이 제한이 없습니다 — 이 에러가 보인다면 아직 옛 코드가 배포되어 있는 것이니 `sam build && sam deploy`로 다시 배포하세요. |

성공했다면 `ref-table-processor`의 설정을 확인해서 Layer가 붙었는지 확인할 수 있습니다.

```bash
aws lambda get-function-configuration \
  --function-name ref-table-processor-<Stage값> \
  --query "Layers"
```

Layer ARN이 하나 나오면(예: `arn:aws:lambda:<리전>:<계정ID>:layer:geoip-mmdb-<Stage값>:1`)
정상적으로 연결된 것입니다.

## 8. 동작 확인

### 8-1. 데모 모드: 테스트 이벤트 발생시키기

데모 모드로 배포했다면, 실제로 아무 IAM 사용자의 Access Key로 AWS CLI 명령을 몇 번 호출해보면
(예: `aws sts get-caller-identity`, `aws iam list-users`) 약 5분 내(CloudTrail 배치 주기) 해당
계정의 CloudTrail 로그가 데모 버킷에 쌓이고, `ref-table-processor`가 트리거됩니다.

### 8-1-B. 폴링 모드(크로스 계정 역할): 수동으로 한 번 실행해보기

`PollSchedule`(기본 5분) 주기를 기다리지 않고 바로 확인하려면 직접 한 번 호출해봅니다.

```bash
aws lambda invoke \
  --function-name ref-table-processor-<Stage값> \
  --cli-binary-format raw-in-base64-out \
  --payload '{}' \
  /tmp/poll-output.json
```

CloudWatch Logs(8-3절)에서 처리한 파일 수가 로그로 찍히는지 확인하고, `ref_poll_cursor-<Stage값>`
테이블에 (계정+리전) prefix별 커서가 생겼는지 확인합니다.

```bash
aws dynamodb scan --table-name ref_poll_cursor-<Stage값>
```

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


## 9. 스택 삭제

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

## 10. 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| `sam build` 시 `make: pip: command not found` 또는 `python3: command not found` | 빌드 머신에 `make` 또는 `python3`/`pip`이 없음. macOS는 Xcode Command Line Tools(`xcode-select --install`)로 `make`를, Linux는 배포판 패키지 매니저로 `python3`/`python3-pip`을 설치 |
| `sam build` 시 pip이 wheel을 못 받아옴 (타임아웃, `Could not find a version`) | 사내 네트워크에서 `pypi.org` 접속이 막혀있을 가능성. `pip.conf`/`PIP_INDEX_URL`로 사내 PyPI 미러를 가리키도록 설정하고, 그 미러에 `manylinux2014_x86_64`/`cp314` wheel이 있는지 확인 |
| 배포 시 `Unsupported runtime` 오류 | 해당 리전에 아직 `python3.14` Lambda 런타임이 제공되지 않음. `template.yaml`의 Runtime과 각 Makefile의 `PY_VERSION`/`PY_ABI`를 함께 `python3.13`/`3.13`/`cp313`으로 낮춰서 재배포 |
| `ref-table-processor`가 트리거되지 않음 (데모 모드) | S3 버킷 NotificationConfiguration이 실제로 등록됐는지 `aws s3api get-bucket-notification-configuration --bucket <버킷명>`으로 확인 |
| `ref-table-processor`가 실행은 되는데 새 파일을 못 찾음 (폴링 모드) | EventBridge 규칙(`PollSchedule`)이 활성화되어 있는지, CloudWatch Logs에서 `poll_bucket_for_new_logs` 관련 에러(권한 부족 등)가 있는지 확인 |
| `AccessDenied` (`sts:AssumeRole`, `CrossAccountS3RoleArn` 사용 시) | Log Archive 계정 쪽 역할의 신뢰 정책(trust policy) Principal이 Audit 계정의 `RefTableProcessorFunctionRole` ARN과 정확히 일치하는지 확인 |
| `AccessDenied` (`s3:ListBucket`/`s3:GetObject`, 폴링 모드) | Log Archive 계정에 만든 역할의 인라인 정책에 `ListBucket`(버킷 자체 ARN)과 `GetObject`(`/*` ARN)가 모두 있는지 확인 |
| GeoIP 국가 정보가 계속 빈 값 | `geoip-layer-builder`를 최초 1회 수동 실행했는지, `ref-table-processor`에 Layer가 붙었는지 7단계로 확인 |
| 알림이 안 옴 | 채널별 트러블슈팅 표 참고: [slack.md](notifications/slack.md#6-트러블슈팅) / [teams.md](notifications/teams.md#7-트러블슈팅). 공통적으로 CloudWatch Logs에서 `ref-suspicious-detector`의 에러 로그부터 확인 |
| `AccessDeniedException` (Secrets Manager) | Lambda 실행 역할의 정책 Resource ARN 패턴(`...secret:<시크릿이름>-*`)과 실제 시크릿 이름이 일치하는지 확인 |

## 11. 원본 Lambda 코드 대비 변경 사항

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
   Python 버전과 무관하게 Lambda 런타임에 맞는 의존성을 내려받습니다.
9. `ref-table-processor.py`: Control Tower SCP 등으로 Log Archive 계정의 CloudTrail 버킷
   정책을 편집할 수 없는 환경을 위해, `CROSS_ACCOUNT_S3_ROLE_ARN` 환경변수가 설정되어 있으면
   해당 역할을 `sts:AssumeRole`로 위임받아 S3에 접근하는 `get_s3_client()`를 추가했습니다.
   설정하지 않으면 기존과 동일하게 자기 자신의 실행 역할로 S3에 접근합니다. (6-2절 참고)
10. `ref-table-processor.py`: S3 이벤트 알림 자체가 SCP로 막혀있는 환경을 위해, S3 Records가
    없는 호출(EventBridge Schedule)을 받으면 `poll_bucket_for_new_logs()`로 버킷을 직접
    스캔하는 폴링 모드를 추가했습니다. `AWSLogs/<Org>/<Account>/CloudTrail/<Region>/` 구조를
    delimiter 기반으로 얕게 탐색해 계정·리전을 자동으로 찾고, (계정+리전)별로 마지막 처리
    위치를 `ref_poll_cursor` 테이블에 저장해 다음 폴링에서 신규 파일만 가져옵니다.
    `CrossAccountS3RoleArn`이 설정된 경우에만 활성화됩니다. (6-2절 참고)
11. `geoip-layer-builder.py`: `publish_layer()`가 zip 바이트를 `publish_layer_version`
    요청에 직접 담아 보내던 방식(`Content.ZipFile`, 50MB 제한)을 S3 경유 방식
    (`Content.S3Bucket`/`S3Key`)으로 바꿨습니다. 최신 `GeoLite2-City.mmdb`는 이미 50MB를
    넘는 경우가 많아, 원래 코드는 실제로는 거의 항상 실패하는 구조였습니다. 이를 위해
    스테이징 전용 S3 버킷(`GeoIpLayerBuildBucket`, 1일 후 자동 만료)을 새로 추가했습니다.

**참고로 로직/임계값은 변경하지 않았으므로, 아래 두 가지는 원본 그대로임을 인지하고 있어야 합니다.**

- `ref-table-processor.py`의 TTL은 **7일**로 계산됩니다 (`docs/architecture.md`의 설계 문서에는
  30일로 기술되어 있어 문서와 코드 간 차이가 있습니다. 필요하면 `timedelta(days=7)` 부분을
  직접 조정하세요).
- 원본 설계 문서(`docs/architecture.md`)는 GeoIP Lambda Layer에 `geoip2`/`maxminddb` 라이브러리도
  함께 포함하는 것으로 설명하지만, 실제 `geoip-layer-builder.py` 코드는 `GeoLite2-City.mmdb`
  파일만 Layer로 발행합니다. 이 SAM 구현에서는 `geoip2`/`maxminddb` 파이썬 라이브러리를
  `ref-table-processor`의 `requirements.txt`로 함수 자체 패키지에 포함시키고, mmdb 데이터 파일만
  동적 Lambda Layer로 관리하는 방식으로 동작합니다.
