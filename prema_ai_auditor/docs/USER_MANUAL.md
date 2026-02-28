# Prema AI Auditor User Manual

## 1. Overview
Prema AI Auditor provides AI-assisted audit monitoring, cleanup planning, proposal approvals, schema inspection, and operational dashboards for Odoo 18.

## 2. Installation steps
1. Place module `prema_ai_auditor` in your Odoo addons path.
2. Update apps list in Odoo.
3. Install **Prema AI Auditor** from Apps.
4. Verify install logs contain no `RPC_ERROR`, invalid model, or invalid field failures.

## 3. Required dependencies
Defined in `__manifest__.py`:
- account
- account_accountant
- fleet
- crm
- sale
- purchase
- documents
- mail
- web
- bus

## 4. Configuration (API keys, parameters)
1. Open Settings and configure system parameters needed by integrated AI services.
2. Ensure keys are stored only in Odoo system parameters and never in code.
3. Use least-privilege API keys and rotate periodically.

## 5. User roles explained
- **Prema AI Master Control** (`group_prema_ai_master`): full operational access.
- **Prema AI Auditor Manager** (`group_prema_ai_auditor`): read-oriented auditing visibility for protected models.

## 6. How to run audit
1. Open **Prema AI Auditor → Dashboard**.
2. Trigger available audit actions from dashboard workflows.
3. For scheduled audits, confirm cron job **Prema AI Audit Scan** is active.

## 7. Understanding logs
- Open **Audit Logs** to inspect finding severity, rule names, and status.
- Use graph/kanban/list/form monitoring views for triage and trend analysis.

## 8. Cleanup plan workflow
1. Open **Cleanup Plans**.
2. Review generated cleanup steps and risk data.
3. Process related **Proposals** through submit/approve/reject/apply states.
4. Apply changes only through the approval workflow.

## 9. Cron explanation
Two production jobs are loaded:
- **Prema AI Audit Scan** → executes `model.run_default_scan()` daily.
- **Prema AI Document Processing** → executes `model.cron_process_pending_documents()` every minute.

## 10. Troubleshooting
- If you encounter `ValueError: Invalid field 'numbercall' on model 'ir.cron'`, remove `numbercall` from cron XML definitions (Odoo 18 no longer supports this field).
- If a menu fails, verify referenced action IDs exist and corresponding models are loaded.
- If access errors occur, confirm user is in the correct Prema AI security group.

## 11. Reference folder explanation
The `reference/` directory is the production alignment source-of-truth for:
- installed models and fields snapshots,
- installed modules list,
- runtime/python model specifications,
- operational environment metadata.

Use these files during audits and hardening to validate model/field mappings before deployment.
