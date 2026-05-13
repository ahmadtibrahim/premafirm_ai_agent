"""
CRM Contact Rotation + Snov.io Escalation
Tracks which contacts have been tried for a lead. When all internal contacts
are exhausted, calls Snov.io to find a new contact and creates a Suggestion lead.
"""
import logging
import re
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# Title priority tiers for ranking Snov.io candidates
_TIER1 = ['vp', 'vice president', 'director', 'chief']
_TIER2 = ['manager', 'head of', 'supervisor', 'lead']


def _domain_from_website(website):
    """Extract bare domain from a URL or email string."""
    if not website:
        return None
    website = website.lower().strip()
    website = re.sub(r'^https?://', '', website)
    website = re.sub(r'^www\.', '', website)
    website = website.split('/')[0].split('?')[0]
    return website or None


def _domain_from_email(email):
    """Extract domain from email address."""
    if email and '@' in email:
        return email.split('@')[1].lower().strip()
    return None


def _snov_title_tier(title):
    title_lc = (title or '').lower()
    for kw in _TIER1:
        if kw in title_lc:
            return 0
    for kw in _TIER2:
        if kw in title_lc:
            return 1
    return 2


class PremafirmCrmContactRotation(models.Model):
    _name = 'premafirm.crm.contact.rotation'
    _description = 'CRM Contact Rotation Log'

    lead_id = fields.Many2one('crm.lead', required=True, ondelete='cascade', string='Lead')
    partner_id = fields.Many2one('res.partner', string='Contact Tried')
    tried_at = fields.Datetime(default=fields.Datetime.now, string='Tried At')
    outcome = fields.Selection(
        [
            ('no_reply', 'No Reply'),
            ('replied', 'Replied'),
            ('bounced', 'Bounced'),
            ('unsubscribed', 'Unsubscribed'),
        ],
        default='no_reply',
        string='Outcome',
    )
    notes = fields.Char(string='Notes')

    # ── Cron entry ────────────────────────────────────────────────
    @api.model
    def run_contact_rotation(self):
        """Cron entry — find stale leads and suggest next contacts or Snov escalation."""
        try:
            profile = self.env['premafirm.business.profile'].sudo().get_profile()
            non_response_days = profile.icp_non_response_days or 5
            icp_titles = profile.icp_title_ids.mapped('name')
        except Exception as exc:
            _logger.warning('Contact rotation: cannot load business profile: %s', exc)
            non_response_days = 5
            icp_titles = []

        threshold_dt = fields.Datetime.now() - timedelta(days=non_response_days)

        try:
            leads = self.env['crm.lead'].sudo().search([
                ('active', '=', True),
                ('x_response_status', '=', 'none'),
                ('x_last_outreach_at', '!=', False),
                ('x_last_outreach_at', '<', threshold_dt),
            ])
        except Exception as exc:
            _logger.warning('Contact rotation: lead query failed: %s', exc)
            return

        leads = leads.filtered(lambda l: l.probability not in (100,) and not l.date_closed)

        for lead in leads:
            try:
                self._process_lead_rotation(lead, icp_titles)
            except Exception as exc:
                _logger.warning('Contact rotation error on lead %s: %s', lead.id, exc)

        _logger.info('Contact rotation: processed %d leads.', len(leads))

    def _process_lead_rotation(self, lead, icp_titles):
        """Process rotation for a single lead."""
        primary_partner = lead.partner_id
        if not primary_partner:
            return

        company_partner = (
            primary_partner.parent_id if primary_partner.parent_id
            else (primary_partner if primary_partner.is_company else None)
        )
        if not company_partner:
            return

        # Contacts already tried for this lead (include current contact)
        tried_partner_ids = self.search([('lead_id', '=', lead.id)]).mapped('partner_id').ids
        if primary_partner.id not in tried_partner_ids:
            tried_partner_ids.append(primary_partner.id)

        # Find untried contacts at the same company that have email
        candidates = self.env['res.partner'].sudo().search([
            ('parent_id', '=', company_partner.id),
            ('email', '!=', False),
            ('id', 'not in', tried_partner_ids),
            ('active', '=', True),
        ])

        if candidates:
            # Rank by ICP title match and suggest best one
            def title_rank(partner):
                if not icp_titles:
                    return 0
                func = (partner.function or '').lower()
                return sum(1 for t in icp_titles if t.lower() in func)

            best = sorted(candidates, key=title_rank, reverse=True)[0]

            self.create({
                'lead_id': lead.id,
                'partner_id': best.id,
                'outcome': 'no_reply',
                'notes': f'Auto-suggested after {lead.x_rotation_count + 1} rotation(s)',
            })
            lead.sudo().write({'x_rotation_count': lead.x_rotation_count + 1})
            lead.message_post(
                body=(
                    f'<b>Contact Rotation Suggestion:</b> '
                    f'No reply from <b>{primary_partner.name}</b>. '
                    f'Consider reaching out to <b>{best.name}</b> '
                    f'({best.function or "No title"}) — '
                    f'<a href="mailto:{best.email}">{best.email}</a>'
                ),
                subtype_xmlid='mail.mt_note',
            )
            return

        # No internal contacts left — escalate to Snov.io
        self._escalate_to_snov(lead, primary_partner, company_partner)

    def _escalate_to_snov(self, lead, primary_partner, company_partner):
        """Call Snov.io to find a new contact and create a Suggestion lead."""
        # Guard: don't call Snov twice for the same company on this lead
        if lead.x_snov_enrichment_requested:
            return

        # Determine domain to search
        domain = (
            _domain_from_website(company_partner.website)
            or _domain_from_email(primary_partner.email)
            or _domain_from_email(company_partner.email)
        )
        if not domain:
            lead.message_post(
                body=(
                    f'<b>Snov.io Escalation:</b> No domain found for '
                    f'<b>{company_partner.name}</b>. Set a website on the company record to enable escalation.'
                ),
                subtype_xmlid='mail.mt_note',
            )
            return

        # Mark as requested before the API call so a retry cron doesn't double-call
        lead.sudo().write({'x_snov_enrichment_requested': True})

        try:
            from ..services.snov_service import SnovService
            snov = SnovService(self.env)
            result = snov.find_emails_by_domain(
                domain,
                positions=snov.get_carrier_prospect_titles(),
                limit=20,
            )
        except Exception as exc:
            _logger.warning('Snov.io escalation failed for lead %s: %s', lead.id, exc)
            lead.message_post(
                body=f'<b>Snov.io Escalation:</b> API call failed — {exc}',
                subtype_xmlid='mail.mt_note',
            )
            return

        emails_raw = result.get('emails') or []
        if not emails_raw:
            lead.message_post(
                body=(
                    f'<b>Snov.io Escalation:</b> No contacts found at domain '
                    f'<b>{domain}</b> for <b>{company_partner.name}</b>.'
                ),
                subtype_xmlid='mail.mt_note',
            )
            return

        # Collect existing emails in Odoo under this company to avoid duplicates
        existing_emails = set(
            self.env['res.partner'].sudo().search([
                '|',
                ('id', '=', company_partner.id),
                ('parent_id', '=', company_partner.id),
            ]).mapped('email_normalized')
        )
        existing_emails = {e.lower() for e in existing_emails if e}

        # Filter to valid, non-duplicate contacts
        candidates = []
        for entry in emails_raw:
            email = (entry.get('email') or '').strip().lower()
            if not email or email in existing_emails:
                continue
            status = (entry.get('emailStatus') or entry.get('status') or '').lower()
            if status in ('invalid', 'bounced'):
                continue
            candidates.append(entry)

        if not candidates:
            lead.message_post(
                body=(
                    f'<b>Snov.io Escalation:</b> All contacts found at '
                    f'<b>{domain}</b> already exist in Odoo or have invalid emails.'
                ),
                subtype_xmlid='mail.mt_note',
            )
            return

        # Pick best candidate by title tier (VP/Director > Manager > Other)
        candidates.sort(key=lambda e: _snov_title_tier(
            e.get('position') or e.get('title') or ''
        ))
        best = candidates[0]

        first = best.get('firstName') or ''
        last = best.get('lastName') or ''
        full_name = f'{first} {last}'.strip() or best.get('email', 'Unknown')
        email = best['email'].strip()
        title = best.get('position') or best.get('title') or ''
        confidence = best.get('confidence') or best.get('emailStatus') or 'unknown'

        # Create the new contact under the same company
        new_partner = self.env['res.partner'].sudo().create({
            'name': full_name,
            'email': email,
            'function': title,
            'parent_id': company_partner.id,
            'type': 'contact',
        })

        # Find or create the Suggestion stage
        suggestion_stage = self.env.ref(
            'premafirm_ai_engine.crm_stage_suggestion', raise_if_not_found=False
        )
        if not suggestion_stage:
            suggestion_stage = self.env['crm.stage'].sudo().search(
                [('name', 'ilike', 'Suggestion')], limit=1
            )

        # Create the Suggestion lead
        suggestion_lead = self.env['crm.lead'].sudo().create({
            'name': f'Snov Suggestion – {company_partner.name}',
            'partner_id': new_partner.id,
            'stage_id': suggestion_stage.id if suggestion_stage else False,
            'team_id': lead.team_id.id if lead.team_id else False,
            'user_id': lead.user_id.id if lead.user_id else False,
            'source_id': lead.source_id.id if lead.source_id else False,
        })

        # Log to original lead
        lead.message_post(
            body=(
                f'<b>Snov.io Escalation:</b> No untried internal contacts at '
                f'<b>{company_partner.name}</b>. '
                f'Snov.io found a new contact at domain <b>{domain}</b>:<br/>'
                f'<b>{full_name}</b> — {title or "No title"} — '
                f'<a href="mailto:{email}">{email}</a> '
                f'(confidence: {confidence})<br/>'
                f'A new <b>Suggestion</b> lead has been created: '
                f'<a href="/odoo/crm/{suggestion_lead.id}">#{suggestion_lead.id} {suggestion_lead.name}</a><br/>'
                f'<i>Review and approve before sending any outreach.</i>'
            ),
            subtype_xmlid='mail.mt_note',
        )

        # Log creation note on the new contact
        new_partner.message_post(
            body=(
                f'Created via Snov.io escalation from lead '
                f'<a href="/odoo/crm/{lead.id}">#{lead.id}</a> '
                f'({primary_partner.name} did not reply). '
                f'Email confidence: {confidence}.'
            ),
            subtype_xmlid='mail.mt_note',
        )

        # Log to suggestion lead
        suggestion_lead.message_post(
            body=(
                f'<b>Snov.io Suggestion:</b> This contact was discovered by Snov.io '
                f'after <b>{primary_partner.name}</b> did not reply on '
                f'<a href="/odoo/crm/{lead.id}">lead #{lead.id}</a>. '
                f'Email confidence: {confidence}. '
                f'<b>Ahmad must review and approve before sending any outreach.</b>'
            ),
            subtype_xmlid='mail.mt_note',
        )

        _logger.info(
            'Snov.io escalation: created suggestion lead %s for company %s (contact: %s)',
            suggestion_lead.id, company_partner.name, full_name,
        )
