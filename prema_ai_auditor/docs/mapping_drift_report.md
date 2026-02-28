# Mapping Drift Report

## Missing Models (HARD FAIL)
- `prema.ai.action.request`: guard feature or remove usage.
- `prema.ai.audit.log`: guard feature or remove usage.
- `prema.ai.document`: guard feature or remove usage.
- `prema.ai.document.processor`: guard feature or remove usage.
- `prema.ai.error.monitor`: guard feature or remove usage.
- `prema.ai.incident`: guard feature or remove usage.
- `prema.ai.integrity.engine`: guard feature or remove usage.
- `prema.ai.mail.monitor`: guard feature or remove usage.
- `prema.ai.memory`: guard feature or remove usage.
- `prema.ai.message`: guard feature or remove usage.
- `prema.ai.performance`: guard feature or remove usage.
- `prema.ai.proposal`: guard feature or remove usage.
- `prema.ai.realtime`: guard feature or remove usage.
- `prema.ai.self.heal`: guard feature or remove usage.
- `prema.ai.session`: guard feature or remove usage.
- `prema.anomaly.engine`: guard feature or remove usage.
- `prema.audit.log`: guard feature or remove usage.
- `prema.audit.session`: guard feature or remove usage.
- `prema.cleanup.plan`: guard feature or remove usage.
- `prema.cleanup.step`: guard feature or remove usage.
- `prema.config.service`: guard feature or remove usage.
- `prema.cra.engine`: guard feature or remove usage.
- `prema.fx.validator`: guard feature or remove usage.
- `prema.llm.service`: guard feature or remove usage.
- `prema.mapping.validator`: guard feature or remove usage.
- `prema.model.introspector`: guard feature or remove usage.
- `prema.openai.client`: guard feature or remove usage.
- `prema.performance.guard`: guard feature or remove usage.
- `prema.realtime.notifier`: guard feature or remove usage.
- `prema.reconcile.advisor`: guard feature or remove usage.
- `prema.risk.matrix`: guard feature or remove usage.
- `prema.schema.browser`: guard feature or remove usage.
- `prema.service.tool.registry`: guard feature or remove usage.
- `prema.severity.classifier`: guard feature or remove usage.
- `prema.tool.registry`: guard feature or remove usage.

## Missing Fields
| model | field | source | remediation |
|---|---|---|---|
| prema.ai.action.request | action_type | models/ai_action_request.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.action.request | expires_at | models/ai_action_request.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.action.request | model_name | models/ai_action_request.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.action.request | payload_json | models/ai_action_request.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.action.request | record_id | models/ai_action_request.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.config.audit | active | models/ai_config_audit.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.dashboard | status | models/ai_dashboard_metrics.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.document.processor | action_type | models/ai_document_processor.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.document.processor | advice | models/ai_document_processor.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.document.processor | amount_total | models/ai_document_processor.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.document.processor | classification | models/ai_document_processor.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.document.processor | move_type | models/ai_document_processor.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.document.processor | name | models/ai_document_processor.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.document.processor | payload_json | models/ai_document_processor.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.document.processor | rationale | models/ai_document_processor.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.document.processor | ref | models/ai_document_processor.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.document.processor | session_id | models/ai_document_processor.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.document.processor | status | models/ai_document_processor.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.document.processor | status | models/ai_document_processor.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.document.processor | target_model | models/ai_document_processor.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.document.processor | target_res_id | models/ai_document_processor.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.error.monitor | level | models/ai_error_monitor.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.integrity.engine | name | models/ai_integrity_engine.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.mail.monitor | state | models/ai_mail_monitor.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.memory | action | models/ai_memory.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.memory | action_request_id | models/ai_memory.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.memory | approved | models/ai_memory.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.memory | details | models/ai_memory.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.memory | executed | models/ai_memory.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.memory | issue_signature | models/ai_memory.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.memory | issue_signature | models/ai_memory.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.memory | model_name | models/ai_memory.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.memory | record_id | models/ai_memory.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.memory | resolution_summary | models/ai_memory.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.memory | status | models/ai_memory.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.memory | success_rate | models/ai_memory.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.memory | token | models/ai_memory.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.memory | usage_count | models/ai_memory.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.performance | create_date | models/ai_performance_monitor.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.performance | message | models/ai_performance_monitor.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.predictive | level | models/ai_predictive_model.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.proposal | apply_log | models/prema_ai_proposal.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.proposal | error_trace_masked | models/prema_ai_proposal.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.proposal | status | models/prema_ai_proposal.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.self.heal | active | models/ai_self_heal_engine.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.write.gate | action | models/ai_write_gate.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.write.gate | action_request_id | models/ai_write_gate.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.write.gate | approved | models/ai_write_gate.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.write.gate | details | models/ai_write_gate.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.write.gate | executed | models/ai_write_gate.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.write.gate | model_name | models/ai_write_gate.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.write.gate | record_id | models/ai_write_gate.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.write.gate | status | models/ai_write_gate.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.ai.write.gate | token | models/ai_write_gate.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.anomaly.engine | explanation | models/anomaly_engine.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.anomaly.engine | model_name | models/anomaly_engine.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.anomaly.engine | move_type | models/anomaly_engine.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.anomaly.engine | record_id | models/anomaly_engine.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.anomaly.engine | rule_name | models/anomaly_engine.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.anomaly.engine | severity | models/anomaly_engine.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.audit.engine | explanation | models/audit_engine.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.audit.engine | model_name | models/audit_engine.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.audit.engine | record_id | models/audit_engine.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.audit.engine | rule_name | models/audit_engine.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.audit.engine | severity | models/audit_engine.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.cleanup.plan | approved | models/cleanup_plan.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.cra.engine | amount_tax | models/cra_rule_engine.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.cra.engine | country_id.code | models/cra_rule_engine.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.cra.engine | explanation | models/cra_rule_engine.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.cra.engine | model_name | models/cra_rule_engine.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.cra.engine | move_type | models/cra_rule_engine.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.cra.engine | partner_id | models/cra_rule_engine.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.cra.engine | record_id | models/cra_rule_engine.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.cra.engine | rule_name | models/cra_rule_engine.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.cra.engine | severity | models/cra_rule_engine.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.cra.engine | state | models/cra_rule_engine.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.cra.engine | supplier_rank | models/cra_rule_engine.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.dedupe.service | file_sha256 | services/dedupe_service.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.fx.validator | currency_id | models/fx_validator.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.fx.validator | explanation | models/fx_validator.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.fx.validator | model_name | models/fx_validator.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.fx.validator | record_id | models/fx_validator.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.fx.validator | rule_name | models/fx_validator.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.fx.validator | severity | models/fx_validator.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.fx.validator | state | models/fx_validator.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.health.score | status | models/health_score.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.integrity.scanner | explanation | models/integrity_scanner.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.integrity.scanner | model_name | models/integrity_scanner.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.integrity.scanner | record_id | models/integrity_scanner.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.integrity.scanner | rule_name | models/integrity_scanner.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.integrity.scanner | severity | models/integrity_scanner.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.llm.service | action | services/llm_service.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.llm.service | details | services/llm_service.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.llm.service | status | services/llm_service.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.performance.guard | action | services/performance_guard.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.performance.guard | create_date | services/performance_guard.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.plan.generator | action_type | services/plan_generator.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.plan.generator | approval_stage | services/plan_generator.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.plan.generator | description | services/plan_generator.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.plan.generator | model_name | services/plan_generator.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.plan.generator | name | services/plan_generator.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.plan.generator | plan_id | services/plan_generator.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.plan.generator | record_id | services/plan_generator.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.plan.generator | risk_score | services/plan_generator.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.plan.generator | sequence | services/plan_generator.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.plan.generator | severity | services/plan_generator.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.plan.generator | status | services/plan_generator.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.plan.generator | summary | services/plan_generator.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.reconcile.advisor | amount_total | models/reconciliation_advisor.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.reconcile.advisor | explanation | models/reconciliation_advisor.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.reconcile.advisor | is_reconciled | models/reconciliation_advisor.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.reconcile.advisor | model_name | models/reconciliation_advisor.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.reconcile.advisor | record_id | models/reconciliation_advisor.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.reconcile.advisor | rule_name | models/reconciliation_advisor.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.reconcile.advisor | severity | models/reconciliation_advisor.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.risk.matrix | status | models/risk_matrix.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.service.tool.registry | action | services/tool_registry.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.service.tool.registry | details | services/tool_registry.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.service.tool.registry | status | services/tool_registry.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.tool.registry | display_type | models/tool_registry.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.tool.registry | is_complete | models/tool_registry.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.tool.registry | is_reconciled | models/tool_registry.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.tool.registry | move_id.state | models/tool_registry.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.tool.registry | move_type | models/tool_registry.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.tool.registry | state | models/tool_registry.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.tool.registry | tax_line_id | models/tool_registry.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.write.gate.service | action | services/write_gate.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.write.gate.service | details | services/write_gate.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.write.gate.service | model_name | services/write_gate.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.write.gate.service | record_id | services/write_gate.py | Add mapping override (ICP) or disable feature via soft guard. |
| prema.write.gate.service | status | services/write_gate.py | Add mapping override (ICP) or disable feature via soft guard. |

## Type Mismatches
- Static type mismatches are marked advisory only in this run; enrich extractor with write-path type inference for strict blocking.

## Studio/Custom-Only Field Risk
- Fields found in SQL snapshot but not runtime specs should be treated as environment-specific and guarded with config mapping.
