# PremaFirm AI Engine — Module Manual

## Overview
Odoo 18 AI-powered logistics module for PremaFirm Inc.

## Core Features
- AI invoice generation from attachments and WhatsApp text
- Prema AI Estimator: trip cost, route, fuel, truck selection
- Dispatch job creation from invoices with trip sheet parsing
- CRM AI assistant (outreach, follow-ups, lead management)
- ML knowledge base for learning from past invoices and jobs
- Geotab ELD integration for GPS truck tracking
- IFTA fuel reporting

## Dispatch System
Dispatch jobs are managed via the Prema AI Estimator (premafirm.rate.estimator).
Prema Dispatch (native Odoo dispatch pipeline) is under development.

## Module Structure
```
models/
  ├── account_move_extension.py   ← Invoice AI + dispatch job creation
  ├── rate_estimator.py           ← Trip cost engine + job management
  ├── estimator_stop.py           ← Per-stop records
  ├── crm_ai_assistant.py         ← CRM AI features
  ├── ml_engine.py                ← ML knowledge base
  ├── geotab_settings.py          ← Geotab GPS integration
  └── ...
services/
  ├── invoice_ai_service.py       ← Invoice scanning + AI extraction
  ├── openai_utils.py             ← OpenAI API wrapper
  ├── mapbox_service.py           ← Geocoding + routing
  └── geotab_service.py           ← Geotab ELD API
```
