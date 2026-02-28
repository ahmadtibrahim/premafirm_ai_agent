# Security Report

## Findings
- `services/openai_client.py:15` `api_key = config.require("openai.api_key", env_key="OPENAI_API_KEY", label="OpenAI API key")`
- `services/llm_client.py:21` `api_key = config.require("prema_ai.openai.api_key", env_key="OPENAI_API_KEY", label="OpenAI API Key")`
- `scripts/full_audit_scan.py:69` `re.compile(r"mapbox", re.I),`
- `scripts/generate_reports.py:83` `for pattern in [r"sk-[A-Za-z0-9]{10,}", r"api[_-]?key\s*=", r"mapbox", r"openai\.api_key", r"journal_id\s*=\s*\d+"]:`
- `scripts/full_audit_scan.py:140` `"- `openai.api_key` / `OPENAI_API_KEY`\n"`
- `models/ai_config_audit.py:18` `if not config.get("openai.api_key", env_key="OPENAI_API_KEY"):`
- `models/wizards/config_audit_wizard.py:13` `("openai.api_key", "OPENAI_API_KEY", True),`
- `services/openai_client.py:15` `api_key = config.require("openai.api_key", env_key="OPENAI_API_KEY", label="OpenAI API key")`
- `services/llm_client.py:21` `api_key = config.require("prema_ai.openai.api_key", env_key="OPENAI_API_KEY", label="OpenAI API Key")`

## Mitigation Plan
- Store secrets only in `ir.config_parameter` / environment variables.
- Mask secret values in all logs/UI diagnostics.
- Rotate keys immediately after any exposure.
