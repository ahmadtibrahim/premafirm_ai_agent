/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { FormRenderer } from "@web/views/form/form_renderer";
import { onMounted, onPatched } from "@odoo/owl";

patch(FormRenderer.prototype, {
    setup() {
        super.setup();
        onMounted(() => this._syncRenegTab());
        onPatched(() => this._syncRenegTab());
    },

    _syncRenegTab() {
        if (!this.el) return;
        const record = this.props.record;
        if (!record || record.resModel !== "sale.order") return;

        const status = record.data?.x_reneg_status;
        const shouldFlash = status === "requested";

        // Find the nav-link whose controlled page contains the reneg content
        const navLinks = this.el.querySelectorAll(".o_notebook .nav-link");
        for (const link of navLinks) {
            if (link.textContent.trim() === "Re-Negotiation") {
                link.classList.toggle("o_reneg_flash", shouldFlash);
                break;
            }
        }
    },
});
