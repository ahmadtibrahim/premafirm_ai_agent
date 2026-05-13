/** @odoo-module **/
/**
 * Company-name autocomplete widget.
 *
 * Priority:
 *   1. Google Places API  (google_maps_api_key system param)  → name + address + phone + website
 *   2. OpenStreetMap / Nominatim  (free, no key required)     → name + address
 *
 * Attach to any Char field.  On selection fills: partner_name + phone + website +
 * street + city + zip + state_id + country_id.
 */

import { CharField } from "@web/views/fields/char/char_field";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { onMounted, onWillUnmount } from "@odoo/owl";
import { loadGooglePlaces, parseGoogleComponents } from "./google_places_utils";

// ── Nominatim address parser ─────────────────────────────────────────────────

function parseNominatim(result) {
    const a = result.address || {};

    // ISO3166-2-lvl4 = "CA-ON" → countryCode="CA", stateCode="ON"
    const iso  = a["ISO3166-2-lvl4"] || "";
    const parts = iso.includes("-") ? iso.split("-") : [];
    const countryCode = (parts[0] || a.country_code || "").toUpperCase();
    const stateCode   = (parts[1] || "").toUpperCase();

    return {
        name:        result.name || "",
        street:      [a.house_number, a.road].filter(Boolean).join(" "),
        city:        a.city || a.town || a.village || a.municipality || "",
        zip:         a.postcode || "",
        stateCode,
        countryCode,
    };
}

// ── Widget ───────────────────────────────────────────────────────────────────

class GoogleCompanyField extends CharField {
    static props = {
        ...CharField.props,
        phoneField:   { type: String, optional: true },
        websiteField: { type: String, optional: true },
        streetField:  { type: String, optional: true },
        cityField:    { type: String, optional: true },
        zipField:     { type: String, optional: true },
        stateField:   { type: String, optional: true },
        countryField: { type: String, optional: true },
    };

    setup() {
        super.setup();
        this.orm = useService("orm");
        this._cleanup    = null;
        this._mounted    = false;
        this._mode       = null;   // "google" | "nominatim"
        // Google services (only when Google key is available)
        this._acService  = null;
        this._plService  = null;

        onMounted(() => {
            this._mounted = true;
            if (!this.props.readonly) this._initAutocomplete();
        });
        onWillUnmount(() => {
            this._mounted = false;
            if (this._cleanup) this._cleanup();
        });
    }

    async _initAutocomplete() {
        const googleKey = await this.orm.call(
            "ir.config_parameter", "get_param", ["google_maps_api_key"]
        );

        if (!this._mounted) return;
        const input = this.input?.el;
        if (!input) return;

        if (googleKey) {
            const loaded = await loadGooglePlaces(googleKey);
            if (loaded && this._mounted) {
                const attrDiv = document.createElement("div");
                this._acService = new window.google.maps.places.AutocompleteService();
                this._plService = new window.google.maps.places.PlacesService(attrDiv);
                this._mode = "google";
                this._attachDropdown(input);
                return;
            }
        }

        // Nominatim fallback — always available, no key needed
        this._mode = "nominatim";
        this._attachDropdown(input);
    }

    // ── Dropdown shell (shared) ───────────────────────────────────────────────

    _attachDropdown(input) {
        input.setAttribute("autocomplete", "off");

        const ul = document.createElement("ul");
        ul.className = "list-group position-absolute shadow";
        ul.style.cssText = [
            "z-index:10000",
            "display:none",
            "max-height:320px",
            "overflow-y:auto",
            "top:100%",
            "left:0",
            "min-width:360px",
            "width:max-content",
            "max-width:540px",
            "margin:2px 0 0",
            "padding:0",
            "border-radius:4px",
            "background:#fff",
            "border:1px solid rgba(0,0,0,.15)",
        ].join(";");

        const wrapper = input.closest(".o_field_widget") || input.parentElement;
        wrapper.style.position = "relative";
        wrapper.appendChild(ul);

        let debounce = null;

        const onInput = () => {
            clearTimeout(debounce);
            const q = input.value.trim();
            if (q.length < 3) { ul.style.display = "none"; return; }
            debounce = setTimeout(() => this._query(q, ul, input), 400);
        };

        const onKeydown  = (e) => { if (e.key === "Escape") ul.style.display = "none"; };
        const onDocClick = (e) => { if (!ul.contains(e.target) && e.target !== input) ul.style.display = "none"; };

        input.addEventListener("input",   onInput);
        input.addEventListener("keydown", onKeydown);
        document.addEventListener("click", onDocClick);

        this._cleanup = () => {
            clearTimeout(debounce);
            input.removeEventListener("input",   onInput);
            input.removeEventListener("keydown", onKeydown);
            document.removeEventListener("click", onDocClick);
            ul.remove();
        };
    }

    _query(q, ul, input) {
        if (this._mode === "google") this._queryGoogle(q, ul, input);
        else                         this._queryNominatim(q, ul, input);
    }

    // ── Google Places backend ─────────────────────────────────────────────────

    _queryGoogle(query, ul, input) {
        this._acService.getPlacePredictions(
            { input: query, types: ["establishment"] },
            (predictions, status) => {
                if (!this._mounted) return;
                const OK = window.google.maps.places.PlacesServiceStatus.OK;
                if (status !== OK || !predictions?.length) { ul.style.display = "none"; return; }

                ul.innerHTML = "";
                predictions.slice(0, 7).forEach(pred => {
                    const main = pred.structured_formatting?.main_text || pred.description;
                    const sub  = pred.structured_formatting?.secondary_text || "";
                    const li = _makeLi(main, sub);
                    li.addEventListener("mousedown", (e) => {
                        e.preventDefault();
                        ul.style.display = "none";
                        this._googleDetails(pred.place_id, main, input);
                    });
                    ul.appendChild(li);
                });
                ul.style.display = "block";
            }
        );
    }

    _googleDetails(placeId, fallbackName, input) {
        this._plService.getDetails(
            {
                placeId,
                fields: ["name","address_components","formatted_phone_number",
                         "international_phone_number","website"],
            },
            (place, status) => {
                if (!this._mounted) return;
                const OK = window.google.maps.places.PlacesServiceStatus.OK;
                const name = (status === OK && place?.name) ? place.name : fallbackName;
                _setInput(input, name);
                if (status === OK && place) {
                    const p = parseGoogleComponents(place.address_components || []);
                    this._commit({
                        phone:   place.formatted_phone_number || place.international_phone_number || "",
                        website: (place.website || "").replace(/\/$/, ""),
                        street:  p.street, city: p.city, zip: p.zip,
                        stateCode: p.stateCode, countryCode: p.countryCode,
                    });
                }
            }
        );
    }

    // ── Nominatim backend ─────────────────────────────────────────────────────

    async _queryNominatim(query, ul, input) {
        try {
            const url =
                "https://nominatim.openstreetmap.org/search" +
                `?q=${encodeURIComponent(query)}` +
                "&format=json&limit=7&addressdetails=1&countrycodes=us,ca";
            const resp = await fetch(url, { headers: { "Accept-Language": "en" } });
            if (!resp.ok || !this._mounted) { ul.style.display = "none"; return; }
            const results = await resp.json();

            // Filter: skip pure geographic results (roads, rivers, forests…)
            const skipClass = new Set(["highway","natural","waterway","boundary","landuse"]);
            const items = results.filter(r => !skipClass.has(r.class)).slice(0, 7);

            if (!items.length) { ul.style.display = "none"; return; }

            ul.innerHTML = "";
            items.forEach(result => {
                const p = parseNominatim(result);
                const sub = [p.city, p.stateCode].filter(Boolean).join(", ");
                const li = _makeLi(p.name || result.display_name, sub);
                li.addEventListener("mousedown", (e) => {
                    e.preventDefault();
                    ul.style.display = "none";
                    _setInput(input, p.name || result.display_name.split(",")[0].trim());
                    this._commit({
                        phone: "", website: "",
                        street: p.street, city: p.city, zip: p.zip,
                        stateCode: p.stateCode, countryCode: p.countryCode,
                    });
                });
                ul.appendChild(li);
            });
            ul.style.display = "block";
        } catch (_) {
            ul.style.display = "none";
        }
    }

    // ── Shared commit ─────────────────────────────────────────────────────────

    async _commit(data) {
        const update = {};

        if (data.phone   && this.props.phoneField)   update[this.props.phoneField]   = data.phone;
        if (data.website && this.props.websiteField) update[this.props.websiteField] = data.website;
        if (data.street  && this.props.streetField)  update[this.props.streetField]  = data.street;
        if (data.city    && this.props.cityField)    update[this.props.cityField]    = data.city;
        if (data.zip     && this.props.zipField)     update[this.props.zipField]     = data.zip;

        const cc = (data.countryCode || "").toUpperCase();
        if (this.props.countryField && cc) {
            try {
                const countries = await this.orm.searchRead(
                    "res.country", [["code", "=", cc]], ["id", "name"], { limit: 1 }
                );
                if (countries.length) {
                    update[this.props.countryField] = { id: countries[0].id, display_name: countries[0].name };
                    const sc = (data.stateCode || "").toUpperCase();
                    if (this.props.stateField && sc) {
                        const states = await this.orm.searchRead(
                            "res.country.state",
                            [["code", "=", sc], ["country_id", "=", countries[0].id]],
                            ["id", "name"],
                            { limit: 1 }
                        );
                        if (states.length) {
                            update[this.props.stateField] = { id: states[0].id, display_name: states[0].name };
                        }
                    }
                }
            } catch (_) {}
        }

        if (!this._mounted) return;
        if (Object.keys(update).length) this.props.record.update(update);
    }
}

// ── DOM helpers ───────────────────────────────────────────────────────────────

function _makeLi(main, secondary) {
    const li = document.createElement("li");
    li.className = "list-group-item list-group-item-action py-2 px-3";
    li.style.cursor = "pointer";
    li.innerHTML =
        `<div style="font-size:.9em;font-weight:600">${_esc(main)}</div>` +
        (secondary ? `<div style="font-size:.78em;color:#6c757d">${_esc(secondary)}</div>` : "");
    return li;
}

function _setInput(input, value) {
    input.value = value || "";
    input.dispatchEvent(new Event("input",  { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
}

function _esc(s) {
    return String(s || "")
        .replace(/&/g, "&amp;").replace(/</g, "&lt;")
        .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ── Registration ──────────────────────────────────────────────────────────────

export const googleCompanyField = {
    component: GoogleCompanyField,
    displayName: "Company Autocomplete (Google / OSM)",
    supportedTypes: ["char"],
    extractProps: ({ attrs, options }) => ({
        isPassword: false,
        dynamicPlaceholder: false,
        dynamicPlaceholderModelReferenceField: "",
        autocomplete: "off",
        placeholder: attrs.placeholder,
        placeholderField: options?.placeholder_field,
        phoneField:   options?.phone_field   || "",
        websiteField: options?.website_field || "",
        streetField:  options?.street_field  || "",
        cityField:    options?.city_field    || "",
        zipField:     options?.zip_field     || "",
        stateField:   options?.state_field   || "",
        countryField: options?.country_field || "",
    }),
};

registry.category("fields").add("google_company", googleCompanyField);
