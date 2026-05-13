/** @odoo-module **/

const GOOGLE_SCRIPT_ID = "premafirm-google-places-api";

export async function loadGooglePlaces(apiKey) {
    if (window.google?.maps?.places) return true;
    return new Promise((resolve) => {
        const poll = () => {
            let tries = 0;
            const t = setInterval(() => {
                if (window.google?.maps?.places) { clearInterval(t); resolve(true); return; }
                if (++tries > 60) { clearInterval(t); resolve(false); }
            }, 200);
        };
        if (document.getElementById(GOOGLE_SCRIPT_ID)) { poll(); return; }
        const s = document.createElement("script");
        s.id = GOOGLE_SCRIPT_ID;
        s.async = true;
        s.src = `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(apiKey)}&libraries=places`;
        s.onload = poll;
        s.onerror = () => resolve(false);
        document.head.appendChild(s);
    });
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
