import json
import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = "account.move"

    # Kept as internal fields (hidden from view — values mapped to native ref field)
    premafirm_po = fields.Char("PO #")
    premafirm_bol = fields.Char("BOL #")
    premafirm_pod = fields.Char("POD #")
    load_reference = fields.Char()

    # IFTA fuel province — required on fuel bills for quarterly IFTA reporting
    x_ifta_province = fields.Selection([
        # ── Canadian provinces / territories ──
        ("AB", "AB — Alberta"),
        ("BC", "BC — British Columbia"),
        ("MB", "MB — Manitoba"),
        ("NB", "NB — New Brunswick"),
        ("NL", "NL — Newfoundland & Labrador"),
        ("NS", "NS — Nova Scotia"),
        ("NT", "NT — Northwest Territories"),
        ("NU", "NU — Nunavut"),
        ("ON", "ON — Ontario"),
        ("PE", "PE — Prince Edward Island"),
        ("QC", "QC — Quebec"),
        ("SK", "SK — Saskatchewan"),
        ("YT", "YT — Yukon"),
        # ── US states (IFTA members) ──
        ("AL", "AL — Alabama"),
        ("AK", "AK — Alaska"),
        ("AZ", "AZ — Arizona"),
        ("AR", "AR — Arkansas"),
        ("CA", "CA — California"),
        ("CO", "CO — Colorado"),
        ("CT", "CT — Connecticut"),
        ("DE", "DE — Delaware"),
        ("FL", "FL — Florida"),
        ("GA", "GA — Georgia"),
        ("ID", "ID — Idaho"),
        ("IL", "IL — Illinois"),
        ("IN", "IN — Indiana"),
        ("IA", "IA — Iowa"),
        ("KS", "KS — Kansas"),
        ("KY", "KY — Kentucky"),
        ("LA", "LA — Louisiana"),
        ("ME", "ME — Maine"),
        ("MD", "MD — Maryland"),
        ("MA", "MA — Massachusetts"),
        ("MI", "MI — Michigan"),
        ("MN", "MN — Minnesota"),
        ("MS", "MS — Mississippi"),
        ("MO", "MO — Missouri"),
        ("MT", "MT — Montana"),
        ("NE", "NE — Nebraska"),
        ("NV", "NV — Nevada"),
        ("NH", "NH — New Hampshire"),
        ("NJ", "NJ — New Jersey"),
        ("NM", "NM — New Mexico"),
        ("NY", "NY — New York"),
        ("NC", "NC — North Carolina"),
        ("ND", "ND — North Dakota"),
        ("OH", "OH — Ohio"),
        ("OK", "OK — Oklahoma"),
        ("OR", "OR — Oregon"),
        ("PA", "PA — Pennsylvania"),
        ("RI", "RI — Rhode Island"),
        ("SC", "SC — South Carolina"),
        ("SD", "SD — South Dakota"),
        ("TN", "TN — Tennessee"),
        ("TX", "TX — Texas"),
        ("UT", "UT — Utah"),
        ("VT", "VT — Vermont"),
        ("VA", "VA — Virginia"),
        ("WA", "WA — Washington"),
        ("WV", "WV — West Virginia"),
        ("WI", "WI — Wisconsin"),
        ("WY", "WY — Wyoming"),
    ], string="IFTA Province / State",
       help="Jurisdiction where fuel was purchased — required for IFTA quarterly reporting.")

    # Tracks the ML knowledge entry created after AI generation — enables correction learning
    premafirm_ml_knowledge_id = fields.Many2one(
        "premafirm.ml.knowledge",
        string="AI Knowledge Entry",
        ondelete="set null",
        readonly=True,
        copy=False,
    )
    dispatch_estimator_id = fields.Many2one(
        "premafirm.rate.estimator",
        string="Dispatch Estimate (Legacy)",
        readonly=True,
        copy=False,
        ondelete="set null",
    )

    x_ai_summary = fields.Text("AI Summary", readonly=True, copy=False)
    x_ai_summary_instruction = fields.Char(
        "Tell AI what to change",
        copy=False,
        help="Type an instruction then click AI Generate (invoices) or AI Summary (bills) to update.",
    )
    x_ai_summary_at = fields.Datetime("Summary Generated At", readonly=True, copy=False)
    dispatch_estimator_ids = fields.One2many(
        "premafirm.rate.estimator",
        "invoice_id",
        string="Dispatch Jobs",
        copy=False,
    )
    job_count = fields.Integer(string="Jobs", compute="_compute_job_counts")
    dispatched_count = fields.Integer(string="Dispatched", compute="_compute_job_counts")
    week_reference = fields.Char(string="Week Reference",
                                  help="e.g. 'Week of May 04 2026'")

    @api.depends("dispatch_estimator_ids", "dispatch_estimator_ids.fleetbase_order_id")
    def _compute_job_counts(self):
        for rec in self:
            jobs = rec.dispatch_estimator_ids
            rec.job_count = len(jobs)
            rec.dispatched_count = len(jobs.filtered("fleetbase_order_id"))

    def action_dispatch_all_jobs(self):
        self.ensure_one()
        pending = self.dispatch_estimator_ids.filtered(lambda e: not e.fleetbase_order_id)
        dispatched = 0
        skipped = 0
        errors = []
        for job in pending:
            try:
                result = job.action_dispatch_to_fleetbase()
                if isinstance(result, dict) and result.get("tag") == "display_notification":
                    skipped += 1
                else:
                    dispatched += 1
            except Exception as e:
                errors.append(f"{job.job_day_ref or job.name}: {e}")
        already = len(self.dispatch_estimator_ids) - len(pending)
        msg = f"Dispatched {dispatched} job(s)."
        if already:
            msg += f" {already} already dispatched (skipped)."
        if errors:
            msg += f" Errors: {'; '.join(errors)}"
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Dispatch Complete",
                "message": msg,
                "type": "success" if not errors else "warning",
                "sticky": bool(errors),
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    def action_add_dispatch_job(self):
        """Open the dispatch wizard to add a new job to this invoice."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Add Dispatch Job",
            "res_model": "premafirm.dispatch.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_invoice_id": self.id,
                "default_job_sequence": len(self.dispatch_estimator_ids) + 1,
            },
        }

    def action_open_dispatch_jobs(self):
        """Smart button — open the list of dispatch jobs for this invoice."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Dispatch Jobs",
            "res_model": "premafirm.rate.estimator",
            "view_mode": "list,form",
            "domain": [("invoice_id", "=", self.id)],
            "context": {"default_invoice_id": self.id},
        }

    @api.model_create_multi
    def create(self, vals_list):
        company_currency = self.env.company.currency_id.id
        for vals in vals_list:
            vals.setdefault("currency_id", company_currency)
        return super().create(vals_list)

    def write(self, vals):
        # Keep the Studio custom "Reference" field (x_studio_reference) in sync with
        # the standard ref field so both the form and the printed invoice stay consistent.
        if "ref" in vals and "x_studio_reference" not in vals:
            vals = dict(vals, x_studio_reference=vals["ref"] or False)
        elif "x_studio_reference" in vals and "ref" not in vals:
            vals = dict(vals, ref=vals["x_studio_reference"] or False)

        # If staff edits the reference after AI generation, teach the ML knowledge entry
        if "ref" in vals:
            for rec in self:
                ml = rec.premafirm_ml_knowledge_id
                if not ml:
                    continue
                new_ref = vals["ref"] or ""
                try:
                    old_output = json.loads(ml.good_output or "{}")
                    old_ref = old_output.get("reference", "")
                    if new_ref and new_ref != old_ref:
                        new_output = dict(old_output)
                        new_output["reference"] = new_ref
                        correction = f"Staff corrected reference: '{old_ref}' → '{new_ref}'"
                        ml.sudo().write({
                            "good_output":     json.dumps(new_output, indent=2),
                            "correction_note": correction,
                            "origin":          "edited",
                            "weight":          min(ml.weight + 0.5, 5.0),
                        })
                        _logger.info("ML knowledge #%s updated from ref edit on %s", ml.id, rec.name)
                except Exception:
                    _logger.exception("ML correction update failed for invoice %s", rec.name)
        return super().write(vals)

    def action_ai_extract_reference(self):
        """Extract only reference numbers from attachments and write to the ref field."""
        self.ensure_one()
        from ..services.invoice_ai_service import InvoiceAIService

        try:
            service = InvoiceAIService(self.env)
            reference = service.extract_reference_only(self)
        except ValueError as e:
            raise UserError(str(e))
        except Exception as e:
            _logger.exception("Reference extraction failed for %s", self.name)
            raise UserError(f"Reference extraction failed: {type(e).__name__}: {e}")

        if not reference:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "No Reference Found",
                    "message": "No BOL, PO, Delivery #, or other reference numbers were found in the attachments.",
                    "type": "warning",
                    "sticky": False,
                },
            }

        # Write to both: ref (used by printed invoice) and x_studio_reference (visible on form)
        self.sudo().with_context(skip_invoice_sync=True).write({
            "ref": reference,
            "x_studio_reference": reference,
        })
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Reference Extracted",
                "message": "Reference field updated.",
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    def action_ai_generate_invoice(self):
        self.ensure_one()

        from ..services.invoice_ai_service import InvoiceAIService

        try:
            service = InvoiceAIService(self.env)
            result = service.analyze_and_generate(self)
        except ValueError as e:
            raise UserError(str(e))
        except Exception as e:
            _logger.exception("Invoice AI generation failed for %s", self.name)
            raise UserError(f"AI generation failed: {type(e).__name__}: {e}")

        if not result:
            raise UserError("AI returned no usable result. Please check the attachments and try again.")

        # Save to ML knowledge base for future improvement
        ml_record = service.save_to_ml(self, result)
        if ml_record:
            self.sudo().write({"premafirm_ml_knowledge_id": ml_record.id})

        reference = result.get("reference") or ""
        description = result.get("description") or ""
        product_id_val = result.get("product_id")
        line_items = result.get("line_items") or []
        amount_val = result.get("amount")
        confidence = result.get("confidence", "unknown")
        is_posted = self.state == "posted"

        amount = None
        if amount_val not in (None, ""):
            try:
                amount = float(str(amount_val).replace(",", "").replace("$", "").strip())
            except (TypeError, ValueError):
                amount = None

        # ref is not in Odoo's unmodifiable_fields list — write directly.
        # skip_invoice_sync=True prevents _sync_dynamic_lines from recomputing tax/payment-term
        # lines (which would trigger the secondary-currency account constraint on posted invoices).
        if reference:
            self._write_safe({"ref": reference})

        existing_product_lines = self.invoice_line_ids.filtered(
            lambda l: l.display_type == "product" and l.product_id
        )

        product = None
        if product_id_val and not existing_product_lines:
            product = self.env["product.product"].browse(int(product_id_val)).exists()
        elif existing_product_lines:
            product = existing_product_lines.sorted("sequence")[0].product_id

        if not is_posted and line_items and product:
            self._apply_ai_schedule_lines_draft(product, line_items)
        elif is_posted:
            self._apply_ai_lines_posted(existing_product_lines, product, description)
        else:
            self._apply_ai_lines_draft(existing_product_lines, product, description, amount)

        self._generate_ai_summary(result)

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "AI Generate Complete",
                "message": f"Reference and description generated ({confidence} confidence).",
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    def action_open_dispatch_estimate(self):
        self.ensure_one()
        if not self.dispatch_estimator_id:
            raise UserError("No dispatch estimate is linked to this invoice yet.")
        return {
            "type": "ir.actions.act_window",
            "name": "Dispatch Estimate",
            "res_model": "premafirm.rate.estimator",
            "res_id": self.dispatch_estimator_id.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_ai_prepare_dispatch_estimate(self):
        self.ensure_one()
        attachments = self.env["ir.attachment"].sudo().search([
            ("res_model", "=", "account.move"),
            ("res_id", "=", self.id),
            ("type", "=", "binary"),
        ], order="id asc")
        if not attachments:
            raise UserError("Upload a route sheet, rate confirmation, or stop document first.")

        Estimator = self.env["premafirm.rate.estimator"].sudo()
        extraction_candidates = []
        for att in attachments:
            if not att.datas:
                continue
            extracted = Estimator.extract_stops_from_file_rpc(
                att.datas,
                att.mimetype or "",
                att.name or "",
                extra_notes=self.narration or self.ref or "",
            )
            if extracted.get("error"):
                continue
            stops = extracted.get("stops") or []
            if len(stops) < 2:
                continue
            score = (
                len(stops) * 100
                + int(float(extracted.get("confidence") or 0.0) * 100)
                + (10 if not extracted.get("used_ai") else 0)
            )
            extraction_candidates.append((score, att, extracted))

        if not extraction_candidates:
            raise UserError(
                "No attached file contained a usable pickup/delivery stop list. "
                "Attach a route sheet, tender, or schedule with addresses and time windows."
            )

        extraction_candidates.sort(key=lambda item: item[0], reverse=True)
        _score, attachment, extracted = extraction_candidates[0]
        stops = extracted.get("stops") or []
        load_metrics = Estimator.infer_load_metrics(stops)
        scheduled_at = Estimator.infer_scheduled_at(stops)
        notes_parts = [
            f"Source attachment: {attachment.name}",
            extracted.get("notes") or "",
            self.narration or "",
        ]
        combined_notes = "\n".join(part.strip() for part in notes_parts if part and part.strip())

        plan = Estimator.suggest_dispatch_plan_rpc(
            stops=stops,
            scheduled_at=scheduled_at or None,
            allow_cross_border=False,
            avoid_tolls=True,
            load_weight_lbs=load_metrics["load_weight_lbs"],
            load_pallets=load_metrics["load_pallets"],
            notes=combined_notes,
            require_liftgate=load_metrics["require_liftgate"],
        )

        if plan.get("success") and (plan.get("estimate") or {}).get("estimate_id"):
            estimate = Estimator.browse(plan["estimate"]["estimate_id"]).exists()
            if estimate:
                estimate.write({
                    "source_invoice_id": self.id,
                    "notes": combined_notes or estimate.notes,
                })
                self.sudo().write({"dispatch_estimator_id": estimate.id})
                return {
                    "type": "ir.actions.act_window",
                    "name": "Dispatch Estimate",
                    "res_model": "premafirm.rate.estimator",
                    "res_id": estimate.id,
                    "view_mode": "form",
                    "target": "current",
                }

        origin = stops[0] if stops else {}
        destination = stops[-1] if stops else {}
        fallback_estimate = Estimator.create({
            "source_invoice_id": self.id,
            "origin_address": origin.get("address", ""),
            "origin_lat": origin.get("lat") or 0.0,
            "origin_lng": origin.get("lng") or 0.0,
            "destination_address": destination.get("address", ""),
            "destination_lat": destination.get("lat") or 0.0,
            "destination_lng": destination.get("lng") or 0.0,
            "stops_json": json.dumps(stops),
            "allow_cross_border": False,
            "avoid_tolls": True,
            "load_weight_lbs": load_metrics["load_weight_lbs"],
            "load_pallets": load_metrics["load_pallets"],
            "scheduled_at": scheduled_at or False,
            "notes": combined_notes or False,
            "state": "error" if plan.get("error") else "saved",
            "error_message": plan.get("error") or False,
        })
        self.sudo().write({"dispatch_estimator_id": fallback_estimate.id})
        return {
            "type": "ir.actions.act_window",
            "name": "Dispatch Estimate",
            "res_model": "premafirm.rate.estimator",
            "res_id": fallback_estimate.id,
            "view_mode": "form",
            "target": "current",
        }

    def _generate_ai_summary(self, result=None):
        """Generate or regenerate the AI Summary field. Called after AI Generate and standalone."""
        from ..services.openai_utils import openai_chat, DEFAULT_MODEL
        ICP = self.env["ir.config_parameter"].sudo()
        api_key = ICP.get_param("openai.api_key") or ICP.get_param("prema_ai.api_key")
        if not api_key:
            return

        is_vendor_bill = self.move_type == "in_invoice"
        instruction = (self.x_ai_summary_instruction or "").strip()

        lines_text = "\n".join(
            f"- {l.name or l.product_id.name or '(line)'}: ${l.price_unit:.2f} × {l.quantity:.0f}"
            for l in self.invoice_line_ids.filtered(lambda l: l.display_type == "product")
        ) or "(no product lines)"

        context_parts = [
            f"Document: {'Vendor Bill' if is_vendor_bill else 'Customer Invoice'} {self.name or 'Draft'}",
            f"Reference: {self.ref or '—'}",
            f"{'Vendor' if is_vendor_bill else 'Customer'}: {self.partner_id.name if self.partner_id else 'Unknown'}",
            f"Total: ${self.amount_total:.2f} {self.currency_id.name}",
            f"Date: {(self.invoice_date or self.date or '—')}",
            f"Lines:\n{lines_text}",
        ]
        if result:
            if result.get("description"):
                context_parts.append(f"AI-generated description: {result['description']}")
            if result.get("reference"):
                context_parts.append(f"Extracted reference: {result['reference']}")
        context_text = "\n".join(context_parts)

        if is_vendor_bill:
            base_system = (
                "You are a logistics operations analyst at PremaFirm Inc., a Canadian trucking company. "
                "Summarize this vendor bill in plain English for internal use. "
                "Cover: what was purchased, whether the amount looks reasonable for a trucking operation, "
                "any flags (unusual vendor, high cost, missing reference, possible duplicate). "
                "Keep it to 3-4 sentences. Be direct — this is for internal staff."
            )
        else:
            base_system = (
                "You are a logistics operations analyst at PremaFirm Inc., a Canadian trucking company. "
                "Summarize this customer invoice in plain English for internal use. "
                "Cover: what service was invoiced, the route/lane if identifiable from the description or reference, "
                "any pricing notes or margin context, customer behavior flags if notable. "
                "Keep it to 3-4 sentences. Be direct — this is for internal staff."
            )

        if instruction:
            base_system += f"\n\nIMPORTANT — apply this instruction to your summary: {instruction}"

        try:
            model = ICP.get_param("prema_ai.fast_model") or DEFAULT_MODEL
            summary = openai_chat(
                messages=[{"role": "user", "content": context_text}],
                system=base_system,
                max_tokens=250,
                api_key=api_key,
                model=model,
                timeout=30,
            )
            self.sudo().write({
                "x_ai_summary": summary.strip(),
                "x_ai_summary_at": fields.Datetime.now(),
                "x_ai_summary_instruction": False,
            })
        except Exception:
            _logger.exception("AI Summary generation failed for %s", self.name)

    def action_generate_ai_summary(self):
        """Standalone button — generates/refreshes AI Summary without running full AI Generate."""
        self.ensure_one()
        self._generate_ai_summary()
        if not self.x_ai_summary:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "AI Summary",
                    "message": "Could not generate summary. Check your AI API key in Settings.",
                    "type": "warning",
                    "sticky": False,
                },
            }
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "AI Summary Updated",
                "message": "Summary generated successfully.",
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    def _apply_ai_lines_draft(self, existing_product_lines, product, description, amount=None):
        """Apply AI product + note lines on a draft invoice (no restrictions)."""
        if not existing_product_lines and product:
            vals_list = [
                {
                    "product_id": product.id,
                    "name": "",
                    "quantity": 1,
                    "price_unit": amount or 0,
                    "display_type": "product",
                }
            ]
            if description:
                vals_list.append({"display_type": "line_note", "name": description})
            self.invoice_line_ids = [(0, 0, v) for v in vals_list]
        else:
            if existing_product_lines and amount is not None:
                primary_line = existing_product_lines.sorted("sequence")[0]
                if not primary_line.price_unit:
                    primary_line.write({"price_unit": amount})
            if description:
                self._upsert_note_line(existing_product_lines, description)

    def _apply_ai_schedule_lines_draft(self, product, line_items):
        """Replace draft invoice lines with dated schedule rows from a rate confirmation."""
        vals_list = []
        for item in line_items:
            name = (item.get("name") or "").strip()
            if not name:
                continue
            try:
                amount = float(item.get("amount") or 0)
            except (TypeError, ValueError):
                amount = 0.0
            vals_list.append({
                "product_id": product.id,
                "name": name,
                "quantity": 1,
                "price_unit": amount,
                "display_type": "product",
            })
        if vals_list:
            self.invoice_line_ids = [(5, 0, 0)] + [(0, 0, v) for v in vals_list]

    def _apply_ai_lines_posted(self, existing_product_lines, product, description):
        """
        Apply AI lines on a posted invoice without the draft/post cycle.

        - ref: already written before this call (not in unmodifiable_fields).
        - Note lines: use skip_readonly_check=True to bypass the unmodifiable_fields
          guard; note lines are excluded from accounting constraint checks so it is safe.
        - Product lines: cannot be added to a posted invoice without re-posting;
          post description to chatter instead and let the user decide.
        """
        if not existing_product_lines and product:
            # Product line addition requires re-posting — show chatter note instead
            body = (
                "<b>AI Generate</b> — invoice is posted.<br/>"
                "Reset to draft to add the service product.<br/>"
                + (f"<b>Generated description:</b><br/>{description.replace(chr(10), '<br/>')}" if description else "")
            )
            self.message_post(body=body)
            return

        if description:
            self._upsert_note_line(
                existing_product_lines,
                description,
                skip_readonly=True,
            )

    def _write_safe(self, vals):
        """Write to this move bypassing readonly and dynamic-line-sync guards.

        safe for ref and note-line changes on posted invoices:
        - skip_readonly_check   → bypasses Odoo's unmodifiable_fields guard
        - skip_invoice_sync     → prevents _sync_dynamic_lines from recomputing
                                  tax/payment-term lines (which triggers the
                                  secondary-currency account constraint)
        """
        self.with_context(
            skip_readonly_check=True,
            skip_invoice_sync=True,
        ).write(vals)

    def _upsert_note_line(self, existing_product_lines, description, skip_readonly=False):
        """Add or update the AI-generated note line below the last service product line.

        Identifies the AI note by content (starts with 'Freight / Delivery Service')
        rather than sequence, which Odoo may renumber after write.
        Also removes any duplicate AI note lines before writing.
        """
        all_lines = self.invoice_line_ids.sorted("sequence")

        # Find all existing AI note lines by content signature
        ai_notes = all_lines.filtered(
            lambda l: l.display_type == "line_note"
            and (l.name or "").startswith("Freight / Delivery Service")
        )

        if ai_notes:
            # Update the first, delete any extras created by double-clicks
            keeper = ai_notes[0]
            duplicates = ai_notes[1:]
            if duplicates:
                duplicates.with_context(skip_invoice_sync=True, force_delete=True).unlink()
            keeper.with_context(skip_invoice_sync=True).write({"name": description})
            return

        # No existing AI note — create one below the last product line
        if existing_product_lines:
            last_service = existing_product_lines.sorted("sequence")[-1]
            new_seq = (last_service.sequence or 100) + 1
        else:
            new_seq = 100

        line_vals = [(0, 0, {
            "display_type": "line_note",
            "name": description,
            "sequence": new_seq,
        })]
        if skip_readonly:
            self._write_safe({"invoice_line_ids": line_vals})
        else:
            self.with_context(skip_invoice_sync=True).write({"invoice_line_ids": line_vals})


class ResCompany(models.Model):
    _inherit = 'res.company'

    x_invoice_bcc_partner_id = fields.Many2one(
        'res.partner',
        string='Invoice BCC Contact',
        help='This contact silently receives a copy of every invoice email sent to customers.',
    )


class AccountMoveSend(models.AbstractModel):
    _inherit = 'account.move.send'

    def _get_invoice_extra_attachments(self, move):
        # invoice_pdf_report_id has res_field set, so ir.attachment._search excludes it
        # from attachment_ids — must add it back explicitly
        return move.invoice_pdf_report_id | move.attachment_ids

    def _send_mail(self, move, mail_template, **kwargs):
        bcc = move.company_id.x_invoice_bcc_partner_id
        if bcc and bcc.id not in (kwargs.get('partner_ids') or []):
            kwargs = dict(kwargs)
            kwargs['partner_ids'] = list(kwargs.get('partner_ids') or []) + [bcc.id]
        super()._send_mail(move, mail_template, **kwargs)
