"""PHASE 18 — CRM tag cleanup: map first, archive never delete.

The tag table grew organically (imports, manual entry): case variants,
leading-space typos and compound aliases. Cleanup follows the spec rules:

  * the merge map is keyed by tag NAME and resolved at runtime — never
    by database id
  * leads are RETAGGED to the canonical tag (the obsolete tag is removed
    only after the canonical is added — a lead never loses its context)
  * every retag is recorded in premafirm.crm.tag.audit (lead / old / new
    / run date / source)
  * obsolete tags are ARCHIVED (active=False), never deleted — tag
    history stays recoverable, and re-runs are idempotent
  * the map is deliberately conservative: only mechanical duplicates
    (case / whitespace / obvious compound alias). Semantic overlaps
    (Logistics vs Logistics & Supply Chain, Reefer vs Reefer Freight,
    Food vs Food & Produce, ON vs Ontario …) are business decisions —
    left untouched and listed in the PHASE 19 review report.

Apply path: the 18.0.6.17.0 migration runs ``run_tag_cleanup`` with
dry_run=False. A dry-run server action is exposed for operators; nothing
else writes.
"""
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# canonical tag name → obsolete names (whitespace-normalized matching).
# Canonical tags that do not exist yet are created (never by id).
_TAG_MERGE_MAP = {
    'Mississauga': ['MISSISSAUGA'],
    'Sudbury': ['SUDBURY'],
    'Reefer Freight': [' Reefer Freight'],          # leading space typo
    'Direct Shipper': [' Direct Shipper'],          # leading space typo
    'Carrier': ['carrier'],                         # lowercase variant
    'Food Processing': ['food processing',
                        'food manufacturing / food processing'],
}


class CrmTag(models.Model):
    _inherit = 'crm.tag'

    # Archive mechanism: obsolete tags go inactive (never deleted).
    active = fields.Boolean(default=True, index=True)

    @api.model
    def _find_tag(self, name, exclude_id=None, include_inactive=True,
                  exact=False):
        """Resolve a tag by name. Never by id.

        exact=True (canonical lookup): case-SENSITIVE '=' match — a
        lowercase 'carrier' must NOT satisfy the canonical 'Carrier'
        (that would skip the merge). exact=False (obsolete lookup):
        case-insensitive '=ilike', whitespace-sensitive first, stripped
        as fallback."""
        domain = []
        if exclude_id:
            domain.append(('id', '!=', exclude_id))
        if include_inactive:
            domain.append(('active', 'in', [True, False]))
        if exact:
            return self.search(domain + [('name', '=', name)], limit=1)
        tag = self.search(domain + [('name', '=ilike', name)], limit=1)
        stripped = name.strip()
        if not tag and stripped != name:
            tag = self.search(domain + [('name', '=ilike', stripped)], limit=1)
        return tag

    @api.model
    def run_tag_cleanup(self, dry_run=True, source='manual'):
        """Apply _TAG_MERGE_MAP. dry_run: compute the plan, write nothing.
        Apply: create missing canonicals, retag every affected lead with an
        audit row, archive obsolete tags. Idempotent — re-runs no-op.

        Returns {canonical_name: {'created': bool, 'leads': int,
                                   'archived': [old names]}} for the
        migration log / battery / dry-run action."""
        Tag = self.env['crm.tag']
        Audit = self.env['premafirm.crm.tag.audit']
        Lead = self.env['crm.lead']
        summary = {}
        for canonical_name, obsolete_names in _TAG_MERGE_MAP.items():
            canonical = self._find_tag(canonical_name, exact=True)
            created = False
            if not canonical:
                if not dry_run:
                    canonical = Tag.create({'name': canonical_name})
                created = True
            elif not canonical.active and not dry_run:
                canonical.active = True  # revive for idempotent re-runs
            # Even in dry-run with a missing canonical, count the leads the
            # apply WOULD move (a dry-run that reports 0 on a first run is
            # misleading — the apply plan must equal the apply result).
            leads_moved = 0
            archived = []
            for oname in obsolete_names:
                obsolete = self._find_tag(
                    oname, exclude_id=canonical.id if canonical else None)
                if not obsolete:
                    continue
                for lead in Lead.search([
                        ('tag_ids', 'in', obsolete.id),
                        ('active', 'in', [True, False])]):
                    # never remove context before the canonical is added;
                    # a lead that already has it just keeps it
                    if not canonical or canonical.id not in lead.tag_ids.ids:
                        if not dry_run:
                            lead.write({'tag_ids': [(4, canonical.id)]})
                            Audit.create({
                                'lead_id': lead.id,
                                'old_tag': obsolete.name,
                                'new_tag': canonical_name,
                                'source': source,
                            })
                        leads_moved += 1
                    if not dry_run:
                        lead.write({'tag_ids': [(3, obsolete.id)]})
                if not dry_run:
                    obsolete.active = False
                archived.append(obsolete.name)
            summary[canonical_name] = {
                'created': created, 'leads': leads_moved,
                'archived': archived}
        return summary


class PremafirmCrmTagAudit(models.Model):
    _name = 'premafirm.crm.tag.audit'
    _description = 'CRM Tag Cleanup Audit'
    _order = 'run_date desc, id desc'

    lead_id = fields.Many2one('crm.lead', 'Lead', ondelete='cascade',
                              index=True, required=True)
    old_tag = fields.Char('Old Tag', required=True)
    new_tag = fields.Char('New Tag', required=True)
    run_date = fields.Datetime('Run Date',
                               default=lambda self: fields.Datetime.now(),
                               index=True)
    source = fields.Selection(
        [('migration', 'Migration'), ('manual', 'Manual Run')],
        default='manual', required=True)
