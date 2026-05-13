from odoo import api, fields, models


class PremafirmEstimatorStop(models.Model):
    _name = "premafirm.estimator.stop"
    _description = "Estimator Stop"
    _order = "estimator_id, sequence"

    estimator_id   = fields.Many2one("premafirm.rate.estimator", required=True,
                                      ondelete="cascade", index=True)
    sequence       = fields.Integer(default=10)
    stop_type      = fields.Selection([
        ("origin",   "Origin (System)"),
        ("pickup",   "Pickup"),
        ("delivery", "Delivery"),
        ("return",   "Return (System)"),
    ], default="pickup", required=True, string="Type")
    is_system      = fields.Boolean(default=False,
                                    help="Auto-added by truck selection (origin/return from ELD).")

    # Address
    name           = fields.Char(string="Place Name")
    address        = fields.Char(string="Formatted Address")
    place_id       = fields.Char(string="Google Place ID")
    lat            = fields.Float(digits=(10, 6))
    lng            = fields.Float(digits=(10, 6))

    # Scheduling
    scheduled_time = fields.Datetime(string="Scheduled Time")

    # Load
    notes          = fields.Char(string="Instructions")
    pallets        = fields.Integer()
    weight_lbs     = fields.Float(string="Weight (lbs)", digits=(10, 1))
    liftgate       = fields.Boolean()

    def as_dict(self):
        """Return a dict compatible with the stops JSON format used by RPC methods."""
        return {
            "type":           self.stop_type,
            "is_system":      self.is_system,
            "name":           self.name or "",
            "address":        self.address or "",
            "place_id":       self.place_id or "",
            "lat":            self.lat,
            "lng":            self.lng,
            "scheduled_time": self.scheduled_time.isoformat() if self.scheduled_time else None,
            "notes":          self.notes or "",
            "pallets":        self.pallets or 0,
            "weight_lbs":     self.weight_lbs or 0.0,
            "liftgate":       self.liftgate,
        }
