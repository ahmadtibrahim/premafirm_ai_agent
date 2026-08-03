import json
import logging
from datetime import datetime

from odoo import api, fields, models, exceptions

_logger = logging.getLogger(__name__)


class PremafirmLoadStop(models.Model):
    _name = "premafirm.load.stop"
    _description = "Load Stop (POD)"
    _order = "load_id, sequence"

    load_id          = fields.Many2one("premafirm.load", required=True, ondelete="cascade", index=True)
    sequence         = fields.Integer(default=10)
    stop_type        = fields.Selection([("pickup", "Pickup"), ("delivery", "Delivery")],
                                         default="pickup", required=True)

    # Address
    name             = fields.Char(string="Company Name")
    address          = fields.Char(string="Address")
    lat              = fields.Float(digits=(10, 6))
    lng              = fields.Float(digits=(10, 6))

    # Scheduling
    scheduled_datetime = fields.Datetime(string="Scheduled")
    arrival_time     = fields.Datetime(string="Actual Arrival")
    departure_time   = fields.Datetime(string="Actual Departure")

    # Load
    pallets          = fields.Integer()
    weight_lbs       = fields.Float(string="Weight (lbs)", digits=(10, 1))
    product_id       = fields.Many2one("product.product", string="Product")

    # POD proof
    signature_name   = fields.Char(string="Signed By")
    signature_image  = fields.Binary(string="Signature")
    photo_ids        = fields.One2many("ir.attachment", "res_id",
                                       domain=[("res_model", "=", "premafirm.load.stop")],
                                       string="Photos")
    notes            = fields.Text()


class PremafirmLoad(models.Model):
    _name = "premafirm.load"
    _description = "Load / POD Record"
    _order = "create_date desc"
    _inherit = ["mail.thread", "mail.activity.mixin"]

    name             = fields.Char(compute="_compute_name", store=True)

    # Relationships
    invoice_id       = fields.Many2one("account.move", string="Invoice", ondelete="set null",
                                        index=True)
    estimator_id     = fields.Many2one("premafirm.rate.estimator", string="Dispatch Job",
                                        ondelete="set null")
    sale_order_id    = fields.Many2one("sale.order", string="Sale Order", ondelete="set null")
    company_id       = fields.Many2one("res.company", default=lambda self: self.env.company)

    # Vehicle / driver
    vehicle_id       = fields.Many2one("fleet.vehicle", string="Truck", ondelete="set null")
    driver_id        = fields.Many2one("res.partner", string="Driver", ondelete="set null")

    # Job info
    job_day_ref      = fields.Char(string="Job Day")
    bol_number       = fields.Char(string="BOL #")
    seal_number      = fields.Char(string="Seal #")

    # Load specs
    reefer_required  = fields.Boolean(string="Reefer Required")
    reefer_setpoint_c = fields.Float(string="Reefer Setpoint (°C)", digits=(5, 1))
    hos_warning_text = fields.Text(string="HOS Warning")

    # POD completion
    stop_ids         = fields.One2many("premafirm.load.stop", "load_id", string="Stops")
    completed_at     = fields.Datetime(string="Completed At")
    driver_notes     = fields.Text(string="Driver Notes")
    pod_pdf_id       = fields.Many2one("ir.attachment", string="POD PDF", readonly=True,
                                        ondelete="set null")

    state            = fields.Selection([
        ("pending",       "Pending"),
        ("in_transit",    "In Transit"),
        ("completed",     "Completed"),
        ("pod_generated", "POD Generated"),
    ], default="pending", tracking=True)

    @api.depends("job_day_ref", "vehicle_id")
    def _compute_name(self):
        for rec in self:
            parts = []
            if rec.job_day_ref:
                parts.append(rec.job_day_ref)
            elif rec.vehicle_id:
                parts.append(rec.vehicle_id.name)
            if not parts:
                parts.append("Load")
            rec.name = " — ".join(parts)

    def _get_pickup_for_delivery(self, delivery_stop):
        """Return the first pickup stop (used by POD report template)."""
        return self.stop_ids.filtered(lambda s: s.stop_type == "pickup").sorted("sequence")[:1]

    def _get_delivery_allocations(self, delivery_stop):
        """Return allocation dicts for a delivery stop (pallets, weight)."""
        return [{"pallets": delivery_stop.pallets, "weight_lbs": delivery_stop.weight_lbs}]

    def action_generate_pod_pdf(self):
        """Render the POD QWeb PDF and attach it to this record and the invoice."""
        self.ensure_one()
        Report = self.env["ir.actions.report"]
        try:
            pdf_content, _mime = Report.sudo()._render_qweb_pdf(
                "premafirm_ai_engine.action_report_premafirm_load_pod",
                res_ids=[self.id],
            )
        except Exception as e:
            _logger.error("POD PDF generation failed for load %s: %s", self.id, e, exc_info=True)
            raise exceptions.UserError(f"Could not generate POD PDF: {e}")

        filename = f"POD - {self.job_day_ref or self.name}.pdf"
        Attach = self.env["ir.attachment"].sudo()
        attachment = Attach.create({
            "name":      filename,
            "type":      "binary",
            "datas":     __import__("base64").b64encode(pdf_content).decode(),
            "res_model": "premafirm.load",
            "res_id":    self.id,
            "mimetype":  "application/pdf",
        })
        self.write({"pod_pdf_id": attachment.id, "state": "pod_generated"})

        # Also attach to the invoice
        if self.invoice_id:
            attachment.copy({
                "res_model": "account.move",
                "res_id":    self.invoice_id.id,
            })
            self.invoice_id.message_post(
                body=f"POD generated for {self.job_day_ref or self.name} and attached to this invoice."
            )

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "POD Generated",
                "message": f"'{filename}' has been attached to this record and the invoice.",
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    @staticmethod
    def _parse_fb_dt(value):
        if not value:
            return None
        text = str(value).strip()
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(text, fmt)
            except Exception:
                continue
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            return None

