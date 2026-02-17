from odoo import api, fields, models

from ..services.dispatch_service import DispatchService


class CrmLead(models.Model):
    _inherit = "crm.lead"

    stop_ids = fields.One2many("premafirm.load.stop", "lead_id", string="Stops")

    x_studio_distance_km = fields.Float(compute="_compute_totals", store=True, readonly=True)
    x_studio_drive_hours = fields.Float(compute="_compute_totals", store=True, readonly=True)
    x_studio_estimated_cost = fields.Float(compute="_compute_totals", store=True, readonly=True)
    x_studio_target_profit = fields.Float(compute="_compute_totals", store=True, readonly=True)
    x_studio_suggested_rate = fields.Float(compute="_compute_totals", store=True, readonly=True)
    x_studio_ai_recommendation = fields.Text(compute="_compute_totals", store=True, readonly=True)

    x_studio_service_type = fields.Selection(
        [("reefer", "Reefer"), ("dry", "Dry")],
        compute="_compute_totals",
        store=True,
        readonly=True,
    )
    x_studio_load_type = fields.Selection(
        [("FTL", "FTL"), ("LTL", "LTL")],
        compute="_compute_totals",
        store=True,
        readonly=True,
    )

    x_studio_assigned_vehicle = fields.Many2one("fleet.vehicle", string="Assigned Vehicle")

    x_studio_total_pickup_dt = fields.Datetime(
        string="Total Pickup Date/Time",
        compute="_compute_totals",
        store=True,
        readonly=True,
    )
    x_studio_total_delivery_dt = fields.Datetime(
        string="Total Delivery Date/Time",
        compute="_compute_totals",
        store=True,
        readonly=True,
    )

    x_studio_aggregated_pallet_count = fields.Integer(
        string="Total Pallets",
        compute="_compute_totals",
        store=True,
        readonly=True,
    )
    x_studio_aggregated_load_weight_lbs = fields.Float(
        string="Total Load Weight Lbs",
        compute="_compute_totals",
        store=True,
        readonly=True,
    )

    @api.depends(
        "stop_ids.sequence",
        "stop_ids.address",
        "stop_ids.stop_pickup_dt",
        "stop_ids.stop_delivery_dt",
        "stop_ids.pallet_count",
        "stop_ids.load_weight_lbs",
        "stop_ids.service_type",
        "stop_ids.load_type",
        "x_studio_assigned_vehicle",
        "x_studio_assigned_vehicle.x_studio_location",
        "x_studio_assigned_vehicle.x_studio_service_type",
        "x_studio_assigned_vehicle.x_studio_load_type",
        "x_studio_assigned_vehicle.x_studio_height_ft",
        "x_studio_assigned_vehicle.x_studio_gvore",
    )
    def _compute_totals(self):
        service = DispatchService(self.env)
        for lead in self:
            result = service.compute_lead_totals(lead)
            lead.x_studio_distance_km = result["distance_km"]
            lead.x_studio_drive_hours = result["drive_hours"]
            lead.x_studio_estimated_cost = result["estimated_cost"]
            lead.x_studio_target_profit = result["target_profit"]
            lead.x_studio_suggested_rate = result["suggested_rate"]
            lead.x_studio_ai_recommendation = result["ai_recommendation"]
            lead.x_studio_service_type = result["service_type"]
            lead.x_studio_load_type = result["load_type"]
            lead.x_studio_total_pickup_dt = result["total_pickup_dt"]
            lead.x_studio_total_delivery_dt = result["total_delivery_dt"]
            lead.x_studio_aggregated_pallet_count = result["aggregated_pallet_count"]
            lead.x_studio_aggregated_load_weight_lbs = result["aggregated_load_weight_lbs"]

    @api.onchange("x_studio_assigned_vehicle")
    def _onchange_assigned_vehicle_prefill_first_stop(self):
        for lead in self:
            if not lead.x_studio_assigned_vehicle or lead.stop_ids:
                continue
            base_address = lead.x_studio_assigned_vehicle.x_studio_location
            if base_address:
                lead.stop_ids = [
                    (0, 0, {
                        "sequence": 1,
                        "is_pickup": True,
                        "address": base_address,
                        "notes": "Autofilled from assigned vehicle location",
                    })
                ]

    @api.model_create_multi
    def create(self, vals_list):
        leads = super().create(vals_list)
        for lead, vals in zip(leads, vals_list):
            if vals.get("stop_ids"):
                continue
            vehicle_id = vals.get("x_studio_assigned_vehicle")
            if not vehicle_id:
                continue
            vehicle = self.env["fleet.vehicle"].browse(vehicle_id)
            if vehicle and vehicle.x_studio_location:
                lead.stop_ids = [
                    (0, 0, {
                        "sequence": 1,
                        "is_pickup": True,
                        "address": vehicle.x_studio_location,
                        "notes": "Autofilled from assigned vehicle location",
                    })
                ]
        return leads
