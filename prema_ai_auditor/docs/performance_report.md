# Performance Report

## Unbounded Searches
- `models/integrity_scanner.py:16` `accounts = self.env["account.account"].search([])`
- `models/integrity_scanner.py:22` `partners = self.env["res.partner"].search([])`
- `models/integrity_scanner.py:28` `products = self.env["product.template"].search([])`
- `models/integrity_scanner.py:34` `vehicles = self.env["fleet.vehicle"].search([])`
- `models/integrity_scanner.py:40` `leads = self.env["crm.lead"].search([])`

## Search-heavy paths (review limits/indexes)
- `scripts/full_audit_scan.py:58` `if "search([" in line or "search_count([" in line:`
- `models/tool_registry.py:72` `lines = self.env["account.bank.statement.line"].search([`
- `models/tool_registry.py:79` `move_lines = self.env["account.move.line"].search([`
- `models/ai_document_processor.py:133` `documents = self.env["prema.ai.document"].sudo().search([`
- `models/ai_document_processor.py:177` `docs = self.env["prema.ai.document"].sudo().search([`
- `models/ai_document_processor.py:212` `docs = self.env["prema.ai.document"].sudo().search([`
- `models/ai_document_processor.py:229` `docs = self.env["prema.ai.document"].sudo().search([`
- `models/risk_matrix.py:9` `logs = self.env["prema.audit.log"].search([("status", "=", "open")])`
- `models/anomaly_engine.py:12` `moves = self.env["account.move"].search([("move_type", "=", "in_invoice")])`
- `models/ai_mail_monitor.py:9` `failed = self.env["mail.mail"].search([("state", "=", "exception")], limit=200)`
- `models/ai_self_heal_engine.py:11` `crons = self.env["ir.cron"].search([("active", "=", True)], limit=50)`
- `models/ai_memory.py:45` `memory = env["prema.ai.memory"].search([("issue_signature", "=", issue_signature)], limit=1)`
- `models/health_score.py:9` `logs = self.env["prema.audit.log"].search([("status", "=", "open")])`
- `models/ai_integrity_engine.py:9` `modules = self.env["ir.module.module"].search([("name", "=", "prema_ai_auditor")], limit=1)`
- `models/integrity_scanner.py:16` `accounts = self.env["account.account"].search([])`
- `models/integrity_scanner.py:22` `partners = self.env["res.partner"].search([])`
- `models/integrity_scanner.py:28` `products = self.env["product.template"].search([])`
- `models/integrity_scanner.py:34` `vehicles = self.env["fleet.vehicle"].search([])`
- `models/integrity_scanner.py:40` `leads = self.env["crm.lead"].search([])`
- `models/ai_dashboard_metrics.py:9` `logs = self.env["prema.audit.log"].search([("status", "=", "open")])`
- `models/ai_config_audit.py:21` `inactive_crons = self.env["ir.cron"].search([("active", "=", False)], limit=1)`
- `controllers/chat_controller.py:63` `records = request.env["prema.ai.incident"].sudo().search([], order="create_date desc", limit=limit)`
- `services/dedupe_service.py:17` `docs = self.env["prema.ai.document"].sudo().search([("file_sha256", "=", digest)], limit=20)`
- `services/plan_generator.py:11` `logs = self.env["prema.audit.log"].search([("status", "=", "open")])`
- `services/predictive_engine.py:9` `self.env["ir.logging"].search([], order="create_date desc", limit=500)`

## Worker/Cron
- Ensure cron handlers batch (`limit`/`seek`) and avoid long blocking HTTP operations.
