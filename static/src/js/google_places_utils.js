/** @odoo-module **/

import { loadGoogleMaps } from "@prema_dispatch/js/google_maps_loader";

// Kept as the public API used by the AI-engine widgets, but now delegates to
// the canonical prema_dispatch loader so the Places API is only ever
// injected once across the codebase.
export async function loadGooglePlaces(apiKey) {
    return loadGoogleMaps(apiKey, { libraries: "places" })
        .then(() => true)
        .catch(() => false);
}

export function parseGoogleComponents(components) {
    const r = { streetNumber: "", route: "", city: "", zip: "", stateCode: "", countryCode: "" };
    for (const c of (components || [])) {
        const t = c.types;
        if (t.includes("street_number"))                        r.streetNumber = c.long_name;
        else if (t.includes("route"))                           r.route = c.long_name;
        else if (t.includes("locality"))                        r.city = c.long_name;
        else if (t.includes("postal_town") && !r.city)         r.city = c.long_name;
        else if (t.includes("sublocality_level_1") && !r.city) r.city = c.long_name;
        else if (t.includes("administrative_area_level_1"))     r.stateCode = c.short_name;
        else if (t.includes("postal_code"))                     r.zip = c.long_name;
        else if (t.includes("country"))                         r.countryCode = c.short_name;
    }
    r.street = [r.streetNumber, r.route].filter(Boolean).join(" ");
    return r;
}
