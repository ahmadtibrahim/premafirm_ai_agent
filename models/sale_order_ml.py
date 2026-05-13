"""
Feed confirmed sale orders into the ML knowledge base so rate-quote learning
happens automatically whenever a quotation is confirmed.
"""
from odoo import models, exceptions


class SaleOrderML(models.Model):
    _inherit = 'sale.order'

    def action_confirm(self):
        result = super().action_confirm()
        for order in self.filtered(lambda o: o.state in ('sale', 'done')):
            self._ml_feed_rate_knowledge(order)
        return result

    def _ml_feed_rate_knowledge(self, order):
        KB = self.env['premafirm.ml.knowledge']
        lines = '\n'.join(
            f"  {l.name or l.product_id.name or 'Line'}: "
            f"{l.product_uom_qty} × {l.price_unit} {order.currency_id.name}"
            for l in order.order_line[:10]
        )
        context = (
            f"Customer: {order.partner_id.name}\n"
            f"Date: {order.date_order.strftime('%Y-%m-%d') if order.date_order else ''}\n"
            f"Reference: {order.name}\n"
            f"Lines:\n{lines}"
        )
        existing = KB.search([
            ('knowledge_type', '=', 'rate_quote'),
            ('input_context', 'like', order.name),
        ], limit=1)
        if existing:
            return

        engine = self.env['premafirm.ml.engine']
        att = engine._attachment_texts('sale.order', order.id)
        if att:
            context += f'\nAttachment:\n{att[:800]}'

        KB.create({
            'knowledge_type': 'rate_quote',
            'input_context':  context,
            'good_output': (
                f"Total: {order.amount_total} {order.currency_id.name}\n"
                f"Payment terms: {order.payment_term_id.name if order.payment_term_id else 'Standard'}"
            ),
            'origin': 'approved',
            'weight':  2.0,
        })

    def action_autofill_from_rate_conf(self):
        """Read the latest attachment on this quotation, extract rate confirmation data via AI,
        and open a draft for review before applying."""
        self.ensure_one()

        attachment = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'sale.order'),
            ('res_id', '=', self.id),
            ('type', '=', 'binary'),
        ], order='id desc', limit=1)

        if not attachment or not attachment.datas:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'No Attachment Found',
                    'message': 'Please upload the rate confirmation PDF or image first, then click Fill from Rate Conf.',
                    'type': 'warning',
                    'sticky': False,
                },
            }

        draft, err = self.env['premafirm.ml.engine'].generate_rate_conf_autofill(
            attachment.datas,
            attachment.mimetype or '',
            attachment.name or '',
            self,
        )

        if err or not draft:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Extraction Failed',
                    'message': err or 'Could not extract data from attachment.',
                    'type': 'warning',
                    'sticky': True,
                },
            }

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'premafirm.ml.draft',
            'res_id': draft.id,
            'view_mode': 'form',
            'target': 'new',
        }
