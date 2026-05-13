import re
import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# Maps first letter of Canadian postal code → province IFTA code
_CA_POSTAL_MAP = {
    'A': 'NL', 'B': 'NS', 'C': 'PE', 'E': 'NB',
    'G': 'QC', 'H': 'QC', 'J': 'QC',
    'K': 'ON', 'L': 'ON', 'M': 'ON', 'N': 'ON', 'P': 'ON',
    'R': 'MB', 'S': 'SK', 'T': 'AB', 'V': 'BC',
    'X': 'NT', 'Y': 'YT',
}

_CA_PROVINCE_NAMES = {
    'ONTARIO': 'ON', 'ALBERTA': 'AB', 'BRITISH COLUMBIA': 'BC',
    'MANITOBA': 'MB', 'SASKATCHEWAN': 'SK', 'QUÉBEC': 'QC', 'QUEBEC': 'QC',
    'NOVA SCOTIA': 'NS', 'NEW BRUNSWICK': 'NB',
    'NEWFOUNDLAND AND LABRADOR': 'NL', 'NEWFOUNDLAND': 'NL',
    'PRINCE EDWARD ISLAND': 'PE', 'NORTHWEST TERRITORIES': 'NT',
    'YUKON': 'YT', 'NUNAVUT': 'NU',
}

_CA_ABBREV_SET = set(_CA_PROVINCE_NAMES.values())

_US_STATE_NAMES = {
    'ALABAMA': 'AL', 'ALASKA': 'AK', 'ARIZONA': 'AZ', 'ARKANSAS': 'AR',
    'CALIFORNIA': 'CA', 'COLORADO': 'CO', 'CONNECTICUT': 'CT', 'DELAWARE': 'DE',
    'FLORIDA': 'FL', 'GEORGIA': 'GA', 'HAWAII': 'HI', 'IDAHO': 'ID',
    'ILLINOIS': 'IL', 'INDIANA': 'IN', 'IOWA': 'IA', 'KANSAS': 'KS',
    'KENTUCKY': 'KY', 'LOUISIANA': 'LA', 'MAINE': 'ME', 'MARYLAND': 'MD',
    'MASSACHUSETTS': 'MA', 'MICHIGAN': 'MI', 'MINNESOTA': 'MN', 'MISSISSIPPI': 'MS',
    'MISSOURI': 'MO', 'MONTANA': 'MT', 'NEBRASKA': 'NE', 'NEVADA': 'NV',
    'NEW HAMPSHIRE': 'NH', 'NEW JERSEY': 'NJ', 'NEW MEXICO': 'NM', 'NEW YORK': 'NY',
    'NORTH CAROLINA': 'NC', 'NORTH DAKOTA': 'ND', 'OHIO': 'OH', 'OKLAHOMA': 'OK',
    'OREGON': 'OR', 'PENNSYLVANIA': 'PA', 'RHODE ISLAND': 'RI',
    'SOUTH CAROLINA': 'SC', 'SOUTH DAKOTA': 'SD', 'TENNESSEE': 'TN', 'TEXAS': 'TX',
    'UTAH': 'UT', 'VERMONT': 'VT', 'VIRGINIA': 'VA', 'WASHINGTON': 'WA',
    'WEST VIRGINIA': 'WV', 'WISCONSIN': 'WI', 'WYOMING': 'WY',
}
_US_ABBREV_SET = set(_US_STATE_NAMES.values())

IFTA_JURISDICTION_SELECTION = [
    ('ON', 'ON – Ontario'),
    ('AB', 'AB – Alberta'),
    ('BC', 'BC – British Columbia'),
    ('MB', 'MB – Manitoba'),
    ('SK', 'SK – Saskatchewan'),
    ('QC', 'QC – Quebec'),
    ('NS', 'NS – Nova Scotia'),
    ('NB', 'NB – New Brunswick'),
    ('NL', 'NL – Newfoundland and Labrador'),
    ('PE', 'PE – Prince Edward Island'),
    ('NT', 'NT – Northwest Territories'),
    ('YT', 'YT – Yukon'),
    ('NU', 'NU – Nunavut'),
    # US jurisdictions (most common for Canadian trucking)
    ('AL', 'AL – Alabama'), ('AZ', 'AZ – Arizona'), ('AR', 'AR – Arkansas'),
    ('CA', 'CA – California'), ('CO', 'CO – Colorado'), ('CT', 'CT – Connecticut'),
    ('DE', 'DE – Delaware'), ('FL', 'FL – Florida'), ('GA', 'GA – Georgia'),
    ('ID', 'ID – Idaho'), ('IL', 'IL – Illinois'), ('IN', 'IN – Indiana'),
    ('IA', 'IA – Iowa'), ('KS', 'KS – Kansas'), ('KY', 'KY – Kentucky'),
    ('LA', 'LA – Louisiana'), ('ME', 'ME – Maine'), ('MD', 'MD – Maryland'),
    ('MA', 'MA – Massachusetts'), ('MI', 'MI – Michigan'), ('MN', 'MN – Minnesota'),
    ('MS', 'MS – Mississippi'), ('MO', 'MO – Missouri'), ('MT', 'MT – Montana'),
    ('NE', 'NE – Nebraska'), ('NV', 'NV – Nevada'), ('NH', 'NH – New Hampshire'),
    ('NJ', 'NJ – New Jersey'), ('NM', 'NM – New Mexico'), ('NY', 'NY – New York'),
    ('NC', 'NC – North Carolina'), ('ND', 'ND – North Dakota'), ('OH', 'OH – Ohio'),
    ('OK', 'OK – Oklahoma'), ('OR', 'OR – Oregon'), ('PA', 'PA – Pennsylvania'),
    ('SC', 'SC – South Carolina'), ('SD', 'SD – South Dakota'), ('TN', 'TN – Tennessee'),
    ('TX', 'TX – Texas'), ('UT', 'UT – Utah'), ('VT', 'VT – Vermont'),
    ('VA', 'VA – Virginia'), ('WA', 'WA – Washington'), ('WV', 'WV – West Virginia'),
    ('WI', 'WI – Wisconsin'), ('WY', 'WY – Wyoming'),
    ('', 'Unknown'),
]


class PremafirmIFTAFuelLog(models.Model):
    _name = 'premafirm.ifta.fuel.log'
    _description = 'IFTA Fuel Purchase Log'
    _order = 'purchase_date desc, id desc'
    _rec_name = 'display_name'

    display_name = fields.Char(compute='_compute_display_name', store=True)
    account_move_id = fields.Many2one('account.move', 'Vendor Bill', ondelete='set null', index=True)
    purchase_date   = fields.Date('Purchase Date', required=True, index=True)
    vendor_id       = fields.Many2one('res.partner', 'Vendor')

    # Station info from receipt (may differ from vendor's registered address)
    station_name        = fields.Char('Station Name')
    station_address     = fields.Char('Station Street')
    station_city        = fields.Char('City')
    station_province    = fields.Char('Province/State (raw)')
    station_postal_code = fields.Char('Postal Code')
    country_code        = fields.Selection([('CA', 'Canada'), ('US', 'United States')],
                                           default='CA', string='Country')

    # IFTA jurisdiction (computed / manually correctable)
    ifta_jurisdiction = fields.Selection(
        IFTA_JURISDICTION_SELECTION, string='IFTA Jurisdiction',
        index=True, help='2-letter province/state code used in IFTA reporting.')
    jurisdiction_source = fields.Selection([
        ('receipt',  'Receipt text'),
        ('postal',   'Postal code'),
        ('ai',       'AI lookup'),
        ('manual',   'Manual'),
    ], string='Jurisdiction Source', readonly=True)

    fuel_type = fields.Selection([
        ('diesel',   'Diesel'),
        ('gasoline', 'Gasoline'),
        ('def',      'DEF'),
        ('other',    'Other'),
    ], default='diesel', required=True)
    fuel_litres  = fields.Float('Litres',      digits=(12, 3))
    fuel_gallons = fields.Float('US Gallons',  digits=(12, 3),
                                compute='_compute_gallons', store=True)
    price_per_litre   = fields.Float('Price/L',  digits=(12, 4))
    amount_before_tax = fields.Float('Net Amount',  digits=(12, 2))
    tax_amount        = fields.Float('Tax Amount',  digits=(12, 2))
    total_amount      = fields.Float('Total Amount', digits=(12, 2))

    quarter = fields.Char('IFTA Quarter', compute='_compute_quarter', store=True,
                          help='e.g. 2026-Q2')
    notes   = fields.Text('Notes')

    @api.depends('purchase_date', 'vendor_id', 'ifta_jurisdiction', 'fuel_litres')
    def _compute_display_name(self):
        for r in self:
            parts = []
            if r.purchase_date:
                parts.append(r.purchase_date.strftime('%Y-%m-%d'))
            if r.vendor_id:
                parts.append(r.vendor_id.name)
            if r.ifta_jurisdiction:
                parts.append(r.ifta_jurisdiction)
            if r.fuel_litres:
                parts.append(f'{r.fuel_litres:.1f}L')
            r.display_name = ' · '.join(parts) if parts else 'IFTA Entry'

    @api.depends('fuel_litres')
    def _compute_gallons(self):
        for r in self:
            r.fuel_gallons = round(r.fuel_litres / 3.78541, 3) if r.fuel_litres else 0.0

    @api.depends('purchase_date')
    def _compute_quarter(self):
        for r in self:
            if r.purchase_date:
                q = (r.purchase_date.month - 1) // 3 + 1
                r.quarter = f'{r.purchase_date.year}-Q{q}'
            else:
                r.quarter = ''

    # ── Province detection ─────────────────────────────────────────

    @api.model
    def detect_jurisdiction(self, province_hint='', postal_code='', ocr_text='', address_line=''):
        """
        Detect the IFTA jurisdiction code from available data.
        Returns (jurisdiction_code, country_code, source) e.g. ('ON', 'CA', 'receipt').
        Falls back to GPT if everything else fails.
        """
        # 1. Province name explicitly given (from AI extraction field)
        if province_hint:
            ph = province_hint.upper().strip()
            if ph in _CA_PROVINCE_NAMES:
                return _CA_PROVINCE_NAMES[ph], 'CA', 'receipt'
            if ph in _CA_ABBREV_SET:
                return ph, 'CA', 'receipt'
            if ph in _US_STATE_NAMES:
                return _US_STATE_NAMES[ph], 'US', 'receipt'
            if ph in _US_ABBREV_SET:
                return ph, 'US', 'receipt'

        # 2. Postal code first-letter mapping (Canadian)
        pc = re.sub(r'\s+', '', postal_code or '').upper()
        if pc and pc[0].isalpha() and pc[0] in _CA_POSTAL_MAP:
            return _CA_POSTAL_MAP[pc[0]], 'CA', 'postal'

        # 3. Scan OCR text for province names (longest match first to avoid 'NEW' matching 'NEW YORK')
        if ocr_text:
            text_up = ocr_text.upper()
            for name in sorted(_CA_PROVINCE_NAMES, key=len, reverse=True):
                if name in text_up:
                    return _CA_PROVINCE_NAMES[name], 'CA', 'receipt'
            for name in sorted(_US_STATE_NAMES, key=len, reverse=True):
                if name in text_up:
                    return _US_STATE_NAMES[name], 'US', 'receipt'
            # Also scan for 2-letter abbreviations preceded by newline/space (e.g. "OAKVILLE\nONTARIO" or "ON L6H")
            for abbrev in _CA_ABBREV_SET:
                if re.search(rf'\b{abbrev}\b', text_up):
                    return abbrev, 'CA', 'receipt'

        # 4. AI fallback — ask GPT to identify province from address
        if address_line:
            return self._ai_province_lookup(address_line)

        return '', 'CA', 'manual'

    def _ai_province_lookup(self, address_line):
        """Ask GPT to identify the Canadian province or US state from an address string."""
        try:
            engine = self.env['premafirm.ml.engine']
            system = (
                'You are a Canadian address expert. '
                'Given an address, reply ONLY with a JSON object: '
                '{"jurisdiction":"XX","country":"CA"} where XX is the 2-letter '
                'province code (ON, AB, BC, MB, SK, QC, NS, NB, NL, PE, NT, YT, NU) '
                'or US state abbreviation. country is "CA" or "US". '
                'If you cannot determine it, return {"jurisdiction":"","country":"CA"}.'
            )
            raw, err = engine._gpt(system, address_line, max_tokens=30)
            if err or not raw:
                return '', 'CA', 'ai'
            import json as _json
            data = _json.loads(raw.strip())
            return data.get('jurisdiction', ''), data.get('country', 'CA'), 'ai'
        except Exception as e:
            _logger.warning('IFTA AI province lookup failed: %s', e)
            return '', 'CA', 'ai'

    # ── Factory ────────────────────────────────────────────────────

    @api.model
    def create_from_bill_data(self, bill, extracted_data, ocr_text=''):
        """
        Create an IFTA fuel log entry from a scanned bill and its extracted JSON.
        Called automatically by documents_ml.py after a fuel bill is created.
        """
        d = extracted_data or {}

        # Only create if it looks like a fuel purchase
        line_items = d.get('line_items') or []
        fuel_line = None
        for item in line_items:
            desc = (item.get('description') or '').upper()
            acc  = (item.get('suggested_account_code') or '')
            if any(k in desc for k in ('FUEL', 'DIESEL', 'GAS', 'PETRO', 'DEF')) \
               or acc.startswith('6102'):
                fuel_line = item
                break

        if not fuel_line and not any(
            (item.get('suggested_account_code') or '').startswith('610') for item in line_items
        ):
            return None  # not a fuel bill

        fuel_type_raw = (d.get('fuel_type') or '').upper()
        if 'DIESEL' in fuel_type_raw:
            fuel_type = 'diesel'
        elif any(k in fuel_type_raw for k in ('GAS', 'PETROL', 'UNLEADED', 'REGULAR', 'PREMIUM')):
            fuel_type = 'gasoline'
        elif 'DEF' in fuel_type_raw:
            fuel_type = 'def'
        else:
            fuel_type = 'diesel'  # trucking default

        # Fuel amounts
        qty        = float((fuel_line or {}).get('quantity') or d.get('fuel_litres') or 0)
        unit_price = float((fuel_line or {}).get('unit_price') or 0)
        subtotal   = float(d.get('subtotal') or (fuel_line or {}).get('amount') or 0)
        tax_amt    = float(d.get('tax_amount') or 0)
        total      = float(d.get('total_amount') or 0)

        # Station address fields from extracted data
        station_name   = (d.get('station_name') or d.get('vendor_name') or '')[:100]
        station_addr   = (d.get('station_address') or '')[:200]
        station_city   = (d.get('station_city') or '')[:100]
        station_prov   = (d.get('station_province') or '')[:100]
        station_postal = (d.get('station_postal_code') or '')[:20]

        # Build a combined address line for AI fallback
        address_line = ' '.join(filter(None, [
            station_addr, station_city, station_prov, station_postal]))

        juris, country, source = self.detect_jurisdiction(
            province_hint=station_prov,
            postal_code=station_postal,
            ocr_text=ocr_text,
            address_line=address_line,
        )

        vals = {
            'account_move_id':    bill.id,
            'purchase_date':      bill.invoice_date or fields.Date.today(),
            'vendor_id':          bill.partner_id.id if bill.partner_id else False,
            'station_name':       station_name,
            'station_address':    station_addr,
            'station_city':       station_city,
            'station_province':   station_prov,
            'station_postal_code': station_postal,
            'country_code':       country or 'CA',
            'ifta_jurisdiction':  juris,
            'jurisdiction_source': source,
            'fuel_type':          fuel_type,
            'fuel_litres':        qty,
            'price_per_litre':    unit_price,
            'amount_before_tax':  subtotal,
            'tax_amount':         tax_amt,
            'total_amount':       total,
        }
        try:
            log = self.create(vals)
            _logger.info('IFTA log created: id=%s jurisdiction=%s source=%s', log.id, juris, source)
            return log
        except Exception as e:
            _logger.warning('IFTA log creation failed: %s', e)
            return None
