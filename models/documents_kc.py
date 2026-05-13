from odoo import api, models

KC_FOLDER_PARAM = 'premafirm.kc_folder_id'
KC_TAG_NAME = 'Knowledge Center'


class DocumentsDocument(models.Model):
    _inherit = 'documents.document'

    def _kc_folder(self):
        fid = int(self.env['ir.config_parameter'].sudo().get_param(KC_FOLDER_PARAM, 0))
        return self.sudo().browse(fid) if fid else self.env['documents.document']

    def _kc_tag(self):
        return self.env['documents.tag'].sudo().search([('name', '=', KC_TAG_NAME)], limit=1)

    def _sync_kc_shortcuts(self):
        """Add or remove KC folder shortcuts based on the Knowledge Center tag."""
        kc_folder = self._kc_folder()
        kc_tag = self._kc_tag()
        if not kc_folder.exists() or not kc_tag:
            return

        for rec in self.sudo():
            # Never process shortcuts themselves or folders
            if rec.shortcut_document_id or rec.type == 'folder':
                continue

            has_tag = kc_tag in rec.tag_ids
            existing = rec.shortcut_ids.filtered(lambda s: s.folder_id == kc_folder)

            if has_tag and not existing:
                self.env['documents.document'].sudo().create({
                    'shortcut_document_id': rec.id,
                    'folder_id': kc_folder.id,
                    'type': rec.type,
                    'name': rec.name,
                })
            elif not has_tag and existing:
                existing.sudo().unlink()

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._sync_kc_shortcuts()
        return records

    def write(self, vals):
        res = super().write(vals)
        if 'tag_ids' in vals:
            self._sync_kc_shortcuts()
        return res
