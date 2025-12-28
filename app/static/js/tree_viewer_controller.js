/**
 * Main controller for the tree viewer.
 * Robust Version: Handles missing elements gracefully.
 */
document.addEventListener('DOMContentLoaded', async () => {
    const JOB_ID = window.JOB_ID || "unknown"; // Safety fallback

    // 1. Safe Element Getter
    const getEl = (id) => document.getElementById(id);

    // 2. Elements
    const container = getEl('tree-container');
    const viewerSelect = getEl('viewer-select');
    const statusMsg = getEl('status-message');

    // Buttons (Action Group)
    const btnPrune = getEl('btn-prune');
    const btnRename = getEl('btn-rename');
    const btnReroot = getEl('btn-reroot');
    const btnRecompute = getEl('btn-recompute');

    // 3. Setup Download Links (Safe Mode)
    const setupLink = (id, url) => {
        const el = getEl(id);
        if (el) el.href = url;
    };

    if (JOB_ID !== "unknown") {
        setupLink('newick-link-original', `/api/job/${JOB_ID}/download/tree/newick/original`);
        setupLink('newick-link-pruned', `/api/job/${JOB_ID}/download/tree/newick/pruned`);
        setupLink('nexus-link', `/api/job/${JOB_ID}/download/tree/nexus`);
        setupLink('fasta-pruned', `/api/job/${JOB_ID}/download/fasta/pruned`);
        setupLink('fasta-original', `/api/job/${JOB_ID}/download/fasta/original`);
    }


    // 4. State
    let currentTreeState = null;
    let selectedNode = null;
    let showSupport = true;

    // 5. Helper: Status Message
    function showStatus(msg, type, timeout = 0) {
        if (!statusMsg) return; // Prevent crash if missing
        statusMsg.className = `alert alert-${type} mt-2`;
        statusMsg.textContent = msg;
        statusMsg.classList.remove('d-none');
        if (timeout > 0) {
            setTimeout(() => {
                statusMsg.classList.add('d-none');
            }, timeout);
        }
    }

    // 6. Helper: Button Updates
    function updateButtons() {
        const hasSelection = !!selectedNode;
        if (btnPrune) btnPrune.disabled = !hasSelection;
        if (btnRename) btnRename.disabled = !hasSelection;
        // Enable reroot if selected AND has a name (identifier)
        if (btnReroot) btnReroot.disabled = !hasSelection || !selectedNode.name;
    }

    // 7. Main Render Logic
    async function renderTree() {
        console.log("renderTree called");
        if (container) container.innerHTML = '';

        const viewerType = viewerSelect ? viewerSelect.value : 'phylotree';

        // Read thresholds & Filter Low Checkbox
        const filterLow = document.getElementById('cb-hide-low-support')?.checked;

        let ppThreshold = 0;
        let bsThreshold = 0;

        if (filterLow) {
            ppThreshold = parseFloat(document.getElementById('input-pp-threshold')?.value || 0.9);
            bsThreshold = parseFloat(document.getElementById('input-bs-threshold')?.value || 70);
        }
        const minTips = parseInt(document.getElementById('input-min-tips')?.value || 0);

        // Read Font Sizing
        const supportBase = parseInt(document.getElementById('input-support-font')?.value || 9);
        const tipBase = parseInt(document.getElementById('input-tip-font')?.value || 12);

        // Shared Callbacks
        const callbacks = {
            onTipClick: (node) => {
                selectedNode = node;
                updateButtons();
                console.log("Selected:", node);
            }
        };

        if (viewerType === 'phylotree') {
            if (typeof renderPhylotree === 'function') {
                try {
                    const resp = await fetch(`/api/job/${JOB_ID}/download/tree/newick`, { cache: "no-store" });
                    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                    const newick = await resp.text();
                    // Call the v2 renderer
                    renderPhylotree(newick, 'tree-container', callbacks, {
                        showSupport: showSupport,
                        ppThreshold: ppThreshold,
                        bootstrapThreshold: bsThreshold,
                        minTips: minTips,
                        supportBasePx: supportBase,
                        tipBasePx: tipBase
                    });
                } catch (err) {
                    if (container) container.textContent = `Failed to load Newick: ${err.message}`;
                }
            } else {
                if (container) container.innerHTML = 'Phylotree library not loaded.';
            }
        }
    }

    // 8. Event Listeners (Only if elements exist)
    const btnToggleSupport = getEl('btn-toggle-support');
    if (btnToggleSupport) {
        btnToggleSupport.addEventListener('click', () => {
            showSupport = !showSupport;
            renderTree();
            // Simple UI feedback
            btnToggleSupport.textContent = showSupport ? "Hide Node Support" : "Show Node Support";
        });
    }

    const inputPP = getEl('input-pp-threshold');
    if (inputPP) {
        inputPP.addEventListener('change', () => renderTree());
    }
    const inputBS = getEl('input-bs-threshold');
    if (inputBS) {
        inputBS.addEventListener('change', () => renderTree());
    }
    const inputMinTips = getEl('input-min-tips');
    if (inputMinTips) {
        inputMinTips.addEventListener('change', () => renderTree());
    }

    // --- Font Listeners (Live update without full re-render) ---
    function applySizing() {
        if (container && typeof container.__applyTextSizingFromZoom === "function") {
            container.__applyTextSizingFromZoom();
        }
    }

    let sizingRAF = null;
    function applySizingRAF() {
        if (sizingRAF) return;
        sizingRAF = requestAnimationFrame(() => {
            sizingRAF = null;
            applySizing();
        });
    }

    ["input-support-font", "input-tip-font"].forEach(id => {
        const el = getEl(id);
        if (el) {
            el.addEventListener("input", applySizingRAF);
            el.addEventListener("change", applySizingRAF);
        }
    });


    if (btnPrune) btnPrune.addEventListener('click', async () => {
        if (!selectedNode || !confirm(`Prune ${selectedNode.name}?`)) return;
        try {
            showStatus("Pruning...", "info");
            currentTreeState = await TreeEditActions.pruneTip(JOB_ID, selectedNode.name);
            selectedNode = null;
            updateButtons();
            await renderTree();
            showStatus("Pruned successfully.", "success", 2000);
        } catch (e) { showStatus(`Prune failed: ${e.message}`, "danger"); }
    });

    if (btnRename) btnRename.addEventListener('click', async () => {
        if (!selectedNode) return;
        const newName = prompt("Enter new name:", selectedNode.display_name || selectedNode.name);
        if (!newName) return;
        try {
            showStatus("Renaming...", "info");
            await TreeEditActions.renameTip(JOB_ID, selectedNode.name, newName);
            selectedNode = null;
            updateButtons();
            await renderTree();
            showStatus("Renamed successfully.", "success", 2000);
        } catch (e) { showStatus(`Rename failed: ${e.message}`, "danger"); }
    });

    if (btnReroot) btnReroot.addEventListener('click', async () => {
        if (!selectedNode || !confirm(`Reroot at ${selectedNode.name}?`)) return;
        try {
            showStatus("Rerooting...", "info");
            await TreeEditActions.reroot(JOB_ID, selectedNode.name);
            selectedNode = null;
            updateButtons();
            await renderTree();
            showStatus("Rerooted successfully.", "success", 2000);
        } catch (e) { showStatus(`Reroot failed: ${e.message}`, "danger"); }
    });

    if (btnRecompute) btnRecompute.addEventListener('click', async () => {
        if (!confirm("Recompute tree?")) return;
        try {
            showStatus("Recomputing...", "info");
            await TreeEditActions.recomputeTree(JOB_ID);
            selectedNode = null;
            updateButtons();
            await renderTree();
            showStatus("Done.", "success", 3000);
        } catch (e) { showStatus(`Failed: ${e.message}`, "danger"); }
    });

    // 9. Initial Load
    renderTree().catch(e => console.error(e));
});
