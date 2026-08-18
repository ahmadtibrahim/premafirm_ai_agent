"""PHASES 11-12 — COMPANY → CONTACTS → OPPORTUNITY structure and the
Freight Profile.

PHASE 11 — the opportunity's ``partner_id`` is the COMPANY; the people
behind the deal are tracked as ``crm.lead.contact`` rows (res.partner
children of the company, ``parent_id``). Every inbound reply or new
inquiry attaches its sender as a contact row, so a reply from any
contact (not just the primary one) stays visible on the opportunity.

PHASE 12 — the Freight Profile tab captures how the customer ships:
customer/freight type, services, equipment, frequency, pallet/weight
volumes, origin, lanes (``crm.lead.freight.lane`` child model),
preferred days, pickup/delivery windows, current carrier + rate,
expected revenue, key contacts, onboarding/contract state.
"""
from odoo import api, fields, models

_CONTACT_ROLES = [
    ('decision_maker', 'Decision Maker'),
    ('logistics', 'Logistics'),
    ('purchasing', 'Purchasing'),
    ('operations', 'Operations'),
    ('accounting', 'Accounting'),
    ('owner', 'Owner'),
    ('warehouse', 'Warehouse'),
    ('other', 'Other'),
]


class CrmLeadContact(models.Model):
    _name = 'crm.lead.contact'
    _description = 'Contact on an Opportunity (company → contacts)'
    _order = 'is_primary desc, id asc'
    _rec_name = 'partner_id'

    lead_id = fields.Many2one('crm.lead', 'Opportunity', required=True,
                              ondelete='cascade', index=True)
    partner_id = fields.Many2one('res.partner', 'Contact', required=True,
                                 ondelete='cascade', index=True)
    role = fields.Selection(_CONTACT_ROLES, 'Role', default='other',
                            index=True)
    is_primary = fields.Boolean('Primary Contact')
    receives_email = fields.Boolean('Receives Email', default=True)
    receives_quotes = fields.Boolean('Receives Quotes', default=True)
    notes = fields.Text('Notes')

    _sql_constraints = [
        ('lead_partner_unique', 'UNIQUE(lead_id, partner_id)',
         'This contact is already on the opportunity.'),
    ]

    @api.model
    def _attach_sender(self, lead_id, partner_id, role='other'):
        """PHASE 11 — idempotently track the sender of an inbound
        reply/new inquiry as a CONTACT on the opportunity: linked as a
        res.partner child of the company, first contact becomes primary,
        sender keeps receiving email. Never raises."""
        try:
            lead = self.env['crm.lead'].browse(lead_id).exists()
            partner = self.env['res.partner'].browse(partner_id).exists()
            if not lead or not partner or not partner.email:
                return self.env['crm.lead.contact']
            company = lead.partner_id
            # res.partner children: fold the sender under the company
            if company and company.id != partner.id and not partner.parent_id:
                partner.sudo().write({'parent_id': company.id})
            existing = self.search([
                ('lead_id', '=', lead.id),
                ('partner_id', '=', partner.id),
            ], limit=1)
            if existing:
                if not existing.receives_email:
                    existing.write({'receives_email': True})
                return existing
            return self.create({
                'lead_id': lead.id,
                'partner_id': partner.id,
                'role': role,
                'is_primary': not bool(self.search_count(
                    [('lead_id', '=', lead.id)])),
                'receives_email': True,
                'receives_quotes': True,
            })
        except Exception:
            # contact tracking must never block mail processing
            return self.env['crm.lead.contact']


class CrmLeadFreightLane(models.Model):
    _name = 'crm.lead.freight.lane'
    _description = 'Freight Lane on an Opportunity'
    _order = 'id asc'
    _rec_name = 'destination'

    lead_id = fields.Many2one('crm.lead', 'Opportunity', required=True,
                              ondelete='cascade', index=True)
    origin = fields.Char('Origin')
    destination = fields.Char('Destination')
    rate = fields.Float('Rate')
    notes = fields.Text('Notes')


class CrmLead(models.Model):
    _inherit = 'crm.lead'

    # ── PHASE 11: contacts ───────────────────────────────────────────
    contact_ids = fields.One2many('crm.lead.contact', 'lead_id',
                                  'Contacts')
    primary_contact_id = fields.Many2one(
        'res.partner', 'Primary Contact', compute='_compute_primary_contact',
        store=False, help='The contact marked primary on the opportunity.')

    @api.depends('contact_ids.partner_id', 'contact_ids.is_primary')
    def _compute_primary_contact(self):
        for lead in self:
            lead.primary_contact_id = lead.contact_ids.filtered(
                'is_primary').partner_id[:1]

    # ── PHASE 12: freight profile ────────────────────────────────────
    customer_type = fields.Selection([
        ('shipper', 'Shipper'),
        ('consignee', 'Consignee'),
        ('broker', 'Broker'),
        ('three_pl', '3PL'),
        ('other', 'Other'),
    ], 'Customer Type', index=True)
    freight_type = fields.Selection([
        ('ftl', 'FTL'),
        ('ltl', 'LTL'),
        ('partial', 'Partial'),
        ('dedicated', 'Dedicated'),
        ('expedited', 'Expedited'),
        ('intermodal', 'Intermodal'),
        ('other', 'Other'),
    ], 'Freight Type', index=True)
    services = fields.Char('Services',
                           help='Comma-separated services (cross-border, '
                                'temperature controlled, live unload, …)')
    equipment = fields.Selection([
        ('reefer', 'Reefer'),
        ('dry_van', 'Dry Van'),
        ('flatbed', 'Flatbed'),
        ('power_only', 'Power Only'),
        ('step_deck', 'Step Deck'),
        ('lowboy', 'Lowboy'),
        ('tanker', 'Tanker'),
        ('container', 'Container'),
        ('other', 'Other'),
    ], 'Equipment', index=True)
    frequency = fields.Selection([
        ('one_off', 'One-Off'),
        ('weekly', 'Weekly'),
        ('bi_weekly', 'Bi-Weekly'),
        ('monthly', 'Monthly'),
        ('quarterly', 'Quarterly'),
        ('seasonal', 'Seasonal'),
        ('spot', 'Spot Market'),
    ], 'Frequency', index=True)
    pallet_min = fields.Integer('Pallets Min')
    pallet_max = fields.Integer('Pallets Max')
    weight_lbs = fields.Float('Typical Weight (lb)')
    origin = fields.Char('Origin')
    preferred_days = fields.Char('Preferred Days')
    pickup_window_start = fields.Datetime('Pickup Window Start')
    pickup_window_end = fields.Datetime('Pickup Window End')
    delivery_window_start = fields.Datetime('Delivery Window Start')
    delivery_window_end = fields.Datetime('Delivery Window End')
    current_carrier = fields.Char('Current Carrier')
    current_rate = fields.Float('Current Rate')
    target_rate = fields.Float('Target Rate')
    est_weekly_revenue = fields.Monetary('Est. Weekly Revenue',
                                         currency_field='company_currency')
    est_monthly_revenue = fields.Monetary('Est. Monthly Revenue',
                                          currency_field='company_currency')
    decision_maker_id = fields.Many2one('res.partner', 'Decision Maker',
                                        ondelete='set null')
    logistics_contact_id = fields.Many2one('res.partner', 'Logistics Contact',
                                           ondelete='set null')
    onboarding = fields.Selection([
        ('not_started', 'Not Started'),
        ('discovery', 'Discovery'),
        ('proposal', 'Proposal'),
        ('onboarding', 'Onboarding'),
        ('complete', 'Complete'),
    ], 'Onboarding', index=True)
    contract_status = fields.Selection([
        ('none', 'None'),
        ('verbal', 'Verbal'),
        ('draft', 'Draft'),
        ('signed', 'Signed'),
        ('under_review', 'Under Review'),
        ('expired', 'Expired'),
        ('renewal', 'Renewal'),
    ], 'Contract Status', index=True)
    next_contact = fields.Date('Next Contact')
    freight_notes = fields.Text('Freight Notes')
    lane_ids = fields.One2many('crm.lead.freight.lane', 'lead_id',
                               'Lanes')
