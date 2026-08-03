import json
import logging
import re

import pytz

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


def _local_8am_utc(env, date_obj):
    """Return 8:00 AM on date_obj in the Odoo user's timezone, as naive UTC for DB storage."""
    from datetime import datetime
    tz_name = env.context.get("tz") or env.user.tz or "UTC"
    try:
        user_tz = pytz.timezone(tz_name)
    except Exception:
        user_tz = pytz.utc
    local_dt = user_tz.localize(datetime(date_obj.year, date_obj.month, date_obj.day, 8, 0))
    return local_dt.astimezone(pytz.utc).replace(tzinfo=None)


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

    x_whatsapp_text = fields.Text(
        string="WhatsApp / Text Message",
        copy=False,
        help="Paste a customer WhatsApp message, SMS, or email here then click 'Generate Invoice Details'.",
    )

    @api.depends("dispatch_estimator_ids")
    def _compute_job_counts(self):
        for rec in self:
            rec.job_count = len(rec.dispatch_estimator_ids)

    def action_generate_all_dispatch_jobs(self):
        """
        Auto-generate a dispatch estimator for every dated product line on this invoice
        by reading the trip sheet PDFs already attached — no re-upload required.

        For each invoice line:
          • Parses the date from the line name (e.g. "Tuesday, May 05, 2026 - ...")
          • Finds the matching trip sheet attachment by that date
          • Extracts each numbered delivery stop (store name + address + box count)
          • Creates a premafirm.rate.estimator with a pickup stop + all delivery stops

        Days whose job_day_ref already has a dispatch job are skipped to avoid duplicates.
        """
        self.ensure_one()
        from ..services.invoice_ai_service import InvoiceAIService
        from datetime import datetime as _dt, date as _date

        svc = InvoiceAIService(self.env)
        attachment_text = svc._collect_attachment_text(self)

        # Full pickup address from rate confirmation
        m_pickup = re.search(
            r"Pickup Address\s+([\d\w\s,./&\-']+?)\s*(?:\n|Pickup Window|First Pickup)",
            attachment_text, re.I,
        )
        pickup_address = (
            re.sub(r"\s+", " ", m_pickup.group(1)).strip()
            if m_pickup else svc._extract_pickup_origin(attachment_text)
        )
        m_window = re.search(r"Pickup Window\s+([^\n]+)", attachment_text, re.I)
        pickup_notes = (
            f"Pickup window: {m_window.group(1).strip()}" if m_window else "Daily pickup"
        )

        # Collect trip sheets: date_str (YYYY-MM-DD) → extracted text
        attachments = self.env["ir.attachment"].search([
            ("res_model", "=", "account.move"), ("res_id", "=", self.id),
        ])
        trip_sheets = {}
        for att in sorted(attachments, key=lambda a: a.id):
            if not (att.name or "").lower().endswith(".pdf"):
                continue
            fb = svc._get_attachment_bytes(att)
            if not fb:
                continue
            text = svc._pdf_extract_text(fb)
            if not text or "Driver Trip Sheet" not in text:
                continue
            dm = re.search(r"Start Date\s+(\d{4}-\d{2}-\d{2})", text)
            if dm:
                trip_sheets[dm.group(1)] = text

        # Stop-line pattern from pdfplumber trip sheet output:
        # "1 ü STORE NAME (CITY #123)  0  0  0  0  0  44"
        stop_line_re = re.compile(r"^\d+\s+\S\s+(.+?)\s+(?:\d+\s+){4,}\d+\s*$")
        date_re = re.compile(r"(\w+),\s+(\w+)\s+(\d{1,2}),\s+(\d{4})", re.I)
        month_map = {m: i + 1 for i, m in enumerate([
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ])}

        product_lines = self.invoice_line_ids.filtered(
            lambda l: l.display_type == "product" and l.product_id
        ).sorted("sequence")
        if not product_lines:
            raise UserError(
                "No product lines found. Run AI Generate first to create dated invoice lines."
            )

        existing_refs = {e.job_day_ref for e in self.dispatch_estimator_ids if e.job_day_ref}
        job_seq = len(self.dispatch_estimator_ids) + 1
        jobs_created = 0
        jobs_skipped = 0

        for line in product_lines:
            name = (line.name or "").strip()
            first_line = name.split("\n")[0]
            m_date = date_re.search(first_line)
            if not m_date:
                continue
            month = month_map.get(m_date.group(2).lower())
            if not month:
                continue
            try:
                day, year = int(m_date.group(3)), int(m_date.group(4))
                job_date = _date(year, month, day)
            except (ValueError, IndexError):
                continue

            date_str = job_date.strftime("%Y-%m-%d")
            job_day_ref = job_date.strftime("%A %B %d %Y")
            is_pickup_only = "pickup only" in first_line.lower()

            if job_day_ref in existing_refs:
                jobs_skipped += 1
                continue

            # Build stops list — pickup origin always first
            stops = []
            if pickup_address:
                stops.append({
                    "type": "pickup", "address": pickup_address,
                    "notes": pickup_notes, "pallets": 0,
                })

            # Delivery stops from matching trip sheet
            trip_text = trip_sheets.get(date_str)
            if trip_text and not is_pickup_only:
                ts_lines = trip_text.split("\n")
                i = 0
                while i < len(ts_lines):
                    tl = ts_lines[i].strip()
                    m_stop = stop_line_re.match(tl)
                    if m_stop:
                        store_name = m_stop.group(1).strip()
                        nums = re.findall(r"\d+", tl)
                        boxes = int(nums[-1]) if nums else 0

                        # Next non-empty line should be the delivery address
                        j = i + 1
                        while j < len(ts_lines) and not ts_lines[j].strip():
                            j += 1
                        address = phone = ""
                        if j < len(ts_lines):
                            cand = ts_lines[j].strip()
                            if re.match(r"^\d+\s+[A-Za-z]", cand) or (
                                "," in cand and re.search(r"\b[A-Z]{2}\b\s*$", cand)
                            ):
                                address = cand
                                i = j
                                # Optional phone number on the line after the address
                                k = i + 1
                                while k < len(ts_lines) and not ts_lines[k].strip():
                                    k += 1
                                if k < len(ts_lines) and re.match(
                                    r"^\d{3}[-.\s]?\d{3}", ts_lines[k].strip()
                                ):
                                    phone = ts_lines[k].strip()
                                    i = k
                        if address:
                            stop_note = store_name + (f" | Tel: {phone}" if phone else "")
                            stops.append({
                                "type": "delivery", "name": store_name,
                                "address": address, "notes": stop_note,
                                "pallets": boxes,
                            })
                    i += 1

            scheduled_at = _dt(year, month, day, 8, 0, 0)

            # Derive stop type from the invoice line description
            first_lower = first_line.lower()
            if "pickup only" in first_lower:
                job_stop_type = "pickup"
            elif "return" in first_lower and "deliver" not in first_lower:
                job_stop_type = "return"
            elif "deliver" in first_lower or "drop" in first_lower:
                job_stop_type = "dropoff"
            else:
                job_stop_type = "other"

            estimator = self.env["premafirm.rate.estimator"].sudo().create({
                "invoice_id": self.id,
                "job_sequence": job_seq,
                "job_day_ref": job_day_ref,
                "job_stop_type": job_stop_type,
                "scheduled_at": scheduled_at,
                "notes": f"Invoice: {self.ref or self.name or ''}\n{name}",
            })
            if stops:
                estimator._json_to_stops(stops)

            jobs_created += 1
            job_seq += 1

        if jobs_created == 0:
            skip_note = f" ({jobs_skipped} already exist)." if jobs_skipped else "."
            raise UserError(
                f"No new dispatch jobs were created{skip_note} "
                "Make sure the invoice has AI-generated product lines with dates."
            )

        msg = f"{jobs_created} dispatch job(s) generated from invoice lines and trip sheets"
        if jobs_skipped:
            msg += f" ({jobs_skipped} already existed, skipped)"
        self.message_post(
            body=f"<b>Generated {jobs_created} dispatch job(s)</b> from invoice lines and attached trip sheets."
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Dispatch Jobs Generated",
                "message": msg + ".",
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    def action_add_dispatch_job(self):
        """Open the dispatch wizard to add a new job to this invoice."""
        self.ensure_one()
        # Pre-populate wizard notes from invoice data so AI can extract stops without uploading files
        context_parts = []
        if self.x_whatsapp_text and self.x_whatsapp_text.strip():
            context_parts.append(self.x_whatsapp_text.strip())
        if self.ref:
            context_parts.append(f"Invoice Reference: {self.ref}")
        first_line = self.invoice_line_ids.filtered(
            lambda l: l.display_type == "product" and l.name
        )[:1]
        if first_line and first_line.name:
            context_parts.append(first_line.name)
        default_notes = "\n\n".join(context_parts) or False
        ctx = {
            "default_invoice_id": self.id,
            "default_job_sequence": len(self.dispatch_estimator_ids) + 1,
        }
        if default_notes:
            ctx["default_notes"] = default_notes
        return {
            "type": "ir.actions.act_window",
            "name": "Add Dispatch Job",
            "res_model": "premafirm.dispatch.wizard",
            "view_mode": "form",
            "target": "new",
            "context": ctx,
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
        # ── Studio Reference sync ─────────────────────────────────────────────
        # Keep the Studio custom "Reference" field (x_studio_reference) in sync
        # with the standard Odoo ref field so both the form and printed invoice
        # stay consistent.
        #
        # The sync is done in a SEPARATE write call with tracking_disable=True
        # to avoid duplicate chatter messages (one for ref, one for x_studio_reference).
        # A recursion guard (_skip_studio_ref_sync) prevents infinite loops.
        if self.env.context.get("_skip_studio_ref_sync"):
            return super().write(vals)

        if "x_studio_reference" in vals and "ref" not in vals:
            # x_studio_reference changed by user → mirror to ref so accounting stays consistent
            vals = dict(vals, ref=vals["x_studio_reference"] or False)

        # ── ML correction learning ────────────────────────────────────────────
        # When staff edits the reference after AI generation, teach the ML entry
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

        # ── Primary write ─────────────────────────────────────────────────────
        result = super().write(vals)

        # Silently sync x_studio_reference if ref changed (no tracking = no duplicate message)
        if "ref" in vals and "x_studio_reference" not in vals:
            self.with_context(
                _skip_studio_ref_sync=True,
                tracking_disable=True,
                mail_notrack=True,
            ).write({"x_studio_reference": vals["ref"] or False})

        return result

    def action_ai_extract_reference(self):
        """Extract reference numbers — from attachments if present, otherwise from pasted text."""
        self.ensure_one()
        from ..services.invoice_ai_service import InvoiceAIService

        service = InvoiceAIService(self.env)
        reference = None
        source = "attachments"

        has_attachments = bool(self.env["ir.attachment"].sudo().search([
            ("res_model", "=", "account.move"),
            ("res_id", "=", self.id),
            ("type", "=", "binary"),
        ], limit=1))

        if has_attachments:
            try:
                reference = service.extract_reference_only(self)
            except ValueError as e:
                raise UserError(str(e))
            except Exception as e:
                _logger.exception("Reference extraction failed for %s", self.name)
                raise UserError(f"Reference extraction failed: {type(e).__name__}: {e}")

        elif self.x_whatsapp_text and self.x_whatsapp_text.strip():
            # No attachments — run full generation from text (fills description + reference)
            return self.action_generate_from_whatsapp()

        else:
            raise UserError(
                "No attachments found and no text pasted.\n"
                "Either attach a document or paste a WhatsApp message in the 'Generate from Text' tab."
            )

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

        self.sudo().with_context(skip_invoice_sync=True).write({
            "ref": reference,
            "x_studio_reference": reference,
        })
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Reference Extracted",
                "message": "Reference field updated from attachments.",
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

        # Tax: clear if the document never mentioned any tax
        tax_vals = [(5, 0, 0)] if not result.get("tax_mentioned", True) else None

        # UoM: resolve hint ("loads" / "pallets") to an Odoo uom.uom record
        uom_id = self._resolve_uom_id(result.get("uom", "loads"))

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
            self._apply_ai_schedule_lines_draft(product, line_items, tax_vals=tax_vals, uom_id=uom_id)
        elif is_posted:
            self._apply_ai_lines_posted(existing_product_lines, product, description)
        else:
            self._apply_ai_lines_draft(
                existing_product_lines, product, description, amount,
                tax_vals=tax_vals, uom_id=uom_id,
            )

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

    def action_generate_from_whatsapp(self):
        """Generate invoice reference + description from a pasted WhatsApp/text message."""
        self.ensure_one()
        if not self.x_whatsapp_text or not self.x_whatsapp_text.strip():
            raise UserError("Paste a WhatsApp message or text into the field first.")

        from ..services.invoice_ai_service import InvoiceAIService

        try:
            service = InvoiceAIService(self.env)
            past_context = service._build_past_invoices_context(self)
            result = service.analyze_from_text(self, self.x_whatsapp_text, past_context)
        except ValueError as e:
            raise UserError(str(e))
        except Exception as e:
            _logger.exception("WhatsApp text AI generation failed for %s", self.name)
            raise UserError(f"AI generation failed: {type(e).__name__}: {e}")

        if not result:
            raise UserError("AI returned no usable result. Please check the text and try again.")

        ml_record = service.save_to_ml(self, result)
        if ml_record:
            self.sudo().write({"premafirm_ml_knowledge_id": ml_record.id})

        reference = result.get("reference") or ""
        description = result.get("description") or ""
        product_id_val = result.get("product_id")
        amount_val = result.get("amount")
        confidence = result.get("confidence", "unknown")
        is_posted = self.state == "posted"

        amount = None
        if amount_val not in (None, ""):
            try:
                amount = float(str(amount_val).replace(",", "").replace("$", "").strip())
            except (TypeError, ValueError):
                amount = None

        tax_vals = [(5, 0, 0)] if not result.get("tax_mentioned", True) else None
        uom_id = self._resolve_uom_id(result.get("uom", "loads"))

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

        if is_posted:
            self._apply_ai_lines_posted(existing_product_lines, product, description)
        else:
            self._apply_ai_lines_draft(
                existing_product_lines, product, description, amount,
                tax_vals=tax_vals, uom_id=uom_id,
            )

        self._generate_ai_summary(result)

        if reference and description:
            msg = f"Reference ({reference}) and description filled in ({confidence} confidence)."
            notif_type = "success"
        elif reference:
            msg = f"Reference filled in: {reference} ({confidence} confidence)."
            notif_type = "success"
        elif description:
            msg = f"Description filled in ({confidence} confidence). No reference generated."
            notif_type = "warning"
        else:
            msg = "AI returned no usable data. Please check the text and try again."
            notif_type = "warning"

        # Auto-create a dispatch job with stops if the AI extracted them from the text.
        # Re-running AI Generate on the same invoice/text must not pile up duplicate
        # estimators — if one already has the same stop addresses, update it in place.
        stops_data = result.get("stops") or []
        scheduled_date_str = result.get("scheduled_date")
        if len(stops_data) >= 2:
            try:
                from datetime import date as _date, datetime as _dt
                sched_date = None
                if scheduled_date_str:
                    try:
                        sched_date = _date.fromisoformat(str(scheduled_date_str))
                    except Exception:
                        pass
                if not sched_date:
                    sched_date = _date.today()

                new_addresses = {
                    (s.get("address") or "").strip().lower()
                    for s in stops_data if s.get("address")
                }
                existing = None
                for est in self.dispatch_estimator_ids:
                    est_addresses = {
                        (a or "").strip().lower()
                        for a in est.stop_ids.mapped("address") if a
                    }
                    if est_addresses and est_addresses == new_addresses:
                        existing = est
                        break

                if existing:
                    existing._json_to_stops(stops_data)
                    existing.write({"notes": self.x_whatsapp_text or existing.notes})
                    msg += (
                        f" Same stops as existing dispatch job #{existing.job_sequence} — "
                        f"updated it instead of creating a duplicate."
                    )
                else:
                    job_sequence = len(self.dispatch_estimator_ids) + 1
                    job_day_ref = sched_date.strftime("%A %B %d %Y")
                    estimator = self.env["premafirm.rate.estimator"].sudo().create({
                        "invoice_id": self.id,
                        "job_sequence": job_sequence,
                        "job_day_ref": job_day_ref,
                        "scheduled_at": _local_8am_utc(self.env, sched_date),
                        "notes": self.x_whatsapp_text or "",
                    })
                    estimator._json_to_stops(stops_data)
                    msg += f" Dispatch job #{job_sequence} created with {len(stops_data)} stop(s) — review in the Dispatch Jobs tab."
            except Exception as exc:
                _logger.warning("Auto-dispatch job creation from text failed: %s", exc)

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "AI Generate Complete",
                "message": msg,
                "type": notif_type,
                "sticky": notif_type == "warning",
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
        from ..services.deepseek_utils import deepseek_chat, get_api_key as _get_deepseek_key, get_model as _get_deepseek_model
        ICP = self.env["ir.config_parameter"].sudo()
        api_key = _get_deepseek_key(self.env)
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
            model = _get_deepseek_model(self.env)
            summary = deepseek_chat(
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

    def action_delete_ai_chatter_notes(self):
        """Remove AI-generated reference tracking notes from the chatter.

        These messages are typically created automatically when the AI writes the
        'ref' field and Odoo's field tracking logs the change.  Regular users cannot
        delete mail.message records, so this sudo action is provided as a one-click
        clean-up button.
        """
        self.ensure_one()
        # Target: log notes / tracking messages on this invoice that look like
        # auto-posted reference strings (contain PS-, BOL-, PO-, or "None" prefix)
        # but are NOT the rich "AI Generate" notes (those have HTML tags).
        all_messages = self.env["mail.message"].sudo().search([
            ("model", "=", "account.move"),
            ("res_id", "=", self.id),
            ("message_type", "in", ["notification", "comment", "email"]),
        ])
        ref_pattern = re.compile(
            r"(NonePS-|NoneBO|NonePO|NoneDE|NoneRE|PS-50|BOL-|PO-\d|REF-\d|\(Reference\))",
            re.I,
        )
        to_delete = all_messages.filtered(
            lambda m: (
                ref_pattern.search(m.body or "")
                and "<b>AI Generate</b>" not in (m.body or "")
                and "<table" not in (m.body or "")
            )
        )
        count = len(to_delete)
        if to_delete:
            to_delete.unlink()
            _logger.info("Deleted %d AI reference chatter note(s) from invoice %s", count, self.name)

        msg = f"Removed {count} AI reference note(s) from the chatter." if count else "No matching notes found."
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Chatter Cleaned",
                "message": msg,
                "type": "success" if count else "warning",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    def _resolve_uom_id(self, uom_hint):
        """Resolve a UoM hint string ('loads', 'pallets') to an Odoo uom.uom record id."""
        name_map = {"loads": "Load(s)", "pallets": "Pallet(s)"}
        name = name_map.get((uom_hint or "").lower())
        if not name:
            return False
        uom = self.env["uom.uom"].search([("name", "=", name)], limit=1)
        return uom.id if uom else False

    def _apply_ai_lines_draft(
        self, existing_product_lines, product, description, amount=None,
        *, tax_vals=None, uom_id=False,
    ):
        """Apply AI product + note lines on a draft invoice (no restrictions)."""
        if not existing_product_lines and product:
            line_vals = {
                "product_id": product.id,
                "name": "",
                "quantity": 1,
                "price_unit": amount or 0,
                "display_type": "product",
            }
            if tax_vals is not None:
                line_vals["tax_ids"] = tax_vals
            if uom_id:
                line_vals["product_uom_id"] = uom_id
            vals_list = [line_vals]
            if description:
                vals_list.append({"display_type": "line_note", "name": description})
            self.invoice_line_ids = [(0, 0, v) for v in vals_list]
        else:
            if existing_product_lines:
                primary_line = existing_product_lines.sorted("sequence")[0]
                update = {}
                if amount is not None and not primary_line.price_unit:
                    update["price_unit"] = amount
                if tax_vals is not None:
                    update["tax_ids"] = tax_vals
                if uom_id:
                    update["product_uom_id"] = uom_id
                if update:
                    primary_line.write(update)
            if description:
                self._upsert_note_line(existing_product_lines, description)

    def _apply_ai_schedule_lines_draft(self, product, line_items, *, tax_vals=None, uom_id=False):
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
            line_vals = {
                "product_id": product.id,
                "name": name,
                "quantity": 1,
                "price_unit": amount,
                "display_type": "product",
            }
            if tax_vals is not None:
                line_vals["tax_ids"] = tax_vals
            if uom_id:
                line_vals["product_uom_id"] = uom_id
            vals_list.append(line_vals)
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
