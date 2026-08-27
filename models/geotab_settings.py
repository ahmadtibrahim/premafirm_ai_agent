from odoo import api, fields, models
from odoo.exceptions import UserError


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # ------------------------------------------------------------------
    # Geotab enable / connection
    # ------------------------------------------------------------------

    geotab_enabled = fields.Boolean(
        string="Enable Geotab Integration",
        config_parameter="geotab.enabled",
    )
    geotab_database = fields.Char(
        string="Geotab Database",
        config_parameter="geotab.database",
    )
    geotab_username = fields.Char(
        string="Geotab Username",
        config_parameter="geotab.username",
    )
    geotab_password = fields.Char(
        string="Geotab Password",
        config_parameter="geotab.password",
    )
    geotab_server_url = fields.Char(
        string="Geotab Server URL",
        config_parameter="geotab.server_url",
        default="my.geotab.com",
    )

    # ------------------------------------------------------------------
    # Sync toggles
    # ------------------------------------------------------------------

    geotab_sync_vehicles_enabled = fields.Boolean(
        string="Sync Vehicles",
        config_parameter="geotab.sync_vehicles_enabled",
    )
    geotab_sync_telematics_enabled = fields.Boolean(
        string="Sync Telematics",
        config_parameter="geotab.sync_telematics_enabled",
    )
    geotab_sync_fuel_enabled = fields.Boolean(
        string="Sync Fuel Averages",
        config_parameter="geotab.sync_fuel_enabled",
    )
    geotab_sync_drivers_enabled = fields.Boolean(
        string="Sync Drivers",
        config_parameter="geotab.sync_drivers_enabled",
    )
    geotab_sync_driver_logs_enabled = fields.Boolean(
        string="Sync Driver Logs",
        config_parameter="geotab.sync_driver_logs_enabled",
    )
    geotab_auto_create_drivers = fields.Boolean(
        string="Auto-Create Driver Contacts",
        config_parameter="geotab.auto_create_drivers",
        help="If enabled, Geotab drivers without a matching Contact will create a new Contact with the Driver tag.",
    )

    # ------------------------------------------------------------------
    # Sync interval / timezone
    # ------------------------------------------------------------------

    geotab_sync_interval_minutes = fields.Integer(
        string="Sync Interval (minutes)",
        config_parameter="geotab.sync_interval_minutes",
        default=30,
    )
    geotab_default_timezone = fields.Char(
        string="Default Timezone",
        config_parameter="geotab.default_timezone",
        default="America/Toronto",
    )
    geotab_last_sync_at = fields.Char(
        string="Last Sync",
        config_parameter="geotab.last_sync_at",
        readonly=True,
    )

    # ------------------------------------------------------------------
    # Google Maps
    # ------------------------------------------------------------------

    google_maps_api_key = fields.Char(
        string="Google Maps API Key",
        config_parameter="google_maps_api_key",
        help="Used for address autocomplete on vehicle location fields. Requires Places API enabled.",
    )

    # ------------------------------------------------------------------
    # Test Connection button
    # ------------------------------------------------------------------

    def set_values(self):
        super().set_values()
        self._apply_geotab_crons()

    def _apply_geotab_crons(self):
        """Activate or deactivate Geotab crons based on the current settings."""
        enabled = self.geotab_enabled
        cron_map = {
            "premafirm_ai_engine.ir_cron_geotab_vehicle_sync":    self.geotab_sync_vehicles_enabled,
            "premafirm_ai_engine.ir_cron_geotab_telematics_sync": self.geotab_sync_telematics_enabled,
            "premafirm_ai_engine.ir_cron_geotab_fuel_averages_sync": self.geotab_sync_fuel_enabled,
            "premafirm_ai_engine.ir_cron_geotab_driver_sync":     self.geotab_sync_drivers_enabled,
            "premafirm_ai_engine.ir_cron_geotab_driver_log_sync": self.geotab_sync_driver_logs_enabled,
        }
        interval = max(10, self.geotab_sync_interval_minutes or 30)
        for xmlid, sync_flag in cron_map.items():
            try:
                cron = self.env.ref(xmlid)
                cron.active = bool(enabled and sync_flag)
                if "telematics" in xmlid:
                    cron.interval_number = interval
                    cron.interval_type = "minutes"
            except Exception:
                pass

    def action_test_geotab_connection(self):
        from ..services.geotab_service import GeotabService
        result = GeotabService(self.env).test_connection()
        if result["success"]:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Geotab Connection OK",
                    "message": result["message"],
                    "type": "success",
                    "sticky": False,
                },
            }
        raise UserError(f"Geotab connection failed: {result['message']}")

    # ------------------------------------------------------------------
    # Run Sync Now button
    # ------------------------------------------------------------------

    def action_run_geotab_sync_now(self):
        self.env["premafirm.geotab.sync"].run_all_syncs()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Geotab Sync Started",
                "message": "All Geotab sync jobs have been triggered.",
                "type": "info",
                "sticky": False,
            },
        }

    # ------------------------------------------------------------------
    # Hub Location (for estimator origin/return)
    # ------------------------------------------------------------------

    hub_location_name = fields.Char(
        string="Hub Location Name",
        config_parameter="estimator.hub_name",
        help="Name of the central hub/depot. Used as the start/end point for estimator route calculations.",
    )
    hub_location_address = fields.Char(
        string="Hub Address",
        config_parameter="estimator.hub_address",
        help="Formatted address of the central hub.",
    )
    hub_location_lat = fields.Float(
        string="Hub Latitude",
        config_parameter="estimator.hub_lat",
        digits=(10, 6),
    )
    hub_location_lng = fields.Float(
        string="Hub Longitude",
        config_parameter="estimator.hub_lng",
        digits=(10, 6),
    )
    hub_location_place_id = fields.Char(
        string="Hub Google Place ID",
        config_parameter="estimator.hub_place_id",
    )

    # ------------------------------------------------------------------
    # Freight Product Mapping (for booking → invoice product selection)
    # ------------------------------------------------------------------

    product_ca_dry_ltl_id = fields.Many2one(
        "product.product", string="Canada Dry LTL Product",
        config_parameter="logistics.product_ca_dry_ltl_id",
        help="Product used for Canadian domestic Dry LTL bookings.",
    )
    product_ca_reefer_ltl_id = fields.Many2one(
        "product.product", string="Canada Reefer LTL Product",
        config_parameter="logistics.product_ca_reefer_ltl_id",
        help="Product used for Canadian domestic Reefer LTL bookings.",
    )
    product_us_dry_ltl_id = fields.Many2one(
        "product.product", string="USA Dry LTL Product",
        config_parameter="logistics.product_us_dry_ltl_id",
        help="Product used for USA/export Dry LTL bookings.",
    )
    product_us_reefer_ltl_id = fields.Many2one(
        "product.product", string="USA Reefer LTL Product",
        config_parameter="logistics.product_us_reefer_ltl_id",
        help="Product used for USA/export Reefer LTL bookings. Leave empty if no USA Reefer product exists.",
    )

    # FTL product mappings
    product_ca_dry_ftl_id = fields.Many2one(
        "product.product", string="Canada Dry FTL Product",
        config_parameter="logistics.product_ca_dry_ftl_id",
        help="Product used for Canadian domestic Dry FTL bookings.",
    )
    product_ca_reefer_ftl_id = fields.Many2one(
        "product.product", string="Canada Reefer FTL Product",
        config_parameter="logistics.product_ca_reefer_ftl_id",
        help="Product used for Canadian domestic Reefer FTL bookings.",
    )
    product_us_dry_ftl_id = fields.Many2one(
        "product.product", string="USA Dry FTL Product",
        config_parameter="logistics.product_us_dry_ftl_id",
        help="Product used for USA/export Dry FTL bookings.",
    )
    product_us_reefer_ftl_id = fields.Many2one(
        "product.product", string="USA Reefer FTL Product",
        config_parameter="logistics.product_us_reefer_ftl_id",
        help="Product used for USA/export Reefer FTL bookings. Leave empty if none.",
    )

    # ------------------------------------------------------------------
    # Invoice BCC
    # ------------------------------------------------------------------

    invoice_bcc_partner_id = fields.Many2one(
        'res.partner',
        string='Invoice BCC Contact',
        related='company_id.x_invoice_bcc_partner_id',
        readonly=False,
        help='Silently receives a copy of every invoice email. Customer cannot see this recipient.',
    )
