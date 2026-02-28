# Crash Risk Report

## Python
- Potential import path hazards:
- None

- Bare except blocks:
- None

## JS/OWL
- RPC route references discovered:
- `static/src/components/ai_chat_upload/ai_chat_upload.js:69` `xhr.open("POST", "/prema_ai/upload_multi");`
- `static/src/js/chat.js:43` `const session = await this.rpc("/prema_ai/session", {});`
- `static/src/js/chat.js:70` `const response = await this.rpc("/prema_ai/chat", {`
- `static/src/js/chat.js:105` `const response = await this.rpc("/prema_ai/document_summary", {`
- `static/src/js/chat.js:125` `this.state.incidents = await this.rpc("/prema_ai/incidents", { limit: 20 });`
- `static/src/js/chat.js:132` `const response = await this.rpc("/prema_ai/schema_model", { model_name: this.state.schemaModel });`
- `static/src/js/chat.js:137` `await this.rpc("/prema_ai/create_drafts", {`
- `static/src/js/chat.js:146` `await this.rpc("/prema_ai/create_drafts", {`

## XML
- Validate view inheritance and external ids during module upgrade (`-u prema_ai_auditor`).
