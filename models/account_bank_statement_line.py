from odoo import api, models


class AccountBankStatementLineFix(models.Model):
    """
    Guards against the ValueError that occurs when currency_id is empty on a
    bank statement line during the reconciliation widget's onchange cycle.

    Root cause: currency_id is a stored computed field that depends on
    journal_id.currency_id.  During the reconciliation widget's virtual-record
    context, journal_id (itself a related from move_id) may be empty, which
    cascades to an empty currency_id.  Core's _compute_is_reconciled then calls
    currency_id.is_zero() which raises "Expected singleton: res.currency()" and
    breaks every reconciliation attempt.

    The error re-appears after each bank-sync fetch because newly imported
    statement lines go through the same virtual-record path on first open.
    """
    _inherit = 'account.bank.statement.line'

    @api.depends('journal_id.currency_id', 'company_id')
    def _compute_currency_id(self):
        super()._compute_currency_id()
        company_currency = self.env.company.currency_id
        for line in self.filtered(lambda l: not l.currency_id):
            line.currency_id = line.company_id.currency_id or company_currency

    @api.depends(
        'journal_id', 'currency_id', 'amount', 'foreign_currency_id', 'amount_currency',
        'move_id.checked',
        'move_id.line_ids.account_id', 'move_id.line_ids.amount_currency',
        'move_id.line_ids.amount_residual_currency', 'move_id.line_ids.currency_id',
        'move_id.line_ids.matched_debit_ids', 'move_id.line_ids.matched_credit_ids',
    )
    def _compute_is_reconciled(self):
        # Safety net for lines where currency_id is NULL (broken stored-compute state).
        # We replicate core logic but avoid currency_id.is_zero() which crashes on empty currency.
        no_currency = self.filtered(lambda l: not l.currency_id)
        for line in no_currency:
            if not line.id:
                line.amount_residual = -(line.amount_currency if line.foreign_currency_id else line.amount)
                line.is_reconciled = False
                continue
            _, suspense_lines, _ = line._seek_for_lines()
            if not line.checked:
                line.amount_residual = -(line.amount_currency if line.foreign_currency_id else line.amount)
                line.is_reconciled = False
            elif suspense_lines:
                if suspense_lines.account_id.reconcile:
                    line.amount_residual = sum(suspense_lines.mapped('amount_residual_currency'))
                else:
                    line.amount_residual = sum(suspense_lines.mapped('amount_currency'))
                line.is_reconciled = (line.amount_residual == 0.0)
            else:
                # No suspense lines at all: fully matched, regardless of amount/currency.
                line.amount_residual = 0.0
                line.is_reconciled = True
        remaining = self - no_currency
        if remaining:
            super(AccountBankStatementLineFix, remaining)._compute_is_reconciled()
