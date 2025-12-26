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
        if (!response.ok) throw new Error("Prune failed");
        return await response.json();
    },

    async renameTip(jobId, oldName, newName) {
        const response = await fetch(`/api/job/${jobId}/tree/rename`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ old_name: oldName, new_name: newName })
        });
        if (!response.ok) throw new Error("Rename failed");
        return await response.json();
    },

    async reroot(jobId, target) {
        const response = await fetch(`/api/job/${jobId}/tree/reroot`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ target: target })
        });
        if (!response.ok) throw new Error("Reroot failed");
        return await response.json();
    },

    async recomputeTree(jobId) {
        const response = await fetch(`/api/job/${jobId}/tree/recompute`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}) // Can pass params here if needed
        });
        if (!response.ok) throw new Error("Recompute failed");
        return await response.json();
    },

    async getTreeState(jobId) {
        const response = await fetch(`/api/job/${jobId}/tree/state`);
        if (!response.ok) throw new Error("Failed to load tree state");
        return await response.json();
    }
};
