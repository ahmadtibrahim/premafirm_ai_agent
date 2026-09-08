from odoo import api, models


class MailTemplate(models.Model):
    _inherit = "mail.template"

    # The corrected body of the core "Invoice: Sending" template
    # (account.email_template_edi_invoice). Kept here in Python instead of a
    # <record> data tag on purpose: the core record sits in a noupdate=1 block,
    # and Odoo 18's _load_records() refuses to update noupdate XMLIDs on module
    # upgrade — so a data-file <record> override would silently never apply.
    # A <function> data tag always executes, on install AND on upgrade.
    premafirm_invoice_email_body = """
<div style="margin: 0px; padding: 0px;">
    <p style="margin: 0px; padding: 0px; font-size: 13px;">
        Dear
        <t t-if="object.partner_id.parent_id">
            <t t-out="object.partner_id.name or ''">Brandon Freeman</t> (<t t-out="object.partner_id.parent_id.name or ''">Azure Interior</t>),
        </t>
        <t t-else="">
            <t t-out="object.partner_id.name or ''">Brandon Freeman</t>,
        </t>
        <br/><br/>
        Here is your
        <t t-if="object.name">
            invoice <span style="font-weight:bold;" t-out="object.name or ''">INV/2021/05/0005</span>
        </t>
        <t t-else="">
            invoice
        </t>
        <t t-if="object.invoice_origin">
            (with reference: <t t-out="object.invoice_origin or ''">SUB003</t>)
        </t>
        <t t-if="object.payment_state in ('paid', 'in_payment')">
            amounting in <span style="font-weight:bold;" t-out="format_amount(object.amount_total, object.currency_id) or ''">$ 143,750.00</span>
            from <t t-out="object.company_id.name or ''">YourCompany</t>.
            This invoice is already paid.
        </t>
        <t t-else="">
            from <t t-out="object.company_id.name or ''">YourCompany</t>, with an outstanding balance of
            <span style="font-weight:bold;" t-out="format_amount(object.amount_residual, object.currency_id) or ''">$ 143,750.00</span>.
            Please remit payment at your earliest convenience.
            <t t-if="object.payment_reference">
                <br/><br/>
                Please use the following communication for your payment: <strong t-out="object.payment_reference or ''">INV/2021/05/0005</strong>
                <t t-if="object.partner_bank_id">
                    on the account <strong t-out="object.partner_bank_id.acc_number"/>
                </t>
                .
            </t>
        </t>
        <t t-if="hasattr(object, 'timesheet_count') and object.timesheet_count">
            <br/><br/>
            PS: you can review your timesheets <a t-att-href="'/my/timesheets?search_in=invoice&amp;search=%s' % object.name">from the portal.</a>
        </t>
        <br/><br/>
        Do not hesitate to contact us if you have any questions.
        <t t-if="not is_html_empty(object.invoice_user_id.signature)" data-o-mail-quote-container="1">
            <br/><br/>
            <t t-out="object.invoice_user_id.signature or ''" data-o-mail-quote="1">--<br data-o-mail-quote="1"/>Mitchell Admin</t>
        </t>
    </p>
</div>
"""

    @api.model
    def premafirm_install_invoice_email_body(self):
        """(Re)apply the corrected invoice-email body to the core template.

        Fixes the INV/2026/00093 defect: the stock body always renders
        format_amount(object.amount_total) — the FULL invoice total — even
        after a down payment / partial payment was applied. The corrected body
        shows the OUTSTANDING BALANCE (amount_residual), or states the invoice
        is already paid.

        Called by data/email_invoice_balance.xml on every install/upgrade (see
        the comment on premafirm_invoice_email_body for why a plain <record>
        override would be silently skipped).
        """
        tpl = self.env.ref("account.email_template_edi_invoice",
                           raise_if_not_found=False)
        if not tpl:
            return
        tpl.with_context(lang="en_US").body_html = self.premafirm_invoice_email_body
