/** @odoo-module **/
import { Component, useState, useRef, onWillStart, markup } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

class EstimatorChatPanel extends Component {
    static template = "premafirm_ai_engine.EstimatorChatPanel";

    setup() {
        this.orm          = useService("orm");
        this.notification = useService("notification");
        this.action       = useService("action");
        this.fileInputRef = useRef("fileInput");

        this.state = useState({
            panelOpen: false,
            loading:   false,
            mode:      "chat",   // "chat" | "files"
            chatText:  "",
            files:     [],       // [{name, b64, mimetype, size}]
            vehicles:  [],
            vehicleId: null,
            avoidTolls: true,
            allowUSA:   false,
            result:    null,     // {html, record_id, suggested_rate}
            error:     null,
            dragOver:  false,
        });

        onWillStart(async () => {
            await this._loadVehicles();
        });
    }

    // ── Panel control ─────────────────────────────────────────────────

    togglePanel() {
        this.state.panelOpen = !this.state.panelOpen;
    }

    resetPanel() {
        Object.assign(this.state, {
            chatText: "",
            files:    [],
            result:   null,
            error:    null,
        });
    }

    setMode(mode) {
        this.state.mode  = mode;
        this.state.error = null;
    }

    // ── Data loading ─────────────────────────────────────────────────

    async _loadVehicles() {
        try {
            const vehicles = await this.orm.searchRead(
                "fleet.vehicle",
                [["active", "=", true]],
                ["id", "name"],
                { limit: 80, order: "name asc" }
            );
            this.state.vehicles = vehicles;
        } catch (_) {
            // silently ignore — vehicles will just be empty
        }
    }

    // ── Form handlers ─────────────────────────────────────────────────

    onVehicleChange(ev) {
        const val = parseInt(ev.target.value, 10);
        this.state.vehicleId = isNaN(val) ? null : val;
    }

    onChatInput(ev) {
        this.state.chatText = ev.target.value;
    }

    onAvoidTollsChange(ev) {
        this.state.avoidTolls = ev.target.checked;
    }

    onAllowUSAChange(ev) {
        this.state.allowUSA = ev.target.checked;
    }

    // ── File handling ─────────────────────────────────────────────────

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
        // reset so same file can be added again if needed
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

    // ── Submit ─────────────────────────────────────────────────────────

    async onSubmit() {
        const { chatText, files, vehicleId, avoidTolls, allowUSA, mode } = this.state;

        if (!vehicleId) {
            this.notification.add("Please select a truck first", { type: "warning" });
            return;
        }
        if (mode === "chat" && !chatText.trim()) {
            this.notification.add("Please enter a rate request message", { type: "warning" });
            return;
        }
        if (mode === "files" && files.length === 0) {
            this.notification.add("Please upload at least one file (PDF or image)", { type: "warning" });
            return;
        }

        this.state.loading = true;
        this.state.result  = null;
        this.state.error   = null;

        try {
            const payload = {
                vehicle_id:         vehicleId,
                text_message:       mode === "chat" ? chatText.trim() : "",
                files:              mode === "files"
                                        ? files.map(f => ({ file_b64: f.b64, mimetype: f.mimetype, filename: f.name }))
                                        : [],
                avoid_tolls:        avoidTolls,
                allow_cross_border: allowUSA,
            };

            const result = await this.orm.call(
                "premafirm.rate.estimator",
                "chat_rate_request_rpc",
                [],
                payload
            );

            if (result && result.error) {
                this.state.error = result.error;
            } else {
                this.state.result = result;
            }
        } catch (err) {
            this.state.error = err.data?.message || err.message || "An unexpected error occurred";
        } finally {
            this.state.loading = false;
        }
    }

    // ── Open full estimate record ──────────────────────────────────────

    async openRecord() {
        if (!this.state.result?.record_id) return;
        await this.action.doAction({
            type:      "ir.actions.act_window",
            res_model: "premafirm.rate.estimator",
            res_id:    this.state.result.record_id,
            views:     [[false, "form"]],
            target:    "current",
        });
        this.state.panelOpen = false;
    }

    // ── Template helper ────────────────────────────────────────────────

    get resultMarkup() {
        return markup(this.state.result?.html || "");
    }
}

registry.category("systray").add(
    "premafirm_estimator_chat",
    { Component: EstimatorChatPanel },
    { sequence: 5 }
);
