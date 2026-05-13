import logging
from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class PremafirmBillScanImport(models.Model):
    _name = "premafirm.bill.scan.import"
    _description = "Bill Scan Import"
    _order = "create_date desc"
    _rec_name = "document_name"

    document_id = fields.Many2one(
        "documents.document", string="Source Document", readonly=True, ondelete="set null"
    )
    document_name = fields.Char(related="document_id.name", string="File Name", store=True, readonly=True)

    state = fields.Selection([
        ("pending",  "Pending"),
        ("done",     "Done"),
        ("error",    "Error"),
    ], default="pending", required=True, readonly=True)

    bill_id = fields.Many2one("account.move", string="Created Bill", readonly=True, ondelete="set null")

    # AI extraction results (all readonly — set by the service)
    ai_vendor_name    = fields.Char("AI: Vendor",     readonly=True)
    ai_invoice_number = fields.Char("AI: Invoice #",  readonly=True)
    ai_invoice_date   = fields.Date("AI: Date",       readonly=True)
    ai_due_date       = fields.Date("AI: Due Date",   readonly=True)
    ai_total          = fields.Float("AI: Total",     readonly=True, digits=(16, 2))
    ai_subtotal       = fields.Float("AI: Subtotal",  readonly=True, digits=(16, 2))
    ai_tax_amount     = fields.Float("AI: Tax",       readonly=True, digits=(16, 2))
    ai_currency       = fields.Char("AI: Currency",   readonly=True)
    ai_raw_json       = fields.Text("AI: Raw JSON",   readonly=True)

    error_message = fields.Text("Error Details", readonly=True)
    processed_at  = fields.Datetime("Processed At",  readonly=True)

    # ── Actions ─────────────────────────────────────────────────────────────────

    def action_open_bill(self):
        self.ensure_one()
        if not self.bill_id:
            raise UserError("No bill has been created for this import yet.")
        return {
            "type": "ir.actions.act_window",
            "name": "Vendor Bill",
            "res_model": "account.move",
            "res_id": self.bill_id.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_reprocess(self):
        """Re-run AI extraction for this record (only useful when state is error)."""
        self.ensure_one()
        if self.state == "done":
            raise UserError("Already successfully processed. Open the bill to review it.")
        self.write({"state": "pending", "error_message": False, "ai_raw_json": False})
        from ..services.bill_scan_service import BillScanService
        BillScanService(self.env).process_import_record(self)

    # ── Model-level: discover files ──────────────────────────────────────────────

    @api.model
    def action_scan_folder_only(self):
        """Step 1 — discover unprocessed files in bills-entry and create pending records."""
        from ..services.bill_scan_service import BillScanService
        try:
            count = BillScanService(self.env).scan_folder_only()
        except ValueError as e:
            raise UserError(str(e))
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Discovery Complete",
                "message": (
                    f"{count} new file(s) queued. Select them below and click 'Process Selected'."
                    if count else
                    "No new files found in bills-entry — all documents are already queued."
                ),
                "type": "success" if count else "warning",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    # ── Recordset-level: process selected ────────────────────────────────────────

    def action_process_selected(self):
        """Step 2 — AI-process each selected pending import record."""
        if not self:
            raise UserError("No records selected. Select one or more pending imports first.")
        from ..services.bill_scan_service import BillScanService
        service = BillScanService(self.env)
        done = errors = skipped = 0
        for imp in self:
            if imp.state == "done":
                skipped += 1
                continue
            service.process_import_record(imp)
            if imp.state == "done":
                done += 1
            else:
                errors += 1

        parts = []
        if done:
            parts.append(f"{done} bill(s) created")
        if errors:
            parts.append(f"{errors} error(s) — check error details")
        if skipped:
            parts.append(f"{skipped} already done")

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Processing Complete",
                "message": "; ".join(parts) + "." if parts else "Nothing to process.",
                "type": "success" if not errors else "warning",
                "sticky": bool(errors),
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }
