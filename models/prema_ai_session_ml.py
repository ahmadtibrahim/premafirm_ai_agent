"""
Bridge between the ML knowledge base and Prema AI chat.

Extends `prema.ai.session._gather_erp_context` to append
relevant few-shot examples from the knowledge base before
the context is sent to GPT — turning accumulated human
feedback into live prompt improvements.

Lost CRM lead notes are injected as warnings when staff asks
to find new leads, so the AI avoids sending them down the
same dead ends again.
"""
import logging
from odoo import models

_logger = logging.getLogger(__name__)

# keywords → knowledge_type mapping for example injection
_TYPE_SIGNALS = [
    ('rate_quote',    ['rate', 'quote', 'price', 'freight', 'load', 'lane', 'carrier',
                       'per mile', 'flat rate', 'linehaul', 'fuel surcharge', 'fsc',
                       'how much', 'cost', 'charge', 'rpm', 'all-in']),
    ('wa_reply',      ['whatsapp', 'wa message', 'text message', 'sms reply',
                       'reply to', 'respond to']),
    ('crm_reply',     ['lead', 'prospect', 'crm', 'pipeline', 'email reply',
                       'draft reply', 'follow up', 'follow-up', 'customer reply',
                       'reach out', 'contact them', 'send email']),
    ('invoice_flag',  ['invoice', 'bill', 'amount', 'charge', 'overcharge',
                       'double billed', 'duplicate', 'flag', 'review invoice']),
    ('customer_tag',  ['tag', 'segment', 'category', 'customer type',
                       'classify', 'label', 'contact type']),
    ('load_tender',   ['load tender', 'negotiation', 'dispatcher offer', 'agreed rate',
                       'wa negotiation', 'counter offer']),
]

# keywords that signal "finding new leads" → inject lost-lead warnings
_LEAD_FIND_SIGNALS = [
    'find leads', 'find companies', 'find prospects', 'research companies',
    'potential customers', 'new customers', 'who should we target',
    'generate leads', 'lead gen', 'prospecting', 'cold outreach',
    'similar companies', 'companies like', 'what companies',
    'who else', 'expand our customer', 'grow our customer',
]


def _detect_types(lower_msg):
    found = []
    for ktype, words in _TYPE_SIGNALS:
        if any(w in lower_msg for w in words):
            found.append(ktype)
    return found


def _is_lead_finding(lower_msg):
    return any(sig in lower_msg for sig in _LEAD_FIND_SIGNALS)


class PremaAISessionML(models.Model):
    _inherit = 'prema.ai.session'

    def _gather_erp_context(self, user_msg, chat_mode='standard',
                            has_attachments=False, skip_att_scan=False):
        base_ctx = super()._gather_erp_context(
            user_msg, chat_mode=chat_mode,
            has_attachments=has_attachments, skip_att_scan=skip_att_scan,
        )
        try:
            sections = []

            # Inject relevant ML examples for the query type
            example_section = self._ml_context_section(user_msg)
            if example_section:
                sections.append(example_section)

            lower = (user_msg or '').lower()

            # Inject live cost parameters when the question is about rates/pricing
            if any(w in lower for w in _TYPE_SIGNALS[0][1]):  # rate_quote signals
                cost_section = self._ml_cost_params_section()
                if cost_section:
                    sections.append(cost_section)

            # When staff is looking for new leads, warn about past dead ends
            if _is_lead_finding(lower):
                warning_section = self._ml_lost_lead_warnings(user_msg)
                if warning_section:
                    sections.append(warning_section)

            if sections:
                return base_ctx + '\n\n' + '\n\n'.join(sections)

        except Exception as e:
            _logger.warning('ML context injection failed: %s', e)
        return base_ctx

    def _ml_cost_params_section(self):
        """Inject live estimator cost parameters AND actual fleet vehicle data.

        This gives GPT accurate numbers so it can calculate rates without
        guessing — eliminating 'truck not selected' errors and speeding up responses.
        """
        try:
            p = self.env['ir.config_parameter'].sudo()
            fuel      = float(p.get_param('estimator.fuel_price_per_l',        '1.55'))
            sys_driver = float(p.get_param('estimator.driver_rate_per_hr',      '28.00'))
            margin    = float(p.get_param('estimator.margin_pct',               '20.0'))
            wt_thresh = float(p.get_param('estimator.weight_threshold_lbs',     '3000'))
            wt_cwt    = float(p.get_param('estimator.weight_surcharge_per_cwt', '5.00'))

            lines = [
                '## PremaFirm Rate Estimation Parameters',
                f'Fuel price: ${fuel:.3f}/L  |  Margin: {margin:.1f}%  |  '
                f'Weight surcharge: ${wt_cwt:.2f}/CWT on loads over {wt_thresh:,.0f} lbs',
            ]

            # Inject actual fleet vehicle data so GPT knows exactly which truck is available
            vehicles = self.env['fleet.vehicle'].sudo().search([('active', '=', True)], limit=3)
            if vehicles:
                lines.append('')
                lines.append('## Available Trucks (use these specs for calculations):')
                for v in vehicles:
                    km_l = v.x_avg_km_per_l_last_week or 0.0
                    ins_km = v.x_insurance_cost_per_km or 0.0
                    maint_km = v.x_maintenance_cost_per_km or 0.0
                    driver_rate = v.x_driver_rate_per_hr if v.x_driver_rate_per_hr and v.x_driver_rate_per_hr > 0 else sys_driver
                    max_payload = v.x_max_payload_lbs or 0.0
                    truck_lines = [
                        f'Truck: {v.name}',
                        f'  Fuel efficiency: {km_l:.2f} km/L' if km_l > 0 else '  Fuel efficiency: not synced yet',
                        f'  Insurance/km: ${ins_km:.4f}' if ins_km > 0 else '  Insurance/km: not set',
                        f'  Maintenance/km: ${maint_km:.4f}' if maint_km > 0 else '  Maintenance/km: not set',
                        f'  Driver rate: ${driver_rate:.2f}/hr',
                        f'  Max payload: {max_payload:,.0f} lbs' if max_payload > 0 else '',
                    ]
                    lines.extend(l for l in truck_lines if l)

                # Show calculation example
                if vehicles[0].x_avg_km_per_l_last_week:
                    v = vehicles[0]
                    km_l = v.x_avg_km_per_l_last_week
                    ins_km = v.x_insurance_cost_per_km or 0.0
                    maint_km = v.x_maintenance_cost_per_km or 0.0
                    driver_rate = v.x_driver_rate_per_hr if v.x_driver_rate_per_hr and v.x_driver_rate_per_hr > 0 else sys_driver
                    lines += [
                        '',
                        '## How to calculate a rate for a given distance (km) and drive time (hrs):',
                        f'  fuel_cost = km / {km_l:.2f} × ${fuel:.3f}',
                        f'  insurance_cost = km × ${ins_km:.4f}',
                        f'  maintenance_cost = km × ${maint_km:.4f}',
                        f'  driver_cost = hrs × ${driver_rate:.2f}',
                        f'  total_cost = fuel + insurance + maintenance + driver',
                        f'  suggested_rate = total_cost × {1 + margin/100:.2f}  (includes {margin:.0f}% margin)',
                        'Note: Always show the cost breakdown in your answer.',
                    ]
            else:
                lines.append(f'Driver rate: ${sys_driver:.2f}/hr')
                lines.append('Note: No active trucks found. Ask the user to check Fleet → Vehicles.')

            return '\n'.join(lines)
        except Exception:
            return ''

    def _ml_context_section(self, user_msg):
        """Inject relevant past examples from the knowledge base."""
        lower = (user_msg or '').lower()
        types = _detect_types(lower)
        if not types:
            return ''

        KB = self.env['premafirm.ml.knowledge']
        blocks = []
        for ktype in types[:2]:  # cap at 2 types to stay concise
            examples = KB._search_similar(ktype, user_msg, limit=3)
            if not examples:
                continue
            lines = [f'## ML Examples — {ktype.replace("_", " ").title()}']
            for ex in examples:
                lines.append(f'Situation: {(ex.input_context or "")[:300]}')
                lines.append(f'Good output: {(ex.good_output or "")[:300]}')
                if ex.correction_note:
                    lines.append(f'Note: {ex.correction_note[:200]}')
                lines.append('---')
            blocks.append('\n'.join(lines))

        return '\n\n'.join(blocks) if blocks else ''

    def _ml_lost_lead_warnings(self, user_msg):
        """
        When the user is looking for new leads, inject a warning block listing
        past lost leads and their reasons — so the AI doesn't suggest the same
        companies or profiles that already failed.
        """
        KB = self.env['premafirm.ml.knowledge']
        lost = KB.search([
            ('knowledge_type', '=', 'crm_reply'),
            ('origin', '=', 'rejected'),
            ('active', '=', True),
        ], order='weight desc, create_date desc', limit=10)

        if not lost:
            return ''

        lines = [
            '## PAST LOST LEADS — DO NOT REPEAT THESE DEAD ENDS',
            'The following companies/profiles were already pursued and did not convert.',
            'If the user asks for similar leads, warn them and suggest alternatives.',
            '',
        ]
        for entry in lost:
            # Pull customer name from good_output ("LOST — CompanyName — reason")
            output = (entry.good_output or '').replace('LOST — ', '', 1)
            correction = entry.correction_note or ''
            # Trim context to the key identifying parts
            ctx_lines = [l for l in (entry.input_context or '').splitlines()
                         if l.startswith(('Customer:', 'Location:', 'Industry:', 'Lead:'))]
            ctx_summary = ' | '.join(ctx_lines[:3])
            lines.append(f'- {output}')
            if ctx_summary:
                lines.append(f'  Context: {ctx_summary}')
            if correction:
                lines.append(f'  Why lost: {correction[:200]}')

        lines += [
            '',
            'Use this list to avoid recommending the same dead-end profiles.',
            'If the user mentions a company that matches one above, flag it proactively.',
        ]
        return '\n'.join(lines)
