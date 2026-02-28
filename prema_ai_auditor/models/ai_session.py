from odoo import fields, models


class AISession(models.Model):
    _name = "prema.ai.session"
    _description = "Prema AI Session"

    name = fields.Char(required=True)
    user_id = fields.Many2one("res.users", required=True, default=lambda self: self.env.user)
    message_ids = fields.One2many("prema.ai.message", "session_id")
