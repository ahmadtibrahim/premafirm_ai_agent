# No Auto-Write Enforcement Report

| location | call | gating_status |
|---|---|---|
| models/prema_ai_incident.py:37 | `incident = self.create(vals)` | review_required |
| models/ai_document_processor.py:129 | `document.write(update_values)` | review_required |
| models/ai_document_processor.py:241 | `proposal = self.env["prema.ai.proposal"].sudo().create(` | review_required |
| models/ai_document_processor.py:259 | `doc.write({"status": "draft_created", "advice": f"Draft proposal generated: {proposal.name}"})` | review_required |
| models/prema_ai_proposal.py:43 | `self.write({"status": "pending_approval"})` | gated |
| models/prema_ai_proposal.py:48 | `self.write({"status": "approved"})` | gated |
| models/prema_ai_proposal.py:51 | `self.write({"status": "rejected"})` | gated |
| models/prema_ai_proposal.py:60 | `proposal.write({"status": "failed", "error_trace_masked": "Target record does not exist."})` | gated |
| models/prema_ai_proposal.py:62 | `target.write(proposal.payload_json)` | gated |
| models/prema_ai_proposal.py:63 | `proposal.write({"status": "applied", "apply_log": "Write proposal applied successfully."})` | gated |
| models/prema_ai_proposal.py:67 | `created = self.env[proposal.target_model].create(proposal.payload_json)` | gated |
| models/prema_ai_proposal.py:68 | `proposal.write({"status": "applied", "apply_log": f"Create proposal applied successfully (ID {created.id})."})` | gated |
| models/anomaly_engine.py:25 | `self.env["prema.audit.log"].create(` | review_required |
| models/prema_ai_document.py:25 | `records = super().create(vals_list)` | review_required |
| models/ai_action_request.py:32 | `return self.create(` | review_required |
| models/ai_memory.py:48 | `memory.write(` | review_required |
| models/ai_memory.py:56 | `memory = env["prema.ai.memory"].create(` | review_required |
| models/ai_memory.py:66 | `env["prema.ai.audit.log"].create(` | review_required |
| models/reconciliation_advisor.py:19 | `self.env["prema.audit.log"].create(` | review_required |
| models/audit_engine.py:12 | `self.env["prema.audit.log"].create(` | review_required |
| models/integrity_scanner.py:51 | `self.env["prema.audit.log"].create(` | review_required |
| models/ai_write_gate.py:32 | `record.write(req.payload_json)` | gated |
| models/ai_write_gate.py:35 | `self.env["prema.ai.audit.log"].create(` | gated |
| models/fx_validator.py:15 | `self.env["prema.audit.log"].create(` | review_required |
| models/cra_rule_engine.py:15 | `self.env["prema.audit.log"].create(` | review_required |
| models/cra_rule_engine.py:35 | `self.env["prema.audit.log"].create(` | review_required |
| models/cleanup_plan.py:32 | `steps.write({"approved": True})` | review_required |
| controllers/chat_controller.py:8 | `session = request.env["prema.ai.session"].sudo().create(` | review_required |
| controllers/chat_controller.py:22 | `request.env["prema.ai.message"].sudo().create(` | review_required |
| controllers/chat_controller.py:49 | `request.env["prema.ai.message"].sudo().create(` | review_required |
| controllers/chat_controller.py:68 | `wizard = request.env["prema.schema.browser"].sudo().create({"model_name": model_name})` | review_required |
| controllers/upload_controller.py:20 | `attachment = request.env["ir.attachment"].sudo().create(` | review_required |
| controllers/upload_controller.py:31 | `request.env["prema.ai.document"].sudo().create(` | review_required |
| services/tool_registry.py:28 | `self.env["prema.ai.audit.log"].create(` | review_required |
| services/plan_generator.py:38 | `plan = self.env["prema.cleanup.plan"].create(` | review_required |
| services/plan_generator.py:48 | `self.env["prema.cleanup.step"].create(` | review_required |
| services/write_gate.py:16 | `self.env["prema.ai.audit.log"].sudo().create(` | gated |
| services/llm_service.py:48 | `self.env["prema.ai.audit.log"].create(` | review_required |
