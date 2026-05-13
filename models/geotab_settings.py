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
    # Fleetbase dispatch integration
    # ------------------------------------------------------------------

    fleetbase_enabled = fields.Boolean(
        string="Enable Fleetbase Integration",
        config_parameter="fleetbase.enabled",
    )
    fleetbase_api_url = fields.Char(
        string="Fleetbase API URL",
        config_parameter="fleetbase.api_url",
        default="https://api.fleetbase.io",
        help="Base URL for your Fleetbase instance, e.g. https://api.fleetbase.io",
    )
    fleetbase_api_key = fields.Char(
        string="Fleetbase API Key",
        config_parameter="fleetbase.api_key",
        help="Bearer token from Fleetbase › Settings › API Keys",
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

    def action_test_fleetbase_connection(self):
        from ..services.fleetbase_service import FleetbaseService
        try:
            FleetbaseService(self.env).test_connection()
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Fleetbase Connection OK",
                    "message": "Successfully connected to Fleetbase API.",
                    "type": "success",
                    "sticky": False,
                },
            }
        except Exception as e:
            raise UserError(f"Fleetbase connection failed: {e}")


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
