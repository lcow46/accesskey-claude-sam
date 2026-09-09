# 알림 채널 설정 — Microsoft Teams

`NotificationProvider=teams`로 배포할 때 필요한 사전 준비와 설정값을 안내합니다. 공통
빌드/배포 절차는 [../sam-deployment-guide.md](../sam-deployment-guide.md)를 먼저 참고하세요.

Slack을 사용한다면 이 문서 대신 [slack.md](slack.md)를 보세요.

> **참고:** Microsoft는 Teams의 기존 Office 365 Connector(Incoming Webhook)를 단계적으로
> 폐지하고 있습니다. 테넌트에 따라 신규 생성이 이미 막혀 있을 수 있으므로, 이 문서는 현재
> 권장되는 **Power Automate 기반 "Workflows" 앱**으로 안내합니다. 조직에 아직 레거시 Incoming
> Webhook 커넥터가 남아있다면 6절의 대안으로도 동일한 코드가 그대로 동작합니다.

## 1. Teams 워크플로(Webhook) 생성

1. 알림을 받을 채널에서 **Workflows** 앱을 엽니다. (채널 상단 `···` 메뉴 → 워크플로, 또는 Teams
   앱 검색에서 "Workflows" 검색 후 채널에 추가)
2. 템플릿 중 **"Webhook 요청이 수신되면 채널에 게시" (Post to a channel when a webhook
   request is received)** 계열 템플릿을 선택합니다. (조직 정책상 템플릿 이름/구성이 다를 수
   있으며, 없다면 "공백에서 만들기"로 같은 트리거를 직접 구성할 수 있습니다.)
3. 알림을 게시할 Team과 채널을 지정하고 워크플로를 만듭니다.
4. 생성이 끝나면 **Webhook URL**이 발급됩니다. 이 URL을 복사해둡니다. (Power Automate 플로우
   URL이며, 노출되면 누구나 그 채널에 메시지를 보낼 수 있으므로 Slack Bot Token과 동일하게
   비밀로 취급해야 합니다.)
5. (선택) 플로우 편집 화면에서 트리거 다음에 **JSON 구문 분석(Parse JSON)** 단계를 추가하고,
   본문 스키마에 아래 예시를 넣으면 이후 단계에서 `title`/`text` 필드를 그대로 참조할 수 있습니다.

   ```json
   {
     "type": "object",
     "properties": {
       "title": { "type": "string" },
       "text": { "type": "string" },
       "summary": { "type": "string" },
       "themeColor": { "type": "string" }
     }
   }
   ```

   이후 **"채널에 메시지 게시" (Post message in a chat or channel)** 액션에서 메시지 본문에
   동적 콘텐츠로 `text`(및 `title`)를 매핑하면, 이 프로젝트의 `ref-suspicious-detector`가 보내는
   알림 내용이 그대로 채널에 표시됩니다.

## 2. Secrets Manager 시크릿 생성

```bash
aws secretsmanager create-secret \
  --name "accesskey-detector/notification-credential" \
  --secret-string '{"teams_webhook_url":"여기에-1단계에서-복사한-Webhook URL"}'
```

시크릿 이름을 바꿨다면 배포 시 `NotificationSecretName` 파라미터에 그 이름을 지정하세요.

## 3. `sam deploy --guided` 파라미터 값

| 파라미터 | 값 |
|---|---|
| `NotificationProvider` | `teams` |
| `SlackChannelId` | 비워둠 (기본값 `""` 그대로 사용 — Teams에서는 사용하지 않음) |
| `NotificationSecretName` | 2단계에서 만든 시크릿 이름 |

## 4. 전송되는 메시지 형식

`ref-suspicious-detector`는 Teams로 아래와 같은 [MessageCard](https://learn.microsoft.com/outlook/actionable-messages/message-card-reference)
형식 JSON을 Webhook URL로 POST합니다. Power Automate 워크플로와 레거시 Incoming Webhook
커넥터 모두 이 형식을 그대로 소비할 수 있습니다.

```json
{
  "@type": "MessageCard",
  "@context": "http://schema.org/extensions",
  "summary": "[시나리오 1] 공격자 초기 정찰 의심",
  "themeColor": "FFD700",
  "title": "⚠️ [시나리오 1] 공격자 초기 정찰 의심",
  "text": "**액세스 키 ID:** AKIA...\n\n**이벤트명:** GetCallerIdentity (sts.amazonaws.com)\n\n..."
}
```

## 5. 동작 확인

먼저 Webhook URL 자체가 정상 동작하는지 curl로 직접 확인해볼 수 있습니다.

```bash
curl -X POST "<1단계에서 복사한 Webhook URL>" \
  -H "Content-Type: application/json" \
  -d '{
    "@type": "MessageCard",
    "@context": "http://schema.org/extensions",
    "summary": "테스트",
    "title": "연동 테스트",
    "text": "이 메시지가 보이면 Webhook 연동이 정상입니다."
  }'
```

채널에 메시지가 도착하면, Lambda 쪽 연동은 아래처럼 확인합니다.

```bash
aws lambda invoke \
  --function-name ref-suspicious-detector-<Stage값> \
  --cli-binary-format raw-in-base64-out \
  --payload file://events/dynamodb-stream-aws-api-insert.json \
  /tmp/detector-output.json
```

## 6. 대안: 레거시 Incoming Webhook 커넥터

조직에 아직 예전 방식의 "Incoming Webhook" 커넥터가 남아있다면(채널 `···` → 커넥터 → Incoming
Webhook), 그 방식으로 발급받은 Webhook URL도 동일하게 `teams_webhook_url`에 넣으면 됩니다.
`_post_teams()`가 보내는 MessageCard 형식은 원래 이 레거시 커넥터를 위해 설계된 표준 스키마라
별도 코드 변경 없이 바로 호환됩니다.

## 7. 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| 워크플로가 생성되지 않음 / Workflows 앱이 안 보임 | 조직의 Teams 관리 정책에서 Workflows 앱 또는 커넥터 생성이 제한되어 있을 수 있음. Teams 관리자에게 문의 |
| curl 테스트는 되는데 Lambda에서는 안 옴 | Secrets Manager에 저장된 URL에 오타/줄바꿈이 없는지 확인 (`aws secretsmanager get-secret-value`로 재확인) |
| 채널에 카드가 아니라 아무 반응 없음 | Parse JSON 이후 "채널에 메시지 게시" 액션에서 `text` 필드를 실제로 매핑했는지 확인 (매핑하지 않으면 트리거는 성공해도 채널에는 아무것도 게시되지 않습니다) |
| 401/403 응답 | Webhook URL이 만료되었거나 삭제됨. 워크플로를 다시 확인하고 필요 시 새 URL로 시크릿 갱신 |
