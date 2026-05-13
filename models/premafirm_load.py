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
    fleetbase_order_id = fields.Char(string="Fleetbase Order ID", index=True)
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

    def action_fetch_from_fleetbase(self):
        """Fetch job completion data from Fleetbase and populate stops."""
        self.ensure_one()
        if not self.fleetbase_order_id:
            raise exceptions.UserError("No Fleetbase Order ID is set on this load.")
        from ..services.fleetbase_service import FleetbaseService
        try:
            fb = FleetbaseService(self.env)
            completion = fb.get_order_completion(self.fleetbase_order_id)
        except Exception as e:
            raise exceptions.UserError(f"Could not fetch from Fleetbase: {e}")

        # Populate stops from completion data
        if completion.get("waypoints"):
            self.stop_ids.unlink()
            stop_vals = []
            for i, wp in enumerate(completion["waypoints"]):
                stop_vals.append({
                    "load_id":            self.id,
                    "sequence":           (i + 1) * 10,
                    "stop_type":          wp.get("type", "delivery") if i > 0 else "pickup",
                    "name":               wp.get("name") or wp.get("place", {}).get("name", ""),
                    "address":            wp.get("address") or wp.get("place", {}).get("formatted_address", ""),
                    "arrival_time":       self._parse_fb_dt(wp.get("arrived_at")),
                    "departure_time":     self._parse_fb_dt(wp.get("departed_at")),
                    "signature_name":     wp.get("pod_signature_name", ""),
                    "notes":              wp.get("tracking_number") or wp.get("notes", ""),
                })
            self.env["premafirm.load.stop"].create(stop_vals)

        update_vals = {"state": "completed"}
        if completion.get("updated_at"):
            update_vals["completed_at"] = self._parse_fb_dt(completion["updated_at"])
        if completion.get("notes"):
            update_vals["driver_notes"] = completion["notes"]
        self.write(update_vals)

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Fetched from Fleetbase",
                "message": f"Loaded {len(self.stop_ids)} stops from order {self.fleetbase_order_id}.",
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

    @api.model
    def action_sync_completed_from_fleetbase(self):
        """Cron entry point: find all dispatched-but-unsynced jobs and sync from Fleetbase."""
        from ..services.fleetbase_service import FleetbaseService
        ICP = self.env["ir.config_parameter"].sudo()
        if not (ICP.get_param("fleetbase.enabled") == "True"):
            return

        try:
            fb = FleetbaseService(self.env)
            from datetime import timedelta
            since = datetime.utcnow() - timedelta(days=7)
            completed_orders = fb.fetch_completed_orders(since)
        except Exception as e:
            _logger.warning("Fleetbase sync failed: %s", e)
            return

        for order_data in completed_orders:
            order_id = order_data.get("id")
            if not order_id:
                continue
            # Find matching load record
            existing = self.search([("fleetbase_order_id", "=", order_id)], limit=1)
            if not existing:
                # Find via estimator
                estimator = self.env["premafirm.rate.estimator"].sudo().search(
                    [("fleetbase_order_id", "=", order_id)], limit=1
                )
                if estimator:
                    existing = self.create({
                        "fleetbase_order_id": order_id,
                        "estimator_id":       estimator.id,
                        "invoice_id":         estimator.invoice_id.id if estimator.invoice_id else False,
                        "vehicle_id":         estimator.vehicle_id.id if estimator.vehicle_id else False,
                        "job_day_ref":        estimator.job_day_ref or "",
                        "state":              "pending",
                    })
            if existing and existing.state not in ("pod_generated",):
                try:
                    existing.action_fetch_from_fleetbase()
                    if existing.state == "completed":
                        existing.action_generate_pod_pdf()
                except Exception as e:
                    _logger.warning("Could not sync/generate POD for load %s: %s", existing.id, e)
