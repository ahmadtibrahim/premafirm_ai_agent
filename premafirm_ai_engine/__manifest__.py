# sftp://root@72.60.115.139/opt/odoo/custom-addons/premafirm_ai_engine/__manifest__.py
{
    "name": "PremaFirm AI Engine",
    "version": "1.3",
    "summary": "AI Pricing + Routing Engine for PremaFirm Logistics",
    "author": "PremaFirm",
    "license": "LGPL-3",
    "depends": [
        "crm",
        "sale_management",
        "mail",
        "fleet",
    ],
    "data": [
        "security/ir.model.access.csv",
        "views/crm_view.xml",
    ],
    "installable": True,
    "application": False,
}
