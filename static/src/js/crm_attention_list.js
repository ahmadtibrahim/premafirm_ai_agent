/** @odoo-module **/
/** Needs Attention — drill-down list client action (CRM menu item).
 *
 * Reads the server-side payload (canonical count, unresolved leads,
 * actionable Inbound Review rows, small Email Health status) and offers
 * per-row actions: Open Lead / Reply for leads, Review Thread / Mark
 * Reviewed / No Reply Needed for queue rows. Opening a lead NEVER clears
 * attention (see ml_orm_hooks) — resolution happens only server-side. */
import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

class CrmAttentionList extends Component {
    static template = "premafirm.AttentionList";

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({
            count: 0,
            leads: [],
            queue: [],
            health: null,
            loading: true,
        });
        onWillStart(async () => this.refresh());
    }

    async refresh() {
        try {
            const data = await this.orm.call(
                "premafirm.inbound.queue", "attention_payload", []
            );
            Object.assign(this.state, {
                count: data.count,
                leads: data.leads,
                queue: data.queue,
                health: data.email_health,
                loading: false,
            });
        } catch (_e) {
            this.state.loading = false;
        }
    }

    openLead(id) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "crm.lead",
            res_id: id,
            views: [[false, "form"]],
        });
    }

    async replyLead(id) {
        // Reuse the lead's own threaded Reply composer when available;
        // fall back to opening the lead form (chatter Reply is there).
        try {
            const act = await this.orm.call(
                "crm.lead", "action_reply_last_email", [id]
            );
            if (act) {
                this.action.doAction(act);
                return;
            }
        } catch (_e) {
            /* fall through */
        }
        this.openLead(id);
    }

    openQueue(id) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "premafirm.inbound.queue",
            res_id: id,
            views: [[false, "form"]],
        });
    }

    async queueReviewed(id) {
        await this.orm.call(
            "premafirm.inbound.queue", "action_mark_reviewed", [[id]]
        );
        this.refresh();
    }

    async queueNoReply(id) {
        await this.orm.call(
            "premafirm.inbound.queue", "action_mark_ignored", [[id]]
        );
        this.refresh();
    }
}

registry.category("actions").add(
    "premafirm.crm_attention_list", CrmAttentionList
);
