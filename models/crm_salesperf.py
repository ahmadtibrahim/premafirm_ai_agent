"""PHASE 26 — Salesperson performance snapshot.

One row per salesperson with the team's core CRM metrics, aggregated
from the PHASE 16-17 analytics fields (x_ana_*) plus calls from the
voipms call log and pipeline value. The row set is a computed snapshot:
``compute_performance()`` rebuilds it from current data (read-only
aggregates — it never writes to crm.lead or voipms.call.log), and the
menu action refreshes it on open.

Reply rate uses the stored ``reply_received`` flag (PHASE 8: meaningful
customer replies only — bounces/OOO never count), and 'emailed' uses
``x_ana_outbound_count > 0`` so a salesperson who never emailed anyone
is not punished by a 0/0.
"""
from datetime import datetime

from odoo import api, fields, models


class PremafirmCrmSalesperf(models.Model):
    _name = 'premafirm.crm.salesperf'
    _description = 'Salesperson Performance'
    _order = 'won_amount desc, open_pipeline desc, user_id'

    user_id = fields.Many2one('res.users', 'Salesperson', readonly=True)
    user_name = fields.Char('Salesperson', readonly=True)
    leads_assigned = fields.Integer('Leads Assigned', readonly=True)
    leads_new_month = fields.Integer('New This Month', readonly=True)
    outbound_emails = fields.Integer('Outbound Emails', readonly=True)
    emailed_leads = fields.Integer('Emailed Leads', readonly=True)
    replied_leads = fields.Integer('Replied Leads', readonly=True)
    reply_rate = fields.Float('Reply Rate (%)', readonly=True)
    avg_first_response_h = fields.Float(
        'Avg First Response (h)', readonly=True)
    calls_answered = fields.Integer('Calls Answered', readonly=True)
    inbound_calls = fields.Integer('Inbound Calls', readonly=True)
    won_count = fields.Integer('Won', readonly=True)
    won_amount = fields.Float('Won Revenue', readonly=True)
    open_pipeline = fields.Float('Open Pipeline', readonly=True)

    @api.model
    def compute_performance(self):
        """Rebuild the snapshot. Read-only against the source models;
        only this snapshot model is written. Returns the new rows."""
        self.search([]).unlink()
        Sales = self.env['res.users'].sudo().search(
            [('share', '=', False),
             ('groups_id', 'in',
              self.env.ref('sales_team.group_sale_salesman').id)],
            order='name')
        month_start = datetime.now().replace(day=1, hour=0, minute=0,
                                             second=0, microsecond=0)
        # Odoo 18 has no crm.stage.is_lost — the lost stage is the
        # module's own xid (same resolution crm_pipeline uses).
        lost_stage = self.env.ref('premafirm_ai_engine.crm_stage_lost',
                                  raise_if_not_found=False)
        CallLog = self.env['voipms.call.log'].sudo()
        Lead = self.env['crm.lead'].sudo()
        rows = []
        for user in Sales:
            leads = Lead.search(
                [('user_id', '=', user.id), ('active', '=', True)])
            emailed = leads.filtered(
                lambda l: (l.x_ana_outbound_count or 0) > 0)
            replied = emailed.filtered(lambda l: l.reply_received)
            first_resp = [l.x_ana_first_response_hours
                          for l in leads if l.x_ana_first_response_hours > 0]
            won = leads.filtered(lambda l: l.stage_id.is_won)
            open_leads = leads.filtered(
                lambda l: not l.stage_id.is_won
                and (not lost_stage or l.stage_id.id != lost_stage.id))
            calls = CallLog.search([('answered_by', '=', user.id)])
            calls_in = CallLog.search(
                [('answered_by', '=', user.id), ('direction', '=', 'inbound')])
            rows.append({
                'user_id': user.id,
                'user_name': user.name,
                'leads_assigned': len(leads),
                'leads_new_month': len(leads.filtered(
                    lambda l: l.date_open and l.date_open >= month_start)),
                'outbound_emails': sum(
                    (l.x_ana_outbound_count or 0) for l in leads),
                'emailed_leads': len(emailed),
                'replied_leads': len(replied),
                'reply_rate': round(
                    len(emailed) and (len(replied) * 100.0 / len(emailed))
                    or 0.0, 1),
                'avg_first_response_h': round(
                    first_resp and sum(first_resp) / len(first_resp) or 0.0,
                    1),
                'calls_answered': len(calls),
                'inbound_calls': len(calls_in),
                'won_count': len(won),
                'won_amount': round(sum(l.expected_revenue for l in won), 2),
                'open_pipeline': round(
                    sum(l.expected_revenue for l in open_leads), 2),
            })
        self.sudo().create(rows)
        return self.sudo().search([])
