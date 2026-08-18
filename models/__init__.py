from . import crm_ai_assistant
from . import crm_followup
from . import sale_order_approval

# ML core (migrated from premafirm_ml)
from . import ml_knowledge
from . import ml_draft
from . import ml_engine
from . import ml_ingestion
from . import ifta_fuel_log

from . import business_profile
from . import ml_ingest_queue
from . import ml_orm_hooks
from . import ml_response_cache
from . import odoo_bot_kc
from . import crm_contact_rotation
from . import crm_outreach

from . import crm_lead_extension
from . import crm_reply_status  # PHASE 8 — reply-status fields (after
                                # crm_lead_extension: overrides its plain
                                # reply_received with the computed one)
from . import crm_lead_contacts  # PHASES 11-12 — company→contacts +
                                 # Freight Profile (after crm_lead_extension:
                                 # its create hook attaches senders)
from . import crm_pipeline  # PHASE 13 — pipeline restructure (after
                            # crm_reply_status: uses the reply flags)
from . import crm_activity_discipline  # PHASE 14 — activity/next-action
                                       # discipline (imported last among the
                                       # crm.lead extends so its create/write
                                       # hooks wrap the others)
from . import fleet_vehicle_extension
from . import sale_order_extension
from . import account_move_extension
from . import invoice_ai_product
from . import mail_compose_message
from . import mail_activity

from . import res_partner_extension

# Geotab / ELD integration
from . import fleet_odometer_extension
from . import fleet_daily_odometer
from . import fleet_driver_assignment
from . import fleet_driver_log
from . import fleet_trip_log
from . import premafirm_geotab_device
from . import fleet_geotab_link_wizard
from . import geotab_sync
from . import geotab_settings
from . import premafirm_geotab_driver
from . import contact_geotab_link_wizard

from . import estimator_stop
from . import rate_estimator
from . import premafirm_load
from . import ai_review_wizards
from . import dispatch_wizard
from . import ml_learning_hooks

from . import crm_bulk_email
from . import snov_contact

# ML model extensions (migrated from premafirm_ml)
from . import crm_lead_ml
from . import account_move_ml
from . import res_partner_ml
from . import sale_order_ml
from . import documents_ml
from . import whatsapp_account_ml
from . import prema_ai_session_ml

# Bill scan importer
from . import bill_scan_import

# Knowledge Center — tag-based shortcuts
from . import documents_kc

# Bank reconciliation currency fix
from . import account_bank_statement_line

# CRM bulk actions
from . import crm_bulk_assign

# Attendance AI daily summary + auto check-in/out + coaching
from . import attendance_summary
from . import auto_attendance
from . import staff_coaching
from . import mail_bot_fix

# PHASES 2-3 — canonical outbound threading + robust reply routing
from . import mail_threading_service
from . import mail_send_hooks
from . import inbound_routing

# PHASE 9 — AI provenance on mail.mail (imported after mail_send_hooks so
# the stamping method it calls exists at model-build time)
from . import mail_mail_provenance

# PHASES 6-7 — safe Fetch Now + inbound dedupe (imported LAST so its
# message_route override runs first in the MRO)
from . import fetchmail_safety
