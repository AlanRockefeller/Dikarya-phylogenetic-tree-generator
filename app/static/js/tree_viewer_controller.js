/**
 * Main controller for the tree viewer.
 */
document.addEventListener('DOMContentLoaded', async () => {
    const container = document.getElementById('tree-container');
    const viewerSelect = document.getElementById('viewer-select');
    const statusMsg = document.getElementById('status-message');

    // Buttons
    const btnPrune = document.getElementById('btn-prune');
    const btnRename = document.getElementById('btn-rename');
    const btnReroot = document.getElementById('btn-reroot');
    const btnRecompute = document.getElementById('btn-recompute');

    // Download links
    const linkNewick = document.getElementById('btn-download-newick');
    const linkNexus = document.getElementById('btn-download-nexus');
    const linkFasta = document.getElementById('btn-download-fasta');
    const linkOriginal = document.getElementById('btn-download-original');

    let currentTreeState = null;
    let selectedNode = null;

    // Initialize download links
    linkNewick.href = `/api/job/${JOB_ID}/download/tree/newick`;
    linkNexus.href = `/api/job/${JOB_ID}/download/tree/nexus`;
    linkFasta.href = `/api/job/${JOB_ID}/download/fasta/pruned`;
    linkOriginal.href = `/api/job/${JOB_ID}/download/fasta/original`;

    // Load initial tree
    try {
        showStatus("Loading tree...", "info");
        currentTreeState = await TreeEditActions.getTreeState(JOB_ID);
        renderTree();
        showStatus("Tree loaded.", "success", 2000);
    } catch (e) {
        showStatus(`Error loading tree: ${e.message}`, "danger");
    }

    // Viewer Switch
    viewerSelect.addEventListener('change', () => {
        renderTree();
    });

    // Actions
    btnPrune.addEventListener('click', async () => {
        if (!selectedNode) return;
        if (!confirm(`Prune ${selectedNode.name}?`)) return;

        try {
            showStatus("Pruning...", "info");
            currentTreeState = await TreeEditActions.pruneTip(JOB_ID, selectedNode.name);
            selectedNode = null;
            updateButtons();
            renderTree();
            showStatus("Pruned successfully.", "success", 2000);
        } catch (e) {
            showStatus(`Prune failed: ${e.message}`, "danger");
        }
    });

    btnRename.addEventListener('click', async () => {
        if (!selectedNode) return;
        const newName = prompt("Enter new name:", selectedNode.display_name || selectedNode.name);
        if (!newName) return;

        try {
            showStatus("Renaming...", "info");
            currentTreeState = await TreeEditActions.renameTip(JOB_ID, selectedNode.name, newName);
            selectedNode = null; // Or keep selected?
            updateButtons();
            renderTree();
            showStatus("Renamed successfully.", "success", 2000);
        } catch (e) {
            showStatus(`Rename failed: ${e.message}`, "danger");
        }
    });

    btnReroot.addEventListener('click', async () => {
        if (!selectedNode) return;
        if (!confirm(`Reroot at ${selectedNode.name}?`)) return;

        try {
            showStatus("Rerooting...", "info");
            currentTreeState = await TreeEditActions.reroot(JOB_ID, selectedNode.name);
            selectedNode = null;
            updateButtons();
            renderTree();
            showStatus("Rerooted successfully.", "success", 2000);
        } catch (e) {
            showStatus(`Reroot failed: ${e.message}`, "danger");
        }
    });

    btnRecompute.addEventListener('click', async () => {
        if (!confirm("Recompute tree? This may take some time.")) return;

        try {
            showStatus("Recomputing... please wait.", "info");
            const result = await TreeEditActions.recomputeTree(JOB_ID);
            // Reload state after recompute
            currentTreeState = await TreeEditActions.getTreeState(JOB_ID);
            selectedNode = null;
            updateButtons();
            renderTree();
            showStatus("Recompute complete.", "success", 3000);
        } catch (e) {
            showStatus(`Recompute failed: ${e.message}`, "danger");
        }
    });

    function renderTree() {
        container.innerHTML = ''; // Clear
        const viewerType = viewerSelect.value;
        const callbacks = {
            onTipClick: (node) => {
                selectedNode = node;
                updateButtons();
                // Visual feedback handled by specific viewer or shared logic?
                // For D3, we might need to re-render or highlight.
                // Let's assume the viewer highlights it.
                console.log("Selected:", node);
            }
        };

        if (viewerType === 'd3') {
            if (typeof renderD3Tree === 'function') {
                renderD3Tree(currentTreeState, 'tree-container', callbacks);
            } else {
                container.innerHTML = 'D3 Viewer not loaded.';
            }
        } else if (viewerType === 'phylotree') {
            if (typeof renderPhylotree === 'function') {
                renderPhylotree(currentTreeState, 'tree-container', callbacks);
            } else {
                container.innerHTML = 'Phylotree.js Viewer not loaded.';
            }
        } else if (viewerType === 'jsphylosvg') {
            if (typeof renderJsPhyloSVG === 'function') {
                renderJsPhyloSVG(currentTreeState, 'tree-container', callbacks);
            } else {
                container.innerHTML = 'jsPhyloSVG Viewer not loaded.';
            }
        }
    }

    function updateButtons() {
        const hasSelection = !!selectedNode;
        btnPrune.disabled = !hasSelection;
        btnRename.disabled = !hasSelection;
        btnReroot.disabled = !hasSelection;
    }

    function showStatus(msg, type, timeout = 0) {
        statusMsg.className = `alert alert-${type} mt-2`;
        statusMsg.textContent = msg;
        statusMsg.classList.remove('d-none');
        if (timeout > 0) {
            setTimeout(() => {
                statusMsg.classList.add('d-none');
            }, timeout);
        }
    }
});
