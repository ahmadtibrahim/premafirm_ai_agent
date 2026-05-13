/** @odoo-module **/
/**
 * Google Places / Nominatim company-search widget for Many2One (partner_id).
 *
 * Replaces the standard `res_partner_many2one` widget (partner_autocomplete IAP)
 * with a version that:
 *   1. Still searches existing Odoo partners (standard DB search preserved)
 *   2. Adds a second source: Google Places (if google_maps_api_key is set)
 *      OR Nominatim/OSM (free fallback, always active)
 *
 * On selection of a Google/OSM result, the widget creates (or finds) a
 * res.partner via `find_or_create_from_google_place` and sets partner_id.
 */

import { Many2XAutocomplete } from "@web/views/fields/relational_utils";
import { Many2OneField, many2OneField } from "@web/views/fields/many2one/many2one_field";
import { registry } from "@web/core/registry";
import { onMounted } from "@odoo/owl";
import { loadGooglePlaces, parseGoogleComponents } from "./google_places_utils";

// ── Nominatim address parser ─────────────────────────────────────────────────

function parseNominatimResult(result) {
    const a = result.address || {};
    const iso   = a["ISO3166-2-lvl4"] || "";
    const parts = iso.includes("-") ? iso.split("-") : [];
    return {
        name:        result.name || "",
        street:      [a.house_number, a.road].filter(Boolean).join(" "),
        city:        a.city || a.town || a.village || a.municipality || "",
        zip:         a.postcode || "",
        stateCode:   (parts[1] || "").toUpperCase(),
        countryCode: (parts[0] || a.country_code || "").toUpperCase(),
    };
}

// ── Inner autocomplete component ──────────────────────────────────────────────

class GooglePartnerAutocomplete extends Many2XAutocomplete {
    setup() {
        super.setup();
        this._mode      = null;   // "google" | "nominatim"
        this._acService = null;
        this._plService = null;
        onMounted(() => this._initGoogleService());
    }

    async _initGoogleService() {
        const googleKey = await this.orm.call(
            "ir.config_parameter", "get_param", ["google_maps_api_key"]
        );
        if (googleKey) {
            const loaded = await loadGooglePlaces(googleKey);
            if (loaded) {
                const attrDiv = document.createElement("div");
                this._acService = new window.google.maps.places.AutocompleteService();
                this._plService = new window.google.maps.places.PlacesService(attrDiv);
                this._mode = "google";
                return;
            }
        }
        this._mode = "nominatim";
    }

    // ── Sources ───────────────────────────────────────────────────────────────

    get sources() {
        return [
            this.optionsSource,         // existing Odoo partners (standard DB search)
            this._googlePlacesSource,   // Google Places / Nominatim
        ];
    }

    get _googlePlacesSource() {
        return {
            placeholder: "Searching companies…",
            options: (request) => {
                if (!this._mode || !request || request.length < 3) return [];
                if (this._mode === "google") return this._googleSuggestions(request);
                return this._nominatimSuggestions(request);
            },
            optionTemplate: "premafirm_ai_engine.GooglePlaceOption",
        };
    }

    // ── Google Places backend ─────────────────────────────────────────────────

    _googleSuggestions(query) {
        return new Promise((resolve) => {
            if (!this._acService) { resolve([]); return; }
            this._acService.getPlacePredictions(
                { input: query, types: ["establishment"] },
                (predictions, status) => {
                    const OK = window.google.maps.places.PlacesServiceStatus.OK;
                    if (status !== OK || !predictions?.length) { resolve([]); return; }
                    resolve(predictions.slice(0, 7).map((pred) => ({
                        id:          pred.place_id,
                        label:       pred.structured_formatting?.main_text || pred.description,
                        description: pred.structured_formatting?.secondary_text || "",
                        isFromGoogle: true,
                        mode:        "google",
                        placeId:     pred.place_id,
                    })));
                }
            );
        });
    }

    _fetchGoogleDetails(placeId) {
        return new Promise((resolve) => {
            if (!this._plService) { resolve(null); return; }
            this._plService.getDetails(
                {
                    placeId,
                    fields: ["name","address_components","formatted_phone_number",
                             "international_phone_number","website"],
                },
                (place, status) => {
                    const OK = window.google.maps.places.PlacesServiceStatus.OK;
                    if (status !== OK || !place) { resolve(null); return; }
                    const p = parseGoogleComponents(place.address_components || []);
                    resolve({
                        name:        place.name || "",
                        phone:       place.formatted_phone_number || place.international_phone_number || "",
                        website:     (place.website || "").replace(/\/$/, ""),
                        street:      p.street,
                        city:        p.city,
                        zip:         p.zip,
                        stateCode:   p.stateCode,
                        countryCode: p.countryCode,
                    });
                }
            );
        });
    }

    // ── Nominatim backend ─────────────────────────────────────────────────────

    async _nominatimSuggestions(query) {
        try {
            const url =
                "https://nominatim.openstreetmap.org/search" +
                `?q=${encodeURIComponent(query)}` +
                "&format=json&limit=7&addressdetails=1&countrycodes=us,ca";
            const resp = await fetch(url, { headers: { "Accept-Language": "en" } });
            if (!resp.ok) return [];
            const results = await resp.json();
            const skip = new Set(["highway","natural","waterway","boundary","landuse"]);
            return results
                .filter(r => !skip.has(r.class))
                .slice(0, 7)
                .map((r) => {
                    const p = parseNominatimResult(r);
                    return {
                        id:           String(r.place_id || Math.random()),
                        label:        p.name || r.display_name.split(",")[0].trim(),
                        description:  [p.city, p.stateCode].filter(Boolean).join(", "),
                        isFromGoogle: true,
                        mode:         "nominatim",
                        resolvedData: p,
                    };
                });
        } catch (_) {
            return [];
        }
    }

    // ── onSelect override ─────────────────────────────────────────────────────

    async onSelect(option, params) {
        if (!option.isFromGoogle) {
            return super.onSelect(option, params);
        }

        // Resolve full place data
        let data = option.resolvedData || {};
        if (option.mode === "google" && option.placeId) {
            const details = await this._fetchGoogleDetails(option.placeId);
            if (details) data = details;
        }

        // Create / find the partner on the server
        const result = await this.orm.call(
            "res.partner",
            "find_or_create_from_google_place",
            [],
            {
                name:         data.name || option.label,
                phone:        data.phone        || "",
                website:      data.website      || "",
                street:       data.street       || "",
                city:         data.city         || "",
                zip_code:     data.zip          || "",
                state_code:   data.stateCode    || "",
                country_code: data.countryCode  || "",
            }
        );

        if (!result?.id) return;

        // Set the Many2One field to the newly created / found partner
        return super.onSelect(
            { value: result.id, displayName: result.display_name },
            params
        );
    }
}

// ── Many2One field wrapper ────────────────────────────────────────────────────

class GooglePartnerMany2One extends Many2OneField {
    static components = {
        ...Many2OneField.components,
        Many2XAutocomplete: GooglePartnerAutocomplete,
    };
}

// Override the standard res_partner_many2one widget (replaces partner_autocomplete IAP)
registry.category("fields").add("res_partner_many2one", {
    ...many2OneField,
    component: GooglePartnerMany2One,
}, { force: true });
