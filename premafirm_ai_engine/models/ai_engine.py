from odoo import api, fields, models
from ..services.dispatch_service import DispatchService


class CrmLead(models.Model):
    _inherit = "crm.lead"

    stop_ids = fields.One2many(
        "premafirm.load.stop",
        "lead_id",
        string="Stops",
    )

    x_studio_distance_km = fields.Float(compute="_compute_totals", store=True)
    x_studio_drive_hours = fields.Float(compute="_compute_totals", store=True)
    x_studio_estimated_cost = fields.Float(compute="_compute_totals", store=True)
    x_studio_target_profit = fields.Float(compute="_compute_totals", store=True)
    x_studio_suggested_rate = fields.Float(compute="_compute_totals", store=True)
    x_studio_ai_recommendation = fields.Text(compute="_compute_totals", store=True)

    x_studio_service_type = fields.Selection(
        [("reefer", "Reefer"), ("dry", "Dry")],
        compute="_compute_totals",
        store=True,
    )

    x_studio_load_type = fields.Selection(
        [("FTL", "FTL"), ("LTL", "LTL")],
        compute="_compute_totals",
        store=True,
    )

    x_studio_assigned_vehicle = fields.Many2one(
        "fleet.vehicle",
        string="Assigned Vehicle",
    )

    x_studio_total_pickup_dt = fields.Datetime(
        compute="_compute_totals", store=True
    )
    x_studio_total_delivery_dt = fields.Datetime(
        compute="_compute_totals", store=True
    )
    x_studio_aggregated_pallet_count = fields.Integer(
        compute="_compute_totals", store=True
    )
    x_studio_aggregated_load_weight_lbs = fields.Float(
        compute="_compute_totals", store=True
    )

    def action_premafirm_ai_price(self):
        service = DispatchService(self.env)
        for lead in self:
            result = service.compute_lead_totals(lead)
            lead.update({
                "x_studio_distance_km": result["distance_km"],
                "x_studio_drive_hours": result["drive_hours"],
                "x_studio_estimated_cost": result["estimated_cost"],
                "x_studio_target_profit": result["target_profit"],
                "x_studio_suggested_rate": result["suggested_rate"],
                "x_studio_ai_recommendation": result["ai_recommendation"],
                "x_studio_service_type": result["service_type"],
                "x_studio_load_type": result["load_type"],
                "x_studio_total_pickup_dt": result["total_pickup_dt"],
                "x_studio_total_delivery_dt": result["total_delivery_dt"],
                "x_studio_aggregated_pallet_count": result["aggregated_pallet_count"],
                "x_studio_aggregated_load_weight_lbs": result["aggregated_load_weight_lbs"],
            })

    @api.depends(
        "stop_ids.sequence",
        "stop_ids.address",
        "stop_ids.stop_type",
        "stop_ids.x_studio_pickup_dt",
        "stop_ids.x_studio_delivery_dt",
        "stop_ids.pallets",
        "stop_ids.weight_lbs",
        "x_studio_assigned_vehicle",
    )
    def _compute_totals(self):
        service = DispatchService(self.env)
        for lead in self:
            result = service.compute_lead_totals(lead)
            lead.update({
                "x_studio_distance_km": result["distance_km"],
                "x_studio_drive_hours": result["drive_hours"],
                "x_studio_estimated_cost": result["estimated_cost"],
                "x_studio_target_profit": result["target_profit"],
                "x_studio_suggested_rate": result["suggested_rate"],
                "x_studio_ai_recommendation": result["ai_recommendation"],
                "x_studio_service_type": result["service_type"],
                "x_studio_load_type": result["load_type"],
                "x_studio_total_pickup_dt": result["total_pickup_dt"],
                "x_studio_total_delivery_dt": result["total_delivery_dt"],
                "x_studio_aggregated_pallet_count": result["aggregated_pallet_count"],
                "x_studio_aggregated_load_weight_lbs": result["aggregated_load_weight_lbs"],
            })
