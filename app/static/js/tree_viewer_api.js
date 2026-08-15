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

    // Alan 7/16/26 - Refresh selected observation-backed Mycomap records and persist changed tip labels.
    async refreshMycomapRecords(jobId, tipNames) {
        const response = await fetch(`/api/job/${jobId}/tree/refresh-mycomap-records`, {
            method: 'POST',
            headers: this._buildHeaders(),
            credentials: "same-origin",
            body: JSON.stringify({ tip_names: tipNames })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const err = new Error(data.error || data.message || `Failed to refresh Mycomap records: ${response.status}`);
            err.details = data;
            throw err;
        }
        return data;
    },

    // Alan 5/29/26 - Persist a display-only child-order rotation for one stable internal node.
    async rotateNode(jobId, nodeId) {
        const response = await fetch(`/api/job/${jobId}/tree/rotate`, {
            method: 'POST',
            headers: this._buildHeaders(),
            credentials: "same-origin",
            body: JSON.stringify({ node_id: nodeId })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const err = new Error(data.error || data.message || `Failed to rotate node: ${response.status}`);
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

    // Alan 5/29/26 - Generic rooting-mode endpoint supporting auto / midpoint /
    // most_divergent_hit / unrooted / manual; replaces ad-hoc rooting calls
    // for new-mode UI while leaving midpointRoot/reroot intact for compat.
    async setRootingMode(jobId, mode, opts = {}) {
        const body = { mode };
        if (opts.target) body.target = opts.target;
        if (opts.sequenceOfInterest) body.sequence_of_interest = opts.sequenceOfInterest;
        const response = await fetch(`/api/job/${jobId}/tree/rooting_mode`, {
            method: 'POST',
            headers: this._buildHeaders(),
            credentials: "same-origin",
            body: JSON.stringify(body)
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const err = new Error(data.error || data.message || "Set rooting mode failed");
            err.details = data;
            throw err;
        }
        return data;
    },

    // Alan 5/29/26 - Persist the focal/sequence-of-interest tip (mirrors into
    // Default selection set server-side to drive existing blue highlight).
    async setSequenceOfInterest(jobId, tipName, source = "user_selected") {
        const response = await fetch(`/api/job/${jobId}/tree/sequence_of_interest`, {
            method: 'POST',
            headers: this._buildHeaders(),
            credentials: "same-origin",
            body: JSON.stringify({ tip_name: tipName, source })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const err = new Error(data.error || data.message || "Set sequence of interest failed");
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
        // Alan 8/15/26 - This posted a bare {message, context, url, stack} body,
        // which /api/log/client discards because it carries no recognised `event`
        // -- so every tree edit failure reported here has been silently dropped.
        // It also sent window.location.href, query string included. Delegating to
        // the shared layer gets sanitizing, dedup and rate limiting for free; the
        // signature and the call sites are unchanged.
        try {
            const error = new Error(String(message || 'tree edit failure'));
            if (context) error.stack = String(context);
            window.reportClientError?.('tree_edit.action_failed', error);
        } catch (e) {
            console.error("Failed to log client error:", e);
        }
    }
};
