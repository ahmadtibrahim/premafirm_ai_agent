# STRUCTURAL AUDIT REPORT

## Missing models (action res_model not found)
- None

## Missing fields
| file | model | field | source |
|---|---|---|---|
| - | - | - | None |

## Missing actions
- None

## Invalid references
- None

## Wrong load order
- None

## Security gaps
- None

## Odoo 18 list view migration
- Any remaining `<tree>` architecture tags and `view_mode=tree` must be migrated to `<list>` and `view_mode=list`.

## Proposed fixes
1. Replace legacy tree declarations with list views in XML and act_window view_mode.
2. Reorder manifest data load to strict production-safe sequence.
3. Add missing access rights entries for local models where required.
