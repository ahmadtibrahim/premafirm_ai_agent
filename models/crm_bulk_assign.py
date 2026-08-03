from odoo import api, fields, models, _


class CrmBulkAssignWizard(models.TransientModel):
    _name = 'crm.bulk.assign.wizard'
    _description = 'Bulk Assign Salesperson'

    user_id = fields.Many2one(
        'res.users', string='Salesperson', required=True,
        domain=[('share', '=', False)],
    )
    lead_ids = fields.Many2many('crm.lead', string='Leads')
    lead_count = fields.Integer(compute='_compute_lead_count')

    @api.depends('lead_ids')
    def _compute_lead_count(self):
        for rec in self:
            rec.lead_count = len(rec.lead_ids)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        active_ids = self.env.context.get('active_ids', [])
        if active_ids:
            res['lead_ids'] = [(6, 0, active_ids)]
        return res

    def action_assign(self):
        self.ensure_one()
        self.lead_ids.write({'user_id': self.user_id.id})
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Assigned'),
                'message': _('%d lead(s) assigned to %s.') % (len(self.lead_ids), self.user_id.name),
                'type': 'success',
                'sticky': False,
            },
        }
