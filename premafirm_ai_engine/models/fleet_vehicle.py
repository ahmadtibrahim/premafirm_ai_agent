from odoo import fields, models, tools


class FleetVehicle(models.Model):
    _inherit = "fleet.vehicle"

    def _auto_init(self):
        """Ensure schema stays aligned when custom fields are added in code.

        In some environments (especially after partial restores or module sync issues),
        the ORM metadata can load while a physical PostgreSQL column is missing.
        This guard creates the backing column if it does not exist yet.
        """
        res = super()._auto_init()
        if not tools.sql.column_exists(self.env.cr, self._table, "x_studio_location"):
            tools.sql.create_column(self.env.cr, self._table, "x_studio_location", "varchar")
        return res

    # REMOVE x_studio_gvore completely
    # You already have Studio field: x_studio_gvwr_lbs

    x_studio_height_ft = fields.Float(string="Height (ft)", store=True)
    x_studio_current_load_lbs = fields.Integer(string="Current Load (lbs)", store=True)
    x_studio_x_is_busy = fields.Boolean(string="Is Busy", store=True)
    x_studio_location = fields.Char(string="Current Location", store=True)

    x_studio_service_type = fields.Selection(
        [("reefer", "Reefer"), ("dry", "Dry")],
        string="Default Service Type",
        store=True,
    )

    x_studio_load_type = fields.Selection(
        [("FTL", "FTL"), ("LTL", "LTL")],
        string="Default Load Type",
        store=True,
    )
