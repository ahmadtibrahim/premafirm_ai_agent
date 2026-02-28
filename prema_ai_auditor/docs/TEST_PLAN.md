# Test Plan

## Unit Tests
- Config service getters: parameter precedence and masking behavior.
- Proposal lifecycle: draft -> pending_approval -> approved/rejected -> applied/failed.
- Mapping validator: model extraction and reference cross-check.

## Integration Tests
- Run mapping scan script against `/reference` exports.
- Simulate LLM timeout and ensure user gets sanitized `UserError`.
- Simulate bus delivery failure path and verify no worker crash.

## Performance Tests
- Large-domain searches for documents and audit logs with pagination.
- Detect N+1 patterns in session/document summarization flow.
