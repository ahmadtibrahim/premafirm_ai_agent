"""
Phase 1 — Business Profile
Singleton model that stores company identity and Ideal Customer Profile (ICP).
Used by AI helpers to build context-aware system prompts.
"""
from odoo import api, fields, models


class PremafirmSnovIndustry(models.Model):
    _name = 'premafirm.snov.industry'
    _description = 'Snov Industry'
    _order = 'name'

    name = fields.Char(required=True)


class PremafirmSnovCompanySize(models.Model):
    _name = 'premafirm.snov.company.size'
    _description = 'Snov Company Size'
    _order = 'sequence'

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)


class PremafirmSnovSeniority(models.Model):
    _name = 'premafirm.snov.seniority'
    _description = 'Snov Seniority Level'
    _order = 'sequence'

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)


class PremafirmSnovJobTitle(models.Model):
    _name = 'premafirm.snov.job.title'
    _description = 'Snov Job Title'
    _order = 'name'

    name = fields.Char(required=True)


class PremafirmSnovRegion(models.Model):
    _name = 'premafirm.snov.region'
    _description = 'Snov Target Region'
    _order = 'sequence, name'

    name = fields.Char(required=True)
    region_type = fields.Selection(
        [('country', 'Country'), ('province', 'Province / State')],
        default='country',
    )
    sequence = fields.Integer(default=10)


class PremafirmBusinessProfile(models.Model):
    """
    Singleton model — always access via get_profile().
    Stores company identity, tone, and Ideal Customer Profile.
    """
    _name = 'premafirm.business.profile'
    _description = 'Business Profile'

    # ── Company Identity ──────────────────────────────────────────
    company_name = fields.Char(default='PremaFirm Inc.')
    company_overview = fields.Text(
        string='Company Overview',
        help='Brief description of what PremaFirm does and its value proposition.',
    )
    services_description = fields.Text(string='Services Description')
    key_differentiators = fields.Text(string='Key Differentiators')
    pricing_context = fields.Text(string='Pricing Context')
    team_info = fields.Text(string='Team Info')
    email_signature = fields.Text(string='Email Signature')
    tone_of_voice = fields.Selection(
        [
            ('professional', 'Professional'),
            ('friendly', 'Friendly'),
            ('direct', 'Direct'),
            ('formal', 'Formal'),
        ],
        default='professional',
        string='Tone of Voice',
    )

    # ── Ideal Customer Profile (ICP) ─────────────────────────────
    icp_industry_ids = fields.Many2many(
        'premafirm.snov.industry',
        relation='biz_profile_industry_rel',
        string='Target Industries',
    )
    icp_company_size_ids = fields.Many2many(
        'premafirm.snov.company.size',
        relation='biz_profile_size_rel',
        string='Target Company Sizes',
    )
    icp_seniority_ids = fields.Many2many(
        'premafirm.snov.seniority',
        relation='biz_profile_seniority_rel',
        string='Target Seniority Levels',
    )
    icp_title_ids = fields.Many2many(
        'premafirm.snov.job.title',
        relation='biz_profile_title_rel',
        string='Target Job Titles',
    )
    icp_region_ids = fields.Many2many(
        'premafirm.snov.region',
        relation='biz_profile_region_rel',
        string='Target Regions',
    )
    icp_non_response_days = fields.Integer(
        default=5,
        string='Non-Response Threshold (days)',
        help='How many days without a reply before triggering contact rotation.',
    )

    # ── Factory ───────────────────────────────────────────────────
    @api.model
    def get_profile(self):
        """Return the singleton business profile, creating it if it does not exist."""
        profile = self.search([], limit=1)
        if not profile:
            profile = self.create({'company_name': 'PremaFirm Inc.'})
        return profile

    def action_save_profile(self):
        """Explicit Save button — writes the record and shows a confirmation."""
        self.ensure_one()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Saved',
                'message': 'Business Profile saved successfully.',
                'type': 'success',
                'sticky': False,
            },
        }

    # ── AI System Prompt ──────────────────────────────────────────
    def get_system_prompt(self, record=None):
        """
        Build a complete AI system prompt combining company identity, ICP,
        relevant KB documents, and optional record context.
        """
        self.ensure_one()
        lines = []

        # ── COMPANY CONTEXT ──
        lines.append('=== COMPANY CONTEXT ===')
        lines.append(f'Company: {self.company_name or "PremaFirm Inc."}')
        if self.tone_of_voice:
            lines.append(f'Tone of voice: {dict(self._fields["tone_of_voice"].selection).get(self.tone_of_voice, self.tone_of_voice)}')
        if self.company_overview:
            lines.append(f'\nOverview:\n{self.company_overview}')
        if self.services_description:
            lines.append(f'\nServices:\n{self.services_description}')
        if self.key_differentiators:
            lines.append(f'\nKey Differentiators:\n{self.key_differentiators}')
        if self.pricing_context:
            lines.append(f'\nPricing Context:\n{self.pricing_context}')
        if self.team_info:
            lines.append(f'\nTeam:\n{self.team_info}')
        if self.email_signature:
            lines.append(f'\nEmail Signature:\n{self.email_signature}')

        # ── IDEAL CUSTOMER PROFILE ──
        lines.append('\n=== IDEAL CUSTOMER PROFILE ===')
        if self.icp_industry_ids:
            lines.append('Target Industries: ' + ', '.join(self.icp_industry_ids.mapped('name')))
        if self.icp_company_size_ids:
            lines.append('Target Company Sizes: ' + ', '.join(self.icp_company_size_ids.mapped('name')))
        if self.icp_seniority_ids:
            lines.append('Target Seniority: ' + ', '.join(self.icp_seniority_ids.mapped('name')))
        if self.icp_title_ids:
            lines.append('Target Job Titles: ' + ', '.join(self.icp_title_ids.mapped('name')))
        if self.icp_region_ids:
            lines.append('Target Regions: ' + ', '.join(self.icp_region_ids.mapped('name')))

        # ── COMPANY DOCUMENTS from Knowledge Center ──
        kc_text = self._get_kc_knowledge()
        if kc_text:
            lines.append('\n=== COMPANY DOCUMENTS ===')
            lines.append(kc_text)

        # ── CURRENT RECORD CONTEXT ──
        if record:
            record_text = self._get_record_context(record)
            if record_text:
                lines.append('\n=== CURRENT RECORD CONTEXT ===')
                lines.append(record_text)

        return '\n'.join(lines)

    def _get_kc_knowledge(self):
        """Return formatted excerpts from the ML knowledge base (company_document type)."""
        try:
            entries = self.env['premafirm.ml.knowledge'].search(
                [('knowledge_type', '=', 'company_document'), ('active', '=', True)],
                limit=8,
                order='weight desc, create_date desc',
            )
            if not entries:
                return ''
            parts = []
            for i, entry in enumerate(entries, 1):
                excerpt = (entry.good_output or entry.input_context or '')[:400]
                if excerpt:
                    parts.append(f'[Doc {i}] {excerpt}')
            return '\n\n'.join(parts)
        except Exception:
            return ''

    def _get_record_context(self, record):
        """Extract relevant fields per model as a readable string."""
        if not record:
            return ''
        try:
            model_name = record._name
            if model_name == 'crm.lead':
                parts = []
                if record.partner_name or record.partner_id:
                    parts.append(f'Contact/Company: {record.partner_name or record.partner_id.name}')
                if record.email_from:
                    parts.append(f'Email: {record.email_from}')
                if record.phone:
                    parts.append(f'Phone: {record.phone}')
                if record.stage_id:
                    parts.append(f'Stage: {record.stage_id.name}')
                if record.description:
                    parts.append(f'Notes: {(record.description or "")[:300]}')
                if hasattr(record, 'outreach_stage') and record.outreach_stage:
                    parts.append(f'Outreach Stage: {record.outreach_stage}')
                return '\n'.join(parts)

            elif model_name == 'res.partner':
                parts = []
                parts.append(f'Name: {record.name}')
                if record.email:
                    parts.append(f'Email: {record.email}')
                if record.phone:
                    parts.append(f'Phone: {record.phone}')
                if record.function:
                    parts.append(f'Job Title: {record.function}')
                if record.company_type == 'company':
                    parts.append('Type: Company')
                elif record.parent_id:
                    parts.append(f'Company: {record.parent_id.name}')
                if record.country_id:
                    parts.append(f'Country: {record.country_id.name}')
                return '\n'.join(parts)

            elif model_name == 'account.move':
                parts = []
                parts.append(f'Invoice: {record.name}')
                if record.partner_id:
                    parts.append(f'Partner: {record.partner_id.name}')
                if record.invoice_date:
                    parts.append(f'Date: {record.invoice_date}')
                parts.append(f'Amount: {record.amount_total} {record.currency_id.name}')
                if record.move_type:
                    parts.append(f'Type: {record.move_type}')
                return '\n'.join(parts)

            elif model_name == 'sale.order':
                parts = []
                parts.append(f'Order: {record.name}')
                if record.partner_id:
                    parts.append(f'Customer: {record.partner_id.name}')
                if record.date_order:
                    parts.append(f'Date: {record.date_order}')
                parts.append(f'Total: {record.amount_total} {record.currency_id.name}')
                if record.state:
                    parts.append(f'State: {record.state}')
                return '\n'.join(parts)

            else:
                return f'Model: {model_name}, ID: {record.id}'
        except Exception:
            return ''
