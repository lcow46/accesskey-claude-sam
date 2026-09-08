# accesskey-claude-sam

AWS 환경의 IAM Access Key 유출 및 악용을 탐지하기 위한 이상탐지 아키텍처입니다.

CloudTrail 이벤트를 실시간으로 수집·분석하여 사전 정의된 5가지 탐지 시나리오를 기반으로 이상 행위를 식별하고, Slack을 통해 보안 담당자에게 즉시 알림을 제공합니다.

자세한 내용은 [아키텍처 문서](docs/architecture.md)를 참고하세요.

## 개요

- **초기 정찰 탐지**: `GetCallerIdentity` 등 정찰성 API의 허용 국가 외부 호출 탐지
- **권한 상승 탐지**: `CreateUser`, `AttachUserPolicy` 등 권한 상승 API의 허용 국가 외부 호출 탐지
- **비정상 오류 패턴 탐지**: 짧은 시간 내 다수의 `AccessDenied` 발생 탐지
- **비정상 리전 리소스 생성 탐지**: 허용 리전 외부에서의 리소스 생성 API 호출 탐지
- **공격 도구 시그니처 탐지**: Pacu, WeirdAAL 등 알려진 공격 도구의 User-Agent 시그니처 탐지

## 구성

- CloudTrail 중앙 수집 (Organization Trail → Log Archive S3)
- Reference Table (DynamoDB): `ref_ip_country`, `ref_region`, `ref_user_agent`, `ref_error_event`, `ref_aws_api`
- `ref-table-processor` Lambda (Python 3.14) + GeoIP Lambda Layer
- `ref-suspicious-detector` Lambda (Python 3.14) + DynamoDB Streams + Slack 알림

전체 구성요소, 탐지 시나리오별 상세 로직, 대응 절차, 운영 가이드는 [docs/architecture.md](docs/architecture.md)에 정리되어 있습니다.

## AWS SAM으로 배포하기

이 저장소는 위 아키텍처 중 Audit 계정 구성요소(Lambda 3종, DynamoDB Reference Table 5종, DynamoDB
Streams, Slack 연동)를 AWS SAM 프로젝트로 구현한 `template.yaml`과 Lambda 소스(`src/`)를 포함하고
있습니다. Organization Trail이 없어도 자체 S3 버킷+CloudTrail로 엔드투엔드 테스트가 가능한 데모
모드를 지원합니다.

```bash
sam build
sam deploy --guided
```

자세한 사전 준비물, 파라미터 설명, 배포 후 수동 설정, 로컬 테스트, 트러블슈팅은
[docs/sam-deployment-guide.md](docs/sam-deployment-guide.md)를 참고하세요.
