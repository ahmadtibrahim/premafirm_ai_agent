from odoo import api, fields, models


class PremafirmMLKnowledge(models.Model):
    """
    The learning corpus. Every approved or corrected draft contributes an entry here.
    Future suggestions search this table for similar past examples (few-shot RAG).
    """
    _name = 'premafirm.ml.knowledge'
    _description = 'ML Knowledge Base'
    _order = 'weight desc, create_date desc'

    knowledge_type = fields.Selection([
        ('rate_quote',    'Rate Quote'),
        ('wa_reply',      'WhatsApp Reply'),
        ('crm_reply',     'CRM Reply'),
        ('invoice_flag',  'Invoice Flag'),
        ('customer_tag',  'Customer Tag'),
        ('load_tender',   'Load Tender'),
        ('bill_import',   'Bill Import'),
        ('bill_autofill',      'Bill Auto-Fill'),
        ('rate_conf_autofill', 'Rate Conf Auto-Fill'),
        ('company_document', 'Company Document'),
        ('general', 'General'),
    ], required=True, index=True)

    # What the situation was — used for keyword similarity search
    input_context = fields.Text('Input / Context', required=True)

    # The correct output that was ultimately used
    good_output = fields.Text('Good Output', required=True)

    # Why it was changed (from feedback note) — this is what teaches the model
    correction_note = fields.Text(
        'Correction Note',
        help='Populated from feedback when a human edited or rejected a draft. '
             'Explains WHY the AI suggestion was wrong so future prompts avoid the same mistake.',
    )

    # Origin
    source_draft_id = fields.Many2one('premafirm.ml.draft', ondelete='set null')
    origin = fields.Selection([
        ('approved',     'Approved as-is'),
        ('edited',       'Human-edited'),
        ('rejected',     'Rejected + corrected'),
        ('manual',       'Manually entered'),
        ('ingested',     'Bulk ingested'),
        ('negotiation',  'WA Negotiation'),
        ('purchase',     'Purchase Order'),
        ('fleet',        'Fleet Vehicle'),
        ('bill_edit',      'Bill Line Edit'),
        ('vendor_profile', 'Vendor Profile'),
        ('manual_seed',    'Manual Seed'),
    ], default='approved')

    # Higher weight = more influence on future prompts
    weight = fields.Float(default=1.0)
    active = fields.Boolean(default=True)

    @api.model
    def _search_similar(self, knowledge_type, query_text, limit=5):
        """
        Keyword-based similarity search over the knowledge base.
        Returns the most relevant entries for the given query.
        No vector DB needed — improves automatically as corpus grows.
        """
        import re
        keywords = re.findall(r'\b\w{4,}\b', (query_text or '').lower())[:8]

        all_entries = self.search(
            [('knowledge_type', '=', knowledge_type), ('active', '=', True)],
            order='weight desc, create_date desc',
        )
        if not keywords:
            return all_entries[:limit]

        scored = []
        for entry in all_entries:
            text = ((entry.input_context or '') + ' ' + (entry.good_output or '')).lower()
            score = sum(1 for kw in keywords if kw in text)
            scored.append((score, entry.id))

        scored.sort(key=lambda x: x[0], reverse=True)
        top_ids = [eid for _, eid in scored[:limit] if scored[0][0] > 0]
        if not top_ids:
            return all_entries[:limit]
        return self.browse(top_ids)
