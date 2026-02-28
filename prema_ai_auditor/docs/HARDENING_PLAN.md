# Hardening Patch Plan

1. Centralize configuration through `prema.config.service` (ir.config_parameter -> env -> safe default).
2. Keep write operations gated behind proposal approval workflow (`prema.ai.proposal`).
3. Maintain read-only default path by generating proposals first for non-user initiated fixes.
4. Guard external LLM calls with timeout, retries, circuit breaker and sanitized user errors.
5. Add schema viewer/diff wizard to compare runtime registry vs reference exports.
6. Keep bus payload minimal and user-scoped via partner-targeted send.
7. Keep `/reference` folder immutable; use it only for mapping validation.
