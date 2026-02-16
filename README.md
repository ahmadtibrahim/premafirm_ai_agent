# PREMAFIRM AI ENGINE
Odoo 18 – Integrated AI Pricing, Routing & Scheduling System
FTL / LTL – 26ft Straight Truck – $400 Net/Day Target

============================================================
OVERVIEW
============================================================

PremaFirm AI Engine is a fully embedded Odoo 18 module for
PremaFirm Logistics.

It eliminates manual quoting and load structuring by:

• Reading broker emails from CRM
• Extracting structured multi-stop freight data via AI
• Calculating truck-safe routing via Mapbox
• Running cost + profit logic (≥ $40/hour target)
• Drafting professional quote replies
• Preparing Sales Orders from PO
• Performing schedule + HOS checks
• Enforcing profit floor rules

No external app required.
Everything runs inside Odoo.


============================================================
CORE ARCHITECTURE
============================================================

MODULE NAME:
premafirm_ai_engine

EXTENDS:
crm.lead
sale.order

CUSTOM MODEL:
premafirm.load.stop

------------------------------------------------------------
LOAD STOP MODEL
------------------------------------------------------------

Fields:

- sequence (Integer)
- stop_type (Selection: pickup / delivery)
- address (Char)
- pallet_count (Integer)
- weight_lbs (Float)
- lead_id (Many2one → crm.lead)

Supports unlimited LTL patterns:

Pickup A – 4 pallets
Pickup B – 2 pallets
Deliver B
Pickup C – 4 pallets
Deliver A


============================================================
PHASE 1 – STRUCTURED CRM DATA
============================================================

Extended Fields on CRM Lead:

Pickup Address (char)
Delivery Address (char)
Pickup Datetime (datetime)
Delivery Datetime (datetime)
Number of Pallets (int)
Weight (lbs) (float)
Service Scope (selection: Local, Regional, Interstate, Crossborder)
Equipment Type (selection: Dry, Reefer)
Strict Appointment (boolean)
Distance KM (float)
Drive Hours (float)
Estimated Fuel Cost (float)
Estimated Total Cost (float)
Target Profit (float)
Suggested Rate (float)
AI Recommendation (text)

Multi-stop field:
load_stop_ids (One2many → premafirm.load.stop)


============================================================
PHASE 2 – MAPBOX ROUTING ENGINE
============================================================

When AI button is clicked:

1) AI extracts all pickup & delivery stops
2) Addresses are geocoded
3) Mapbox Directions API called with:

   Profile: driving-traffic
   Avoid: toll
   Height: 4.11m (13'6")
   Weight: 14969kg (~33,000 lbs GVWR truck legal)

Returns:
- Distance (meters)
- Duration (seconds)

Converted:
distance_km = meters / 1000
drive_hours = seconds / 3600

Stored on CRM.


============================================================
PHASE 3 – COST & PROFIT ENGINE
============================================================

Base Assumptions (configurable later):

Fuel economy: 3 km per liter
Fuel price: 1.60 CAD/L
Maintenance reserve: 0.25 CAD per km
Insurance allocation:
  - 50 CAD if drive > 4h
  - 25 CAD if local short
Factoring: 3%

CALCULATIONS:

fuel_cost = (distance_km / 3) * 1.60
maintenance_cost = distance_km * 0.25
insurance_daily = 50 if drive_hours > 4 else 25

base_cost = fuel_cost + maintenance_cost + insurance_daily

target_profit = drive_hours * 40

suggested_rate = base_cost + target_profit

All written to CRM automatically.


============================================================
PHASE 4 – AI EMAIL RESPONSE ENGINE
============================================================

Button:
"AI Price & Draft Reply"

Workflow:

1) Extract structured load data from email
2) Run route + cost engine
3) Send structured JSON to OpenAI:

{
  pickup: "",
  delivery: "",
  pallets: ,
  weight_lbs: ,
  distance_km: ,
  drive_hours: ,
  calculated_cost: ,
  target_profit: ,
  suggested_rate:
}

AI must:
- Confirm pricing logic
- Keep tone competitive
- Highlight same-day capability
- Avoid sounding greedy
- Return final recommended rate
- Generate professional email draft

System:
• Posts draft in chatter
• Does NOT auto-send


============================================================
PHASE 5 – PO RECEIVED WORKFLOW
============================================================

When PO received:

1) CRM stage → "Booked"
2) Click "Create Sales Order"
3) Sales Order auto-filled from CRM
4) Suggested Rate inserted
5) Load stops copied
6) PO number stored

Sales Order acts as Load Confirmation.


============================================================
PHASE 6 – SCHEDULING & HOS CHECK
============================================================

Before confirming Sales Order:

System checks:

• Existing confirmed loads
• Travel time between last drop & new pickup
• 13-hour Canada HOS rule
• 30-min break after 8h driving
• Delivery window strictness

If risk detected:

Popup:
"Schedule Risk – Delivery window tight"

AI suggests:
• Adjust pickup time
• Add buffer
• Increase rate due to risk


============================================================
PHASE 7 – DELIVERY & INVOICE FLOW
============================================================

After delivery:

1) Upload POD
2) Move stage → Delivered
3) Click Create Invoice
4) Attach POD automatically
5) Factoring workflow begins


============================================================
PHASE 8 – AI DECISION MODES
============================================================

MODE 1 – Advisory
AI suggests rate + draft only.

MODE 2 – Assisted Auto-Fill
AI fills Suggested Rate but requires approval.

MODE 3 – Strict Profit Guard
If profit < $400 per 10h equivalent,
System blocks confirmation unless overridden.


============================================================
SYSTEM PARAMETERS REQUIRED
============================================================

Odoo → Settings → Technical → System Parameters

openai.api_key = YOUR_OPENAI_KEY
mapbox.api.key = YOUR_MAPBOX_TOKEN


============================================================
TARGET OPERATION PROFILE
============================================================

Designed for:

• 26ft Straight Truck
• LTL / Multi-stop freight
• Ontario regional lanes
• Cross-border expansion ready
• Owner-operator profit protection
• Fully integrated inside Odoo


============================================================
FINAL WORKFLOW
============================================================

EMAIL RECEIVED
↓
CRM LEAD CREATED
↓
AI PRICE BUTTON CLICKED
↓
Stops Extracted
↓
Route Calculated (Truck-Safe)
↓
Cost + Profit Engine
↓
AI Draft Generated
↓
You Send Email
↓
PO Received
↓
Sales Order Created
↓
Schedule + HOS Check
↓
Dispatch
↓
Delivered
↓
Invoice + POD
↓
Factoring


============================================================
DESIGN PRINCIPLES
============================================================

• AI assists — does not control
• Profit floor enforced
• Truck-legal routing only
• Multi-stop native support
• No external automation tools
• Clean CRM → SO → Invoice pipeline


============================================================
RECOMMENDED BUTTON STRATEGY
============================================================

Start with ONE universal smart button:
"AI Price & Analyze"

Add separate specialized buttons later if needed.

