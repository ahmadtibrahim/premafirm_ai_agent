/** @odoo-module **/
/** Global "Needs Attention" chip on the CRM pipeline kanban.
 *
 * Injected into the kanban control panel. The count is computed
 * server-side (premafirm.inbound.queue._attention_count) on every render
 * and refreshed live via the Odoo bus whenever queue/lead state changes.
 * Purely informational and persistent: clicking it opens the drill-down
 * list and NEVER clears it (clear-on-open was removed 2026-08-22). */
import { useService } from "@web/core/utils/hooks";
import { patch } from "@web/core/utils/patch";
import { KanbanController } from "@web/views/kanban/kanban_controller";

async function refreshChip(controller) {
    const el = controller.el;
    if (!el) {
        return;
    }
    let chip = el.querySelector(".o_premafirm_attention_chip");
    if (!chip) {
        const buttons = el.querySelector(
            ".o_cp_buttons, .o_cp_always_buttons, .o_cp_right, .o_control_panel"
        );
        if (!buttons) {
            return;
        }
        chip = document.createElement("a");
        chip.className = "o_premafirm_attention_chip";
        chip.setAttribute(
            "title", "Needs Attention — click to open the list"
        );
        chip.addEventListener("click", (ev) => {
            ev.preventDefault();
            controller.actionService.doAction({
                type: "ir.actions.client",
                tag: "premafirm.crm_attention_list",
            });
        });
        buttons.prepend(chip);
    }
    try {
        const count = await controller.env.services.orm.call(
            "premafirm.inbound.queue", "attention_count", []
        );
        chip.textContent = `🔴 Needs Attention — ${count}`;
        chip.classList.toggle("o_premafirm_attention_chip_active", count > 0);
        chip.style.display = count > 0 ? "" : "none";
    } catch (_e) {
        /* keep the last state on transient errors */
    }
}

patch(KanbanController.prototype, {
    setup() {
        super.setup();
        if (this.props.resModel !== "crm.lead") {
            return;
        }
        this.actionService = this.actionService || useService("action");
        const bus = this.env.services.bus_service;
        if (bus) {
            try {
                bus.addChannel("crm_needs_attention");
                this._attentionUnsub = bus.subscribe(
                    "crm_needs_attention", () => refreshChip(this)
                );
            } catch (_e) {
                this._attentionUnsub = null;
            }
        }
    },
    onMounted() {
        super.onMounted();
        if (this.props.resModel === "crm.lead") {
            refreshChip(this);
        }
    },
    onPatched() {
        super.onPatched();
        if (this.props.resModel === "crm.lead") {
            refreshChip(this);
        }
    },
    onWillUnmount() {
        super.onWillUnmount();
        if (this._attentionUnsub) {
            try {
                this._attentionUnsub();
            } catch (_e) {
                /* noop */
            }
            this._attentionUnsub = null;
        }
    },
});
