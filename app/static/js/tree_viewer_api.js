/**
 * API helper for tree editing actions
 */
const TreeEditActions = {
    _getCsrfToken() {
        return document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
    },

    _buildHeaders(includeContentType = true) {
        const headers = {};
        if (includeContentType) headers['Content-Type'] = 'application/json';
        const token = this._getCsrfToken();
        if (token) headers['X-CSRFToken'] = token;
        return headers;
    },

    async getTreeState(jobId) {
        const response = await fetch(`/api/job/${jobId}/tree/state`, {
            cache: "no-store",
            credentials: "same-origin"
        });
        const text = await response.text();
        let data = {};
        try { data = JSON.parse(text); } catch (_) { /* non-JSON */ }

        if (!response.ok) {
            const err = new Error(data.error || data.message || `Failed to get tree state: ${response.status}`);
            err.details = data;
            err.raw = text;
            throw err;
        }
        return data;
    },

    async pruneTip(jobId, tipName) {
        // Legacy wrapper
        return this.pruneTaxa(jobId, [tipName]);
    },

    async pruneTaxa(jobId, tipNames) {
        const response = await fetch(`/api/job/${jobId}/tree/prune`, {
            method: 'POST',
            headers: this._buildHeaders(),
            credentials: "same-origin",
            body: JSON.stringify({ tip_names: tipNames })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const err = new Error(data.error || data.message || `Failed to prune: ${response.status}`);
            err.details = data;
            throw err;
        }
        return data;
    },

    async renameTip(jobId, oldName, newName) {
        const response = await fetch(`/api/job/${jobId}/tree/rename`, {
            method: 'POST',
            headers: this._buildHeaders(),
            credentials: "same-origin",
            body: JSON.stringify({ old_name: oldName, new_name: newName })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const err = new Error(data.error || data.message || `Failed to rename: ${response.status}`);
            err.details = data;
            throw err;
        }
        return data;
    },

    async reroot(jobId, nodeName) {
        const response = await fetch(`/api/job/${jobId}/tree/reroot`, {
            method: 'POST',
            headers: this._buildHeaders(),
            credentials: "same-origin",
            // Send the field the backend expects; include legacy names for compatibility
            body: JSON.stringify({ root_target: nodeName, target: nodeName, node_name: nodeName })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const err = new Error(data.error || data.message || `Failed to reroot: ${response.status}`);
            err.details = data;
            throw err;
        }
        return data;
    },

    async midpointRoot(jobId) {
        const response = await fetch(`/api/job/${jobId}/tree/midpoint_root`, {
            method: 'POST',
            headers: this._buildHeaders(false),
            credentials: "same-origin"
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const err = new Error(data.error || data.message || "Midpoint root failed");
            err.details = data;
            throw err;
        }
        return data;
    },

    async midpointRootToggle(jobId) {
        const response = await fetch(`/api/job/${jobId}/tree/midpoint_root_toggle`, {
            method: 'POST',
            headers: this._buildHeaders(false),
            credentials: "same-origin"
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const err = new Error(data.error || data.message || "Midpoint root toggle failed");
            err.details = data;
            throw err;
        }
        return data;
    },

    async recomputeTree(jobId) {
        const response = await fetch(`/api/job/${jobId}/tree/recompute`, {
            method: 'POST',
            headers: this._buildHeaders(),
            credentials: "same-origin",
            body: JSON.stringify({})
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const err = new Error(data.error || data.message || `Failed to recompute: ${response.status}`);
            err.details = data;
            throw err;
        }
        return data;
    },

    async addSequences(jobId, input) {
        const response = await fetch(`/api/job/${jobId}/sequences/add`, {
            method: 'POST',
            headers: this._buildHeaders(),
            credentials: "same-origin",
            body: JSON.stringify({ input: input })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const err = new Error(data.error || data.message || `Failed to add sequences: ${response.status}`);
            err.details = data;
            throw err;
        }
        return data;
    },

    async logClientError(message, context = null) {
        try {
            await fetch('/api/log/client', {
                method: 'POST',
                headers: this._buildHeaders(),
                credentials: "same-origin",
                body: JSON.stringify({ message, context, url: window.location.href, stack: new Error().stack }),
                keepalive: true
            });
        } catch (e) {
            console.error("Failed to log client error:", e);
        }
    }
};
