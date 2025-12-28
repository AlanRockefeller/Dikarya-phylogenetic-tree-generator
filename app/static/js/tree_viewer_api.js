/**
 * API helper for tree editing actions
 */
const TreeEditActions = {
    async getTreeState(jobId) {
        const response = await fetch(`/api/job/${jobId}/tree/state`, {
            cache: "no-store"
        });
        if (!response.ok) {
            throw new Error(`Failed to get tree state: ${response.status}`);
        }
        return await response.json();
    },

    async pruneTip(jobId, tipName) {
        const response = await fetch(`/api/job/${jobId}/tree/prune`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tip_name: tipName })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(data.error || `Failed to prune: ${response.status}`);
        }
        return data;
    },

    async renameTip(jobId, oldName, newName) {
        const response = await fetch(`/api/job/${jobId}/tree/rename`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ old_name: oldName, new_name: newName })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(data.error || `Failed to rename: ${response.status}`);
        }
        return data;
    },

    async reroot(jobId, nodeName) {
        const response = await fetch(`/api/job/${jobId}/tree/reroot`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            // Send the field the backend expects; include legacy names for compatibility
            body: JSON.stringify({ root_target: nodeName, target: nodeName, node_name: nodeName })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(data.error || `Failed to reroot: ${response.status}`);
        }
        return data;
    },

    async midpointRoot(jobId) {
        const response = await fetch(`/api/job/${jobId}/tree/midpoint_root`, {
            method: 'POST'
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(data.error || "Midpoint root failed");
        }
        return data;
    },

    async recomputeTree(jobId) {
        const response = await fetch(`/api/job/${jobId}/tree/recompute`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(data.error || `Failed to recompute: ${response.status}`);
        }
        return data;
    }
};
