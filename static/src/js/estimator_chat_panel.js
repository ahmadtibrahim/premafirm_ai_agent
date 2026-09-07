/** @odoo-module **/
import { Component, useState, useRef, onWillStart, markup } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

/**
 * Estimator panel — ONE customer message → ONE consolidated response
 * (master §9-13):
 *   • unified itinerary (reposition / numbered Pickup-Delivery / return)
 *   • three scenario cards (dedicated one-way, round trip, scheduled
 *     corridor) with availability/capacity/ELD blockers and alternatives
 *   • customer pricing intel (their own booking history, isolated)
 *   • load pairing + corridor context
 *   • route-development week view (button, read-only)
 * Nothing here sends, confirms or books — the only mutating action is
 * "Create Draft Rate Confirmation", which drafts UNPRICED on an explicit
 * human click (server side reuses the CRM bridge).
 */
class EstimatorChatPanel extends Component {
    static template = "premafirm_ai_engine.EstimatorChatPanel";

    setup() {
        this.orm          = useService("orm");
        this.notification = useService("notification");
        this.action       = useService("action");
        this.fileInputRef = useRef("fileInput");

        this.state = useState({
            panelOpen:   false,
            loading:     false,
            chatText:    "",
            files:       [],       // [{name, b64, mimetype, size}]
            vehicles:    [],
            vehicleId:   null,
            vehicleReefer: false,
            customers:   [],
            customerId:  0,
            customerTerm:"",
            customerBusy:false,
            pickupDate:  "",
            returnHome:  true,
            avoidTolls:  true,
            allowUSA:    false,
            result:      null,     // API response JSON
            resultHtml:  "",
            error:       null,
            dragOver:    false,
            routeDev:    null,     // API response JSON
            routeDevBusy:false,
        });

        onWillStart(async () => {
            await this._loadVehicles();
        });
    }

    // ── Panel control ─────────────────────────────────────────────

    togglePanel() {
        this.state.panelOpen = !this.state.panelOpen;
    }

    resetPanel() {
        Object.assign(this.state, {
            chatText: "", files: [], result: null, resultHtml: "",
            error: null, routeDev: null, pickupDate: "",
            returnHome: true,
        });
    }

    // ── Data loading ───────────────────────────────────────────────

    async _loadVehicles() {
        try {
            const vehicles = await this.orm.searchRead(
                "fleet.vehicle",
                [["active", "=", true]],
                ["id", "name", "x_reefer", "x_operational_logistics"],
                { limit: 80, order: "name asc" }
            );
            this.state.vehicles = vehicles;
        } catch (_) {
            // vehicles simply stay empty on failure
        }
    }

    async onCustomerInput(ev) {
        const term = ev.target.value;
        this.state.customerTerm = term;
        if (term.trim().length < 2) {
            this.state.customers = [];
            return;
        }
        if (this._customerTimer) {
            clearTimeout(this._customerTimer);
        }
        this._customerTimer = setTimeout(() => this._searchCustomers(term), 350);
    }

    async _searchCustomers(term) {
        this.state.customerBusy = true;
        try {
            const partners = await this.orm.searchRead(
                "res.partner",
                [["name", "ilike", term]],
                ["id", "name", "email"],
                { limit: 12, order: "name asc" }
            );
            this.state.customers = partners;
        } catch (_) {
            this.state.customers = [];
        } finally {
            this.state.customerBusy = false;
        }
    }

    onCustomerSelect(ev) {
        const id = parseInt(ev.target.value, 10);
        this.state.customerId = isNaN(id) ? 0 : id;
    }

    onVehicleChange(ev) {
        const val = parseInt(ev.target.value, 10);
        this.state.vehicleId = isNaN(val) ? null : val;
        const vehicle = this.state.vehicles.find(v => v.id === this.state.vehicleId);
        this.state.vehicleReefer = Boolean(vehicle && vehicle.x_reefer);
    }

    onChatInput(ev) {
        this.state.chatText = ev.target.value;
    }

    onPickupDateChange(ev) {
        this.state.pickupDate = ev.target.value || "";
    }

    onReturnHomeChange(ev) {
        this.state.returnHome = ev.target.checked;
    }

    onAvoidTollsChange(ev) {
        this.state.avoidTolls = ev.target.checked;
    }

    onAllowUSAChange(ev) {
        this.state.allowUSA = ev.target.checked;
    }

    // ── File handling ──────────────────────────────────────────────

    onDropzoneClick() {
        this.fileInputRef.el && this.fileInputRef.el.click();
    }

    onDragOver(ev) {
        ev.preventDefault();
        this.state.dragOver = true;
    }

    onDragLeave() {
        this.state.dragOver = false;
    }

    onFilesDrop(ev) {
        ev.preventDefault();
        this.state.dragOver = false;
        this._addFiles(ev.dataTransfer.files);
    }

    onFilesChange(ev) {
        this._addFiles(ev.target.files);
        ev.target.value = "";
    }

    onRemoveFileClick(ev) {
        const idx = parseInt(ev.currentTarget.dataset.index, 10);
        this.state.files = this.state.files.filter((_, i) => i !== idx);
    }

    _addFiles(fileList) {
        const allowed = ["application/pdf", "image/jpeg", "image/png", "image/webp"];
        for (const file of fileList) {
            if (!allowed.includes(file.type)) {
                this.notification.add(
                    `${file.name}: only PDF and images (JPG, PNG, WebP) are supported`,
                    { type: "warning" }
                );
                continue;
            }
            const reader = new FileReader();
            reader.onload = (e) => {
                const b64 = e.target.result.split(",")[1];
                this.state.files = [
                    ...this.state.files,
                    { name: file.name, b64, mimetype: file.type, size: file.size },
                ];
            };
            reader.readAsDataURL(file);
        }
    }

    formatFileSize(bytes) {
        if (!bytes) return "";
        if (bytes < 1024) return `${bytes} B`;
        if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
        return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    }

    // ── Submit ─────────────────────────────────────────────────────

    async onSubmit() {
        const st = this.state;
        if (!st.vehicleId) {
            this.notification.add("Please select a truck first", { type: "warning" });
            return;
        }
        if (!st.chatText.trim() && st.files.length === 0) {
            this.notification.add("Add the customer message or an attachment (PDF/image)", { type: "warning" });
            return;
        }

        st.loading = true;
        st.result = null;
        st.resultHtml = "";
        st.error = null;
        st.routeDev = null;

        try {
            const payload = {
                vehicle_id:         st.vehicleId,
                text_message:       st.chatText.trim(),
                files:              st.files.map(f => ({ file_b64: f.b64, mimetype: f.mimetype, filename: f.name })),
                partner_id:         st.customerId,
                avoid_tolls:        st.avoidTolls,
                allow_cross_border: st.allowUSA,
                scheduled_at:       st.pickupDate || null,
                return_to_home:     st.returnHome,
            };
            const result = await this.orm.call(
                "premafirm.estimator.scenario.request",
                "estimate_scenarios_rpc",
                [],
                payload
            );
            if (result && result.error) {
                st.error = result.error;
            } else {
                st.result = result;
                st.resultHtml = this._buildResultHtml(result);
            }
        } catch (err) {
            st.error = err.data?.message || err.message || "An unexpected error occurred";
        } finally {
            st.loading = false;
        }
    }

    // ── Result rendering (server data → HTML, escaped) ─────────────

    _esc(v) {
        return String(v ?? "").replace(/[&<>"']/g, c => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
        }[c]));
    }

    _money(v, digits = 2) {
        if (v === false || v === null || v === undefined) return "—";
        const n = Number(v);
        if (isNaN(n)) return "—";
        return "$" + n.toLocaleString("en-CA", {
            minimumFractionDigits: digits, maximumFractionDigits: digits,
        });
    }

    _num(v, digits = 1) {
        if (v === false || v === null || v === undefined) return "—";
        const n = Number(v);
        if (isNaN(n)) return "—";
        return n.toLocaleString("en-CA", {
            minimumFractionDigits: 0, maximumFractionDigits: digits,
        });
    }

    _kv(label, value, cls = "") {
        const val = (value === "" || value === undefined || value === null) ? "—" : value;
        return `<div class="o_est_kv ${cls}"><span class="o_est_kv_l">${this._esc(label)}</span><span class="o_est_kv_v">${val}</span></div>`;
    }

    _chips(items, kind) {
        if (!items || !items.length) return "";
        return `<div class="o_est_chips">${items.map(i =>
            `<span class="o_est_chip chip-${kind}">${this._esc(i)}</span>`
        ).join("")}</div>`;
    }

    _buildResultHtml(res) {
        const h = [];
        const route = res.route || {};
        const warnings = res.warnings || [];

        h.push(`<div class="o_est_req_head">`);
        if (res.dispatch_online === false) {
            h.push(`<div class="o_est_banner banner-offline">Dispatch integration is offline — scenario cards, availability and pricing intel are unavailable on this database.</div>`);
        }
        if (res.message) {
            h.push(`<div class="o_est_banner banner-info">${this._esc(res.message)}</div>`);
        }
        if (res.state === "error") {
            h.push(`<div class="o_est_banner banner-error">Computation stopped: the request could not be completed.</div>`);
        }
        if (warnings.length) {
            h.push(this._chips(warnings, "warn"));
        }
        h.push(`</div>`);

        // 1. Unified itinerary
        const rows = route.itinerary || [];
        h.push(`<div class="o_est_section"><div class="o_est_sec_title">🚛 Itinerary
            <span class="o_est_sec_meta">${this._num(route.distance_km)} km · ${this._num(route.duration_hrs)} h driving${route.reposition_to_first_km ? ` · reposition ${this._num(route.reposition_to_first_km)} km` : ""}${route.return_leg_km ? ` · return ${this._num(route.return_leg_km)} km` : ""}</span>
            </div>`);
        if (rows.length) {
            h.push(`<table class="o_est_table"><thead><tr>
                <th>#</th><th>Stop</th><th>Company / address</th><th>Qty</th><th>Segment</th><th>Drive</th></tr></thead><tbody>`);
            for (const row of rows) {
                const icon = row.row === "reposition" ? "↩" :
                    row.row === "return" ? "🏠" :
                    row.row === "pickup" ? "📦" : "📍";
                const seg = row.segment_km === false || row.segment_km === null
                    ? "—" : `${this._num(row.segment_km)} km`;
                const hrs = row.segment_hrs === false || row.segment_hrs === null
                    ? "—" : `${this._num(row.segment_hrs)} h`;
                h.push(`<tr class="o_est_row-${this._esc(row.row)}">
                    <td class="o_est_tc">${row.seq || "·"}</td>
                    <td class="o_est_tc">${icon} ${this._esc(row.label)}</td>
                    <td><b>${this._esc(row.name)}</b>${row.address ? `<div class="o_est_sub">${this._esc(row.address)}${row.fsa ? ` · <b>${this._esc(row.fsa)}</b>` : ""}</div>` : `<div class="o_est_sub muted">${this._esc(row.name)}</div>`}</td>
                    <td class="o_est_tc">${this._esc(row.qty) || "—"}</td>
                    <td class="o_est_tc">${seg}</td><td class="o_est_tc">${hrs}</td></tr>`);
            }
            h.push(`</tbody></table>`);
        } else {
            h.push(`<div class="o_est_empty">No routable itinerary (missing coordinates).</div>`);
        }
        h.push(`</div>`);

        // 2. Scenario cards
        const scenarios = res.scenarios || [];
        if (scenarios.length) {
            h.push(`<div class="o_est_section"><div class="o_est_sec_title">🔄 Three ways to move it</div>`);
            for (const card of scenarios) {
                const badge = card.badge || (card.feasible ? "feasible" : "infeasible");
                h.push(`<div class="o_est_card card-${this._esc(badge)}">
                    <div class="o_est_card_head">
                        <span class="o_est_card_title">${this._esc(card.title)}</span>
                        <span class="o_est_badge badge-${this._esc(badge)}">${this._esc(badge).replace("_", " ")}</span>
                    </div>
                    <div class="o_est_card_body">`);
                if (card.truck) h.push(this._kv("Truck", this._esc(card.truck)));
                h.push(this._kv("Pickup", card.pickup_date || "—", "kvm"));
                if (card.delivery_date || card.return_date) {
                    h.push(this._kv("Delivery", card.delivery_date || "—", "kvm"));
                }
                if (card.return_date) h.push(this._kv("Return home", card.return_date, "kvm"));
                h.push(this._kv("Distance", card.distance_km === false ? "—" : `${this._num(card.distance_km)} km`, "kvm"));
                if (card.drive_hrs !== false) h.push(this._kv("Drive + service", card.drive_hrs === false ? "—" : `${this._num(card.drive_hrs)} h`, "kvm"));
                if (card.cost !== false) {
                    h.push(this._kv("Operating cost", this._money(card.cost), "kvm"));
                    h.push(this._kv("Suggested sell", `<b>${this._money(card.suggested_sell)}</b>`));
                    h.push(this._kv("Markup on cost", `${this._num(card.markup_pct_on_cost, 0)}%`, "kvm"));
                    if (card.gross_margin_pct !== false) {
                        h.push(this._kv("Gross margin (on revenue)", `${this._num(card.gross_margin_pct, 0)}%`, "kvm"));
                    }
                    h.push(this._kv("Margin $", this._money(card.profit), "kvm"));
                } else {
                    h.push(this._kv("Sell", card.suggested_sell === false ? "—" : this._money(card.suggested_sell)));
                    if (card.key === "scheduled_ltl") {
                        h.push(this._kv("Cost", "List price — assigned at booking", "kvm"));
                    }
                }
                if (card.incremental_km) h.push(this._kv("Empty mileage", `${this._num(card.incremental_km, 0)} km`, "kvm"));
                const avail = card.availability || {};
                if (card.key !== "scheduled_ltl") {
                    const status = avail.status === "free" ? `<span class="o_est_ok">free ✓</span>` :
                        avail.status === "conflicts" ? `<span class="o_est_bad">conflicts</span>` : "—";
                    h.push(this._kv("Truck availability", status));
                    if (avail.conflicts && avail.conflicts.length) {
                        h.push(`<div class="o_est_conflicts">`);
                        for (const c of avail.conflicts) {
                            h.push(`<div class="o_est_conflict">⚠ ${this._esc(c.kind === "job" ? `Job ${this._esc(c.label)}` : `Planned ${this._esc(c.label)}`)} — ${this._esc(c.start || "")}${c.end ? ` → ${this._esc(c.end)}` : ""}${c.pallets ? ` · ${this._esc(c.pallets)} plt` : ""}${c.stage ? ` · ${this._esc(c.stage)}` : ""}</div>`);
                        }
                        h.push(`</div>`);
                    }
                    if (avail.next_free_date) {
                        h.push(this._kv("Earliest free", this._esc(avail.next_free_date), "kvm"));
                    }
                }
                if (card.key === "scheduled_ltl") {
                    h.push(this._kv("Network", `${this._esc(card.corridor || "")}${card.suggested_sell === false ? "" : ""}`));
                }
                if (card.blocking && card.blocking.length) {
                    h.push(this._chips(card.blocking, "block"));
                }
                if (card.warnings && card.warnings.length) {
                    h.push(this._chips(card.warnings, "warn"));
                }
                if (card.alternatives && card.alternatives.length) {
                    h.push(`<div class="o_est_alt_title">Alternative corridor dates</div>`);
                    h.push(`<table class="o_est_table"><thead><tr><th>Date</th><th>Delivered</th><th>Price</th><th>Corridor</th><th>Free</th></tr></thead><tbody>`);
                    for (const alt of card.alternatives) {
                        h.push(`<tr><td>${this._esc(alt.date || "—")}</td><td>${this._esc(alt.delivery_date || "—")}</td><td>${this._money(alt.price)}</td><td>${this._esc(alt.corridor || "")}</td><td>${alt.free_pallets === false ? "—" : this._esc(alt.free_pallets)}</td></tr>`);
                    }
                    h.push(`</tbody></table>`);
                }
                if (card.assumptions && card.assumptions.length) {
                    h.push(`<div class="o_est_note">${card.assumptions.map(a => "· " + this._esc(a)).join("<br/>")}</div>`);
                }
                h.push(`</div></div>`);
            }
            h.push(`</div>`);
        }

        // 3. Customer pricing intel
        const intel = res.intel || {};
        if (intel.message) {
            h.push(`<div class="o_est_section"><div class="o_est_sec_title">📊 Customer pricing intel</div>
                <div class="o_est_empty">${this._esc(intel.message)}</div></div>`);
        } else if (intel.scope === "customer") {
            h.push(`<div class="o_est_section"><div class="o_est_sec_title">📊 Customer pricing intel · ${this._esc(intel.partner_name || "")} <span class="o_est_sec_meta">${this._esc(intel.request_region_pair || "")}</span></div>`);
            const stats = intel.stats || {};
            h.push(`<div class="o_est_kv"><span class="o_est_kv_l">History</span><span class="o_est_kv_v">${this._esc(stats.n || 0)} booking(s)${stats.n_overridden ? `, ${stats.n_overridden} manually overridden` : ""} · ${stats.verdict === "thin" ? "thin history — indicative only" : "adequate sample"}</span></div>`);
            if (stats.median_price !== false) {
                h.push(this._kv("Median price", `${this._money(stats.median_price)} (band ${this._money(stats.band_low)} – ${this._money(stats.band_high)})`));
                if (stats.median_rate_per_km !== false) h.push(this._kv("Median $/km", this._money(stats.median_rate_per_km, 3), "kvm"));
                if (stats.price_pct_over_60d !== false) h.push(this._kv("60-day trend", `${stats.price_pct_over_60d > 0 ? "▲" : "▼"} ${this._num(Math.abs(stats.price_pct_over_60d), 0)}% vs older`, "kvm"));
            }
            for (const flag of intel.flags || []) {
                h.push(`<div class="o_est_note note-${this._esc(flag.level)}">${this._esc(flag.text)}</div>`);
            }
            const rowsC = intel.comparables || [];
            if (rowsC.length) {
                h.push(`<table class="o_est_table"><thead><tr><th>Ref</th><th>State</th><th>Price</th><th>$</th><th>$/km</th><th>Pallet</th><th>Age</th><th>Override</th></tr></thead><tbody>`);
                for (const c of rowsC) {
                    h.push(`<tr><td>${this._esc(c.reference)}</td><td>${this._esc(c.state)}</td><td>${this._money(c.calculated_price)}</td><td>${this._esc(c.confirmed_at ? c.confirmed_at.slice(0, 10) : "—")}</td><td>${c.rate_per_km ? this._money(c.rate_per_km, 3) : "—"}</td><td>${this._esc(c.pallets || 0)}</td><td>${this._esc(c.age_days || 0)}d</td><td>${c.manual_override ? "⚠ manual" : ""}</td></tr>`);
                }
                h.push(`</tbody></table>`);
            }
            h.push(`</div>`);
        }

        // 4. Load pairing
        const pairing = res.pairing || {};
        if (pairing.message) {
            h.push(`<div class="o_est_section"><div class="o_est_sec_title">🔗 Load pairing</div>
                <div class="o_est_empty">${this._esc(pairing.message)}</div></div>`);
        } else if (pairing.scope) {
            h.push(`<div class="o_est_section"><div class="o_est_sec_title">🔗 Load pairing <span class="o_est_sec_meta">window ${this._esc(pairing.window_start || "")} → ${this._esc(pairing.window_end || "")} · region pair only, verify routing at dispatch</span></div>`);
            const back = pairing.backhauls || [];
            const cons = pairing.consolidations || [];
            const ctxRows = pairing.corridor_context || [];
            if (!back.length && !cons.length && !ctxRows.length) {
                h.push(`<div class="o_est_empty">No combinable freight found in the corridor window.</div>`);
            }
            const pairTable = (rows, kind) => rows.length ? `<table class="o_est_table"><thead><tr><th>Type</th><th>Booking</th><th>State</th><th>Customer</th><th>Qty</th><th>Pickup</th><th>Est. price</th></tr></thead><tbody>` + rows.map(r => `<tr><td>${this._esc(r.kind || kind)}</td><td>${this._esc(r.reference)}</td><td>${this._esc(r.state)}</td><td>${this._esc(r.partner)}</td><td>${this._esc(r.pallets || 0)} plt</td><td>${this._esc(r.pickup_date || "—")}</td><td>${this._money(r.estimated_price)}</td></tr>`).join("") + `</tbody></table>` : "";
            h.push(pairTable(back, "backhaul"));
            h.push(pairTable(cons, "consolidation"));
            if (ctxRows.length) {
                h.push(`<div class="o_est_alt_title">Corridor commitments (context)</div>`);
                h.push(`<table class="o_est_table"><thead><tr><th>Date</th><th>Type</th><th>Label</th><th>Vehicle</th><th>Pallets</th></tr></thead><tbody>` + ctxRows.map(r => `<tr><td>${this._esc(r.date || "—")}</td><td>${this._esc(r.kind)}</td><td>${this._esc(r.label)}</td><td>${this._esc(r.vehicle || "")}</td><td>${this._esc(r.pallets ?? "")}</td></tr>`).join("") + `</tbody></table>`);
            }
            h.push(`</div>`);
        }

        // 5. Route development
        if (this.state.routeDev) {
            const rd = this.state.routeDev;
            h.push(`<div class="o_est_section"><div class="o_est_sec_title">🗓 Route development <span class="o_est_sec_meta">week ${this._esc(rd.week_start || "")} → ${this._esc(rd.week_end || "")}</span></div>`);
            if (rd.error) {
                h.push(`<div class="o_est_banner banner-error">${this._esc(rd.message || rd.error)}</div>`);
            } else {
                const mkTable = (rows, title, cols) => rows.length
                    ? `<div class="o_est_alt_title">${this._esc(title)}</div><table class="o_est_table"><thead><tr>${cols.map(c => `<th>${this._esc(c)}</th>`).join("")}</tr></thead><tbody>` + rows.map(r => `<tr>${cols.map(c => `<td>${this._esc(r[c] ?? "—")}</td>`).join("")}</tr>`).join("") + `</tbody></table>`
                    : `<div class="o_est_empty">${this._esc(title)} — none scheduled this week.</div>`;
                h.push(mkTable(rd.truck_rows || [], `Truck schedule — ${this._esc(rd.vehicle_name || "truck")}`, ["date", "time", "corridor", "peak_pallets", "free_pallets"]));
                h.push(mkTable(rd.pair_rows || [], "Corridor runs serving this region pair", ["date", "time", "corridor", "peak_pallets", "free_pallets"]));
                if ((rd.gaps || []).length) {
                    h.push(`<div class="o_est_alt_title">Development gaps (needs a dispatcher to schedule)</div>`);
                    h.push(`<div class="o_est_chips">` + rd.gaps.map(g => `<span class="o_est_chip chip-block">${this._esc(g.from_city + " (" + g.from_region + ") → " + g.to_city + " (" + g.to_region + ") · " + g.corridor)}</span>`).join("") + `</div>`);
                }
                if ((rd.city_context || []).length) {
                    const cities = rd.city_context.map(c => `${this._esc(c.main_city || c.region_name)} (${this._esc(c.region_code)})`).join(" · ");
                    h.push(`<div class="o_est_note">Regions: ${cities}</div>`);
                }
            }
            h.push(`</div>`);
        }

        return h.join("");
    }

    // ── Footer actions ─────────────────────────────────────────────

    async openRecord() {
        if (!this.state.result?.request_id) return;
        await this.action.doAction({
            type:      "ir.actions.act_window",
            res_model: "premafirm.estimator.scenario.request",
            res_id:    this.state.result.request_id,
            views:     [[false, "form"]],
            target:    "current",
        });
        this.state.panelOpen = false;
    }

    async onRouteDev() {
        const st = this.state;
        if (!st.result?.request_id || st.routeDevBusy) return;
        st.routeDevBusy = true;
        try {
            const result = await this.orm.call(
                "premafirm.estimator.scenario.request",
                "route_development_rpc",
                [],
                { request_id: st.result.request_id }
            );
            st.routeDev = result;
            st.resultHtml = this._buildResultHtml(st.result);  // re-render w/ section
        } catch (err) {
            this.notification.add(err.data?.message || err.message || "Route development failed", { type: "danger" });
        } finally {
            st.routeDevBusy = false;
        }
    }

    async onCreateDraftRC() {
        const st = this.state;
        if (!st.result?.request_id) return;
        const result = await this.orm.call(
            "premafirm.estimator.scenario.request",
            "action_rate_confirmation_rpc",
            [],
            { request_id: st.result.request_id }
        );
        if (result?.action) {
            this.notification.add(result.message || "Draft created", { type: "success" });
            await this.action.doAction(result.action);
            return;
        }
        this.notification.add(result?.message || result?.error || "Could not create the draft", { type: "warning" });
    }

    // ── Template helpers ───────────────────────────────────────────

    get resultMarkup() {
        return markup(this.state.resultHtml || "");
    }

    get canDraftRC() {
        const res = this.state.result;
        return Boolean(res && res.ok && res.lead_id);
    }

    get hasRouteDev() {
        return Boolean(this.state.result && this.state.result.ok);
    }
}

registry.category("systray").add(
    "premafirm_estimator_chat",
    { Component: EstimatorChatPanel },
    { sequence: 5 }
);
