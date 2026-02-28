/** @odoo-module **/

import { Component, useState } from "@odoo/owl";

export class AIChatUpload extends Component {
    setup() {
        this.state = useState({
            dragging: false,
            uploading: false,
            progress: 0,
            files: [],
            done: 0,
            total: 0,
        });
    }

    onDragOver(ev) {
        ev.preventDefault();
        this.state.dragging = true;
    }

    onDragLeave(ev) {
        ev.preventDefault();
        this.state.dragging = false;
    }

    async onDrop(ev) {
        ev.preventDefault();
        this.state.dragging = false;
        const files = ev.dataTransfer.files;
        await this.uploadFiles(files);
    }

    async onFileSelect(ev) {
        const files = ev.target.files;
        await this.uploadFiles(files);
        ev.target.value = "";
    }

    onAttachClick() {
        this.refs.fileInput.click();
    }

    _isImage(mimetype) {
        return (mimetype || "").startsWith("image/");
    }

    async uploadFiles(fileList) {
        if (!fileList || !fileList.length || !this.props.sessionId) {
            return;
        }

        this.state.uploading = true;
        this.state.total = fileList.length;
        this.state.done = 0;
        this.state.progress = 0;
        this.state.files = [];

        for (const file of fileList) {
            this.state.files.push({ name: file.name, progress: 0, status: "uploading", attachment_id: false, mimetype: file.type });
            const index = this.state.files.length - 1;

            const formData = new FormData();
            formData.append("file", file);
            formData.append("session_id", this.props.sessionId);

            await new Promise((resolve, reject) => {
                const xhr = new XMLHttpRequest();
                xhr.open("POST", "/prema_ai/upload_multi");
                xhr.withCredentials = true;

                xhr.upload.onprogress = (event) => {
                    if (event.lengthComputable) {
                        this.state.files[index].progress = Math.round((event.loaded / event.total) * 100);
                        const overall = ((this.state.done + (event.loaded / event.total)) / this.state.total) * 100;
                        this.state.progress = Math.round(overall);
                    }
                };

                xhr.onload = () => {
                    if (xhr.status >= 200 && xhr.status < 300) {
                        const result = JSON.parse(xhr.responseText);
                        this.state.files[index].attachment_id = result.attachment_id;
                        this.state.files[index].mimetype = result.mimetype;
                        this.state.files[index].name = result.name;
                        this.state.files[index].progress = 100;
                        this.state.files[index].status = "done";
                        this.state.done += 1;
                        this.state.progress = Math.round((this.state.done / this.state.total) * 100);
                        resolve();
                        return;
                    }
                    this.state.files[index].status = "error";
                    reject(new Error(xhr.responseText || "Upload failed"));
                };

                xhr.onerror = () => {
                    this.state.files[index].status = "error";
                    reject(new Error("Network error"));
                };

                xhr.send(formData);
            });
        }

        this.state.progress = 100;
        this.state.uploading = false;
        if (this.props.onUploaded) {
            this.props.onUploaded();
        }
    }
}

AIChatUpload.template = "prema_ai_auditor.AIChatUpload";
