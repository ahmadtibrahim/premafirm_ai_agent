"""
Suggestion Stage Lead Engine
Daily cron: picks target company domains, queries Snov.io for contacts,
creates leads in Suggestion stage (up to 10 per day).
"""
import logging

from odoo import api, fields, models

from ..services.snov_service import CARRIER_PROSPECT_TITLES, SnovService

_logger = logging.getLogger(__name__)


class PremafirmProspectDomain(models.Model):
    _name = 'premafirm.prospect.domain'
    _description = 'Target Domain for Prospecting'
    _order = 'status asc, searched_at asc, id asc'
    _rec_name = 'company_name'

    company_name = fields.Char(string='Company Name', required=True)
    domain = fields.Char(string='Domain', required=True, help='e.g. loblaw.com')
    country = fields.Char(default='Canada')
    industry = fields.Char(string='Industry / Notes')
    searched_at = fields.Datetime(string='Last Searched')
    leads_created = fields.Integer(string='Leads Created', default=0, readonly=True)
    status = fields.Selection([
        ('pending',   'Pending'),
        ('searched',  'Searched'),
        ('exhausted', 'No Contacts Found'),
    ], default='pending', string='Status')

    _sql_constraints = [
        ('unique_domain', 'UNIQUE(domain)', 'This domain is already in the target list.'),
    ]

    # ── Daily cron entry point ───────────────────────────────────────────────

    @api.model
    def run_suggestion_cron(self):
        """Pick up to 3 pending domains, search Snov.io, create Suggestion leads."""
        suggestion = self.env['crm.stage'].sudo().search([('name', '=', 'Suggestion')], limit=1)
        if not suggestion:
            _logger.warning('Suggestion cron: "Suggestion" stage not found, skipping.')
            return

        snov = SnovService(self.env)
        try:
            snov.authenticate()
        except ValueError as e:
            _logger.error('Suggestion cron: Snov.io auth failed — %s', e)
            return

        # Prefer pending; fall back to oldest-searched to recycle
        domains = self.sudo().search([('status', '=', 'pending')], limit=3, order='id asc')
        if not domains:
            domains = self.sudo().search(
                [('status', '=', 'searched')], limit=3, order='searched_at asc'
            )
        if not domains:
            _logger.info('Suggestion cron: no target domains configured yet.')
            return

        total = 0
        for target in domains:
            if total >= 10:
                break
            total += self._prospect_one(snov, target, suggestion, cap=10 - total)

        _logger.info('Suggestion cron: created %d new Suggestion leads', total)

    # ── Per-domain search + lead creation ───────────────────────────────────

    def _prospect_one(self, snov, target, suggestion_stage, cap=5):
        try:
            data = snov.find_emails_by_domain(
                domain=target.domain,
                positions=CARRIER_PROSPECT_TITLES,
                limit=cap,
            )
        except ValueError as e:
            _logger.warning('Suggestion cron: domain %s failed — %s', target.domain, e)
            target.write({'status': 'exhausted', 'searched_at': fields.Datetime.now()})
            return 0

        emails = data.get('emails') or []
        if not emails:
            target.write({'status': 'exhausted', 'searched_at': fields.Datetime.now()})
            return 0

        created = 0
        for item in emails:
            email = (item.get('email') or '').strip().lower()
            if not email:
                continue
            # Skip if already in CRM or contacts
            if self.env['crm.lead'].sudo().search([('email_from', '=ilike', email)], limit=1):
                continue
            if self.env['res.partner'].sudo().search([('email', '=ilike', email)], limit=1):
                continue
            self._create_suggestion_lead(item, target.domain, suggestion_stage)
            created += 1

        target.write({
            'status': 'searched',
            'searched_at': fields.Datetime.now(),
            'leads_created': target.leads_created + created,
        })
        return created

    def _create_suggestion_lead(self, item, domain, suggestion_stage):
        first = item.get('firstName') or ''
        last = item.get('lastName') or ''
        full_name = f"{first} {last}".strip()
        email = (item.get('email') or '').strip()
        title = item.get('currentTitle') or item.get('position') or ''
        company_name = item.get('currentCompany') or domain

        # Company partner — find or create
        company = self.env['res.partner'].sudo().search(
            [('name', '=ilike', company_name), ('is_company', '=', True)], limit=1
        )
        if not company:
            company = self.env['res.partner'].sudo().create({
                'name': company_name,
                'is_company': True,
                'website': f'https://www.{domain}',
            })

        # Person partner — find or create
        person = self.env['res.partner'].sudo().search(
            [('email', '=ilike', email)], limit=1
        ) if email else self.env['res.partner']
        if not person and (full_name or email):
            person = self.env['res.partner'].sudo().create({
                'name': full_name or company_name,
                'email': email,
                'function': title,
                'parent_id': company.id,
            })

        tag = self._snov_tag()
        self.env['crm.lead'].sudo().create({
            'name': f"{company_name} — {title}" if title else company_name,
            'stage_id': suggestion_stage.id,
            'partner_id': person.id if person else company.id,
            'partner_name': company_name,
            'contact_name': full_name,
            'email_from': email,
            'description': (
                f'Auto-generated via Snov.io\n'
                f'Domain: {domain} | Title: {title}'
            ),
            'tag_ids': [(4, tag.id)],
        })

    def _snov_tag(self):
        tag = self.env['crm.tag'].sudo().search([('name', '=', 'Snov.io Lead')], limit=1)
        if not tag:
            tag = self.env['crm.tag'].sudo().create({'name': 'Snov.io Lead'})
        return tag
