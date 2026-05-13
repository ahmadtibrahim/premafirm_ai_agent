/** @odoo-module **/

import { CharField } from "@web/views/fields/char/char_field";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { onMounted, onWillUnmount } from "@odoo/owl";
import { loadGooglePlaces, parseGoogleComponents } from "./google_places_utils";

function parseMapboxFeature(feat) {
    const r = { street: "", city: "", zip: "", stateCode: "", countryCode: "" };
    const streetNum = feat.address || "";
    const streetName = feat.text || "";
    r.street = [streetNum, streetName].filter(Boolean).join(" ");
    for (const ctx of (feat.context || [])) {
        const id = ctx.id || "";
        if (id.startsWith("place."))    r.city = ctx.text;
        if (id.startsWith("postcode.")) r.zip = ctx.text;
        if (id.startsWith("region."))   r.stateCode = (ctx.short_code || "").split("-").pop();
        if (id.startsWith("country."))  r.countryCode = (ctx.short_code || "").toUpperCase();
    }
    return r;
}

// ── Widget ───────────────────────────────────────────────────────────────────

class GoogleAddressField extends CharField {
    static props = {
        ...CharField.props,
        latField:     { type: String, optional: true },
        lngField:     { type: String, optional: true },
        cityField:    { type: String, optional: true },
        zipField:     { type: String, optional: true },
        stateField:   { type: String, optional: true },
        countryField: { type: String, optional: true },
    };

    setup() {
        super.setup();
        this.orm = useService("orm");
        this._cleanup = null;
        this._mounted = false;

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
        const [googleKey, mapboxKey1, mapboxKey2] = await Promise.all([
            this.orm.call("ir.config_parameter", "get_param", ["google_maps_api_key"]),
            this.orm.call("ir.config_parameter", "get_param", ["mapbox.access_token"]),
            this.orm.call("ir.config_parameter", "get_param", ["mapbox_api_key"]),
        ]);
        const mapboxKey = mapboxKey1 || mapboxKey2;

        const input = this.input?.el;
        if (!input) return;

        if (googleKey) {
            const loaded = await loadGooglePlaces(googleKey);
            if (loaded) { this._attachGoogle(input); return; }
        }

        if (mapboxKey) {
            this._attachMapbox(input, mapboxKey);
        }
    }

    // ── Google Places ────────────────────────────────────────────────────────

    _attachGoogle(input) {
        input.setAttribute("autocomplete", "off");
        const ac = new window.google.maps.places.Autocomplete(input, {
            types: ["address"],
            fields: ["address_components", "formatted_address", "geometry"],
        });
        ac.addListener("place_changed", () => {
            const place = ac.getPlace();
            if (!place?.address_components) return;
            const parsed = parseGoogleComponents(place.address_components);
            this._commitParsed(input, parsed,
                place.geometry?.location?.lat(),
                place.geometry?.location?.lng());
        });
    }

    // ── Mapbox Geocoding ─────────────────────────────────────────────────────

    _attachMapbox(input, apiKey) {
        input.setAttribute("autocomplete", "off");

        const ul = document.createElement("ul");
        ul.className = "list-group position-absolute w-100 shadow-sm";
        ul.style.cssText = "z-index:10000;display:none;max-height:240px;overflow-y:auto;top:100%;left:0;margin:0;padding:0;border-radius:4px;";
        input.parentElement.style.position = "relative";
        input.insertAdjacentElement("afterend", ul);

        let debounce = null;

        const onInput = () => {
            clearTimeout(debounce);
            const q = input.value.trim();
            if (q.length < 3) { ul.style.display = "none"; return; }
            debounce = setTimeout(() => this._queryMapbox(q, apiKey, ul, input), 350);
        };

        const onDocClick = (e) => {
            if (!ul.contains(e.target) && e.target !== input) ul.style.display = "none";
        };

        input.addEventListener("input", onInput);
        document.addEventListener("click", onDocClick);

        this._cleanup = () => {
            clearTimeout(debounce);
            input.removeEventListener("input", onInput);
            document.removeEventListener("click", onDocClick);
            ul.remove();
        };
    }

    async _queryMapbox(query, apiKey, ul, input) {
        try {
            const url =
                `https://api.mapbox.com/geocoding/v5/mapbox.places/${encodeURIComponent(query)}.json` +
                `?access_token=${encodeURIComponent(apiKey)}&country=us,ca&types=address&autocomplete=true&limit=6`;
            const resp = await fetch(url);
            if (!resp.ok) { ul.style.display = "none"; return; }
            const features = (await resp.json()).features || [];
            ul.innerHTML = "";
            if (!features.length) { ul.style.display = "none"; return; }
            features.forEach(feat => {
                const li = document.createElement("li");
                li.className = "list-group-item list-group-item-action py-2 px-3";
                li.style.cursor = "pointer";
                li.textContent = feat.place_name;
                li.addEventListener("mousedown", (e) => {
                    e.preventDefault();
                    const [lng, lat] = feat.center || [];
                    const parsed = parseMapboxFeature(feat);
                    this._commitParsed(input, parsed, lat, lng);
                    ul.style.display = "none";
                });
                ul.appendChild(li);
            });
            ul.style.display = "block";
        } catch (_) {
            ul.style.display = "none";
        }
    }

    // ── Shared commit: fills all address fields ──────────────────────────────

    async _commitParsed(input, parsed, lat, lng) {
        // Update the street input synchronously so the field feels responsive
        const streetVal = parsed.street || "";
        input.value = streetVal;
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.dispatchEvent(new Event("change", { bubbles: true }));

        const update = {};

        if (lat != null && this.props.latField) update[this.props.latField] = lat;
        if (lng != null && this.props.lngField) update[this.props.lngField] = lng;
        if (this.props.cityField && parsed.city) update[this.props.cityField] = parsed.city;
        if (this.props.zipField  && parsed.zip)  update[this.props.zipField]  = parsed.zip;

        // Resolve country → state via Odoo ORM (keeps DB integrity)
        if (this.props.countryField && parsed.countryCode) {
            try {
                const countries = await this.orm.searchRead(
                    "res.country",
                    [["code", "=", parsed.countryCode.toUpperCase()]],
                    ["id", "name"],
                    { limit: 1 }
                );
                if (countries.length) {
                    update[this.props.countryField] = { id: countries[0].id, display_name: countries[0].name };

                    if (this.props.stateField && parsed.stateCode) {
                        const states = await this.orm.searchRead(
                            "res.country.state",
                            [
                                ["code", "=", parsed.stateCode.toUpperCase()],
                                ["country_id", "=", countries[0].id],
                            ],
                            ["id", "name"],
                            { limit: 1 }
                        );
                        if (states.length) {
                            update[this.props.stateField] = { id: states[0].id, display_name: states[0].name };
                        }
                    }
                }
            } catch (_) {
                // ORM lookup failed silently — street/city/zip still filled above
            }
        }

        if (!this._mounted) return;
        if (Object.keys(update).length) this.props.record.update(update);
    }
}

export const googleAddressField = {
    component: GoogleAddressField,
    displayName: "Address Autocomplete",
    supportedTypes: ["char"],
    extractProps: ({ attrs, options }) => ({
        isPassword: false,
        dynamicPlaceholder: false,
        dynamicPlaceholderModelReferenceField: "",
        autocomplete: "off",
        placeholder: attrs.placeholder,
        placeholderField: options?.placeholder_field,
        latField:     options?.lat_field     || "",
        lngField:     options?.lng_field     || "",
        cityField:    options?.city_field    || "",
        zipField:     options?.zip_field     || "",
        stateField:   options?.state_field   || "",
        countryField: options?.country_field || "",
    }),
};

registry.category("fields").add("google_address", googleAddressField);
