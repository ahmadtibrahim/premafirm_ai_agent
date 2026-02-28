/** @odoo-module **/

import { Component, useState, useRef, onPatched } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { AIChatUpload } from "../components/ai_chat_upload/ai_chat_upload";

class PremaAIChat extends Component {
    static components = { AIChatUpload };

    setup() {
        this.rpc = this.env.services?.rpc || null;
        this.bus = this.env.services?.bus_service || null;
        this.chatWindowRef = useRef("chatWindow");
        const actionContext = this.props.action?.context || {};
        const aiEnabled = actionContext.ai_enabled;
        this.state = useState({
            messages: [],
            input: "",
            healthScore: 100,
            sessionId: null,
            mode: "advice_only",
            showBatchModal: false,
            isSending: false,
            errorMessage: "",
            showAiDisabledWarning: !aiEnabled,
            batchSummary: { total: 0, clean: 0, duplicate: 0, missing_vendor: 0 },
            incidents: [],
            schemaModel: actionContext.model_name || "account.move",
            schemaFields: "",
        });

        if (!this.rpc) {
            this.state.errorMessage = "Chat service is unavailable in this context.";
            this.state.messages.push({
                role: "system",
                content: "Chat service is unavailable in this context.",
            });
            return;
        }

        this._initializeSession();

        if (this.bus) {
            this.bus.addChannel("prema_ai_channel");
            this.bus.addEventListener("notification", this.onNotification.bind(this));
        }

        onPatched(() => {
            const el = this.chatWindowRef.el;
            if (el) {
                el.scrollTop = el.scrollHeight;
            }
        });
    }

    async _initializeSession() {
        if (!this.rpc) {
            return;
        }
        const session = await this.rpc("/prema_ai/session", {});
        this.state.sessionId = session.id;
    }

    onNotification({ detail }) {
        for (const notification of detail || []) {
            if (notification.type === "prema_ai_channel") {
                this.state.messages.push({
                    role: "system",
                    content: `Realtime Alert: ${JSON.stringify(notification.payload)}`,
                });
            }
        }
    }

    async send() {
        if (!this.state.input || !this.state.sessionId || this.state.isSending) {
            return;
        }

        const userMsg = this.state.input;
        this.state.messages.push({ role: "user", content: userMsg });
        this.state.input = "";
        this.state.isSending = true;
        this.state.errorMessage = "";

        try {
            const response = await this.rpc("/prema_ai/chat", {
                message: userMsg,
                session_id: this.state.sessionId,
                mode: this.state.mode,
            });

            this.state.messages.push({
                role: "assistant",
                content: response.reply,
            });

            this.state.healthScore = response.health_score;
            await this.loadIncidents();
        } catch {
            this.state.errorMessage = "Could not fetch AI response. Please retry.";
            this.state.messages.push({
                role: "system",
                content: "Request failed. Please retry.",
            });
        } finally {
            this.state.isSending = false;
        }
    }

    onInputKeydown(ev) {
        if (ev.key === "Enter" && !ev.shiftKey) {
            ev.preventDefault();
            this.send();
        }
    }

    async refreshDocumentSummary() {
        if (!this.rpc || !this.state.sessionId) {
            return;
        }
        const response = await this.rpc("/prema_ai/document_summary", {
            session_id: this.state.sessionId,
        });
        if (response?.summary) {
            this.state.messages.push({
                role: "system",
                content: response.summary,
            });
        }
        if ((response?.batch_summary?.total || 0) > 1) {
            this.state.batchSummary = response.batch_summary;
            this.state.showBatchModal = true;
        }
    }

    closeBatchModal() {
        this.state.showBatchModal = false;
    }

    async loadIncidents() {
        if (!this.rpc) {
            return;
        }
        this.state.incidents = await this.rpc("/prema_ai/incidents", { limit: 20 });
    }

    async loadSchemaModel() {
        if (!this.rpc || !this.state.schemaModel) {
            return;
        }
        const response = await this.rpc("/prema_ai/schema_model", { model_name: this.state.schemaModel });
        this.state.schemaFields = response.fields || "";
    }

    async createCleanOnlyDrafts() {
        if (!this.rpc || !this.state.sessionId) {
            return;
        }
        await this.rpc("/prema_ai/create_drafts", {
            session_id: this.state.sessionId,
            clean_only: true,
        });
        this.state.showBatchModal = false;
        this.state.messages.push({ role: "system", content: "Created clean documents as draft bills." });
    }

    async createAllDrafts() {
        if (!this.rpc || !this.state.sessionId) {
            return;
        }
        await this.rpc("/prema_ai/create_drafts", {
            session_id: this.state.sessionId,
            clean_only: false,
        });
        this.state.showBatchModal = false;
        this.state.messages.push({ role: "system", content: "Created all processed documents as draft bills." });
    }
}

PremaAIChat.template = "prema_ai_auditor.ChatTemplate";
registry.category("actions").add("prema_ai_chat_action", {
    component: PremaAIChat,
});
