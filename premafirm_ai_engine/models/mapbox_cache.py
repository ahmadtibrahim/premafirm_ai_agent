from odoo import fields, models


class PremafirmMapboxCache(models.Model):
    _name = "premafirm.mapbox.cache"
    _description = "Premafirm Mapbox Route Cache"

    origin = fields.Char(required=True, index=True)
    destination = fields.Char(required=True, index=True)
    waypoints_hash = fields.Char(index=True)
    departure_date = fields.Datetime(index=True)
    distance_km = fields.Float()
    duration_minutes = fields.Float()
    polyline = fields.Text()
    cached_at = fields.Datetime(default=fields.Datetime.now, required=True)

    _sql_constraints = [
        ("origin_destination_departure_idx", "unique(origin, destination, waypoints_hash, departure_date)", "Cache entry already exists for this route."),
    ]
