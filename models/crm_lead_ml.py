"""
CRM Lead ML hooks — complete learning loop.

What gets learned automatically:
  - Lead marked Won  → saves winning context + last reply as crm_reply example (weight 3.0)
  - Lead marked Lost → saves lost context + ALL chatter notes as rejected example (weight 2.0)
                       so future lead searches avoid the same dead ends
  - Staff sends email on a lead → captured as crm_reply example (weight 1.0, once per 7 days)

Manual:
  - "Save as ML Example" button → staff bookmarks any lead at any stage
"""
import logging
import re

from dateutil.relativedelta import relativedelta
from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class CrmLeadML(models.Model):
    _inherit = 'crm.lead'

    # ── Existing button actions (unchanged) ──────────────────────────

    def action_ml_draft_reply(self):
        """Generate an AI draft reply for this lead and open the draft for review."""
        self.ensure_one()
        draft, err = self.env['premafirm.ml.engine'].generate_crm_reply(self)
        if err or not draft:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'ML Draft Failed',
                    'message': err or 'Could not generate a draft.',
                    'type': 'warning',
                    'sticky': False,
                },
            }
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'premafirm.ml.draft',
            'res_id': draft.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_ml_rate_quote(self):
        """Generate a rate quote draft from this lead."""
        self.ensure_one()
        msg = f'Rate request for lead: {self.name}. Customer: {self.partner_name or (self.partner_id.name if self.partner_id else "")}'
        draft, err = self.env['premafirm.ml.engine'].generate_rate_quote(
            message_text=msg,
            partner=self.partner_id,
            source_model='crm.lead',
            source_id=self.id,
        )
        if err or not draft:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {'title': 'ML Draft Failed', 'message': err or 'Could not generate.',
                           'type': 'warning', 'sticky': False},
            }
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'premafirm.ml.draft',
            'res_id': draft.id,
            'view_mode': 'form',
            'target': 'new',
        }

    # ── Won / Lost hooks ─────────────────────────────────────────────

    def action_set_won(self):
        result = super().action_set_won()
        for lead in self:
            try:
                lead._ml_learn_from_outcome('won')
            except Exception as e:
                _logger.warning('ML won-learning failed for lead %s: %s', lead.id, e)
        return result

    def action_set_lost(self, **additional_values):
        result = super().action_set_lost(**additional_values)
        for lead in self:
            try:
                lead._ml_learn_from_outcome('lost')
            except Exception as e:
                _logger.warning('ML lost-learning failed for lead %s: %s', lead.id, e)
        return result

    # ── Outgoing email hook ──────────────────────────────────────────

    def message_post(self, **kwargs):
        result = super().message_post(**kwargs)
        # Capture outgoing staff emails — but throttle to once per 7 days per lead
        if (result
                and kwargs.get('message_type') == 'email'
                and result.author_id
                and result.author_id.user_ids):
            try:
                self._ml_learn_from_email(result)
            except Exception as e:
                _logger.warning('ML email-learning failed for lead %s: %s', self.id, e)
        return result

    # ── Manual bookmark ──────────────────────────────────────────────

    def action_save_as_ml_example(self):
        """Staff manually marks this lead as a high-quality ML example."""
        self.ensure_one()
        anchor = f'[crm:{self.id}]'
        context = f"{anchor}\n{self._ml_build_context()}"
        KB = self.env['premafirm.ml.knowledge']

        # Best staff message as the "good output"
        good_output = self._ml_best_staff_message() or f"Lead: {self.name}"

        existing = KB.search([
            ('knowledge_type', '=', 'crm_reply'),
            ('input_context', 'like', anchor),
        ], limit=1)

        if existing:
            existing.write({
                'input_context': context,
                'good_output': good_output,
                'weight': min(existing.weight + 0.5, 5.0),
            })
            msg = 'ML example updated — weight boosted.'
        else:
            KB.create({
                'knowledge_type': 'crm_reply',
                'input_context':  context,
                'good_output':    good_output,
                'origin':         'manual',
                'weight':         2.5,
            })
            msg = 'Saved as ML example. Future AI drafts will reference this lead.'

        _logger.info('ML: lead %s manually saved as example', self.id)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Saved to ML',
                'message': msg,
                'type': 'success',
                'sticky': False,
            },
        }

    # ── Core helpers ─────────────────────────────────────────────────

    def _ml_extract_notes(self):
        """
        Pull all chatter notes from this lead — internal notes, emails, log notes.
        Returns a concatenated string (most recent first, capped at 2000 chars).
        This is the raw source of "why we lost" and "what was discussed".
        """
        snippets = []
        for msg in self.message_ids.sorted('date', reverse=True)[:15]:
            body = re.sub(r'<[^>]+>', ' ', msg.body or '').strip()
            body = re.sub(r'\s+', ' ', body).strip()
            if not body or len(body) < 15:
                continue
            author = msg.author_id.name or 'Unknown'
            label = {
                'email':   'Email',
                'comment': 'Note',
            }.get(msg.message_type, 'Message')
            snippets.append(f"[{label} — {author}]: {body[:350]}")
        return '\n'.join(snippets)[:2000]

    def _ml_build_context(self):
        """Build a rich, searchable context string for this lead."""
        customer = (self.partner_name
                    or (self.partner_id.name if self.partner_id else '')
                    or 'Unknown')
        parts = [
            f"Customer: {customer}",
            f"Lead: {self.name}",
        ]
        if self.partner_id and self.partner_id.city:
            parts.append(f"Location: {self.partner_id.city}"
                         + (f", {self.partner_id.state_id.name}" if self.partner_id.state_id else ''))
        if self.partner_id and self.partner_id.industry_id:
            parts.append(f"Industry: {self.partner_id.industry_id.name}")
        if self.expected_revenue:
            parts.append(f"Expected revenue: ${self.expected_revenue:,.0f}")
        if self.stage_id:
            parts.append(f"Stage: {self.stage_id.name}")
        if self.tag_ids:
            parts.append(f"Tags: {', '.join(self.tag_ids.mapped('name'))}")
        if self.description:
            desc = re.sub(r'<[^>]+>', ' ', self.description or '').strip()
            if desc:
                parts.append(f"Description: {desc[:300]}")
        notes = self._ml_extract_notes()
        if notes:
            parts.append(f"Conversation notes:\n{notes}")
        return '\n'.join(parts)

    def _ml_best_staff_message(self):
        """Return the body text of the most recent outgoing staff email on this lead."""
        msg = self.message_ids.filtered(
            lambda m: m.message_type == 'email' and m.author_id.user_ids
        ).sorted('date', reverse=True)[:1]
        if not msg:
            msg = self.message_ids.filtered(
                lambda m: m.message_type == 'comment' and m.author_id.user_ids
            ).sorted('date', reverse=True)[:1]
        if not msg:
            return ''
        return re.sub(r'<[^>]+>', ' ', msg.body or '').strip()[:600]

    def _ml_learn_from_outcome(self, outcome):
        """
        Save Won/Lost outcome + all notes to the ML knowledge base.
        Lost entries carry a correction_note with the reason, so the AI
        can warn staff when future leads look similar to past dead ends.
        """
        KB = self.env['premafirm.ml.knowledge']
        anchor = f'[crm:{self.id}]'
        existing = KB.search([
            ('knowledge_type', '=', 'crm_reply'),
            ('input_context', 'like', anchor),
        ], limit=1)

        context = f"{anchor}\n{self._ml_build_context()}"

        if outcome == 'won':
            good_output = self._ml_best_staff_message() or 'Deal closed successfully.'
            vals = {
                'knowledge_type': 'crm_reply',
                'input_context':  context,
                'good_output':    f"WON — ${self.expected_revenue:,.0f}\n{good_output}",
                'correction_note': '',
                'origin':         'approved',
                'weight':         3.0,
            }
            _logger.info('ML: CRM lead %s WON — saving to knowledge base', self.id)

        else:  # lost
            lost_reason = ''
            if self.lost_reason_id:
                lost_reason = self.lost_reason_id.name
            notes = self._ml_extract_notes()

            # Build a clear correction note — this is what future searches will warn against
            correction_parts = []
            if lost_reason:
                correction_parts.append(f"Lost reason: {lost_reason}")
            if notes:
                correction_parts.append(f"From conversation: {notes[:500]}")
            correction = '\n'.join(correction_parts) or 'No specific reason recorded.'

            customer = (self.partner_name
                        or (self.partner_id.name if self.partner_id else 'Unknown'))
            vals = {
                'knowledge_type': 'crm_reply',
                'input_context':  context,
                'good_output':    f"LOST — {customer} — {lost_reason or 'Reason not specified'}",
                'correction_note': correction,
                'origin':         'rejected',
                'weight':         2.0,
            }
            _logger.info('ML: CRM lead %s LOST — saving with reason: %s', self.id, lost_reason or 'none')

        if existing:
            existing.write(vals)
        else:
            KB.create(vals)

    def _ml_learn_from_email(self, message):
        """
        Capture an outgoing staff email as a crm_reply example.
        Throttled: saves at most once per 7 days per lead to avoid flooding.
        """
        body = re.sub(r'<[^>]+>', ' ', message.body or '').strip()
        if not body or len(body) < 30:
            return

        anchor = f'[crm:{self.id}]'
        KB = self.env['premafirm.ml.knowledge']
        cutoff = fields.Datetime.now() - relativedelta(days=7)
        recent = KB.search([
            ('knowledge_type', '=', 'crm_reply'),
            ('input_context', 'like', anchor),
            ('create_date', '>=', cutoff),
        ], limit=1)
        if recent:
            return  # Already have a recent example for this lead

        context = f"{anchor}\n{self._ml_build_context()}"
        KB.create({
            'knowledge_type': 'crm_reply',
            'input_context':  context,
            'good_output':    body[:600],
            'origin':         'approved',
            'weight':         1.0,
        })
        _logger.info('ML: outgoing email on lead %s saved as crm_reply example', self.id)
