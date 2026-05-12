/**
 * Main controller for the tree viewer.
 * Uses DikaryaTreeViewer class for robust, incremental updates.
 */
document.addEventListener('DOMContentLoaded', async () => {
    const JOB_ID = window.JOB_ID || "unknown";
    const getEl = (id) => document.getElementById(id);
    const DEBUG_MODE = new URLSearchParams(window.location.search).has('debug');

    // --- ELEMENTS ---
    const container = getEl('tree-container');
    const statusMsg = getEl('status-message');
    const btnPrune = getEl('btn-prune');
    const btnRename = getEl('btn-rename');
    const btnReroot = getEl('btn-reroot');
    const btnMidpoint = getEl('btn-midpoint');
    // Alan 5/11/26 - Track the viewer-only deselect control separately from persistent selection-set actions.
    const btnDeselect = getEl('btn-deselect');
    const btnRecompute = getEl('btn-recompute');
    const btnMakeCopy = getEl('btn-make-copy');
    // Alan 5/11/26 - Cache rename modal elements for editing the current visible selection.
    const renameModal = getEl('modal-rename-sequences');
    const renameModalRows = getEl('rename-modal-rows');
    const renameModalSubtitle = getEl('rename-modal-subtitle');
    const btnRenameModalSave = getEl('btn-rename-modal-save');
    const btnRenameModalCancel = getEl('btn-rename-modal-cancel');
    const btnRenameModalClose = getEl('btn-rename-modal-close');
    const renameModalBackdrop = getEl('rename-modal-backdrop');

    // --- HELPER: STATUS MESSAGE ---
    let currentStatusType = null;
    function showStatus(msg, type, timeout = 0) {
        if (!statusMsg) return;
        const colorMap = {
            'info': ['bg-blue-50', 'text-blue-800', 'dark:bg-blue-900/30', 'dark:text-blue-200'],
            'success': ['bg-green-50', 'text-green-800', 'dark:bg-green-900/30', 'dark:text-green-200'],
            'warning': ['bg-yellow-50', 'text-yellow-800', 'dark:bg-yellow-900/30', 'dark:text-yellow-200'],
            'danger': ['bg-red-50', 'text-red-800', 'dark:bg-red-900/30', 'dark:text-red-200']
        };
        if (currentStatusType && currentStatusType !== type) statusMsg.classList.remove(...colorMap[currentStatusType]);
        statusMsg.classList.add(...(colorMap[type] || colorMap['info']));
        currentStatusType = type;
        statusMsg.textContent = msg;
        statusMsg.classList.remove('hidden');
        if (timeout > 0) setTimeout(() => statusMsg.classList.add('hidden'), timeout);
    }

    // --- STATE ---
    let viewer = null;
    // selectedNode Removed - using viewer state
    let isProcessing = false;
    let rerootMode = false;
    let uiWired = false;
    let isLoadingTree = false;
    let isMidpointRooted = true; // Default: midpoint rooted on load
    // Reroot Capture State (Moved top-level to fix reference errors)
    let rerootCaptureHandler = null;
    let filterDebounce = null;
    let selectionSaveDebounce = null;
    // Alan 5/9/26 - Throttle live sequence metric filter redraws to one frame while the user drags.
    let sequenceFilterFrame = null;
    // Alan 5/11/26 - Hold only the clicked/current selection being edited in the rename modal.
    let pendingRenameItems = [];
    let updateSelectionSetUI = () => {}; // assigned in wireUI()

    async function saveSelectionSets() {
        if (!viewer || JOB_ID === 'unknown') return;
        try {
            const payload = viewer.getSelectionSetsData();
            await fetch(`/api/job/${JOB_ID}/tree/selection_sets`, {
                method: 'POST',
                headers: TreeEditActions._buildHeaders(),
                body: JSON.stringify(payload)
            });
        } catch (e) {
            console.warn('Could not save selection sets:', e);
        }
    }

    function debouncedSaveSelectionSets() {
        if (selectionSaveDebounce) clearTimeout(selectionSaveDebounce);
        selectionSaveDebounce = setTimeout(saveSelectionSets, 800);
    }

    // Alan 5/11/26 - Keep the Box Select toolbar button visually synchronized with viewer mode.
    function updateBoxSelectButton() {
        // Alan 5/11/26 - Look up lazily so the helper can run before UI wiring finishes.
        const btnBoxSelect = getEl('btn-box-select');
        // Alan 5/11/26 - Nothing to update when the toolbar button is absent.
        if (!btnBoxSelect) return;
        // Alan 5/11/26 - Read the viewer mode through its public API.
        const active = Boolean(viewer?.getBoxSelectMode?.());
        // Alan 5/11/26 - Use active styling to make the persistent drag mode discoverable.
        btnBoxSelect.classList.toggle('active', active);
        // Alan 5/11/26 - Add gold active styling without relying on a separate CSS build step.
        btnBoxSelect.classList.toggle('bg-journal-gold/20', active);
        // Alan 5/11/26 - Add gold active styling without relying on a separate CSS build step.
        btnBoxSelect.classList.toggle('text-journal-dark', active);
        // Alan 5/11/26 - Add gold active styling without relying on a separate CSS build step.
        btnBoxSelect.classList.toggle('dark:text-journal-gold-light', active);
        // Alan 5/11/26 - Add gold active styling without relying on a separate CSS build step.
        btnBoxSelect.classList.toggle('border-journal-gold', active);
        // Alan 5/11/26 - Apply active styling inline because Tailwind CDN may not see classes added from JS.
        btnBoxSelect.style.backgroundColor = active ? 'rgba(201,169,98,.18)' : '';
        // Alan 5/11/26 - Apply active styling inline because Tailwind CDN may not see classes added from JS.
        btnBoxSelect.style.borderColor = active ? '#c9a962' : '';
        // Alan 5/11/26 - Apply active styling inline because Tailwind CDN may not see classes added from JS.
        btnBoxSelect.style.color = active ? '#c9a962' : '';
        // Alan 5/11/26 - Mirror the mode for assistive technology and CSS hooks.
        btnBoxSelect.setAttribute('aria-pressed', active ? 'true' : 'false');
        // Alan 5/11/26 - Explain modifier behavior in the tooltip without adding in-app instruction text.
        btnBoxSelect.title = active
            ? 'Box Select is on. Drag empty tree background to select tips; Alt removes; Ctrl/Cmd toggles; Esc cancels.'
            : 'Box Select. Right-drag empty tree background anytime, or turn this on for left-drag selection.';
    }

    function saveDisplayPrefs() {
        if (JOB_ID === 'unknown') return;
        try {
            localStorage.setItem(`dikarya_tree_${JOB_ID}`, JSON.stringify({
                supportFont: Number(getEl('input-support-font')?.value) || 9,
                tipFont: Number(getEl('input-tip-font')?.value) || 12,
                spacingX: viewer?.spacingState?.x || 0,
                spacingY: viewer?.spacingState?.y || 0,
            }));
        } catch (e) {}
    }

    function restoreDisplayPrefs() {
        if (JOB_ID === 'unknown' || !viewer) return;
        try {
            const raw = localStorage.getItem(`dikarya_tree_${JOB_ID}`);
            if (!raw) return;
            const prefs = JSON.parse(raw);
            const sIn = getEl('input-support-font');
            const tIn = getEl('input-tip-font');
            if (sIn && prefs.supportFont) sIn.value = prefs.supportFont;
            if (tIn && prefs.tipFont) tIn.value = prefs.tipFont;
            viewer.applyTextSizing();
            if (prefs.spacingX || prefs.spacingY) {
                viewer.updateSpacing(prefs.spacingX || 0, prefs.spacingY || 0);
            }
        } catch (e) {}
    }

    function removeRerootCapture() {
        if (!container || !rerootCaptureHandler) return;
        container.removeEventListener("click", rerootCaptureHandler, true);
        container.removeEventListener("contextmenu", rerootCaptureHandler, true);
        rerootCaptureHandler = null;
    }

    // Alan 5/11/26 - Open a modal for renaming the current visible clicked selections only.
    function openRenameModal(nodes) {
        if (!renameModal || !renameModalRows || !Array.isArray(nodes) || nodes.length === 0) return;
        pendingRenameItems = nodes.map((node, index) => {
            const data = node?.data || node || {};
            const originalName = data.__original_name || data.original_name || data.name || node?.name || "";
            const displayName = data.name || data.display_name || originalName;
            return { index, originalName, displayName };
        }).filter(item => item.originalName);
        if (pendingRenameItems.length === 0) return;

        renameModalRows.innerHTML = "";
        pendingRenameItems.forEach(item => {
            const row = document.createElement('div');
            row.className = 'rounded-lg border border-gray-200 dark:border-journal-dark p-3 bg-gray-50 dark:bg-journal-dark/40';

            const label = document.createElement('label');
            label.className = 'block text-xs font-semibold text-gray-500 dark:text-gray-300 mb-1';
            label.htmlFor = `rename-input-${item.index}`;
            label.textContent = `Current name ${item.index + 1}`;

            const input = document.createElement('input');
            input.type = 'text';
            input.id = `rename-input-${item.index}`;
            input.dataset.renameIndex = String(item.index);
            input.value = item.displayName;
            input.className = 'w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-journal-dark text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-sm';
            input.autocomplete = 'off';

            const original = document.createElement('p');
            original.className = 'mt-1 text-xs text-gray-500 dark:text-gray-400 break-all';
            original.textContent = item.originalName === item.displayName ? 'Original ID preserved for tree edits.' : `Original ID: ${item.originalName}`;

            row.appendChild(label);
            row.appendChild(input);
            row.appendChild(original);
            renameModalRows.appendChild(row);
        });

        if (renameModalSubtitle) {
            const count = pendingRenameItems.length;
            renameModalSubtitle.textContent = `Editing ${count} selected sequence${count === 1 ? '' : 's'}.`;
        }
        renameModal.classList.remove('hidden');
        document.body.classList.add('overflow-hidden');
        renameModalRows.querySelector('input')?.focus();
    }

    // Alan 5/11/26 - Close the rename modal without changing any saved selection-set state.
    function closeRenameModal() {
        if (!renameModal) return;
        renameModal.classList.add('hidden');
        document.body.classList.remove('overflow-hidden');
        pendingRenameItems = [];
    }

    // Alan 5/11/26 - Submit only changed names from the current visible selection rename modal.
    async function submitRenameModal() {
        if (!renameModalRows || pendingRenameItems.length === 0 || isProcessing) return;
        const changes = [];
        for (const item of pendingRenameItems) {
            const input = renameModalRows.querySelector(`input[data-rename-index="${item.index}"]`);
            const newName = input?.value.trim() || "";
            if (!newName) {
                showStatus("Names cannot be blank.", "warning", 2500);
                input?.focus();
                return;
            }
            if (newName !== item.displayName) {
                changes.push({ oldName: item.originalName, newName });
            }
        }

        if (changes.length === 0) {
            closeRenameModal();
            showStatus("No rename changes.", "info", 1500);
            return;
        }

        closeRenameModal();
        const count = changes.length;
        runBackendAction(`Renaming ${count} sequence${count === 1 ? '' : 's'}`, async () => {
            for (const change of changes) {
                await TreeEditActions.renameTip(JOB_ID, change.oldName, change.newName);
            }
        // Alan 5/11/26 - Renaming changes labels only, so saved selection sets should not be cleared.
        }, { clearSelections: false });
    }

    // --- HELPER: BACKEND ACTION WRAPPER ---
    // Alan 5/11/26 - Let non-structural actions such as rename reload without clearing saved selection sets.
    async function runBackendAction(name, actionFn, options = {}) {
        if (isProcessing) return;
        isProcessing = true;
        updateButtons(); // Disable
        try {
            showStatus(name + "...", "info");
            await actionFn();
            showStatus(name + " completed.", "success", 2000);

            // Post-action cleanup
            if (viewer && options.clearSelections !== false) {
                // Alan 5/10/26 - Persist cleared selections before reload so pruned nodes cannot reappear as selected.
                viewer.clearSelection();
                // Alan 5/10/26 - Cancel pending selection saves that may still contain pre-prune IDs.
                if (selectionSaveDebounce) {
                    selectionSaveDebounce = clearTimeout(selectionSaveDebounce);
                }
                // Alan 5/10/26 - Save the empty selection state before fetching tree_state again.
                await saveSelectionSets();
            }
            rerootMode = false;
            if (rerootCaptureHandler) removeRerootCapture();

            await loadTree();
        } catch (err) {
            console.error(err);

            // 1. Construct detailed message
            let msg = err.message;
            if (err.details && err.details.error) {
                msg = err.details.error; // Prefer server error message
            }

            // 2. Show UI feedback
            showStatus(name + " failed: " + msg, "danger", 5000);

            // 3. Log to server
            if (window.TreeEditActions && typeof TreeEditActions.logClientError === 'function') {
                TreeEditActions.logClientError(`Action '${name}' failed: ${msg}`, `Details: ${JSON.stringify(err.details || {})}`);
            }

        } finally {
            isProcessing = false;
            updateButtons();
        }
    }

    // --- 1. INITIALIZE VIEWER ---
    function initViewer() {
        if (viewer) return;
        if (!window.DikaryaTreeViewer) {
            console.error("DikaryaTreeViewer class not found.");
            return;
        }

        const callbacks = {
            onTipClick: (payload) => {
                // State is managed by Viewer now. We just update UI.
                updateButtons();
            },
            onSelectionChange: (count) => {
                updateButtons();
                debouncedSaveSelectionSets();
            },
            // Alan 5/11/26 - Let the viewer report completed box-select gestures for concise feedback.
            onBoxSelect: (result) => {
                // Alan 5/11/26 - Refresh buttons immediately after a rectangle selection.
                updateButtons();
                // Alan 5/11/26 - Avoid noisy messages when the rectangle missed every tip.
                if (!result || result.matched === 0) {
                    showStatus("No sequences in box.", "info", 1500);
                    return;
                }
                // Alan 5/12/26 - Treat Alt/right-drag boxes as direct prune requests, matching Select + Prune.
                if (result.mode === 'remove') {
                    // Alan 5/12/26 - Copy the boxed tip IDs defensively before the tree reloads.
                    const names = Array.isArray(result.ids) ? result.ids.slice() : [];
                    // Alan 5/12/26 - Ignore an impossible empty prune payload even if matched was nonzero.
                    if (names.length === 0) return;
                    // Alan 5/12/26 - Run the same backend prune flow used by the Prune button.
                    runBackendAction(`Pruning ${names.length} sequence${names.length === 1 ? '' : 's'}`, async () => {
                        // Alan 5/12/26 - Prune the boxed terminal names through the existing API contract.
                        await TreeEditActions.pruneTaxa(JOB_ID, names);
                    });
                    // Alan 5/12/26 - Do not also show a selection-set remove toast for prune boxes.
                    return;
                }
                // Alan 5/11/26 - Use action-specific wording so modifier drags are clear.
                const verb = result.mode === 'remove' ? 'removed' : (result.mode === 'toggle' ? 'toggled' : 'selected');
                // Alan 5/11/26 - Report matched tips rather than only changed tips so users know what the box covered.
                showStatus(`Box ${verb} ${result.matched} sequence${result.matched === 1 ? '' : 's'}.`, "success", 1800);
            },
            // Alan 5/11/26 - Keep the toolbar button synchronized when Esc or code changes box-select mode.
            onBoxSelectModeChange: () => {
                // Alan 5/11/26 - Refresh only the box-select button state for mode changes.
                updateBoxSelectButton();
            }
        };

        const initialOptions = {
            showSupport: true,
            layout: 'linear',
            alignTips: false,
            // grab initial DOM values
            minTips: parseInt(getEl('input-min-tips')?.value || 0),
            ppThreshold: parseFloat(getEl('input-pp-threshold')?.value || 0.9),
            bootstrapThreshold: parseInt(getEl('input-bs-threshold')?.value || 70),
            // Alan 5/9/26 - Pass stored per-sequence BLAST metrics into the viewer for tip filtering.
            sequenceMetrics: Array.isArray(window.SEQUENCE_METRICS) ? window.SEQUENCE_METRICS : [],
            treeMethod: window.TREE_METHOD || ''
        };

        viewer = new DikaryaTreeViewer('tree-container', callbacks, initialOptions);

        // One-time UI wiring
        wireUI();

        // Initial button check (for view-only mode etc calling updateButtons)
        updateButtons();
    }

    async function loadTree() {
        if (isLoadingTree) return;
        isLoadingTree = true;
        try {
            if (!viewer) initViewer();
            if (!viewer) return;

            const resp = await fetch(`/api/job/${JOB_ID}/download/tree/newick`, { cache: "no-store" });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const newick = await resp.text();

            // Validate format (catch HTML error pages)
            const trimmed = newick.trim();
            if (!trimmed) throw new Error("Empty tree data received.");
            if (trimmed.startsWith("<")) throw new Error("Invalid tree format: Server returned HTML.");

            await viewer.render(newick);

            // Fetch tree state to get midpoint rooting status and restore selection sets
            try {
                const treeState = await TreeEditActions.getTreeState(JOB_ID);
                isMidpointRooted = treeState.is_midpoint_rooted ?? true;
                updateMidpointButton();
                // Alan 5/11/26 - Reapply saved rename labels after loading the raw Newick tree.
                if (treeState.renames && viewer && typeof viewer.applyRenames === 'function') {
                    viewer.applyRenames(treeState.renames);
                }
                if (treeState.selection_sets && viewer) {
                    viewer.restoreSelectionSets({
                        sets: treeState.selection_sets,
                        active: treeState.active_selection_set || 'Default'
                    });
                    updateSelectionSetUI();
                }
            } catch (stateErr) {
                console.warn("Could not fetch tree state:", stateErr);
            }

            // Restore display preferences (font sizes, spacing) from localStorage
            restoreDisplayPrefs();

            // Post-render sync
            updateSupportUI(viewer.getStats());
            // Alan 5/9/26 - Size and label the sequence metric sliders after the tree has loaded.
            syncSequenceFilterUI(viewer.getSequenceFilterStats());

        } catch (err) {
            console.error("Tree Load Error:", err);
            if (container) container.textContent = `Failed to load Newick: ${err.message}`;
            // Log to server
            if (window.TreeEditActions && typeof TreeEditActions.logClientError === 'function') {
                TreeEditActions.logClientError(`Tree Load Failed: ${err.message}`, `Stack: ${err.stack}`);
            }
        } finally {
            isLoadingTree = false;
        }
    }

    // --- 2. SETUP DOWNLOAD LINKS ---
    const setupLink = (id, url) => { const el = getEl(id); if (el) el.href = url; };
    if (JOB_ID !== "unknown") {
        setupLink('newick-link-original', `/api/job/${JOB_ID}/download/tree/newick/original`);
        // 'newick-link-pruned' is now a client-side export, wired below
        setupLink('nexus-link', `/api/job/${JOB_ID}/download/tree/nexus`);
        setupLink('fasta-pruned', `/api/job/${JOB_ID}/download/fasta/pruned`);
        setupLink('fasta-original', `/api/job/${JOB_ID}/download/fasta/original`);
    }

    // Current Newick export (client-side with selection annotations)
    const newickCurrentLink = getEl('newick-link-pruned');
    if (newickCurrentLink) {
        newickCurrentLink.removeAttribute('href');
        newickCurrentLink.style.cursor = 'pointer';
        newickCurrentLink.addEventListener('click', (e) => {
            e.preventDefault();
            if (!viewer) {
                showStatus("Tree not loaded yet.", "warning", 2000);
                return;
            }
            const newickStr = viewer.getNewickString();
            if (!newickStr) {
                showStatus("No tree data to export.", "warning", 2000);
                return;
            }
            // Create blob and trigger download
            const blob = new Blob([newickStr], { type: "text/plain;charset=utf-8" });
            const url = URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.href = url;
            link.download = "tree_current.nwk";
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            URL.revokeObjectURL(url);
            showStatus("Downloaded tree_current.nwk", "success", 2000);
        });
    }

    // --- 3. UI EVENT WIRING (SINGLETON) ---
    function wireUI() {
        if (uiWired) return;
        uiWired = true;

        // Alan 5/11/26 - Wire rename modal controls for current clicked selections.
        btnRenameModalSave?.addEventListener('click', submitRenameModal);
        btnRenameModalCancel?.addEventListener('click', closeRenameModal);
        btnRenameModalClose?.addEventListener('click', closeRenameModal);
        renameModalBackdrop?.addEventListener('click', closeRenameModal);
        renameModalRows?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                submitRenameModal();
            }
        });

        // Layout
        getEl('btn-layout-linear')?.addEventListener('click', () => viewer?.updateLayout('linear'));
        getEl('btn-layout-radial')?.addEventListener('click', () => viewer?.updateLayout('radial'));

        // Alan 5/11/26 - Wire the visible Box Select toggle for trackpads and discoverability.
        const btnBoxSelect = getEl('btn-box-select');
        // Alan 5/11/26 - Toggle persistent left-drag box selection from the toolbar.
        btnBoxSelect?.addEventListener('click', () => {
            // Alan 5/11/26 - Ignore clicks before the viewer is available.
            if (!viewer?.setBoxSelectMode) return;
            // Alan 5/11/26 - Flip the mode through the viewer so cursor and callbacks stay centralized.
            const enabled = viewer.setBoxSelectMode(!viewer.getBoxSelectMode());
            // Alan 5/11/26 - Keep the toolbar synchronized immediately after toggling.
            updateBoxSelectButton();
            // Alan 5/11/26 - Give brief feedback without adding persistent explanatory text.
            showStatus(enabled ? "Box Select on. Drag empty tree background." : "Box Select off.", "info", 1500);
        });
        // Alan 5/11/26 - Initialize button state after wiring.
        updateBoxSelectButton();

        // Align
        const btnAlign = getEl('btn-align-tips');
        btnAlign?.addEventListener('click', () => {
            if (!viewer) return;
            const isAligned = btnAlign.classList.toggle('active');
            viewer.updateLayout(null, isAligned);
            btnAlign.innerHTML = isAligned ? '<i class="fa fa-outdent"></i> Unalign' : '<i class="fa fa-indent"></i> Align';
        });

        // Sort (Ladderize)
        const btnLadderize = getEl('btn-ladderize');
        let sortMode = 'original';
        btnLadderize?.addEventListener('click', () => {
            if (!viewer) return;
            if (sortMode === 'original') { sortMode = 'asc'; btnLadderize.classList.add('active'); }
            else if (sortMode === 'asc') { sortMode = 'desc'; btnLadderize.classList.add('active'); }
            else { sortMode = 'original'; btnLadderize.classList.remove('active'); }

            viewer.sortNodes(sortMode);

            if (sortMode === 'asc') btnLadderize.innerHTML = '<i class="fa fa-sort-amount-down"></i> Asc';
            else if (sortMode === 'desc') btnLadderize.innerHTML = '<i class="fa fa-sort-amount-up"></i> Desc';
            else btnLadderize.innerHTML = '<i class="fa fa-sort"></i> Sort';
        });

        // Zoom & Fit
        getEl('btn-fit')?.addEventListener('click', () => viewer?.fitToView());
        getEl('btn-zoom-in')?.addEventListener('click', () => triggerZoom(-300));
        getEl('btn-zoom-out')?.addEventListener('click', () => triggerZoom(300));

        // Spacing - Use larger increments for visible effect
        const showSpacingFeedback = () => {
            if (!viewer) return;
            const { x, y } = viewer.spacingState;
            showStatus(`Spacing: H${x > 0 ? '+' : ''}${x}, V${y > 0 ? '+' : ''}${y}`, "info", 1500);
        };

        getEl('btn-spacing-x-inc')?.addEventListener('click', () => {
            if (DEBUG_MODE) console.log('X-Inc clicked: adding 5 to X');
            viewer?.updateSpacing(5, 0);
            showSpacingFeedback();
            saveDisplayPrefs();
        });
        getEl('btn-spacing-x-dec')?.addEventListener('click', () => {
            if (DEBUG_MODE) console.log('X-Dec clicked: subtracting 5 from X');
            viewer?.updateSpacing(-5, 0);
            showSpacingFeedback();
            saveDisplayPrefs();
        });
        getEl('btn-spacing-y-inc')?.addEventListener('click', () => {
            if (DEBUG_MODE) console.log('Y-Inc clicked: adding 5 to Y');
            viewer?.updateSpacing(0, 5);
            showSpacingFeedback();
            saveDisplayPrefs();
        });
        getEl('btn-spacing-y-dec')?.addEventListener('click', () => {
            if (DEBUG_MODE) console.log('Y-Dec clicked: subtracting 5 from Y');
            viewer?.updateSpacing(0, -5);
            showSpacingFeedback();
            saveDisplayPrefs();
        });

        // Font Size Controls
        ['input-support-font', 'input-tip-font'].forEach(id => {
            const el = getEl(id);
            if (el) {
                el.addEventListener('input', () => { viewer?.applyTextSizing(); saveDisplayPrefs(); });
                el.addEventListener('change', () => { viewer?.applyTextSizing(); saveDisplayPrefs(); });
            }
        });

        // Alan 5/9/26 - Wire live sequence metric sliders so dragging them hides matching tips in the rendered tree.
        ['slider-query-cover', 'slider-subject-cover', 'slider-identity'].forEach(id => {
            getEl(id)?.addEventListener('input', scheduleSequenceFilterUpdate);
        });
        getEl('btn-sequence-filter-reset')?.addEventListener('click', () => {
            if (!viewer) return;
            const stats = viewer.resetSequenceFilters();
            syncSequenceFilterUI(stats);
            showStatus("Sequence filters reset.", "info", 1200);
        });

        // Filter / Search
        const inputFilter = getEl('input-branch-filter');
        const btnFilterClear = getEl('btn-filter-clear');
        if (inputFilter) {
            inputFilter.addEventListener('input', (e) => {
                if (!viewer) return;
                const val = e.target.value;
                if (filterDebounce) clearTimeout(filterDebounce);
                filterDebounce = setTimeout(() => {
                    // Safe cleanup check: ensure viewer and element still exist
                    if (!viewer || !inputFilter || !document.body.contains(inputFilter)) return;

                    viewer.applyFilter(val);
                    if (btnFilterClear) {
                        if (val) btnFilterClear.classList.remove('hidden');
                        else btnFilterClear.classList.add('hidden');
                    }
                }, 300);
            });
            btnFilterClear?.addEventListener('click', () => {
                if (!viewer) return;
                inputFilter.value = '';
                viewer.applyFilter('');
                btnFilterClear.classList.add('hidden');
            });
        }

        // Selection Actions
        document.querySelectorAll('.btn-selection-action').forEach(btn => {
            btn.addEventListener('click', () => {
                if (!viewer) return;
                const action = btn.getAttribute('data-action');

                // Handle special selection methods that need their own logic
                if (action === 'select-ancestors') {
                    if (viewer.getSelectionCount() === 0) {
                        showStatus("Select at least one node first.", "warning", 2000);
                        return;
                    }
                    viewer.selectMaxParsimony();
                    showStatus("Selected MRCA and connecting paths.", "success", 2000);
                    return;
                }

                if (action === 'select-descendants') {
                    if (viewer.getSelectionCount() === 0) {
                        showStatus("Select at least one node first.", "warning", 2000);
                        return;
                    }
                    viewer.selectAllDescendants();
                    showStatus("Selected all descendants.", "success", 2000);
                    return;
                }

                // Predicate for select-filtered
                let predicate = null;
                if (action === 'select-filtered') {
                    const filterVal = inputFilter?.value || "";
                    if (!filterVal) { showStatus("No filter text applied.", "warning", 2000); return; }
                    predicate = (node) => {
                        const name = node.data.name || "";
                        return name.toLowerCase().includes(filterVal.toLowerCase());
                    };
                }

                // Replacing selection behavior: Clear active set first, then apply action
                viewer.clearActiveSelection();
                viewer.selectionAction(action, predicate);

                // Visual feedback (optional)
                const setEl = getEl('select-selection-set');
                if (setEl) {
                    setEl.classList.add('ring-2', 'ring-green-400');
                    setTimeout(() => setEl.classList.remove('ring-2', 'ring-green-400'), 400);
                }
            });
        });

        // --- SELECTION SET MANAGEMENT ---
        const selectSet = getEl('select-selection-set');
        const colorSwatch = getEl('selection-set-color');
        const btnNewSet = getEl('btn-new-selection-set');
        const btnDeleteSet = getEl('btn-delete-selection-set');

        // Helper to sync UI with viewer selection set state (also assigned to outer scope for loadTree access)
        updateSelectionSetUI = function updateSelectionSetUI() {
            if (!viewer || !selectSet) return;

            const names = viewer.getSelectionSetNames();
            const active = viewer.getActiveSelectionSet();

            // Rebuild dropdown options
            selectSet.innerHTML = '';
            names.forEach(name => {
                const opt = document.createElement('option');
                opt.value = name;
                opt.textContent = name;
                if (name === active) opt.selected = true;
                selectSet.appendChild(opt);
            });

            // Update color swatch
            if (colorSwatch) {
                const color = viewer.getSelectionSetColor(active);
                colorSwatch.style.background = color || '#1f77b4';
            }

            // Enable/disable delete button (always disable for 'Default')
            if (btnDeleteSet) {
                btnDeleteSet.disabled = (active === 'Default');
            }
        }

        // Initial sync after viewer is ready
        if (viewer) updateSelectionSetUI();

        // Dropdown change: switch active set
        selectSet?.addEventListener('change', () => {
            if (!viewer) return;
            const name = selectSet.value;
            if (viewer.setActiveSelectionSet(name)) {
                updateSelectionSetUI();
                showStatus(`Switched to "${name}" selection set.`, "info", 1500);
            }
        });

        // New Set button
        btnNewSet?.addEventListener('click', () => {
            if (!viewer) return;
            const name = prompt("Enter a name for the new selection set:");
            if (!name) return;

            if (viewer.createSelectionSet(name.trim())) {
                viewer.setActiveSelectionSet(name.trim());
                updateSelectionSetUI();
                saveSelectionSets();
                showStatus(`Created selection set "${name.trim()}".`, "success", 2000);
            } else {
                showStatus(`Set "${name.trim()}" already exists or is invalid.`, "warning", 2500);
            }
        });

        // Delete Set button
        btnDeleteSet?.addEventListener('click', () => {
            if (!viewer) return;
            const active = viewer.getActiveSelectionSet();
            if (active === 'Default') {
                showStatus("Cannot delete the Default set.", "warning", 2000);
                return;
            }
            if (!confirm(`Delete selection set "${active}"? This will clear its selections.`)) return;

            if (viewer.deleteSelectionSet(active)) {
                updateSelectionSetUI();
                saveSelectionSets();
                showStatus(`Deleted selection set "${active}".`, "success", 2000);
            }
        });

        // Display Options (Incremental)
        const updateOpts = () => {
            if (!viewer) return;
            const elMin = getEl('input-min-tips');
            const elPp = getEl('input-pp-threshold');
            const elBs = getEl('input-bs-threshold');
            const chkFilterLow = getEl('cb-hide-low-support');
            const applyFilter = chkFilterLow ? chkFilterLow.checked : true;

            const ppVal = elPp ? Math.max(0, Math.min(1, parseFloat(elPp.value) || 0)) : 0;
            const bsVal = elBs ? (parseInt(elBs.value) || 0) : 0;

            viewer.setOptions({
                minTips: elMin ? (parseInt(elMin.value) || 0) : 0,
                ppThreshold: applyFilter ? ppVal : 0, // If disabled, effectively 0
                bootstrapThreshold: applyFilter ? bsVal : 0
            });
            // Update UI state (disable inputs if filter is off)
            updateSupportUI(viewer.getStats());
        };
        ['input-min-tips', 'input-pp-threshold', 'input-bs-threshold'].forEach(id => {
            getEl(id)?.addEventListener('change', updateOpts);
        });

        // Support Toggle
        const btnToggleSupport = getEl('btn-toggle-support');
        if (btnToggleSupport) {
            // Init state from viewer
            btnToggleSupport.classList.toggle('active', viewer.options.showSupport);

            btnToggleSupport.addEventListener('click', () => {
                if (!viewer) return;
                const nextState = !viewer.options.showSupport;
                viewer.toggleSupport(nextState);
                btnToggleSupport.classList.toggle('active', nextState);

                // Sync UI with correct support-type-specific enabling
                updateSupportUI(viewer.getStats());
            });
        }

        // Filter Low Support Checkbox
        const chkFilterLow = getEl('cb-hide-low-support');
        chkFilterLow?.addEventListener('change', () => {
            // Don't modify inputs, just update options logic
            updateOpts();
        });

        // SVG Save
        getEl('btn-save-svg')?.addEventListener('click', (e) => {
            e.preventDefault();
            if (!viewer) { showStatus("Tree not loaded yet.", "warning", 2000); return; }
            showStatus("Preparing SVG download\u2026", "info");
            try {
                viewer.exportSVG();
                showStatus("SVG downloaded.", "success", 2500);
            } catch (err) {
                console.error("SVG export error:", err);
                const msg = err instanceof Error ? err.message : String(err);
                showStatus("SVG export failed: " + msg, "danger", 5000);
            }
        });

        // JPG Save
        getEl('btn-save-jpg')?.addEventListener('click', async (e) => {
            e.preventDefault();
            if (!viewer) { showStatus("Tree not loaded yet.", "warning", 2000); return; }
            showStatus("Preparing JPG download\u2026", "info");
            try {
                await viewer.exportJPG();
                showStatus("JPG downloaded.", "success", 2500);
            } catch (err) {
                console.error("JPG export error:", err);
                const msg = err instanceof Error ? err.message : String(err);
                showStatus("JPG export failed: " + msg, "danger", 5000);
            }
        });

        // Tree Edit Actions (Backend)
        wireBackendActions();
    }

    const triggerZoom = (delta) => {
        const svg = container?.querySelector('svg');
        if (!svg) return;
        const rect = svg.getBoundingClientRect();
        const cx = rect.left + rect.width / 2;
        const cy = rect.top + rect.height / 2;
        svg.dispatchEvent(new WheelEvent('wheel', {
            clientX: cx, clientY: cy, deltaY: delta,
            bubbles: true, cancelable: true, view: window
        }));
    };

    function wireBackendActions() {
        if (btnPrune) btnPrune.addEventListener('click', () => {
            if (!viewer) return;
            const nodes = viewer.getSelectedNodes();
            if (nodes.length === 0) return;

            const names = nodes.map(n => n.data.__original_name || n.data.name);
            const displayNames = nodes.map(n => n.data.name).join(", ");
            // No confirmation requested

            runBackendAction(`Pruning ${nodes.length} nodes`, async () => {
                await TreeEditActions.pruneTaxa(JOB_ID, names);
            });
        });

        if (btnRename) btnRename.addEventListener('click', () => {
            if (!viewer) return;
            const nodes = viewer.getSelectedNodes();
            // Alan 5/11/26 - Rename every currently visible clicked selection, independent of selection sets.
            if (nodes.length === 0) return;
            openRenameModal(nodes);
        });

        if (btnReroot) btnReroot.addEventListener('click', () => {
            if (isProcessing) return;
            if (rerootMode) {
                rerootMode = false;
                removeRerootCapture();
                showStatus("Reroot cancelled.", "info", 1000);
            } else {
                rerootMode = true;
                if (viewer) viewer.clearSelection(); // Clear selection for reroot mode
                installRerootCapture();
                showStatus("Click a node to reroot.", "info");
            }
            updateButtons();
        });

        if (btnMidpoint) btnMidpoint.addEventListener('click', () => {
            const actionName = isMidpointRooted ? "Disabling midpoint rooting" : "Enabling midpoint rooting";
            runBackendAction(actionName, async () => {
                const result = await TreeEditActions.midpointRootToggle(JOB_ID);
                isMidpointRooted = result.is_midpoint_rooted ?? !isMidpointRooted;
                updateMidpointButton();
            });
        });

        // Alan 5/11/26 - Clear the currently visible selection without deleting saved selection-set membership.
        if (btnDeselect) btnDeselect.addEventListener('click', () => {
            if (!viewer || typeof viewer.deselectCurrentSelection !== 'function') return;
            const cleared = viewer.deselectCurrentSelection();
            updateButtons();
            if (cleared > 0) {
                // Alan 5/11/26 - Report deselected sequences using the same visible-selection count shown on the button.
                showStatus(`Deselected ${cleared} sequence${cleared === 1 ? '' : 's'}.`, "info", 1500);
            }
        });

        if (btnRecompute) btnRecompute.addEventListener('click', () => {
            if (!confirm("Recompute tree?")) return;
            runBackendAction("Recomputing", async () => {
                await TreeEditActions.recomputeTree(JOB_ID);
            });
        });



        document.addEventListener('keydown', (e) => {
            // Alan 5/11/26 - Escape closes the rename modal before handling reroot cancellation.
            if (e.key === "Escape" && renameModal && !renameModal.classList.contains('hidden')) {
                closeRenameModal();
                return;
            }
            // Alan 5/11/26 - Escape turns off persistent Box Select mode when no modal has focus.
            if (e.key === "Escape" && viewer?.getBoxSelectMode?.()) {
                // Alan 5/11/26 - Route through the viewer so cursor and button callbacks reset together.
                viewer.setBoxSelectMode(false);
                // Alan 5/11/26 - Confirm the mode change without interrupting other viewer state.
                showStatus("Box Select off.", "info", 1000);
                return;
            }
            if (e.key === "Escape" && rerootMode) {
                rerootMode = false; removeRerootCapture();
                showStatus("Reroot cancelled.", "info", 1000); updateButtons();
            }
        });

        // Reroot Capture
        function installRerootCapture() {
            if (!container || rerootCaptureHandler) return;
            rerootCaptureHandler = async (e) => {
                if (!rerootMode || !viewer) return;

                // Use robust helper
                let nodeName = null;
                if (typeof viewer.getNodeIdFromEvent === 'function') {
                    nodeName = viewer.getNodeIdFromEvent(e);
                } else {
                    // Fallback
                    const g = e.target.closest('g.node, g.internal-node');
                    if (g && window.d3v7) {
                        const d = window.d3v7.select(g).datum();
                        nodeName = d?.data?.__original_name || d?.data?.name || d?.data?.id || d?.name;
                    }
                }

                if (!nodeName) {
                    // Only complain if they clicked something that looks like a node
                    if (e.target.closest('g.node, g.internal-node')) {
                        showStatus("Can't reroot: no stable ID.", "warning", 2500);
                    }
                    return;
                }

                // Stop prop only if we found a node
                e.preventDefault(); e.stopPropagation();
                if (e.stopImmediatePropagation) e.stopImmediatePropagation();

                runBackendAction("Rerooting", async () => {
                    await TreeEditActions.reroot(JOB_ID, nodeName);
                });
            };
            container.addEventListener("click", rerootCaptureHandler, true);
            container.addEventListener("contextmenu", rerootCaptureHandler, true);
        }
        // removeRerootCapture is now top-level
    }

    function updateButtons() {
        if (!viewer) return;
        // Alan 5/11/26 - Keep Box Select styling current whenever broader toolbar state refreshes.
        updateBoxSelectButton();

        // Multi-select check
        let selCount = 0;
        if (typeof viewer.getSelectionCount === 'function') {
            selCount = viewer.getSelectionCount();
        }

        // Alan 5/11/26 - Keep Deselect tied to visible active selections, not saved selection sets.
        const updateDeselectButton = (forceDisabled = false) => {
            if (!btnDeselect) return;
            const disabled = forceDisabled || selCount === 0;
            btnDeselect.disabled = disabled;
            btnDeselect.title = selCount > 0 ? `Deselect ${selCount} sequence${selCount === 1 ? '' : 's'}` : "No current selection";
            // Alan 5/11/26 - Show the Deselect count directly on the button like Prune does.
            btnDeselect.innerHTML = selCount > 0 ? '<i class="fa fa-mouse-pointer"></i> Deselect (' + selCount + ')' : '<i class="fa fa-mouse-pointer"></i> Deselect';
            btnDeselect.classList.toggle('opacity-50', disabled);
            btnDeselect.classList.toggle('cursor-not-allowed', disabled);
        };

        // View Only Mode Logic
        if (window.VIEW_ONLY) {
            const disableBtn = (btn) => {
                if (btn) {
                    btn.disabled = true;
                    btn.classList.add('opacity-50', 'cursor-not-allowed');
                    btn.title = "View Only - Make an editable copy to use this feature";
                }
            };
            disableBtn(btnPrune);
            disableBtn(btnRename);
            disableBtn(btnReroot);
            disableBtn(btnMidpoint);
            disableBtn(btnRecompute);
            // btnMakeCopy remains enabled
            // Alan 5/11/26 - Leave Deselect available in view-only mode because it only changes local highlighting.
            updateDeselectButton(false);
            return;
        }

        const isMulti = selCount > 1;
        const hasSelection = selCount === 1; // Only enable if exactly 1 node selected

        // Processing / Reroot overrides
        if (isProcessing) {
            if (btnPrune) btnPrune.disabled = true;
            if (btnRename) btnRename.disabled = true;
            if (btnReroot) btnReroot.disabled = true;
            if (btnMidpoint) btnMidpoint.disabled = true;
            if (btnRecompute) btnRecompute.disabled = true;
            if (btnMakeCopy) btnMakeCopy.disabled = true;
            // Alan 5/11/26 - Freeze Deselect while backend actions are refreshing the tree.
            updateDeselectButton(true);
            return;
        }

        // Normal state
        // Alan 5/11/26 - Refresh Deselect whenever normal edit buttons are refreshed.
        updateDeselectButton(false);
        if (btnPrune) {
            btnPrune.disabled = !hasSelection && !isMulti;
            btnPrune.title = (hasSelection || isMulti) ? `Prune ${selCount} node${selCount > 1 ? 's' : ''}` : "Select nodes to prune";
            // Alan 5/10/26 - Always show the active prune count, including a single selected sequence.
            btnPrune.innerHTML = selCount > 0 ? '<i class="fa fa-cut"></i> Prune (' + selCount + ')' : '<i class="fa fa-cut"></i> Prune';
        }
        if (btnRename) {
            // Alan 5/11/26 - Allow multi-rename and show the active visible selection count.
            btnRename.disabled = selCount === 0;
            btnRename.title = selCount > 0 ? `Rename ${selCount} sequence${selCount === 1 ? '' : 's'}` : "Select sequences to rename";
            btnRename.innerHTML = selCount > 0 ? '<i class="fa fa-edit"></i> Rename (' + selCount + ')' : '<i class="fa fa-edit"></i> Rename';
        }

        if (btnReroot) {
            btnReroot.disabled = false;
            if (rerootMode) {
                btnReroot.classList.add("active");
                btnReroot.innerHTML = '<i class="fa fa-times"></i> Cancel Reroot';
            } else {
                btnReroot.classList.remove("active");
                btnReroot.innerHTML = '<i class="fa fa-map-pin"></i> Reroot';
            }
        }
        if (btnMidpoint) btnMidpoint.disabled = false;
        if (btnRecompute) btnRecompute.disabled = false;
    }

    function updateMidpointButton() {
        if (!btnMidpoint) return;
        if (isMidpointRooted) {
            btnMidpoint.classList.add('active');
            btnMidpoint.innerHTML = '<i class="fa fa-balance-scale"></i> Midpoint (On)';
            btnMidpoint.title = 'Click to disable midpoint rooting';
        } else {
            btnMidpoint.classList.remove('active');
            btnMidpoint.innerHTML = '<i class="fa fa-balance-scale"></i> Midpoint';
            btnMidpoint.title = 'Click to enable midpoint rooting';
        }
    }

    // Alan 5/9/26 - Keep sequence metric sliders and labels synced to the loaded tree.
    function syncSequenceFilterUI(stats) {
        const sliderConfig = [
            ['slider-query-cover', 'query-cover-value', 'queryCoverThreshold'],
            ['slider-subject-cover', 'subject-cover-value', 'subjectCoverThreshold'],
            ['slider-identity', 'identity-value', 'identityThreshold']
        ];
        const metricsAvailable = Boolean(stats?.metricsAvailable);
        const countBadge = getEl('sequence-filter-count');

        if (countBadge) {
            const visibleTips = Math.max(0, Number(stats?.visibleTips) || 0);
            const totalTips = Math.max(0, Number(stats?.totalTips) || 0);
            const metricTips = Math.max(0, Number(stats?.metricTips) || 0);
            countBadge.textContent = metricsAvailable
                ? `Metrics: ${visibleTips}/${totalTips} tips (${metricTips})`
                : 'Metrics: none';
        }

        sliderConfig.forEach(([sliderId, valueId, statKey]) => {
            const slider = getEl(sliderId);
            const valueEl = getEl(valueId);
            const rawValue = Number(stats?.[statKey]);
            const value = Number.isFinite(rawValue) ? Math.max(0, Math.min(100, Math.round(rawValue))) : 0;
            // Alan 5/9/26 - Flip only the visible slider label/position so 0% is on the left while leftward dragging still raises the hidden threshold.
            const displayValue = 100 - value;
            if (slider) {
                // Alan 5/9/26 - Use the display value for the thumb position; the renderer still receives the inverted threshold.
                slider.value = String(displayValue);
                slider.disabled = !metricsAvailable;
                slider.classList.toggle('opacity-40', !metricsAvailable);
                slider.classList.toggle('cursor-not-allowed', !metricsAvailable);
            }
            if (valueEl) {
                // Alan 5/9/26 - Show the visual percentage scale rather than the inverted filtering threshold.
                valueEl.textContent = metricsAvailable ? `${displayValue}%` : '--';
            }
        });
    }

    // Alan 5/9/26 - Read current metric slider values into renderer filter options.
    function getSequenceFilterSliderOptions() {
        const readSlider = (id) => {
            const slider = getEl(id);
            // Alan 5/9/26 - Invert the visual scale so dragging left still increases the filter threshold.
            return slider && !slider.disabled ? 100 - parseInt(slider.value, 10) : 0;
        };
        return {
            queryCoverThreshold: readSlider('slider-query-cover'),
            subjectCoverThreshold: readSlider('slider-subject-cover'),
            identityThreshold: readSlider('slider-identity')
        };
    }

    // Alan 5/9/26 - Apply metric slider changes on animation frames for responsive tree filtering.
    function scheduleSequenceFilterUpdate() {
        if (!viewer) return;
        if (sequenceFilterFrame) cancelAnimationFrame(sequenceFilterFrame);
        sequenceFilterFrame = requestAnimationFrame(() => {
            sequenceFilterFrame = null;
            const stats = viewer.setSequenceFilterOptions(getSequenceFilterSliderOptions());
            syncSequenceFilterUI(stats);
        });
    }

    function updateSupportUI(stats) {
        if (!stats) return;
        const badge = getEl('support-type-badge');
        const ppInput = getEl('input-pp-threshold');
        const bsInput = getEl('input-bs-threshold');
        const chkFilterLow = getEl('cb-hide-low-support');
        // If checkbox is missing, default to true (filtering enabled by default)
        const filterEnabled = chkFilterLow ? chkFilterLow.checked : true;

        // Truth Table:
        // 1. Show Support OFF -> All Disabled
        // 2. Filter Low OFF -> All Disabled (per user request: strict table)
        // 3. Otherwise -> Enabled based on type

        const showSupport = viewer ? viewer.options.showSupport : true;

        if (badge) {
            let label = 'None';
            if (stats.supportType === 'BS') label = 'Bootstrap';
            else if (stats.supportType === 'PP') label = 'Posterior';
            else if (stats.supportType === 'SH') label = 'FastTree SH-like';
            else if (stats.supportType === 'mixed') label = 'Mixed';

            badge.textContent = `Support: ${label}`;
            badge.className = "px-2 py-0.5 text-xs font-semibold rounded shrink-0 transition-colors";

            if (stats.supportType === 'BS') {
                badge.classList.add('text-blue-800', 'bg-blue-100', 'dark:text-blue-200', 'dark:bg-blue-900/40');
            }
            else if (stats.supportType === 'PP') {
                badge.classList.add('text-purple-800', 'bg-purple-100', 'dark:text-purple-200', 'dark:bg-purple-900/40');
            }
            else if (stats.supportType === 'SH') {
                // Teal for FastTree SH
                badge.classList.add('text-teal-800', 'bg-teal-100', 'dark:text-teal-200', 'dark:bg-teal-900/40');
            }
            else if (stats.supportType === 'mixed') {
                badge.classList.add('text-amber-800', 'bg-amber-100', 'dark:text-amber-200', 'dark:bg-amber-900/40');
            }
            else {
                badge.classList.add('text-gray-800', 'bg-gray-100', 'dark:text-gray-200', 'dark:bg-gray-700/40');
            }
        }

        const setInput = (inp, en) => {
            if (!inp) return;
            inp.disabled = !en;
            if (en) inp.classList.remove('opacity-40', 'cursor-not-allowed');
            else inp.classList.add('opacity-40', 'cursor-not-allowed');
        };

        const globalEnable = showSupport && filterEnabled;
        const s = stats.supportType;

        // Dynamic Label for PP/SH input
        if (ppInput) {
            const container = ppInput.parentElement;
            const labelSpan = container.querySelector('span');
            if (labelSpan) {
                if (s === 'SH') labelSpan.textContent = "SH >";
                else labelSpan.textContent = "PP >";
            }
            setInput(ppInput, globalEnable && (s === 'PP' || s === 'mixed' || s === 'SH'));
        }

        if (bsInput) setInput(bsInput, globalEnable && (s === 'BS' || s === 'mixed'));
    }

    // START
    loadTree();
});
