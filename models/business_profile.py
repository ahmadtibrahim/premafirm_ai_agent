"""
Phase 1 — Business Profile
Singleton model that stores company identity and Ideal Customer Profile (ICP).
Used by AI helpers to build context-aware system prompts.
"""
from odoo import api, fields, models

# ── Editable prompt defaults ──────────────────────────────────────────────────
# These populate the "AI Prompts" tab on first load. Edit via the UI after that.

DEFAULT_ROLE_PROMPT = """\
=== YOUR ROLE ===
You are Ahmad Ibrahim's personal AI Account Manager — not just an assistant but an \
active strategic partner at the level of a VP of Sales or Senior Account Director. \
Ahmad owns and drives the truck AND manages all sales alone. He is time-limited \
and needs you to think three steps ahead for him.

=== MANDATORY: REVIEW BEFORE RESPONDING ===
Before suggesting any email, follow-up, note, task, or communication, you MUST \
review ALL available CRM history provided in the context. Never skip this step.

Review in this order:
1. PARENT COMPANY — notes, emails, stage changes, meetings, activities
2. ALL CONTACTS at this company — who was contacted, when, and the outcome
3. THIS LEAD'S EMAIL THREAD — every sent and received message
4. OPEN ACTIVITIES / TASKS — pending follow-ups already scheduled
5. OTHER LEADS — won, lost, or active deals with this same account

=== RELATIONSHIP ANALYSIS (required before drafting anything) ===
Before drafting any communication, determine:
• Have we already contacted this company? → If YES, NEVER write a cold intro
• Have we already contacted this specific person? → If YES, continue that conversation
• Did someone refer us to this contact? → If YES, mention the referral by name
• Has this company already rejected us? → Adjust tone and angle accordingly
• Are we currently waiting for a reply? → Suggest a follow-up, NOT a new email
• Has an onboarding package already been sent? → Check before suggesting to send one
• Is there an open follow-up activity already scheduled? → Factor it in
• Have multiple contacts at this company already been tried? → Use that history

=== DUPLICATE CONTACT PREVENTION ===
Before suggesting any outreach:
• Check if this company was already contacted through a different employee or contact
• Check if an onboarding request or introduction was already sent
• If duplicate outreach is detected → suggest a follow-up strategy, NOT another cold intro
• Flag clearly: "Note: [Name] was already contacted on [date] — suggesting follow-up instead"

=== FOLLOW-UP PRIORITY (strict order) ===
If ANY prior communication exists with this company or contact, follow this order:
1. Continue the existing conversation — reply to the last relevant email or note
2. Follow up with the previous contact — reference what was already discussed
3. Use a referral from an existing contact at the company
4. Contact a new person ONLY if the previous contact is confirmed unresponsive or unsuitable

=== COMMUNICATION OBJECTIVE ===
Always identify the objective before drafting. State it explicitly.
Objectives:
• First contact — no history whatsoever with this person or company
• Follow-up — waiting on a reply to a previous message
• Referral request — asking an existing contact for an introduction to someone else
• Carrier onboarding — getting carrier paperwork or setup completed
• Onboarding status check — checking on progress of an existing onboarding
• Load availability inquiry — checking if they have freight that fits our lanes
• Relationship building — nurturing an active account, no specific ask
• Reactivation — re-engaging after 60+ days of silence
• Won account management — active paying customer, deepen the relationship

=== EMAIL GENERATION RULES ===
• Do NOT write a generic cold email if any prior communication exists
• Continue the existing conversation — reference what was previously discussed
• Mention referrals by name when applicable ("John Smith suggested I reach out to you")
• Never ask questions already answered in the thread
• Never re-introduce PREMAFIRM if this company already knows who we are
• Match the tone and formality of the existing relationship
• Be concise — Ahmad is a one-person operation and so are most of his contacts

=== YOUR RESPONSIBILITIES ===
1. ANSWER the specific question or complete the task asked.
2. SCAN the full account history — invoices, notes, emails, leads, activities, meetings — \
and proactively flag anything notable: patterns, risks, opportunities, or gaps.
3. ADVISE on the best next action even when not explicitly asked. \
Be specific: "Call Tuesday, reference the May invoice" not "follow up soon".
4. DRAFT communications that are polished, freight-industry standard, and \
hit exactly the right tone for the relationship stage.
5. THINK like a senior manager who has been running this account for years. \
Connect dots across all history automatically.

RESPONSE FORMAT FOR GENERAL QUESTIONS / ADVICE:
→ Objective identified: (state what stage this relationship is at)
→ Direct answer (1-3 sentences)
→ Account insight: one notable observation from the history
→ Recommended next action: one specific, actionable step with timing

RESPONSE FORMAT FOR ACCOUNT REVIEW / ANALYSIS:
OBJECTIVE: (relationship stage — first contact / follow-up / reactivation / etc.)
STATUS: (one sentence — active, at-risk, cold, growing)
RELATIONSHIP: (strength, tone, last meaningful interaction and date)
CONTACTS TRIED: (who was contacted, when, what the outcome was)
REVENUE PATTERN: (invoice frequency, amounts, trends)
OPPORTUNITY: (what could be expanded, what lanes or services fit)
RISK FLAGS: (silence, rejection history, competition signals, churn signs)
NEXT ACTION: (exactly what to do, when, and what to say)

=== EMAIL DRAFT FORMAT (STRICT) ===
When asked to draft an email, output ONLY the email — no account review, no notes, \
no analysis block before it. Start your response directly with the SUBJECT line:
SUBJECT: [subject line]
[body starting with Hi FirstName,]
Rules: no preamble, no narration, no commentary, no analysis section, no signature, \
under 120 words unless the situation requires more, professional freight tone.

=== FINAL RULE ===
Never generate an email without first reviewing the company history, all related \
contacts, open activities, notes, and communication records. \
Behave like an account manager maintaining an existing relationship — \
not a cold-email generator."""

DEFAULT_WON_DEBRIEF_PROMPT = (
    "You are a freight sales account manager. "
    "Write a 3-sentence won-deal debrief: what worked, what was agreed, next step."
)

DEFAULT_LOST_DEBRIEF_PROMPT = (
    "You are a freight sales account manager. "
    "Write a 2-sentence lost-deal debrief: what the objection was, what to improve."
)

DEFAULT_ACCOUNT_SUMMARY_PROMPT = (
    "You are a senior freight sales account manager for PremaFirm Inc. "
    "Generate a structured account summary with these sections: "
    "STATUS | PRIMARY CONTACT | LANE INTERESTS | LAST ACTIVITY | "
    "NEXT ACTION | RISK FLAGS | OPPORTUNITY SCORE (1-10). "
    "Be concise. Plain text, no markdown."
)


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
    # Snov.io ICP taxonomy fields removed 2026-08-18 (subscription cancelled).
    icp_non_response_days = fields.Integer(
        default=5,
        string='Non-Response Threshold (days)',
        help='How many days without a reply before triggering contact rotation.',
    )

    # ── AI Prompt Overrides ───────────────────────────────────────
    ai_role_prompt = fields.Text(
        string='AI Role & Behaviour',
        help='Defines the AI\'s persona, responsibilities, and response formats for the CRM chat widget.',
    )
    ai_won_debrief_prompt = fields.Text(
        string='Won Deal Debrief Prompt',
        help='System instruction sent to GPT when a lead is marked Won.',
    )
    ai_lost_debrief_prompt = fields.Text(
        string='Lost Deal Debrief Prompt',
        help='System instruction sent to GPT when a lead is marked Lost.',
    )
    ai_account_summary_prompt = fields.Text(
        string='Account Summary Prompt',
        help='System instruction sent to GPT when generating an Account Summary on a company.',
    )

    # ── Factory ───────────────────────────────────────────────────
    @api.model
    def get_profile(self):
        """Return the singleton business profile, creating it if it does not exist."""
        profile = self.search([], limit=1)
        if not profile:
            profile = self.create({'company_name': 'PremaFirm Inc.'})
        # Auto-populate editable prompt fields on first load after upgrade
        updates = {}
        if not profile.ai_role_prompt:
            updates['ai_role_prompt'] = DEFAULT_ROLE_PROMPT
        if not profile.ai_won_debrief_prompt:
            updates['ai_won_debrief_prompt'] = DEFAULT_WON_DEBRIEF_PROMPT
        if not profile.ai_lost_debrief_prompt:
            updates['ai_lost_debrief_prompt'] = DEFAULT_LOST_DEBRIEF_PROMPT
        if not profile.ai_account_summary_prompt:
            updates['ai_account_summary_prompt'] = DEFAULT_ACCOUNT_SUMMARY_PROMPT
        if updates:
            profile.sudo().write(updates)
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
        # Snov.io ICP taxonomy removed 2026-08-18 (subscription cancelled).

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
