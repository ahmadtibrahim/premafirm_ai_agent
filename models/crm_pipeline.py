"""PHASE 13 — Clean pipeline structure (the target pipeline).

Legacy stages are remapped onto the single clean pipeline:

  NEW / UNCONTACTED → OUTREACH SENT → ENGAGED / REPLIED →
  QUALIFIED / DATA COLLECTED → QUOTE REQUESTED → QUOTE SENT →
  NEGOTIATION → ONBOARDING → WON / ACTIVE CUSTOMER
  (+ separate LOST and PAUSED / ON HOLD)

HISTORY IS NEVER DESTROYED:

* Before any move, an AUDIT FILE (lead id, name, old stage, new stage,
  tags, note) is exported as an ir.attachment AND written to the local
  path configured in ``premafirm.pipeline.audit_path`` when set.
* ``action_premafirm_pipeline_restructure(dryrun=True)`` (the default)
  computes the full plan and exports the audit file WITHOUT touching the
  database. Pass dryrun=False to execute.
* Obsolete stage records are ARCHIVED after the migration (fold + moved
  to the end of the sequence) — never deleted. Stages that still hold
  leads after the remap are left untouched and reported.
* Segment-stage nuance survives as crm.tag records (Call Back, Call
  Approach, Dedicated Corridors Campaign, Retail, Suggestion) — never
  as pipeline stages.

Intelligent split (Contacted / Call Back / Call Approach): a lead that
ever received a meaningful reply goes to ENGAGED / REPLIED, otherwise to
OUTREACH SENT.
"""
import csv
import io
import logging
from datetime import date

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


def _norm(name):
    return (name or '').strip().lower()


# Old stage name → (target stage name | None = intelligent split,
#                    segment tag(s), note). Keys normalized (lowercase)
# — the lookup normalizes the live stage name the same way.
_OLD_TO_NEW = {
    _norm('New'): ('NEW / UNCONTACTED', (), ''),
    _norm('Outreach'): ('OUTREACH SENT', (), ''),
    _norm('Replied'): ('ENGAGED / REPLIED', (), ''),
    _norm('Contacted'): (None, (), 'intelligent split on reply history'),
    _norm('Data Collection'): ('QUALIFIED / DATA COLLECTED', (), ''),
    _norm('Approved'): ('WON / ACTIVE CUSTOMER', (), ''),
    _norm('Onboarding'): ('ONBOARDING', (), ''),
    _norm('Paused'): ('PAUSED / ON HOLD', (), ''),
    _norm('Lost / Cancelled'): ('LOST', (), ''),
    _norm('Call Back'): (None, ('Call Back',),
                         'intelligent split + call-context tag'),
    _norm('Call Approach'): (None, ('Call Approach',),
                             'intelligent split + call-context tag'),
    _norm('Dedicated Corridors Campaign'): (
        'OUTREACH SENT', ('Dedicated Corridors Campaign',),
        'segment tag preserved'),
    _norm('Retail'): ('NEW / UNCONTACTED', ('Retail',),
                      'segment tag preserved'),
    _norm('Suggestion'): ('NEW / UNCONTACTED', ('Suggestion',),
                          'pending-contact suggestion'),
}

# Normalized (lowercase) — compared against _norm()'d stage names.
_TARGET_NAMES = (
    'new / uncontacted', 'outreach sent', 'engaged / replied',
    'qualified / data collected', 'quote requested', 'quote sent',
    'negotiation', 'onboarding', 'won / active customer',
    'lost', 'paused / on hold',
)

# The module's own stage xmlids (data/crm_stages.xml, noupdate=1) —
# deterministic resolution; "onboarding" collides with the legacy
# "Onboarding" stage name, so name matching is never the primary lookup.
_TARGET_XMLIDS = {
    'new / uncontacted': 'premafirm_ai_engine.crm_stage_new_uncontacted',
    'outreach sent': 'premafirm_ai_engine.crm_stage_outreach_sent',
    'engaged / replied': 'premafirm_ai_engine.crm_stage_engaged_replied',
    'qualified / data collected': 'premafirm_ai_engine.crm_stage_qualified',
    'quote requested': 'premafirm_ai_engine.crm_stage_quote_requested',
    'quote sent': 'premafirm_ai_engine.crm_stage_quote_sent',
    'negotiation': 'premafirm_ai_engine.crm_stage_negotiation',
    'onboarding': 'premafirm_ai_engine.crm_stage_onboarding',
    'won / active customer': 'premafirm_ai_engine.crm_stage_won_active',
    'lost': 'premafirm_ai_engine.crm_stage_lost',
    'paused / on hold': 'premafirm_ai_engine.crm_stage_paused',
}

_AUDIT_PATH_PARAM = 'premafirm.pipeline.audit_path'


class CrmLead(models.Model):
    _inherit = 'crm.lead'

    # ── helpers ──────────────────────────────────────────────────────

    @api.model
    def _premafirm_target_stages(self):
        """Resolve the target pipeline stage records by XMLID (never by
        hardcoded id, never by name-first): the legacy pipeline has a
        stage ALSO named "Onboarding", so name matching is ambiguous.
        Name search is only a fallback for DB-created targets without an
        xmlid (the noupdate=1 XML records always exist after upgrade).
        Raises if a target is missing — the restructure must not run
        against an incomplete pipeline."""
        out = {}
        missing = []
        for norm_name, xmlid in _TARGET_XMLIDS.items():
            rec = self.env.ref(xmlid, raise_if_not_found=False)
            if not rec:
                rec = self.env['crm.stage'].sudo().search(
                    [('name', '=ilike', norm_name),
                     ('fold', '=', False)], limit=1)
            if not rec:
                missing.append(norm_name)
                continue
            out[norm_name] = rec
        if missing:
            raise ValueError(
                'Pipeline restructure aborted — target stages missing: %s'
                % ', '.join(missing))
        return out

    @api.model
    def _premafirm_ensure_tags(self, names):
        """Idempotent crm.tag creation by name."""
        Tag = self.env['crm.tag'].sudo()
        out = self.env['crm.tag']
        for name in names:
            tag = Tag.search([('name', '=', name)], limit=1)
            if not tag:
                tag = Tag.create({'name': name})
            out |= tag
        return out

    @api.model
    def _premafirm_intelligent_target(self, lead):
        if lead.last_meaningful_reply_at:
            return 'ENGAGED / REPLIED'
        return 'OUTREACH SENT'

    # ── the restructure ──────────────────────────────────────────────

    @api.model
    def action_premafirm_pipeline_restructure(self, dryrun=True):
        """PHASE 13 — map the legacy pipeline onto the target pipeline.

        dryrun=True (default): compute the plan + export the audit file,
        touch NOTHING. dryrun=False: remap the leads, tag the segment
        leads, then archive the emptied obsolete stage records (never
        delete). Returns a report dict. Idempotent — a second execute
        finds no leads in legacy stages and nothing to archive."""
        targets = self._premafirm_target_stages()
        Stage = self.env['crm.stage'].sudo()
        all_stages = Stage.search([])
        by_name = {_norm(s.name): s for s in all_stages}
        # ALL leads — active AND archived (active=False): the audit must
        # cover every lead, and archived leads must leave the legacy
        # stages too (their stage is cosmetic but must not dangle on a
        # stage we archive). An explicit 'active' leaf disables the
        # implicit active filter (Odoo 18 search has no active_test).
        leads = self.sudo().search(
            [('active', 'in', [True, False])], order='id')

        rows = []          # audit rows
        moves = {}         # new stage name → count
        tags_map = {}      # lead id → tag records to add
        skip_no_map = []   # leads in stages we do not map
        already = []       # leads already in a target stage

        for lead in leads:
            old = by_name.get(_norm(lead.stage_id.name))
            if not old:
                skip_no_map.append((lead.id, lead.name,
                                    lead.stage_id.name or '?'))
                continue
            old_norm = _norm(old.name)
            target_name = old_norm
            tag_names = []
            note = ''
            if old_norm not in _TARGET_NAMES:
                spec = _OLD_TO_NEW.get(old_norm)
                if not spec:
                    skip_no_map.append((lead.id, lead.name, old.name))
                    continue
                target_name, tag_names, note = spec
                if target_name is None:
                    target_name = self._premafirm_intelligent_target(lead)
            else:
                already.append(lead.id)
                note = 'already in target stage'
            rows.append({
                'lead_id': lead.id,
                'lead_name': lead.name,
                'old_stage': old.name,
                'new_stage': target_name,
                'tags': ','.join(tag_names),
                'note': note,
            })
            moves[target_name] = moves.get(target_name, 0) + 1
            if tag_names:
                tags_map[lead.id] = tag_names

        # ── audit export (always, even dryrun) ────────────────────────
        audit_id = self._premafirm_export_audit(rows)
        local_path = None
        param = self.env['ir.config_parameter'].sudo()
        path = param.get_param(_AUDIT_PATH_PARAM, '')
        if path:
            try:
                with open(path, 'w', newline='') as fh:
                    self._premafirm_write_audit_csv(fh, rows)
                local_path = path
            except OSError as exc:  # pragma: no cover — fs edge
                _logger.warning('PHASE 13: audit path %s not writable: %s',
                                path, exc)

        report = {
            'dryrun': bool(dryrun),
            'leads_total': len(leads),
            'to_move': len(rows),
            'already_in_target': len(already),
            'skip_unmapped_stage': len(skip_no_map),
            'unmapped': skip_no_map[:20],
            'moves': moves,
            'audit_attachment_id': audit_id,
            'audit_path': local_path,
        }
        if dryrun:
            _logger.info(
                'PHASE 13: DRY RUN — %s leads, %s to move, audit attached #%s',
                len(leads), len(rows), audit_id)
            return report

        # ── execute: remap ────────────────────────────────────────────
        now = fields.Datetime.now()
        for lead in leads:
            row = next((r for r in rows if r['lead_id'] == lead.id), None)
            if not row:
                continue
            target = targets[_norm(row['new_stage'])]
            if row['tags']:
                lead.write({'tag_ids': [(4, t.id) for t in
                                        self._premafirm_ensure_tags(
                                            row['tags'].split(','))]})
            if lead.stage_id.id == target.id:
                continue  # already in the target stage — no touch
            # crm.lead's own write override sets date_closed when moving
            # to a won stage (crm_lead.py) — no manual stamping needed.
            lead.write({'stage_id': target.id})

        # ── archive the emptied obsolete stages (never delete) ────────
        archived = []
        seq = 90
        for stage in all_stages:
            if _norm(stage.name) in _TARGET_NAMES:
                continue
            if stage.is_won:
                continue  # Approved: won column, zero leads — untouched
            if stage.fold:
                continue  # already archived
            # count ALL leads — archived ones included (same active
            # bypass as the remap search above)
            if self.sudo().search_count(
                    [('stage_id', '=', stage.id),
                     ('active', 'in', [True, False])]):
                report.setdefault('stages_still_holding_leads', []).append(
                    stage.name)
                continue
            stage.write({'fold': True, 'sequence': seq})
            seq += 1
            archived.append(stage.name)

        report['archived_stages'] = archived
        _logger.info(
            'PHASE 13: EXECUTED — %s leads moved, %s stages archived (%s)',
            len(rows), len(archived), ', '.join(archived))
        return report

    # ── audit export ─────────────────────────────────────────────────

    @api.model
    def _premafirm_write_audit_csv(self, fh, rows):
        writer = csv.writer(fh)
        writer.writerow(['lead_id', 'lead_name', 'old_stage', 'new_stage',
                         'tags', 'note'])
        for row in rows:
            writer.writerow([row['lead_id'], row['lead_name'],
                             row['old_stage'], row['new_stage'],
                             row['tags'], row['note']])

    @api.model
    def _premafirm_export_audit(self, rows):
        buf = io.StringIO()
        self._premafirm_write_audit_csv(buf, rows)
        name = 'pipeline_migration_audit_%s.csv' % date.today().isoformat()
        att = self.env['ir.attachment'].sudo().search(
            [('name', '=', name)], limit=1)
        data = buf.getvalue().encode('utf-8')
        if att:
            att.write({'raw': data})
        else:
            att = self.env['ir.attachment'].sudo().create({
                'name': name, 'raw': data, 'mimetype': 'text/csv',
                'res_model': 'crm.lead', 'res_id': 0,
            })
        return att.id
