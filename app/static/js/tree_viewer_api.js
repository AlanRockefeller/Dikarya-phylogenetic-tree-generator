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
        if (!response.ok) {
            throw new Error(`Failed to prune: ${response.status}`);
        }
        return await response.json();
    },

    async renameTip(jobId, oldName, newName) {
        const response = await fetch(`/api/job/${jobId}/tree/rename`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ old_name: oldName, new_name: newName })
        });
        if (!response.ok) {
            throw new Error(`Failed to rename: ${response.status}`);
        }
        return await response.json();
    },

    async reroot(jobId, nodeName) {
        const response = await fetch(`/api/job/${jobId}/tree/reroot`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ node_name: nodeName })
        });
        if (!response.ok) {
            throw new Error(`Failed to reroot: ${response.status}`);
        }
        return await response.json();
    },

    async recomputeTree(jobId) {
        const response = await fetch(`/api/job/${jobId}/tree/recompute`, {
            method: 'POST'
        });
        if (!response.ok) {
            throw new Error(`Failed to recompute: ${response.status}`);
        }
        return await response.json();
    }
};
