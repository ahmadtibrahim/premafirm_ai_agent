from odoo import fields, models


class AIMessage(models.Model):
    _name = "prema.ai.message"
    _description = "Prema AI Message"
    _order = "create_date asc"

    session_id = fields.Many2one("prema.ai.session", required=True, ondelete="cascade")
    role = fields.Selection([("user", "User"), ("assistant", "Assistant")], required=True)
    content = fields.Text(required=True)
