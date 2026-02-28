# Audit Report

## Key Failures / Risks
- **High**: Hardcoded external endpoint/model values in LLM stack reduce environment portability.
- **High**: Error propagation from external calls included raw exception text in user-visible errors.
- **Medium**: Bus channel uses global channel name; ensure only partner-targeted payloads are sent.
- **Medium**: No central runtime schema comparison report existed.

## Hardcoded/Secret Scan Findings
- `models/ai_config_audit.py:18` -> `if not config.get("openai.api_key", env_key="OPENAI_API_KEY"):`
- `models/ai_config_audit.py:19` -> `issues.append("OpenAI API key missing")`
- `models/wizards/config_audit_wizard.py:13` -> `("openai.api_key", "OPENAI_API_KEY", True),`
- `models/wizards/config_audit_wizard.py:14` -> `("prema_ai_auditor.openai_model", "OPENAI_MODEL", False),`
- `services/llm_service.py:18` -> `"model": config.get("prema_ai_auditor.openai_model", env_key="OPENAI_MODEL", default="gpt-4.1"),`
- `services/llm_service.py:33` -> `response = self.env["prema.openai.client"].call(payload, timeout=45, retries=2)`
- `services/__init__.py:2` -> `from . import openai_client`
- `services/openai_client.py:9` -> `class OpenAIClient(models.AbstractModel):`
- `services/openai_client.py:10` -> `_name = "prema.openai.client"`
- `services/openai_client.py:11` -> `_description = "OpenAI Client"`
- `services/openai_client.py:15` -> `api_key = config.require("openai.api_key", env_key="OPENAI_API_KEY", label="OpenAI API key")`
- `services/openai_client.py:31` -> `config.get("prema_ai_auditor.openai_endpoint", env_key="OPENAI_ENDPOINT", default="https://api.openai.com/v1/chat/completions"),`
- `services/openai_client.py:43` -> `raise UserError("OpenAI request failed. Please verify remote connectivity and credentials.")`
- `services/plan_generator.py:23` -> `response = self.env["prema.openai.client"].call(`
- `scripts/full_audit_scan.py:69` -> `re.compile(r"mapbox", re.I),`
- `scripts/full_audit_scan.py:70` -> `re.compile(r"openai", re.I),`
- `scripts/full_audit_scan.py:140` -> `"- `openai.api_key` / `OPENAI_API_KEY`\n"`
- `scripts/full_audit_scan.py:141` -> `"- `prema_ai_auditor.openai_endpoint` / `OPENAI_ENDPOINT`\n"`
- `scripts/full_audit_scan.py:142` -> `"- `prema_ai_auditor.openai_model` / `OPENAI_MODEL`\n"`
