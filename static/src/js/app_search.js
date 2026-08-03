/** @odoo-module **/
/**
 * Home menu app search — admin only, desktop + tablet.
 *
 * Odoo Enterprise already has an o_search_hidden input wired to OWL's
 * _onInputSearch. We make it visible and styled instead of injecting
 * duplicate DOM and fighting OWL's reactivity.
 */

import { registry } from "@web/core/registry";
import { user } from "@web/core/user";

function activateBuiltinSearch(homeMenu) {
    // Already activated this session?
    if (homeMenu.querySelector(".o_premafirm_search_active")) return;

    const nativeInput = homeMenu.querySelector("input.o_search_hidden");
    if (!nativeInput) return;

    // Mark as activated so we don't repeat on re-renders
    nativeInput.classList.add("o_premafirm_search_active");

    // Remove the class that hides it (visually-hidden uses Bootstrap's
    // clip/position trick — easiest to just override inline)
    nativeInput.classList.remove("visually-hidden");
    nativeInput.placeholder = "Search apps…";
    nativeInput.setAttribute("aria-label", "Search Odoo apps");

    // Auto-focus when the home menu opens
    requestAnimationFrame(() => setTimeout(() => nativeInput.focus(), 80));

    // Escape clears and re-hides the keyboard on mobile
    nativeInput.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
            nativeInput.value = "";
            nativeInput.dispatchEvent(new Event("input", { bubbles: true }));
        }
    });
}

const appSearchService = {
    start() {
        const isAdmin = !!(user.isAdmin || user.isSystem);
        if (!isAdmin) return {};

        const observer = new MutationObserver(() => {
            const homeMenu = document.querySelector(".o_home_menu");
            if (homeMenu) activateBuiltinSearch(homeMenu);
        });
        observer.observe(document.body, { childList: true, subtree: true });

        return {};
    },
};

registry.category("services").add("premafirm_app_search", appSearchService);
