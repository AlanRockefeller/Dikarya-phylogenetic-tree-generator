/**
 * API client for tree editing operations.
 */
const TreeEditActions = {
    async pruneTip(jobId, tipName) {
        const response = await fetch(`/api/job/${jobId}/tree/prune`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tip_name: tipName })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || "Prune failed");
        return data;
    },

    async renameTip(jobId, oldName, newName) {
        const response = await fetch(`/api/job/${jobId}/tree/rename`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ old_name: oldName, new_name: newName })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || "Rename failed");
        return data;
    },

    async reroot(jobId, rootTarget) {
        const response = await fetch(`/api/job/${jobId}/tree/reroot`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ root_target: rootTarget })
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
            method: 'POST'
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || "Recompute failed");
        return data;
    },

    async getTreeState(jobId) {
        const response = await fetch(`/api/job/${jobId}/tree/state`);
        if (!response.ok) throw new Error("Failed to load tree state");
        return await response.json();
    }
};
