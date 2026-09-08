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
        this.customerInputRef = useRef("customerInput");

        this.state = useState({
            panelOpen:   false,
            loading:     false,
            chatText:    "",
            files:       [],       // [{name, b64, mimetype, size}]
            vehicles:    [],
            vehicleId:   null,
            vehicleReefer: false,
            // Searchable customer combobox (single control): type to
            // search name/email/phone; arrow keys + Enter or mouse pick;
            // "No customer selected (new enquiry)" is the cleared state.
            customerOptions: [],   // [{id, name, email, phone, city}]
            customerOpen: false,
            customerActiveIndex: -1,
            customerId:  0,
            customerLabel:"",
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
            marginPct:   null,     // markup-on-cost override (null =
                                   // server default; synced from the best
                                   // card once a result arrives)
            // MP2 structured stops — the reviewed authority for routing/
            // pricing. Populated by extraction; editable before/after.
            stops:       [],       // [{id, stop_type, status, company_name,
                                   //   address, city, province, postal_code,
                                   //   pallets, cases, weight_lbs, stop_date,
                                   //   time_window_type, exact_time,
                                   //   window_start, window_end,
                                   //   instructions, saved_location_id,
                                   //   saved_location_name, locTerm,
                                   //   locOptions, locOpen}]
            conflicts:   [],       // [{sequence, current, proposed}]
            applyChanges:false,
            // §2 scenario selection + §1 cost breakdown modal
            selectedScenario: null,  // scenario key
            costModalKey:   null,    // scenario key whose breakdown is open
            // §1 manual road-distance override — only ever shown when the
            // stops could not be routed; nothing auto-guesses distance.
            manualKm:       "",
            manualHrs:      "",
            manualOverrideUsed: false,
            manualSignature: null,
            quoteWorkflow:  [],      // quote -> approval -> booking chain
            quoteName:      "",
        });

        onWillStart(async () => {
            await this._loadVehicles();
        });
    }

    // ── Panel control ─────────────────────────────────────────────

    togglePanel() {
        this.state.panelOpen = !this.state.panelOpen;
        if (this.state.panelOpen && !this.state.customerId) {
            this._preselectLeadCustomer();
        }
    }

    resetPanel() {
        Object.assign(this.state, {
            chatText: "", files: [], result: null, resultHtml: "",
            error: null, routeDev: null, pickupDate: "",
            returnHome: true, marginPct: null,
            stops: [], conflicts: [], applyChanges: false,
            selectedScenario: null, costModalKey: null,
            manualKm: "", manualHrs: "", manualOverrideUsed: false,
            manualSignature: null,
            quoteWorkflow: [], quoteName: "",
        });
        // A fresh estimate is a new enquiry — a stale customer pick from
        // an earlier estimate must never silently ride along.
        this._releaseCustomer();
        this.state.customerTerm = "";
    }

    // ── Lead-context customer preselect ─────────────────────────────

    async _preselectLeadCustomer() {
        // The estimator opened from a CRM lead form should start on that
        // lead's customer (or stay empty — never an unrelated customer).
        try {
            const ctrl = this.action.currentController;
            const props = ctrl && ctrl.props;
            if (!props || props.resModel !== "crm.lead" || !props.resId) {
                return;
            }
            const [lead] = await this.orm.read(
                "crm.lead", [props.resId], ["partner_id"]);
            const pid = lead && lead.partner_id;
            if (!pid) return;
            const [partner] = await this.orm.read(
                "res.partner", [pid], ["name", "email", "phone", "city"]);
            if (partner) this._pickCustomer(partner);
        } catch (_) {
            // no lead context (or read failed) — the box stays empty
        }
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
        // Editing the box after a pick starts a NEW search → the picked
        // customer is released (the estimate falls back to "new enquiry").
        if (this.state.customerId && term.trim() !== this.state.customerLabel) {
            this._releaseCustomer();
        }
        if (term.trim().length < 2) {
            this.state.customerOptions = [];
            this.state.customerOpen = false;
            return;
        }
        if (this._customerTimer) {
            clearTimeout(this._customerTimer);
        }
        this._customerTimer = setTimeout(() => this._searchCustomers(term), 350);
    }

    onCustomerFocus() {
        const st = this.state;
        // A picked customer sits in the box as its label: refocusing just
        // re-opens nothing — editing it (input) releases the selection and
        // starts a new search; the × button clears it outright.
        if (st.customerId && st.customerTerm === st.customerLabel) return;
        const term = (st.customerTerm || "").trim();
        if (st.customerOpen) return;
        if (term.length >= 2 && !this.state.customerOptions.length) {
            this._searchCustomers(term);
            return;
        }
        this.state.customerActiveIndex = this.state.customerOptions.length ? 0 : -1;
        this.state.customerOpen = true;
    }

    onCustomerBlur() {
        this.state.customerOpen = false;
        this.state.customerActiveIndex = -1;
    }

    onCustomerHover(index) {
        this.state.customerActiveIndex = index;
    }

    onCustomerKeydown(ev) {
        const st = this.state;
        if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
            ev.preventDefault();
            const n = st.customerOptions.length;
            if (!n) return;
            if (!st.customerOpen) {
                st.customerOpen = true;
                st.customerActiveIndex = 0;
                return;
            }
            const step = ev.key === "ArrowDown" ? 1 : -1;
            st.customerActiveIndex = (st.customerActiveIndex + step + n) % n;
        } else if (ev.key === "Enter") {
            const opt = st.customerOptions[st.customerActiveIndex];
            if (st.customerOpen && opt) {
                // Enter on the highlighted row picks it (kept out of the
                // search term); Enter without a highlighted row falls
                // through and submits the estimate as typed.
                if (st.customerId !== opt.id) this._pickCustomer(opt);
            }
        } else if (ev.key === "Escape") {
            st.customerOpen = false;
            st.customerActiveIndex = -1;
        } else if (ev.key === "Tab") {
            st.customerOpen = false;
        }
    }

    onCustomerPick(index) {
        const opt = this.state.customerOptions[index];
        if (opt) this._pickCustomer(opt);
    }

    _pickCustomer(c) {
        const st = this.state;
        st.customerId = c.id;
        st.customerLabel = c.name;
        st.customerTerm = c.name;
        st.customerOptions = [];
        st.customerOpen = false;
        st.customerActiveIndex = -1;
        // The label is the new "value" of the box: a full re-search is
        // offered the moment the user edits it again.
    }

    _releaseCustomer() {
        const st = this.state;
        st.customerId = 0;
        st.customerLabel = "";
        st.customerOptions = [];
        st.customerOpen = false;
        st.customerActiveIndex = -1;
    }

    clearCustomer() {
        this._releaseCustomer();
        this.state.customerTerm = "";
        this.customerInputRef.el && this.customerInputRef.el.focus();
    }

    async _searchCustomers(term) {
        this.state.customerBusy = true;
        try {
            const partners = await this.orm.searchRead(
                "res.partner",
                [
                    "|", "|",
                    ["name", "ilike", term],
                    ["email", "ilike", term],
                    ["phone", "ilike", term],
                ],
                ["id", "name", "email", "phone", "city"],
                { limit: 12, order: "name asc" }
            );
            this.state.customerOptions = partners;
            this.state.customerActiveIndex = partners.length ? 0 : -1;
            this.state.customerOpen = true;
        } catch (_) {
            this.state.customerOptions = [];
            this.state.customerActiveIndex = -1;
            this.state.customerOpen = true;
        } finally {
            this.state.customerBusy = false;
        }
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
        // §5 — when the selected date changes, dependent ETAs and the
        // schedule recalculate (the explicit date is never overwritten).
        if (this.state.result && this.state.result.ok && !this.state.loading) {
            this._runEstimate();
        }
    }

    // ── §2 scenario selection / §1 cost breakdown ────────────────────

    selectScenario(key) {
        // Pure review state — selecting a scenario never creates,
        // sends, confirms or books anything.
        this.state.selectedScenario = key;
    }

    openCostModal(key) {
        this.state.costModalKey = key;
    }

    closeCostModal() {
        // Closing returns to the SAME estimate — nothing is re-run and
        // no edit is lost.
        this.state.costModalKey = null;
    }

    onResultClick(ev) {
        const closeEl = ev.target.closest("[data-close-cost]");
        if (closeEl) {
            this.closeCostModal();
            return;
        }
        const costEl = ev.target.closest("[data-cost-key]");
        if (costEl) {
            this.openCostModal(costEl.dataset.costKey);
            return;
        }
        const cardEl = ev.target.closest("[data-card-key]");
        if (cardEl) {
            this.selectScenario(cardEl.dataset.cardKey);
        }
    }

    onManualKmChange(ev) {
        this.state.manualKm = ev.target.value;
    }

    onManualHrsChange(ev) {
        this.state.manualHrs = ev.target.value;
    }

    applyManualRoute() {
        // §1 — an explicit human override of the road distance when the
        // stops cannot be routed: km is required; hours left blank are
        // estimated by the server at ~70 km/h. The box disappears once a
        // route/cost exists, and the override stays on later re-prices
        // (margin) until the user resets the panel.
        const km = parseFloat(this.state.manualKm);
        if (!(km > 0)) {
            this.notification.add(
                "Enter a road distance in km to apply the manual override",
                { type: "warning" });
            return;
        }
        const hrs = parseFloat(this.state.manualHrs);
        // Signature: the override applies only while the request it was
        // entered for is unchanged — a new message/stops/truck/date
        // silently discards it instead of mis-pricing the next request.
        this.state.manualSignature = [
            this.state.vehicleId, this.state.pickupDate,
            this.state.chatText, JSON.stringify(this._stopsPayload()),
        ].join("|");
        this.state.manualOverrideUsed = true;
        this._runEstimate();
    }

    get needsManualOverride() {
        // Shown ONLY when a direct scenario exists and could not be
        // routed (no distance, no cost) — never on a routable estimate.
        const r = this.state.result;
        if (!r || r.ok === false || !r.scenarios) return false;
        if (this.state.manualOverrideUsed) return false;
        return r.scenarios.some(c =>
            (c.key === "dedicated_one_way"
             || c.key === "dedicated_round_trip")
            && c.cost === false && c.distance_km === false);
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

    // ── MP2 structured stops ─────────────────────────────────────────

    _newStop(kind) {
        return {
            id: 0, stop_type: kind, source: "manual", status: "incomplete",
            saved_location_id: 0, saved_location_name: "",
            company_name: "", address: "", city: "", province: "",
            postal_code: "", pallets: 0, cases: 0, weight_lbs: 0,
            stop_date: "", time_window_type: "any", exact_time: 0,
            window_start: 0, window_end: 0, instructions: "",
            locTerm: "", locOptions: [], locOpen: false,
        };
    }

    addStop(kind) {
        this.state.stops = [...this.state.stops, this._newStop(kind)];
    }

    removeStop(index) {
        this.state.stops = this.state.stops.filter((_, i) => i !== index);
    }

    moveStop(index, dir) {
        const stops = this.state.stops;
        const to = index + dir;
        if (to < 0 || to >= stops.length) return;
        const copy = [...stops];
        [copy[index], copy[to]] = [copy[to], copy[index]];
        // The stop's type rides along with the row — reordering never
        // changes pickup/delivery.
        this.state.stops = copy;
    }

    updateStop(index, field, value) {
        const stops = [...this.state.stops];
        stops[index] = { ...stops[index], [field]: value };
        this.state.stops = stops;
    }

    fmtHour(v) {
        // 8.5 -> "08:30" for time inputs
        const n = Number(v);
        if (isNaN(n) || n === 0) return "";
        const h = Math.floor(n), m = Math.round((n - h) * 60);
        return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
    }

    parseHour(str) {
        if (!str) return 0;
        const [h, m] = str.split(":").map(Number);
        return (h || 0) + (m || 0) / 60;
    }

    async onStopLocInput(index, ev) {
        const term = ev.target.value;
        this.updateStop(index, "locTerm", term);
        const stop = this.state.stops[index];
        if (stop.saved_location_id && term.trim() !== stop.saved_location_name) {
            this.clearStopLocation(index);
        }
        if (term.trim().length < 2) {
            this.updateStop(index, "locOptions", []);
            this.updateStop(index, "locOpen", false);
            return;
        }
        if (this._locTimer) clearTimeout(this._locTimer);
        this._locTimer = setTimeout(() => this._searchStopLocations(index, term), 350);
    }

    async _searchStopLocations(index, term) {
        try {
            const options = await this.orm.call(
                "premafirm.estimator.scenario.request",
                "location_search_rpc",
                [],
                { term }
            );
            this.updateStop(index, "locOptions", options || []);
            this.updateStop(index, "locOpen", true);
        } catch (_) {
            this.updateStop(index, "locOptions", []);
        }
    }

    pickStopLocation(index, opt) {
        const stops = [...this.state.stops];
        stops[index] = {
            ...stops[index],
            saved_location_id: opt.id,
            saved_location_name: opt.name,
            locTerm: opt.name,
            locOptions: [],
            locOpen: false,
            company_name: stops[index].company_name || opt.name,
            address: opt.address || stops[index].address,
            city: opt.city || stops[index].city,
            province: opt.province_code || stops[index].province,
            postal_code: opt.postal_code || stops[index].postal_code,
        };
        this.state.stops = stops;
    }

    clearStopLocation(index) {
        const stops = [...this.state.stops];
        stops[index] = {
            ...stops[index],
            saved_location_id: 0, saved_location_name: "",
            locTerm: "", locOptions: [], locOpen: false,
        };
        this.state.stops = stops;
    }

    _stopsPayload() {
        return this.state.stops.map((s, i) => ({
            id: s.id || 0,
            sequence: (i + 1) * 10,
            stop_type: s.stop_type,
            source: s.source || "manual",
            saved_location_id: s.saved_location_id || 0,
            company_name: s.company_name || "",
            address: s.address || "",
            city: s.city || "",
            province: s.province || "",
            postal_code: s.postal_code || "",
            pallets: Number(s.pallets) || 0,
            cases: Number(s.cases) || 0,
            weight_lbs: Number(s.weight_lbs) || 0,
            stop_date: s.stop_date || false,
            time_window_type: s.time_window_type || "any",
            exact_time: Number(s.exact_time) || 0,
            window_start: Number(s.window_start) || 0,
            window_end: Number(s.window_end) || 0,
            instructions: s.instructions || "",
        }));
    }

    _applyStopsResult(res) {
        // The server's structured stops are the truth after every run.
        if (Array.isArray(res.structured_stops) && res.structured_stops.length) {
            this.state.stops = res.structured_stops.map(s => ({
                ...this._newStop(s.stop_type),
                ...s,
                locTerm: s.saved_location_name || "",
                locOptions: [], locOpen: false,
                exact_time: s.exact_time || 0,
                window_start: s.window_start || 0,
                window_end: s.window_end || 0,
            }));
        }
        this.state.conflicts = res.stop_conflicts || [];
    }

    acceptStopChanges() {
        this.state.applyChanges = true;
        this.state.conflicts = [];
        this._runEstimate();
    }

    keepReviewedStops() {
        // The reviewed rows already stand — just dismiss the banner.
        this.state.conflicts = [];
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
        await this._runEstimate();
    }

    onMarginChange(ev) {
        const val = parseFloat(ev.target.value);
        this.state.marginPct = isNaN(val) ? null : Math.max(0, Math.min(500, val));
    }

    async applyMargin() {
        // Re-prices the scenarios with the entered markup-on-cost.
        // Reuses the same request text/files; the server recomputes the
        // cards as the pricing authority.
        if (this.state.loading) return;
        const val = this.state.marginPct;
        if (val === null || val === undefined) {
            this.notification.add("Enter a margin percentage first", { type: "warning" });
            return;
        }
        await this._runEstimate();
    }

    async _runEstimate() {
        const st = this.state;
        // §1 — a manual distance override entered for an earlier request
        // is dropped the moment the request itself changes (see
        // applyManualRoute): it never prices a different posting.
        if (st.manualOverrideUsed) {
            const sig = [
                st.vehicleId, st.pickupDate,
                st.chatText, JSON.stringify(this._stopsPayload()),
            ].join("|");
            if (sig !== st.manualSignature) {
                st.manualOverrideUsed = false;
                st.manualSignature = null;
            }
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
                margin_pct:         st.marginPct,
                stops_input:        this._stopsPayload(),
                request_id:         st.result?.request_id || 0,
                apply_changes:      st.applyChanges,
                // §1 manual road-distance override (only after the user
                // applied one; null otherwise — server ignores).
                manual_distance_km: st.manualOverrideUsed
                    ? (parseFloat(st.manualKm) > 0
                       ? parseFloat(st.manualKm) : null)
                    : null,
                manual_duration_hrs: st.manualOverrideUsed
                    ? (parseFloat(st.manualHrs) > 0
                       ? parseFloat(st.manualHrs) : null)
                    : null,
            };
            st.applyChanges = false;
            const result = await this.orm.call(
                "premafirm.estimator.scenario.request",
                "estimate_scenarios_rpc",
                [],
                payload
            );
            if (result && result.success === false) {
                // Structured failure — rendered as banners (headline +
                // per-problem bullets + small-print technical detail), so
                // the raw exception is never the only thing the user sees.
                st.result = result;
                st.resultHtml = this._buildResultHtml(result);
                this._applyStopsResult(result);
            } else if (result && result.error) {
                // Legacy/defensive path (route-dev, rate-confirmation RPCs
                // or a bridge-shaped error) — top alert, as before.
                st.error = result.error;
            } else {
                st.result = result;
                st.resultHtml = this._buildResultHtml(result);
                this._applyStopsResult(result);
                // Mirror the applied markup into the control so it always
                // shows the number the server actually priced with.
                if (st.marginPct === null) {
                    const cards = result.scenarios || [];
                    const priced = cards.find(c => c.markup_pct_on_cost !== false)
                        || cards[0];
                    if (priced && priced.markup_pct_on_cost !== false
                        && priced.markup_pct_on_cost !== undefined) {
                        st.marginPct = Math.round(priced.markup_pct_on_cost);
                    }
                }
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
        const validationErrors = res.validation_errors || [];
        if (res.success === false || res.state === "error" || validationErrors.length) {
            // Structured failure: friendly headline + per-problem bullets
            // + optional small-print technical detail.  No itinerary /
            // scenario sections on a failed estimate — that is the audit
            // record's job.
            h.push(`<div class="o_est_banner banner-error"><b>${this._esc(res.message || "The estimate could not be completed.")}</b></div>`);
            for (const ve of validationErrors) {
                h.push(`<div class="o_est_conflict">✖ ${this._esc(ve)}</div>`);
            }
            if (res.error_detail) {
                h.push(`<div class="o_est_sub">detail: ${this._esc(res.error_detail)}</div>`);
            }
            h.push(`</div>`);
            return h.join("");
        }
        if (res.dispatch_online === false) {
            h.push(`<div class="o_est_banner banner-offline">Dispatch integration is offline — scenario cards, availability and pricing intel are unavailable on this database.</div>`);
        }
        if (res.message) {
            h.push(`<div class="o_est_banner banner-info">${this._esc(res.message)}</div>`);
        }
        const opWarnings = res.operational_warnings || [];
        if (opWarnings.length) {
            h.push(this._chips(opWarnings, "warn"));
        } else if (warnings.length) {
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

        // 2. Scenario cards — §2 selectable, §1 clickable cost breakdown,
        // §6 reviewable schedule, §4 per-destination corridor rows.
        const scenarios = res.scenarios || [];
        if (scenarios.length) {
            h.push(`<div class="o_est_section"><div class="o_est_sec_title">🔄 Three ways to move it <span class="o_est_sec_meta">select a scenario to review its schedule</span></div>`);
            const badgeLabel = (b) => ({
                "feasible": "feasible",
                "verified": "verified",
                "conditional": "conditional — verification required",
                "infeasible": "infeasible",
                "requires_estimate": "requires estimate",
                "estimate_unavailable": "estimate unavailable",
                "alternative_dates": "date change required",
            }[b] || b);
            for (const card of scenarios) {
                const badge = card.badge || (card.feasible ? "feasible" : "infeasible");
                const selected = this.state.selectedScenario === card.key;
                h.push(`<div class="o_est_card card-${this._esc(badge)}${selected ? " o_est_card_selected" : ""}" data-card-key="${this._esc(card.key)}">
                    <div class="o_est_card_head">
                        <span class="o_est_card_title">${this._esc(card.title)}</span>
                        <span class="o_est_badge badge-${this._esc(badge)}">${this._esc(badgeLabel(badge))}</span>
                    </div>
                    <div class="o_est_card_body">`);
                if (card.truck) h.push(this._kv("Truck", this._esc(card.truck)));
                h.push(this._kv("Pickup", card.pickup_date || "—", "kvm"));
                if (card.key === "scheduled_ltl" && card.date_requested
                        && card.pickup_date
                        && card.date_requested !== card.pickup_date) {
                    h.push(this._kv("Next available", `📅 <b>${this._esc(card.pickup_date)}</b> — requested ${this._esc(card.date_requested)}`, "kvm"));
                }
                if (card.delivery_date || card.return_date) {
                    h.push(this._kv("Delivery", card.delivery_date || "—", "kvm"));
                }
                if (card.return_date) h.push(this._kv("Return home", card.return_date, "kvm"));
                if (card.hold_nights) {
                    // §2 — the delivery date on the card IS the requested
                    // date (the move holds overnight to reach it); the
                    // sub-line makes the hold visible, not hidden.
                    h.push(`<div class="o_est_sub">🌙 arrives the day before — holds ${this._esc(card.hold_nights)} night${card.hold_nights > 1 ? "s" : ""}${card.requested_delivery_date ? ` (driver off duty) until the requested delivery ${this._esc(card.requested_delivery_date)}` : ""}; overnight fee and reefer runtime are itemized in the COST breakdown</div>`);
                }
                h.push(this._kv("Distance", card.distance_km === false ? "—" : `${this._num(card.distance_km)} km`, "kvm"));
                if (card.drive_hrs !== false) h.push(this._kv("Drive + service", card.drive_hrs === false ? "—" : `${this._num(card.drive_hrs)} h`, "kvm"));
                if (card.cost !== false) {
                    // §3 — the COST figure itself opens the itemized
                    // breakdown (qty × rate = amount; rows sum exactly to
                    // the total); the planned sell price is deliberately
                    // separate — never an operating cost line.
                    const costVal = card.cost_breakdown
                        ? `<a class="o_est_cost_amt" data-cost-key="${this._esc(card.key)}" title="Open itemized cost breakdown">${this._money(card.cost)}</a> <a class="o_est_cost_link" data-cost-key="${this._esc(card.key)}">⧉ itemized</a>`
                        : this._money(card.cost);
                    h.push(this._kv("Operating cost", costVal, "kvm"));
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
                    if (card.conditional_reasons && card.conditional_reasons.length) {
                        h.push(`<div class="o_est_sub">No operating cost shown: ${this._esc(card.conditional_reasons[0])}</div>`);
                    }
                }
                if (card.incremental_km) h.push(this._kv("Empty mileage", `${this._num(card.incremental_km, 0)} km`, "kvm"));
                const avail = card.availability || {};
                if (card.key !== "scheduled_ltl") {
                    const status = avail.status === "conflicts"
                        ? `<span class="o_est_bad">conflicts</span>`
                        : `<span class="o_est_ok">${avail.checked ? "no conflicting booking found" : "not checked"}</span>`;
                    h.push(this._kv("Truck availability", status));
                    if (avail.note) h.push(`<div class="o_est_sub">${this._esc(avail.note)}</div>`);
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
                    // §5 — a corridor service on OTHER dates than the
                    // request is flagged as a date-change alternative with
                    // the reason — never silently "feasible".
                    if (card.date_change_reason) {
                        h.push(`<div class="o_est_note note-altdate">📅 ${this._esc(card.date_change_reason)}</div>`);
                    }
                    // §5 — verify the claimed service: legs chain with
                    // corridor / hub / feeder legs, per-leg dates and the
                    // assigned vehicle.
                    const legs = card.legs || [];
                    if (legs.length) {
                        h.push(`<div class="o_est_alt_title">How it runs — corridor legs (verified route chain)</div>`);
                        for (const leg of legs) {
                            h.push(`<div class="o_est_leg">${this._esc(leg)}</div>`);
                        }
                    }
                    const pd = card.per_destination || [];
                    if (pd.length) {
                        h.push(`<div class="o_est_alt_title">Each destination</div>`);
                        h.push(`<table class="o_est_table"><thead><tr><th>Destination</th><th>Pallets</th><th>Pickup</th><th>Delivered</th><th>Service legs</th><th>Reason</th></tr></thead><tbody>`);
                        for (const d of pd) {
                            const legsCell = (d.legs || []).map(x => this._esc(x)).join("<br/>");
                            h.push(`<tr><td>${this._esc(d.name)}</td><td>${this._esc(d.pallets || 0)}</td><td>${this._esc(d.served_date || "—")}</td><td>${this._esc(d.delivery_date || "—")}</td><td>${legsCell || this._esc(d.corridor || "—")}</td><td>${this._esc(d.reason || "")}</td></tr>`);
                        }
                        h.push(`</tbody></table>`);
                    }
                }
                // §4 — dispatch readiness is separated from the estimate:
                // a missing driver/ELD proof reads as "dispatch
                // verification required", NOT as an infeasible move.
                const dv = card.dispatch_verification || {};
                if (dv.required && dv.items && dv.items.length) {
                    h.push(`<div class="o_est_alt_title">🚩 Dispatch verification required before dispatch</div>`);
                    h.push(this._chips(dv.items, "warn"));
                }
                if (card.conditions && card.conditions.length) {
                    h.push(`<div class="o_est_alt_title">⚠ Conditions — provisional estimate</div>`);
                    h.push(this._chips(card.conditions, "cond"));
                }
                if (card.conditional_reasons && card.conditional_reasons.length) {
                    h.push(`<div class="o_est_alt_title">Verification required</div>`);
                    h.push(this._chips(card.conditional_reasons, "cond"));
                }
                if (card.blocking && card.blocking.length) {
                    h.push(this._chips(card.blocking, "block"));
                }
                if (card.warnings && card.warnings.length) {
                    // §7 — repetitive technical warnings collapse to one
                    // notice with expandable details.
                    const uniq = [...new Set(card.warnings)];
                    if (uniq.length === 1) {
                        h.push(this._chips(uniq, "warn"));
                    } else {
                        h.push(`<details class="o_est_details"><summary>⚠ ${this._esc(uniq.length)} notices/warnings — show details</summary>`);
                        h.push(this._chips(uniq, "warn"));
                        h.push(`</details>`);
                    }
                }
                if (card.alternatives && card.alternatives.length) {
                    h.push(`<div class="o_est_alt_title">Alternative corridor dates</div>`);
                    h.push(`<table class="o_est_table"><thead><tr><th>Date</th><th>Delivered</th><th>Price</th><th>Corridor</th><th>Free</th></tr></thead><tbody>`);
                    for (const alt of card.alternatives) {
                        h.push(`<tr><td>${this._esc(alt.date || "—")}</td><td>${this._esc(alt.delivery_date || "—")}</td><td>${this._money(alt.price)}</td><td>${this._esc(alt.corridor || "")}</td><td>${alt.free_pallets === false ? "—" : this._esc(alt.free_pallets)}</td></tr>`);
                    }
                    h.push(`</tbody></table>`);
                }
                if (selected && card.schedule && card.schedule.stops && card.schedule.stops.length) {
                    h.push(`<div class="o_est_alt_title">Stop-by-stop schedule — ${this._esc(card.schedule.start_at || "")} <span class="o_est_sec_meta">${this._esc(card.schedule.source || "")}</span></div>`);
                    h.push(`<table class="o_est_table"><thead><tr><th>#</th><th>Stop</th><th>Arrive</th><th>Wait</th><th>Service</th><th>Depart</th><th>Onboard</th><th>Note</th></tr></thead><tbody>`);
                    for (const s of card.schedule.stops) {
                        h.push(`<tr><td>${s.seq}</td><td>${s.stop_type === "pickup" ? "📦" : "📍"} ${this._esc(s.name)}</td><td>${this._esc(s.arrival)}</td><td>${s.wait_min ? this._esc(s.wait_min) + " min" : "—"}</td><td>${this._esc(s.service_min)} min</td><td>${this._esc(s.departure)}</td><td>${s.onboard_pallets} plt${s.onboard_cases ? " / " + this._esc(s.onboard_cases) + " cs" : ""}</td><td>${this._esc(s.note || "")}</td></tr>`);
                    }
                    h.push(`</tbody></table>`);
                    const tc = card.schedule.truck_commitments || [];
                    if (tc.length) {
                        h.push(`<div class="o_est_alt_title">Truck's existing commitments (read-only)</div>`);
                        h.push(`<table class="o_est_table"><thead><tr><th>Job</th><th>Pickup</th><th>Done</th><th>Pallets</th></tr></thead><tbody>`);
                        for (const j of tc) {
                            h.push(`<tr><td>${this._esc(j.job)}</td><td>${this._esc(j.pickup)}</td><td>${this._esc(j.eta_done)}</td><td>${this._esc(j.pallets || 0)}</td></tr>`);
                        }
                        h.push(`</tbody></table>`);
                    }
                    for (const n of card.schedule.notes || []) {
                        h.push(`<div class="o_est_note note-warn">${this._esc(n)}</div>`);
                    }
                }
                if (card.assumptions && card.assumptions.length) {
                    h.push(`<div class="o_est_note">${card.assumptions.map(a => "· " + this._esc(a)).join("<br/>")}</div>`);
                }
                h.push(`</div></div>`);
            }
            h.push(`</div>`);

            // §1 cost breakdown modal (X top-right; closing returns to
            // the same estimate without losing anything)
            if (this.state.costModalKey) {
                const mc = scenarios.find(c => c.key === this.state.costModalKey);
                if (mc && mc.cost_breakdown) {
                    const cb = mc.cost_breakdown;
                    h.push(`<div class="o_est_modal_backdrop"><div class="o_est_modal">
                        <div class="o_est_modal_head">
                            <b>Operating cost — ${this._esc(mc.title)}</b>
                            <button type="button" class="o_est_modal_x" data-close-cost="1" title="Close">✕</button>
                        </div>
                        <div class="o_est_modal_body">
                        <table class="o_est_table"><thead><tr><th>Component</th><th>Quantity</th><th>Rate</th><th>Amount</th><th>Basis</th></tr></thead><tbody>`);
                    for (const c of cb.components || []) {
                        const assumed = (c.note || "").includes("assumed");
                        h.push(`<tr><td>${this._esc(c.label)}</td><td>${this._esc(c.qty || "")}</td><td>${this._esc(c.rate || "")}</td><td>${this._money(c.amount)}</td><td class="o_est_sub ${assumed ? "o_est_assumed" : ""}" ${assumed ? 'title="Rate not configured — code default in use"' : ""}>${this._esc(c.note || "")}</td></tr>`);
                    }
                    h.push(`</tbody></table>
                        <div class="o_est_modal_total">Total operating cost <b>${this._money(cb.total)}</b> <span class="o_est_sub">— exactly the COST on the card (same calculation)</span></div>
                        <div class="o_est_sub o_est_modal_foot">Each row: quantity × rate = amount, and the rows sum to the total. Rates marked “assumed” are code fallbacks because the parameter is not configured — the “configured” ones come from system parameters and the vehicle costing tab. The planned sell price is NOT an operating cost and is never part of this total.</div>
                        </div></div></div>`);
                } else if (mc) {
                    h.push(`<div class="o_est_modal_backdrop"><div class="o_est_modal">
                        <div class="o_est_modal_head">
                            <b>Operating cost — ${this._esc(mc.title)}</b>
                            <button type="button" class="o_est_modal_x" data-close-cost="1" title="Close">✕</button>
                        </div>
                        <div class="o_est_modal_body">
                        <div class="o_est_empty">No itemized breakdown is available for this scenario${(mc.conditional_reasons || []).length ? ":" : "."} ${(mc.conditional_reasons || []).map(r => this._esc(r)).join(" ")}</div>
                        </div></div></div>`);
                }
            }
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
            { request_id: st.result.request_id,
              scenario_key: st.selectedScenario }
        );
        if (result?.ok) {
            st.quoteWorkflow = result.workflow || [];
            st.quoteName = result.quote_name || "";
            this.notification.add(result.message || "Draft created", { type: "success" });
            if (result.action) {
                await this.action.doAction(result.action);
            }
            return;
        }
        this.notification.add(result?.message || result?.error || "Could not create the draft", { type: "warning" });
    }

    async refreshQuoteWorkflow() {
        const st = this.state;
        if (!st.result?.request_id) return;
        const result = await this.orm.call(
            "premafirm.estimator.scenario.request",
            "quote_workflow_status_rpc",
            [],
            { request_id: st.result.request_id }
        );
        st.quoteWorkflow = result?.workflow || [];
        st.quoteName = result?.quote_name || st.quoteName || "";
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
