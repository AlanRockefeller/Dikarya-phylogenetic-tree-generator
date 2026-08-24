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

    // Alan 8/15/26 - Save the whole layered clade-annotation configuration in one
    // atomic request. Partial saves would let a rejected layer strand the
    // annotations that reference it, so layers and annotations always travel together.
    async saveCladeAnnotations(jobId, layers, annotations) {
        const response = await fetch(`/api/job/${jobId}/tree/annotations`, {
            method: 'POST',
            headers: this._buildHeaders(),
            credentials: "same-origin",
            body: JSON.stringify({ layers: layers, annotations: annotations })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const err = new Error(data.error || data.message || `Failed to save annotations: ${response.status}`);
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
            body: JSON.stringify({ async: true })
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

    // Alan 8/17/26 - New client for the "Analyze with Claude" endpoint used by the tree viewer.
    // Ask Claude to assess this job's alignment and tree. The server caches the
    // result against the tree's current statistics, so this is cheap to call on
    // repeat opens; pass refresh:true only when the user explicitly re-runs it.
    // Deliberately no client-side timeout -- the server already caps the call.
    async claudeReview(jobId, { refresh = false } = {}) {
        const response = await fetch(`/api/job/${jobId}/analysis/review`, {
            method: 'POST',
            headers: this._buildHeaders(),
            credentials: "same-origin",
            body: JSON.stringify({ refresh: refresh })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const err = new Error(data.error || data.message || `Review failed: ${response.status}`);
            err.details = data;
            err.status = response.status;
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
            // Alan 8/23/26 - Optional-chaining away the call made the report vanish
            // silently when the shared reporting layer had not loaded -- the exact
            // outcome the comment above is about.
            if (typeof window.reportClientError === 'function') {
                window.reportClientError('tree_edit.action_failed', error);
            } else {
                console.error('tree_edit.action_failed', error);
            }
        } catch (e) {
            console.error("Failed to log client error:", e);
        }
    }
};
