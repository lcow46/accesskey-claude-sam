# 알림 채널 설정 — Slack

`NotificationProvider=slack`(기본값)으로 배포할 때 필요한 사전 준비와 설정값을 안내합니다.
공통 빌드/배포 절차는 [../sam-deployment-guide.md](../sam-deployment-guide.md)를 먼저 참고하세요.

Teams를 사용한다면 이 문서 대신 [teams.md](teams.md)를 보세요.

## 1. Slack App 및 Bot 생성

1. Slack API 사이트(api.slack.com)에서 워크스페이스에 새 App을 생성합니다. ("From scratch")
2. 좌측 메뉴 **OAuth & Permissions**로 이동해 **Bot Token Scopes**에 `chat:write`를 추가합니다.
3. 페이지 상단의 **Install to Workspace**(또는 조직 정책에 따라 관리자 승인)로 앱을 설치합니다.
4. 설치가 끝나면 발급되는 **Bot User OAuth Token**(`xoxb-`로 시작)을 복사해둡니다.

## 2. 알림 채널 준비

1. 알림을 받을 채널을 만들거나 정합니다. (예: `#security-alerts`)
2. 그 채널에서 `/invite @앱이름` 명령으로 Bot을 초대합니다. (초대하지 않으면 `not_in_channel` 오류가 발생합니다)
3. 채널 ID를 확인합니다: 채널 이름 클릭 → 채널 정보 패널 하단에 `C`로 시작하는 ID가 표시됩니다.

## 3. Secrets Manager 시크릿 생성

```bash
aws secretsmanager create-secret \
  --name "accesskey-detector/notification-credential" \
  --secret-string '{"slack_bot_token":"xoxb-여기에-실제-토큰"}'
```

시크릿 이름을 바꿨다면 배포 시 `NotificationSecretName` 파라미터에 그 이름을 지정하세요.

## 4. `sam deploy --guided` 파라미터 값

| 파라미터 | 값 |
|---|---|
| `NotificationProvider` | `slack` |
| `SlackChannelId` | 2단계에서 확인한 채널 ID (예: `C0123456789`) |
| `NotificationSecretName` | 3단계에서 만든 시크릿 이름 |

## 5. 동작 확인

```bash
aws lambda invoke \
  --function-name ref-suspicious-detector-<Stage값> \
  --cli-binary-format raw-in-base64-out \
  --payload file://events/dynamodb-stream-aws-api-insert.json \
  /tmp/detector-output.json
```

허용 국가 외부 IP 조건이 맞지 않으면 알림이 발송되지 않을 수 있습니다 — 이 샘플 이벤트만으로는
`ref_ip_country` 테이블에 대응하는 항목이 없어 `is_foreign_ip`가 False로 평가되어 알림이 가지 않는
것이 정상입니다. 실제 알림 발송 여부는 [../sam-deployment-guide.md](../sam-deployment-guide.md)
8-4절의 방법으로 확인하세요.

## 6. 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| `not_in_channel` | Bot이 알림 채널에 초대되어 있지 않음. `/invite @앱이름` 실행 |
| `invalid_auth` / `token_revoked` | Bot Token이 잘못되었거나 앱이 재설치되어 토큰이 바뀜. Secrets Manager 시크릿 값 갱신 |
| `channel_not_found` | `SlackChannelId`가 실제 채널 ID와 다름. 채널 정보 패널에서 ID 재확인 |
| 알림이 아예 안 옴 (에러도 없음) | CloudWatch Logs에서 `ref-suspicious-detector`가 실제로 트리거됐는지, 탐지 조건(허용 국가/리전 등)에 맞는 이벤트인지 확인 |
