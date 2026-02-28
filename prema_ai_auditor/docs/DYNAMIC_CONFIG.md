# Dynamic Config Plan

Resolution order for all config values:
1. `ir.config_parameter`
2. Environment variable
3. Safe default (non-secret only)

Required keys:
- `openai.api_key` / `OPENAI_API_KEY`
- `prema_ai_auditor.openai_endpoint` / `OPENAI_ENDPOINT`
- `prema_ai_auditor.openai_model` / `OPENAI_MODEL`
- `web.base.url` / `WEB_BASE_URL`
- `mail.catchall.domain` / `MAIL_CATCHALL_DOMAIN`

Secret logging policy: show masked values only (`ABCD...WXYZ`).
