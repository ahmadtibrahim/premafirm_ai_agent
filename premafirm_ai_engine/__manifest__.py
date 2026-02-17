{
    "name": "PremaFirm AI Engine",
    "version": "1.1",
    "summary": "AI Pricing + Routing Engine for PremaFirm Logistics",
    "author": "PremaFirm",
    "depends": ["crm", "sale_management", "mail", "fleet"],
    "data": [
        "security/ir.model.access.csv",
        "views/crm_view.xml",
        "views/multi_stop_views.xml",
        "views/fleet_vehicle_views.xml",
    ],
    "installable": True,
    "application": False,
}
