import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class PremafirmSaleDepositWizard(models.TransientModel):
    """Record a customer deposit that was received on a quotation / sale order
    (e-transfer, cheque, cash…) BEFORE the job is invoiced.

    Why this exists — INV/2026/00093 (2026-09-08): the customer paid a $170
    deposit by e-transfer while only a quotation existed. A bank statement
    line can only be validated (reconciled) against a POSTED journal entry,
    so nothing could be applied while the order was open or the invoice was
    still draft — the deposit just sat as an open on-account item and the
    user had to remember to apply it by hand after posting the final invoice.

    This wizard formalizes the deposit at quotation time the Odoo-native way:
      1. creates the down-payment sale order line(s) + a down-payment invoice
         (core sale.advance.payment.inv machinery, so the final invoice nets
         the deposit automatically);
      2. POSTS that down-payment invoice immediately ("validating" it, so it
         becomes reconcile-able);
      3. auto-applies any matching unmatched bank payment already sitting on
         the customer's account (oldest first, capped at the deposit amount).
    """

    _name = "premafirm.sale.deposit.wizard"
    _description = "Record Customer Deposit on Quotation / Order"

    sale_order_id = fields.Many2one(
        "sale.order", string="Quotation / Order", required=True,
        ondelete="cascade")
    company_id = fields.Many2one(
        "res.company", related="sale_order_id.company_id", readonly=True)
    currency_id = fields.Many2one(
        "res.currency", related="sale_order_id.currency_id", readonly=True)
    customer = fields.Char(
        related="sale_order_id.partner_id.display_name",
        string="Customer", readonly=True)
    order_total = fields.Monetary(
        related="sale_order_id.amount_total", string="Order Total",
        currency_field="currency_id", readonly=True)
    amount = fields.Monetary(
        string="Deposit Amount Received",
        currency_field="currency_id", required=True)

    def action_apply(self):
        self.ensure_one()
        order = self.sale_order_id
        if order.state != "sale":
            raise UserError(_("Confirm the quotation first (state \"Sale Order\") "
                              "before recording a deposit."))
        if order.order_line.filtered(lambda l: l.is_downpayment):
            raise UserError(_("A deposit was already recorded on %s — see its "
                              "down-payment invoice on the Invoices smart button.")
                            % order.name)
        if self.amount <= 0.0:
            raise UserError(_("The deposit amount must be positive."))
        if order.currency_id.compare_amounts(self.amount, order.amount_total) > 0:
            raise UserError(_("The deposit (%s) cannot exceed the order total (%s).")
                            % (order.currency_id.format(self.amount),
                               order.currency_id.format(order.amount_total)))

        # 1. Down-payment sale order line(s) + draft down-payment invoice (core flow).
        core_wizard = self.env["sale.advance.payment.inv"].create({
            "sale_order_ids": [(6, 0, order.ids)],
            "advance_payment_method": "fixed",
            "fixed_amount": self.amount,
        })
        dp_invoice = core_wizard._create_invoices(order)

        # 2. Validate the down-payment invoice so bank payments can be applied to it.
        dp_invoice.with_user(self.env.user).action_post()

        # 3. Auto-apply matching unmatched bank payments on the customer account.
        applied = dp_invoice._apply_open_customer_payments()  # list of (aml, applied_amount)
        applied_total = sum(amount for _aml, amount in applied)

        body = ("Down payment of %s recorded: %s created and validated (posted). "
                "It will be deducted automatically from the final invoice."
                % (order.currency_id.format(self.amount), dp_invoice._get_html_link()))
        if applied:
            lines = "\n".join(
                "- %s — %s (%s)" % (
                    (aml.statement_line_id.payment_ref
                     or aml.statement_line_id.partner_name
                     or aml.name or "bank payment"),
                    aml.statement_line_id.move_id.name or "",
                    order.currency_id.format(amount),
                )
                for aml, amount in applied
            )
            body += "<br/><br/>Applied from unmatched bank payment(s):<br/>" + lines
        dp_invoice.message_post(body=body)

        if applied:
            message = "%s posted for %s — %s auto-applied from unmatched bank payment(s)." % (
                dp_invoice.name, order.name, order.currency_id.format(applied_total))
        else:
            message = ("%s posted for %s. No matching unmatched bank payment found — "
                       "link it from the bank statement line when it arrives."
                       % (dp_invoice.name, order.name))
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Deposit Recorded",
                "message": message,
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }
