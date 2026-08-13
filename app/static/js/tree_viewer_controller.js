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
    // Alan 5/13/26 - Cache the Alignment Viewer launcher so its count stays synced to tree state.
    const btnAlignmentViewer = getEl('btn-alignment-viewer');
    // Alan 5/29/26 - Cache new rooting-mode dropdown + sequence-of-interest button.
    const rootingModeSelect = getEl('rooting-mode-select');
    const btnSetSoi = getEl('btn-set-soi');
    const btnRecompute = getEl('btn-recompute');
    const btnMakeCopy = getEl('btn-make-copy');
    // Alan 5/12/26 - Cache clear-color control for selected tips.
    const btnUncolorSelection = getEl('btn-uncolor-selection');
    // Alan 5/11/26 - Cache rename modal elements for editing the current visible selection.
    const renameModal = getEl('modal-rename-sequences');
    const renameModalRows = getEl('rename-modal-rows');
    const renameModalSubtitle = getEl('rename-modal-subtitle');
    const btnRenameModalSave = getEl('btn-rename-modal-save');
    const btnRenameModalCancel = getEl('btn-rename-modal-cancel');
    const btnRenameModalClose = getEl('btn-rename-modal-close');
    const renameModalBackdrop = getEl('rename-modal-backdrop');
    // Alan 7/20/26 - Cache keyboard-help and advanced-selection menu controls for click and hotkey access.
    const shortcutHelpModal = getEl('modal-tree-shortcuts');
    const shortcutHelpBackdrop = getEl('tree-shortcuts-backdrop');
    const btnShortcutHelpClose = getEl('btn-tree-shortcuts-close');
    // Alan 7/21/26 - Cache the visible toolbar launcher for the shared keyboard-shortcut help modal.
    const btnShortcutHelpOpen = getEl('btn-tree-shortcuts-help');
    const btnSelectionMore = getEl('btn-selection-more');
    const selectionMoreMenu = getEl('selection-more-menu');

    // --- HELPER: STATUS MESSAGE ---
    let currentStatusType = null;
    // Alan 6/1/26 - Track the pending hide-timer so a new message cancels the
    // previous one. Without this, an earlier timed message (e.g. "...completed.")
    // hides a later sticky message when its old timeout fires.
    let statusHideTimer = null;
    function showStatus(msg, type, timeout = 0) {
        if (!statusMsg) return;
        if (statusHideTimer) { clearTimeout(statusHideTimer); statusHideTimer = null; }
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
        if (timeout > 0) statusHideTimer = setTimeout(() => statusMsg.classList.add('hidden'), timeout);
    }

    // --- STATE ---
    let viewer = null;
    // selectedNode Removed - using viewer state
    let isProcessing = false;
    let rerootMode = false;
    let uiWired = false;
    let isLoadingTree = false;
    // Alan 7/15/26 - Serialize tree loads so an edit-triggered redraw waits behind any load already in progress.
    let treeLoadQueue = Promise.resolve();
    let isMidpointRooted = true; // Default: midpoint rooted on load
    // Alan 5/29/26 - Track rooting state so the SOI button can hide unless auto root needs help.
    let needsSequenceOfInterest = false;
    // Alan 5/29/26 - Keep the loaded tree state around so other viewers can reuse persisted metadata.
    let treeState = null;
    // Reroot Capture State (Moved top-level to fix reference errors)
    let rerootCaptureHandler = null;
    let filterDebounce = null;
    let selectionSaveDebounce = null;
    // Alan 5/9/26 - Throttle live sequence metric filter redraws to one frame while the user drags.
    let sequenceFilterFrame = null;
    // Alan 5/11/26 - Hold only the clicked/current selection being edited in the rename modal.
    let pendingRenameItems = [];
    let updateSelectionSetUI = () => {}; // assigned in wireUI()

    // Alan 7/20/26 - Toggle the advanced selection menu through both mouse and keyboard-accessible state.
    function setSelectionMoreMenuOpen(open) {
        if (!btnSelectionMore || !selectionMoreMenu) return;
        selectionMoreMenu.classList.toggle('hidden', !open);
        btnSelectionMore.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    // Alan 7/20/26 - Open a compact reference for the tree viewer's supported keyboard shortcuts.
    function openShortcutHelp() {
        if (!shortcutHelpModal) return;
        shortcutHelpModal.classList.remove('hidden');
        document.body.classList.add('overflow-hidden');
        btnShortcutHelpClose?.focus();
    }

    // Alan 7/20/26 - Close keyboard help without changing tree selection or viewer mode.
    function closeShortcutHelp() {
        if (!shortcutHelpModal) return;
        shortcutHelpModal.classList.add('hidden');
        document.body.classList.remove('overflow-hidden');
    }

    // Alan 7/20/26 - Share one direct deselect action so the D hotkey cannot be blocked by stale button state.
    function deselectCurrentTreeSelection() {
        if (isProcessing || !viewer || typeof viewer.deselectCurrentSelection !== 'function') return 0;
        const cleared = viewer.deselectCurrentSelection();
        updateButtons();
        if (cleared > 0) {
            showStatus(`Deselected ${cleared} sequence${cleared === 1 ? '' : 's'}.`, "info", 1500);
        }
        return cleared;
    }

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

    // Alan 6/2/26 - Match the backend's stable internal-node ID hash. Sort by Unicode code
    // point and hash per code point (not UTF-16 unit) so this fallback agrees with Python's
    // sorted()/ord() even for non-BMP tip names.
    function stableInternalNodeIdFromTipNames(tipNames) {
        const names = (Array.isArray(tipNames) ? tipNames : [])
            .filter(Boolean)
            .map(String)
            .sort((a, b) => {
                const ca = Array.from(a), cb = Array.from(b);
                const n = Math.min(ca.length, cb.length);
                for (let i = 0; i < n; i += 1) {
                    const d = ca[i].codePointAt(0) - cb[i].codePointAt(0);
                    if (d !== 0) return d;
                }
                return ca.length - cb.length;
            });
        const key = names.join('\x1f');
        let hash = 2166136261;
        for (const ch of key) {
            hash ^= ch.codePointAt(0);
            hash = Math.imul(hash, 16777619) >>> 0;
        }
        return `internal:${hash.toString(16).padStart(8, '0')}`;
    }

    // Alan 5/29/26 - Collect descendant tip IDs from a rendered D3 tree node without using child index paths.
    function getDescendantTipNames(node) {
        const names = [];
        const visit = (current) => {
            if (!current) return;
            const children = current.children || current.data?.children || [];
            if (!children || children.length === 0) {
                const data = current.data || current;
                const name = data.__original_name || data.original_name || data.name || current.name;
                if (name) names.push(name);
                return;
            }
            children.forEach(visit);
        };
        visit(node);
        return names;
    }

    // Alan 6/4/26 - Resolve a right-clicked node into the backend prune target for leaves or internal subtrees.
    function getPruneTargetForNode(node) {
        const data = node?.data || node || {};
        const children = node?.children || data.children || [];
        if (!children || children.length === 0) return data.__original_name || data.original_name || data.name || node?.name || null;
        const tipNames = getDescendantTipNames(node);
        return data.stable_id || stableInternalNodeIdFromTipNames(tipNames);
    }

    // Alan 6/4/26 - Prefer the displayed sequence label for context-menu clipboard copies.
    function getDisplaySequenceName(node) {
        const data = node?.data || node || {};
        return data.name || node?.name || data.__original_name || data.original_name || "";
    }

    // Alan 6/4/26 - Copy sequence labels with a textarea fallback for older browsers.
    async function copyTextToClipboard(text) {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(text);
            return;
        }
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.setAttribute('readonly', '');
        textarea.style.position = 'fixed';
        textarea.style.left = '-9999px';
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand('copy');
        document.body.removeChild(textarea);
    }

    // Alan 5/29/26 - Alignment Viewer should follow the persisted focal tip, falling back to the single saved Default member.
    function getPreferredAlignmentTip(state) {
        if (!state) return null;
        if (state.sequence_of_interest) return state.sequence_of_interest;
        const selectionSets = state.selection_sets || {};
        const defaultMembers = Array.isArray(selectionSets.Default) ? selectionSets.Default : [];
        if (defaultMembers.length === 1) return defaultMembers[0];
        const activeName = state.active_selection_set;
        const activeMembers = activeName && Array.isArray(selectionSets[activeName]) ? selectionSets[activeName] : [];
        if (activeMembers.length === 1) return activeMembers[0];
        return null;
    }

    function debouncedSaveSelectionSets() {
        if (selectionSaveDebounce) clearTimeout(selectionSaveDebounce);
        selectionSaveDebounce = setTimeout(saveSelectionSets, 800);
    }

    // Alan 5/12/26 - Save selection/color sets immediately after destructive prune cleanup.
    async function saveSelectionSetsNow() {
        // Alan 5/12/26 - Cancel pending debounced saves so stale pre-prune colors cannot overwrite cleanup.
        if (selectionSaveDebounce) {
            // Alan 5/12/26 - Clear the pending save timer before forcing a fresh save.
            clearTimeout(selectionSaveDebounce);
            // Alan 5/12/26 - Reset the timer handle after cancellation.
            selectionSaveDebounce = null;
        }
        // Alan 5/12/26 - Persist the viewer's current selection/color state.
        await saveSelectionSets();
    }

    // Alan 6/4/26 - Allow internal-node prune callers to clean up descendant tip color annotations too.
    async function pruneTaxaPreservingSelectionColors(names, cleanupNames = names) {
        // Alan 5/12/26 - Run the existing backend prune action first so failed prunes do not mutate saved colors.
        const pruneResult = await TreeEditActions.pruneTaxa(JOB_ID, names);
        // Warn about names the backend could not resolve; the rest still pruned.
        const unresolved = pruneResult?.prune_unresolved;
        if (Array.isArray(unresolved) && unresolved.length) {
            showStatus(
                `Pruned the rest; ${unresolved.length} node${unresolved.length > 1 ? 's were' : ' was'} not found in the tree.`,
                "warning",
                4000
            );
        }
        // Alan 5/12/26 - Remove only the pruned tips from saved selection sets; keep other colored labels.
        if (viewer?.removeIdsFromSelectionSets) viewer.removeIdsFromSelectionSets(cleanupNames);
        // Alan 5/12/26 - Persist the pruned selection-set cleanup before the tree reloads.
        await saveSelectionSetsNow();
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
        // Alan 7/20/26 - Keep the B shortcut visible whenever an optional Box Select toolbar button is present.
        btnBoxSelect.title = active
            ? 'Box Select is on (B). Drag empty tree background to select tips; Alt removes; Ctrl/Cmd toggles; Esc cancels.'
            : 'Box Select (B). Right-drag empty tree background anytime, or turn this on for left-drag selection.';
    }

    // Alan 7/20/26 - Toggle Box Select directly so the B hotkey works even when no toolbar button is rendered.
    function toggleBoxSelectMode() {
        if (!viewer?.setBoxSelectMode) return false;
        const enabled = viewer.setBoxSelectMode(!viewer.getBoxSelectMode());
        updateBoxSelectButton();
        showStatus(enabled ? "Box Select on. Drag empty tree background." : "Box Select off.", "info", 1500);
        return true;
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
            // Alan 7/17/26 - Replace spacing with the saved values so repeated tree redraws cannot compound them.
            viewer.setSpacingState(prefs.spacingX || 0, prefs.spacingY || 0);
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
    // Alan 5/31/26 - Build the final status line for a rooting action and refresh
    // needsSequenceOfInterest. Returned to runBackendAction so it can be shown
    // AFTER the tree reloads; otherwise the generic "completed" message and the
    // reload clobber it and "Auto root chose: X" only flashes for an instant.
    // Alan 8/4/26 - Mirror the rooting message into a persistent banner above the tree.
    // It used to exist only as a toast, so how the tree was rooted disappeared as soon
    // as the next action fired a new status. The banner shares a container with the
    // server-rendered duplicate-removal notice.
    // Returns true when the banner took the message, so callers can skip the toast
    // instead of showing the same sentence twice.
    function setRootingNotice(text) {
        const wrap = getEl('tree-notices');
        const row = getEl('notice-rooting');
        const slot = getEl('notice-rooting-text');
        if (!wrap || !row || !slot) return false;
        if (!text) {
            row.classList.add('hidden');
            row.classList.remove('flex');
            return false;
        }
        slot.textContent = text;
        row.classList.remove('hidden');
        row.classList.add('flex');
        wrap.classList.remove('hidden');
        return true;
    }

    function rootingFinalStatus(mode, result) {
        needsSequenceOfInterest = !!(result && result.needs_sequence_of_interest);
        const info = (result && result.rooting_info) || {};
        if (needsSequenceOfInterest) {
            return {
                msg: "Click the sequence you care about, then press \"Set Sequence of Interest\"; Auto root will then choose a useful display root from the other high-quality hits.",
                type: "warning",
                timeout: 0
            };
        }
        if (info.chosen_by === 'midpoint_fallback') {
            return { msg: `Auto root fell back to midpoint (${info.reason || 'no_acceptable_candidates'}).`, type: "warning", timeout: 6000 };
        }
        if (info.chosen_by === 'auto' || info.chosen_by === 'most_divergent_hit') {
            const target = info.chosen_root_target || (result && result.root_target) || 'selected tip';
            const label = info.chosen_by === 'most_divergent_hit'
                ? `Auto root used the most divergent hit: ${target}`
                : `Auto root chose outgroup: ${target}`;
            // Alan 5/31/26 - Keep the chosen-outgroup message up (sticky) so it can actually be read.
            // Alan 8/4/26 - The persistent banner now carries this, so suppress the duplicate
            // toast whenever the banner accepted it; a short confirmation is enough.
            if (setRootingNotice(label)) {
                return { msg: "Rooting updated.", type: "success", timeout: 2500 };
            }
            return { msg: label, type: "success", timeout: 0 };
        }
        if (mode === 'midpoint') return { msg: "Midpoint root applied.", type: "success", timeout: 2500 };
        if (mode === 'unrooted') return { msg: "Unrooted layout is not yet rendered; tree shown as rooted.", type: "warning", timeout: 4000 };
        return null;
    }

    async function runBackendAction(name, actionFn, options = {}) {
        if (isProcessing) return;
        isProcessing = true;
        updateButtons(); // Disable
        try {
            showStatus(name + "...", "info");
            const actionResult = await actionFn();
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

            await loadTree({ fromAction: true });

            // Alan 5/31/26 - Show the action's own final status after the reload so
            // it persists instead of being overwritten by the interim message.
            if (actionResult && actionResult.finalStatus && actionResult.finalStatus.msg) {
                const fs = actionResult.finalStatus;
                showStatus(fs.msg, fs.type || "info", fs.timeout != null ? fs.timeout : 4000);
            }
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
                // Alan 5/12/26 - Current selection is transient, so selection changes only refresh action controls.
                updateButtons();
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
                        // Alan 5/12/26 - Prune boxed names without clearing unrelated selection-set colors.
                        await pruneTaxaPreservingSelectionColors(names);
                    // Alan 5/12/26 - Selection-set cleanup is handled inside pruneTaxaPreservingSelectionColors.
                    }, { clearSelections: false });
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
            // Alan 8/4/26 - These used to read the threshold inputs directly, ignoring the
            // "Apply thresholds (Filter Low)" checkbox, which ships unchecked. On first load
            // that silently hid every node below PP 0.80 / BS 70 until the user happened to
            // touch a filter input (which runs updateOpts and zeroes them). Low support is
            // information, so honour the checkbox from the start and show everything by default.
            ppThreshold: getEl('cb-hide-low-support')?.checked
                ? parseFloat(getEl('input-pp-threshold')?.value || 0.9) : 0,
            bootstrapThreshold: getEl('cb-hide-low-support')?.checked
                ? parseInt(getEl('input-bs-threshold')?.value || 70) : 0,
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

    // Alan 5/31/26 - opts.fromAction marks reloads triggered by runBackendAction so
    // the load-time "Auto root chose" message stays out of the way and lets the
    // action's own final status show instead of clobbering it.
    // Alan 7/15/26 - Queue requested loads instead of dropping a post-edit redraw while another load is active.
    function loadTree(opts = {}) {
        // Alan 7/15/26 - Chain the redraw after the active load and keep the caller waiting until it finishes.
        treeLoadQueue = treeLoadQueue.then(() => loadTreeNow(opts));
        // Alan 7/15/26 - Return the queued promise so backend actions remain disabled until the new tree is rendered.
        return treeLoadQueue;
    }

    // Alan 7/15/26 - Execute one serialized tree load using the existing render and state-restoration flow.
    async function loadTreeNow(opts = {}) {
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
                const loadedTreeState = await TreeEditActions.getTreeState(JOB_ID);
                // Alan 5/29/26 - Cache the latest tree state for alignment-viewer launches and other UI reads.
                treeState = loadedTreeState;
                isMidpointRooted = loadedTreeState.is_midpoint_rooted ?? true;
                updateMidpointButton();
                // Alan 5/29/26 - Sync rooting-mode dropdown to persisted root_mode and warn when auto needs a focal tip.
                if (rootingModeSelect) {
                    const mode = (loadedTreeState.root_mode || '').toLowerCase();
                    const allowed = ['auto', 'midpoint', 'most_divergent_hit', 'unrooted', 'manual'];
                    if (allowed.includes(mode)) rootingModeSelect.value = mode;
                    else if (mode === 'tip') rootingModeSelect.value = 'manual';
                }
                // Alan 5/29/26 - Only prompt for the focal tip when auto root is the chosen mode and we genuinely can't resolve one.
                needsSequenceOfInterest = !!loadedTreeState.needs_sequence_of_interest;
                const loadedMode = (loadedTreeState.root_mode || '').toLowerCase();
                const loadedInfo = loadedTreeState.rooting_info || {};
                if (needsSequenceOfInterest && loadedMode === 'auto') {
                    showStatus(
                        "Auto root needs to know which sequence you care about. " +
                        "Click the sequence of interest, then press \"Set Sequence of Interest\".",
                        "warning"
                    );
                // Alan 5/31/26 - On initial page load (not post-action reloads), tell the user
                // which sequence auto root chose as the outgroup when the tree opens already auto-rooted.
                } else if (!opts.fromAction && loadedMode === 'auto' && (loadedInfo.chosen_by === 'auto' || loadedInfo.chosen_by === 'most_divergent_hit') && loadedInfo.chosen_root_target) {
                    const label = loadedInfo.chosen_by === 'most_divergent_hit'
                        ? `Auto root used the most divergent hit: ${loadedInfo.chosen_root_target}`
                        : `Auto root chose outgroup: ${loadedInfo.chosen_root_target}`;
                    // Alan 8/4/26 - Prefer the persistent banner; only fall back to the sticky
                    // toast when the banner is missing, otherwise the same rooting sentence
                    // renders twice on screen.
                    if (!setRootingNotice(label)) showStatus(label, "info", 0);
                }
                // Alan 5/11/26 - Reapply saved rename labels after loading the raw Newick tree.
                if (loadedTreeState.renames && viewer && typeof viewer.applyRenames === 'function') {
                    viewer.applyRenames(loadedTreeState.renames);
                }
                if (loadedTreeState.selection_sets && viewer) {
                    viewer.restoreSelectionSets({
                        sets: loadedTreeState.selection_sets,
                        // Alan 5/12/26 - Restore the last active color group for compatibility with saved states.
                        active: loadedTreeState.active_selection_set || 'Default',
                        // Alan 5/12/26 - Restore user-selected color metadata when present.
                        colors: loadedTreeState.selection_set_colors || {}
                    });
                    updateSelectionSetUI();
                }
                // Alan 6/2/26 - Render the focal/sequence-of-interest highlight directly from
                // state (no longer mirrored into the user-editable Default color group).
                if (viewer && typeof viewer.setFocalTip === 'function') {
                    viewer.setFocalTip(loadedTreeState.sequence_of_interest || null);
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
            // Alan 5/14/26 - Refresh toolbar buttons after render so Alignment Viewer sees the loaded tip order.
            updateButtons();

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
        // Alan 7/20/26 - Wire keyboard-help close controls using the same modal interaction pattern as Rename.
        btnShortcutHelpClose?.addEventListener('click', closeShortcutHelp);
        shortcutHelpBackdrop?.addEventListener('click', closeShortcutHelp);
        // Alan 7/21/26 - Open the same complete shortcut reference from the new toolbar button and question-mark hotkey.
        btnShortcutHelpOpen?.addEventListener('click', openShortcutHelp);
        // Alan 7/20/26 - Make the three-dot advanced selection control toggle on click instead of hover only.
        btnSelectionMore?.addEventListener('click', (e) => {
            e.stopPropagation();
            setSelectionMoreMenuOpen(btnSelectionMore.getAttribute('aria-expanded') !== 'true');
        });
        // Alan 7/20/26 - Close the advanced selection menu after one of its actions is chosen.
        selectionMoreMenu?.addEventListener('click', (e) => {
            if (e.target.closest('.btn-selection-action')) setSelectionMoreMenuOpen(false);
        });
        // Alan 7/20/26 - Dismiss the advanced selection menu when the user clicks elsewhere on the page.
        document.addEventListener('click', (e) => {
            if (btnSelectionMore?.contains(e.target) || selectionMoreMenu?.contains(e.target)) return;
            setSelectionMoreMenuOpen(false);
        });
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
        // Alan 7/20/26 - Keep an optional Box Select button and the B hotkey on one shared toggle path.
        btnBoxSelect?.addEventListener('click', toggleBoxSelectMode);
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
            // Alan 7/20/26 - Keep the S shortcut discoverable after the sort button label changes.
            btnLadderize.title = 'Cycle node sorting (S)';
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
                // Alan 5/12/26 - Flash the color-chip strip now that the dropdown is gone.
                const setEl = getEl('color-group-chips');
                if (setEl) {
                    setEl.classList.add('ring-2', 'ring-green-400');
                    setTimeout(() => setEl.classList.remove('ring-2', 'ring-green-400'), 400);
                }
            });
        });

        // --- SELECTION SET MANAGEMENT ---
        // Alan 5/12/26 - Render color groups as direct action chips instead of an indirect dropdown.
        const colorGroupChips = getEl('color-group-chips');
        const btnNewSet = getEl('btn-new-selection-set');
        const btnDeleteSet = getEl('btn-delete-selection-set');
        // Alan 5/12/26 - Cache color editing controls for user-chosen group colors.
        const btnEditSetColor = getEl('btn-edit-selection-set-color');
        const colorInput = getEl('input-selection-set-color');
        const colorGroupPopover = getEl('color-group-popover');
        const groupNameInput = getEl('input-color-group-name');
        const groupNewColorInput = getEl('input-color-group-new-color');
        const btnCreateColorGroup = getEl('btn-create-color-group');
        const btnCancelColorGroup = getEl('btn-cancel-color-group');
        // Alan 7/3/26 - Cache quick preset swatches for one-click coloring of the current selection.
        const colorPresetButtons = Array.from(document.querySelectorAll('.btn-color-preset'));
        // Alan 7/3/26 - Cache popover preset swatches so manual group creation can use named colors without typing hex values.
        const colorPresetCreateButtons = Array.from(document.querySelectorAll('.btn-color-preset-create'));
        // Alan 7/3/26 - Track when the shared native color picker should apply a custom color to selected tips.
        let colorInputAppliesToSelection = false;

        // Alan 5/12/26 - Read the current transient selection count defensively for chip actions.
        const getCurrentSelectionCount = () => viewer?.getSelectionCount?.() || 0;

        // Alan 7/3/26 - Normalize swatch and custom-picker colors before saving them as reusable color groups.
        const normalizeQuickColor = (color, fallback = '#1f77b4') => {
            // Alan 7/3/26 - Accept only full hex values because tree labels are styled inline in SVG.
            const raw = typeof color === 'string' ? color.trim().toLowerCase() : '';
            // Alan 7/3/26 - Use the requested hex color when it is safe for inline SVG styling.
            if (/^#[0-9a-f]{6}$/.test(raw)) return raw;
            // Alan 7/3/26 - Fall back to the existing default blue for malformed preset metadata.
            return fallback;
        };

        // Alan 7/3/26 - Find an existing color group by human-facing name without creating case variants.
        const findColorGroupByName = (requestedName) => {
            // Alan 7/3/26 - Normalize the requested name for a case-insensitive comparison.
            const target = (requestedName || '').trim().toLowerCase();
            // Alan 7/3/26 - Empty names cannot match a real saved group.
            if (!target || !viewer?.getSelectionSetNames) return null;
            // Alan 7/3/26 - Reuse the first matching saved group so preset clicks stay idempotent.
            return viewer.getSelectionSetNames().find(name => name.toLowerCase() === target) || null;
        };

        // Alan 7/3/26 - Choose a non-conflicting group name when a preset base name is already taken.
        const nextAvailableColorGroupName = (baseName) => {
            // Alan 7/3/26 - Build a lowercase lookup from current color group names.
            const existing = new Set((viewer?.getSelectionSetNames?.() || []).map(name => name.toLowerCase()));
            // Alan 7/3/26 - Start with the requested display name for clean preset chips.
            let candidate = (baseName || 'Color').trim() || 'Color';
            // Alan 7/3/26 - Add a numeric suffix only when the base name already exists.
            let suffix = 2;
            // Alan 7/3/26 - Keep suffixing until createSelectionSet can succeed without overwriting another group.
            while (existing.has(candidate.toLowerCase())) {
                // Alan 7/3/26 - Append a compact suffix that reads well in the existing chip strip.
                candidate = `${baseName} ${suffix}`;
                // Alan 7/3/26 - Advance the suffix for any additional collision.
                suffix += 1;
            }
            // Alan 7/3/26 - Return the final display name for creation.
            return candidate;
        };

        // Alan 7/3/26 - Create or reuse a normal saved color group for quick preset/custom color actions.
        const ensureQuickColorGroup = (baseName, color) => {
            // Alan 7/3/26 - Quick colors require an initialized editable viewer.
            if (!viewer) return null;
            // Alan 7/3/26 - Normalize the color once before using it for group creation or update.
            const normalizedColor = normalizeQuickColor(color);
            // Alan 7/3/26 - Use the requested swatch name as the saved chip label.
            const requestedName = (baseName || 'Color').trim() || 'Color';
            // Alan 7/3/26 - Prefer reusing an existing named group so repeated swatch clicks do not duplicate chips.
            const existingName = findColorGroupByName(requestedName);
            // Alan 7/3/26 - Existing preset groups are updated to the clicked swatch color for predictable results.
            if (existingName) {
                // Alan 7/3/26 - Persist the swatch color on the reused group.
                viewer.setSelectionSetColor(existingName, normalizedColor);
                // Alan 7/3/26 - Activate the reused group before applying selected tips.
                viewer.setActiveSelectionSet(existingName);
                // Alan 7/3/26 - Tell callers this was a reuse rather than a new group.
                return { name: existingName, created: false };
            }
            // Alan 7/3/26 - Pick a clean name for the new preset-backed group.
            const groupName = nextAvailableColorGroupName(requestedName);
            // Alan 7/3/26 - Create the group through the existing persistence-aware viewer API.
            if (!viewer.createSelectionSet(groupName, normalizedColor)) return null;
            // Alan 7/3/26 - Make the new group active so the existing apply path colors the current selection.
            viewer.setActiveSelectionSet(groupName);
            // Alan 7/3/26 - Tell callers a new user-visible chip was created.
            return { name: groupName, created: true };
        };

        // Alan 5/12/26 - Hide the new-color-group popover after create/cancel.
        const hideColorGroupPopover = () => {
            // Alan 5/12/26 - Guard because older templates may not have the popover during cache transitions.
            if (colorGroupPopover) colorGroupPopover.classList.add('hidden');
        };

        // Alan 5/12/26 - Apply the active color group and consume the temporary action selection.
        const applyActiveColorGroupToSelection = async (createdName = null) => {
            // Alan 5/12/26 - Ignore color application before the viewer is initialized.
            if (!viewer?.addCurrentSelectionToActiveColorGroup) return 0;
            // Alan 5/12/26 - Do not persist color edits from view-only copies.
            if (window.VIEW_ONLY || isProcessing) return 0;
            // Alan 5/12/26 - Capture the active group name before selection is cleared.
            const activeName = viewer.getActiveSelectionSet();
            // Alan 5/12/26 - Move selected tips into the active group, replacing prior colors.
            const changed = viewer.addCurrentSelectionToActiveColorGroup();
            // Alan 5/12/26 - Color application consumes the temporary selection so later colors start fresh.
            viewer.clearActiveSelection();
            // Alan 5/12/26 - Re-render chips and action buttons after consuming selection.
            updateSelectionSetUI();
            // Alan 5/12/26 - Refresh edit buttons after consuming selection.
            updateButtons();
            // Alan 5/12/26 - Persist user color assignments immediately.
            await saveSelectionSetsNow();
            // Alan 5/12/26 - Mention creation and coloring together when a new group was just made.
            const prefix = createdName ? `Created "${createdName}" and colored` : 'Colored';
            // Alan 5/12/26 - Report the number of selected sequences handled by the color chip.
            showStatus(`${prefix} ${changed} sequence${changed === 1 ? '' : 's'} in "${activeName}".`, "success", 1800);
            // Alan 5/12/26 - Return count for callers that need no-op handling.
            return changed;
        };

        // Alan 7/3/26 - Apply a preset/custom quick color to the current selection through normal color groups.
        const applyQuickColorToSelection = async (name, color) => {
            // Alan 7/3/26 - Ignore quick color actions before the editable viewer is ready.
            if (!viewer || window.VIEW_ONLY || isProcessing) return;
            // Alan 7/3/26 - Require a current selection so swatches behave as direct mark buttons.
            if (getCurrentSelectionCount() === 0) {
                // Alan 7/3/26 - Use the existing status surface instead of adding persistent helper copy.
                showStatus("Select sequences first, then choose a color.", "warning", 2200);
                // Alan 7/3/26 - Stop before creating empty preset groups.
                return;
            }
            // Alan 7/3/26 - Create or reuse the saved color group represented by the quick color.
            const group = ensureQuickColorGroup(name, color);
            // Alan 7/3/26 - Surface a guarded failure if group creation was rejected.
            if (!group) {
                // Alan 7/3/26 - Keep the message short because the failure is unlikely and non-destructive.
                showStatus("Could not create color group.", "danger", 2500);
                // Alan 7/3/26 - Stop before attempting to persist an incomplete color action.
                return;
            }
            // Alan 7/3/26 - Refresh chips so a newly created preset group appears before the selection is consumed.
            updateSelectionSetUI();
            // Alan 7/3/26 - Apply through the existing one-color-per-tip persistence flow.
            await applyActiveColorGroupToSelection(group.created ? group.name : null);
        };

        // Helper to sync UI with viewer selection set state (also assigned to outer scope for loadTree access)
        updateSelectionSetUI = function updateSelectionSetUI() {
            // Alan 5/12/26 - Skip chip rendering until the viewer and template controls both exist.
            if (!viewer || !colorGroupChips) return;

            // Alan 5/12/26 - Read current group names from the backward-compatible selection-set API.
            const names = viewer.getSelectionSetNames();
            // Alan 5/12/26 - Read the currently active color group.
            const active = viewer.getActiveSelectionSet();
            // Alan 5/12/26 - Clear old chip DOM before rebuilding.
            colorGroupChips.innerHTML = '';
            names.forEach(name => {
                // Alan 5/12/26 - Resolve each group's editable color.
                const color = viewer.getSelectionSetColor(name) || '#1f77b4';
                // Alan 5/12/26 - Build a chip that both selects and applies a color group.
                const chip = document.createElement('button');
                // Alan 5/12/26 - Keep chips compact and horizontally scrollable in the toolbar.
                chip.type = 'button';
                // Alan 5/12/26 - Mark chips for direct color-group application.
                chip.className = 'color-group-chip inline-flex items-center gap-1 rounded px-2 py-1 text-xs font-medium border whitespace-nowrap text-gray-700 dark:text-gray-200 disabled:opacity-40 disabled:cursor-not-allowed';
                // Alan 5/12/26 - Highlight the active group with its chosen color.
                chip.style.borderColor = name === active ? color : 'rgba(107, 114, 128, .45)';
                // Alan 5/12/26 - Add a subtle color wash to make the chip read as the color itself.
                chip.style.background = `${color}22`;
                // Alan 5/12/26 - Explain direct chip behavior in the tooltip.
                chip.title = getCurrentSelectionCount() > 0 ? `Color current selection as ${name}` : `Set active color group: ${name}`;
                // Alan 5/12/26 - Create a visible color dot without injecting group names as HTML.
                const dot = document.createElement('span');
                // Alan 5/12/26 - Size the dot consistently inside compact chips.
                dot.className = 'inline-block h-2.5 w-2.5 rounded-full border border-black/20';
                // Alan 5/12/26 - Fill the dot with the user's chosen color.
                dot.style.background = color;
                // Alan 5/12/26 - Create the group label as text content for safety.
                const label = document.createElement('span');
                // Alan 5/12/26 - Keep long group names from stretching the toolbar.
                label.className = 'max-w-[90px] truncate';
                // Alan 5/12/26 - Assign text content so custom group names are not interpreted as markup.
                label.textContent = name;
                // Alan 5/12/26 - Add dot and label to the chip.
                chip.append(dot, label);
                // Alan 5/12/26 - Clicking a chip selects it, and colors current tips when any are selected.
                chip.addEventListener('click', async () => {
                    // Alan 5/12/26 - Ignore chip actions before the viewer is ready.
                    if (!viewer) return;
                    // Alan 5/12/26 - Switch active color group first so color application uses this chip.
                    if (!viewer.setActiveSelectionSet(name)) return;
                    // Alan 5/12/26 - Refresh chip active state immediately.
                    updateSelectionSetUI();
                    // Alan 5/12/26 - Apply color directly when there is a current selection.
                    if (getCurrentSelectionCount() > 0 && !window.VIEW_ONLY && !isProcessing) {
                        // Alan 5/12/26 - Persist the one-color-per-tip assignment.
                        await applyActiveColorGroupToSelection();
                        return;
                    }
                    // Alan 5/12/26 - Otherwise just switch the active edit group.
                    showStatus(`Active color group: "${name}".`, "info", 1500);
                });
                // Alan 5/12/26 - Add the chip to the toolbar.
                colorGroupChips.appendChild(chip);
            });

            // Alan 5/12/26 - Keep the hidden native color picker synchronized with the active group.
            if (colorInput) {
                // Alan 5/12/26 - Use active group color as the edit starting point.
                const color = viewer.getSelectionSetColor(active);
                // Alan 5/12/26 - Assign a valid fallback for native color inputs.
                colorInput.value = color || '#1f77b4';
            }

            // Alan 5/12/26 - New/edit/delete are persisted color-group edits, so lock them in view-only mode.
            const disableGroupEdits = Boolean(window.VIEW_ONLY || isProcessing);
            // Alan 5/12/26 - Disable creation when color groups cannot be persisted.
            if (btnNewSet) btnNewSet.disabled = disableGroupEdits;
            // Alan 5/12/26 - Disable color editing when color groups cannot be persisted.
            if (btnEditSetColor) btnEditSetColor.disabled = disableGroupEdits || !active;
            // Enable/disable delete button (always disable for 'Default')
            if (btnDeleteSet) {
                // Alan 5/12/26 - Default remains protected and all group edits lock in view-only/processing states.
                btnDeleteSet.disabled = disableGroupEdits || (active === 'Default');
            }
            // Alan 7/3/26 - Keep preset swatches tied to the current selection so they read as direct mark buttons.
            const currentSelectionCount = getCurrentSelectionCount();
            // Alan 7/3/26 - Disable quick swatches when no selected tips can be colored or edits are locked.
            colorPresetButtons.forEach((button) => {
                // Alan 7/3/26 - Resolve the preset name from markup for compact dynamic tooltips.
                const presetName = button.dataset.presetName || 'Color';
                // Alan 7/3/26 - Swatches need a selection because they apply immediately instead of setting a mode.
                const disabled = disableGroupEdits || currentSelectionCount === 0;
                // Alan 7/3/26 - Reflect the action availability on the native button state.
                button.disabled = disabled;
                // Alan 7/3/26 - Explain the exact quick action without adding visible toolbar text.
                button.title = currentSelectionCount > 0
                    ? `Mark ${currentSelectionCount} selected sequence${currentSelectionCount === 1 ? '' : 's'} ${presetName.toLowerCase()}`
                    : `Select sequences to mark ${presetName.toLowerCase()}`;
            });
            // Alan 7/3/26 - Make the palette icon switch between quick custom coloring and active-group editing.
            if (btnEditSetColor) {
                // Alan 7/3/26 - Use a context-sensitive tooltip because the button now has two useful flows.
                btnEditSetColor.title = currentSelectionCount > 0 ? 'Choose a custom color for selected sequences' : 'Change Active Color Group Color';
            }
        }

        // Initial sync after viewer is ready
        if (viewer) updateSelectionSetUI();

        // Alan 6/2/26 - Register the display-only rotate action as a native phylotree menu item
        // (coexists with Collapse/Select). The viewer invokes this with the clicked node.
        if (viewer && typeof viewer.setRotateNodeHandler === 'function') {
            viewer.setRotateNodeHandler((node) => {
                if (window.VIEW_ONLY || isProcessing) return;
                const tipNames = getDescendantTipNames(node);
                if (tipNames.length < 2) return;
                const nodeId = node?.data?.stable_id || stableInternalNodeIdFromTipNames(tipNames);
                if (!nodeId) return;
                runBackendAction("Rotating node", async () => {
                    await TreeEditActions.rotateNode(JOB_ID, nodeId);
                }, { clearSelections: false });
            });
        }

        // Alan 7/17/26 - Register count-aware context pruning so multiple selected tips use the bulk backend flow.
        if (viewer && typeof viewer.setPruneNodeHandler === 'function') {
            // Alan 7/17/26 - Accept the target nodes resolved by the viewer when it built the context-menu label.
            viewer.setPruneNodeHandler((node, nodes = []) => {
                if (window.VIEW_ONLY || isProcessing) return;
                // Alan 7/17/26 - Fall back to the clicked node for compatibility with viewer implementations that omit targets.
                const pruneNodes = Array.isArray(nodes) && nodes.length ? nodes : [node];
                // Alan 7/17/26 - Convert every selected tip or clicked internal node into a backend prune target.
                const targets = Array.from(new Set(pruneNodes.map(getPruneTargetForNode).filter(Boolean)));
                // Alan 7/17/26 - Reject the action only when none of the resolved nodes has a stable prune target.
                if (!targets.length) {
                    showStatus("Can't prune: no stable node ID.", "warning", 2500);
                    return;
                }
                // Alan 7/17/26 - Include every pruned descendant tip in local selection-color cleanup.
                const cleanupNames = Array.from(new Set([
                    // Alan 7/17/26 - Retain backend targets so leaf IDs and internal stable IDs are both cleaned safely.
                    ...targets,
                    // Alan 7/17/26 - Flatten descendant tips from every context-menu prune target.
                    ...pruneNodes.reduce((names, pruneNode) => names.concat(getDescendantTipNames(pruneNode)), [])
                ]));
                // Alan 7/17/26 - Report the number of terminal sequences represented by this context action.
                const sequenceCount = new Set(pruneNodes.reduce((names, pruneNode) => names.concat(getDescendantTipNames(pruneNode)), [])).size;
                // Alan 7/17/26 - Use the same singular/plural count shown in the menu while the backend mutation runs.
                runBackendAction(`Pruning ${sequenceCount} node${sequenceCount === 1 ? '' : 's'}`, async () => {
                    // Alan 7/17/26 - Send all resolved targets so multi-tip context pruning removes the advertised count.
                    await pruneTaxaPreservingSelectionColors(targets, cleanupNames);
                }, { clearSelections: false });
            });
        }

        // Alan 8/13/26 - Reuse the existing single-sequence rename modal from the tip context menu.
        if (viewer && typeof viewer.setRenameNodeHandler === 'function') {
            viewer.setRenameNodeHandler((node) => {
                if (window.VIEW_ONLY || isProcessing || !node) return;
                openRenameModal([node]);
            });
        }

        // Alan 7/14/26 - Copy one clicked name or multiple selected sequence names as separate clipboard lines.
        if (viewer && typeof viewer.setCopySequenceNameHandler === 'function') {
            viewer.setCopySequenceNameHandler(async (node, nodes = []) => {
                const copyNodes = Array.isArray(nodes) && nodes.length ? nodes : [node];
                const names = copyNodes.map(getDisplaySequenceName).filter(Boolean).map(String);
                if (!names.length) {
                    showStatus("No sequence name to copy.", "warning", 2000);
                    return;
                }
                try {
                    await copyTextToClipboard(names.join('\r\n'));
                    const copied = names.length === 1 ? 'Sequence name copied.' : 'Sequence names copied.';
                    showStatus(copied, "success", 1500);
                } catch (err) {
                    console.error(err);
                    showStatus("Copy failed.", "danger", 2500);
                }
            });
        }

        // Alan 6/25/26 - Register context-menu copying for iNaturalist observation numbers on clicked or selected tips.
        if (viewer && typeof viewer.setCopyInaturalistNumbersHandler === 'function') {
            viewer.setCopyInaturalistNumbersHandler(async (node, numbers = []) => {
                const uniqueNumbers = Array.from(new Set((Array.isArray(numbers) ? numbers : []).filter(Boolean).map(String)));
                if (!uniqueNumbers.length) {
                    showStatus("No iNaturalist number to copy.", "warning", 2000);
                    return;
                }
                try {
                    await copyTextToClipboard(uniqueNumbers.join(' '));
                    const copied = uniqueNumbers.length === 1 ? 'iNaturalist number copied.' : 'iNaturalist numbers copied.';
                    showStatus(copied, "success", 1500);
                } catch (err) {
                    console.error(err);
                    showStatus("Copy failed.", "danger", 2500);
                }
            });
        }

        // Alan 7/16/26 - Refresh clicked/highlighted observation records and reload any persisted label changes.
        if (viewer && typeof viewer.setRefreshMycomapRecordsHandler === 'function') {
            viewer.setRefreshMycomapRecordsHandler((node, nodes = []) => {
                if (window.VIEW_ONLY || isProcessing) return;
                const recordNodes = Array.isArray(nodes) && nodes.length ? nodes : [node];
                const tipNames = Array.from(new Set(recordNodes.map((recordNode) => {
                    const data = recordNode?.data || recordNode || {};
                    return data.__original_name || data.original_name || data.name || recordNode?.name || '';
                }).filter(Boolean).map(String)));
                if (!tipNames.length) {
                    showStatus("No iNaturalist or Mushroom Observer record to refresh.", "warning", 2500);
                    return;
                }

                const references = new Set(recordNodes.map((recordNode) => {
                    const parser = window.phylotree?.extractObservationRecordReference;
                    return typeof parser === 'function' ? parser(recordNode)?.reference : null;
                }).filter(Boolean));
                const recordCount = references.size || tipNames.length;
                const actionName = recordCount === 1 ? 'Refreshing Mycomap record' : `Refreshing ${recordCount} Mycomap records`;
                runBackendAction(actionName, async () => {
                    const result = await TreeEditActions.refreshMycomapRecords(JOB_ID, tipNames);
                    const refreshedCount = Number(result.refreshed_count) || recordCount;
                    const updatedCount = Number(result.updated_tip_count) || 0;
                    const warningCount = Array.isArray(result.warnings) ? result.warnings.length : 0;
                    const refreshedText = `${refreshedCount} Mycomap record${refreshedCount === 1 ? '' : 's'} refreshed.`;
                    const updatedText = updatedCount
                        ? ` ${updatedCount} tree label${updatedCount === 1 ? '' : 's'} updated.`
                        : ' Tree labels already match the refreshed records.';
                    const warningText = warningCount
                        ? ` ${warningCount} label update warning${warningCount === 1 ? '' : 's'}.`
                        : '';
                    return {
                        finalStatus: {
                            msg: refreshedText + updatedText + warningText,
                            type: warningCount ? 'warning' : 'success',
                            timeout: warningCount ? 6000 : 3000
                        }
                    };
                }, { clearSelections: false });
            });
        }

        // Alan 7/3/26 - Wire preset swatches as one-click color actions for the current selection.
        colorPresetButtons.forEach((button) => {
            // Alan 7/3/26 - Each swatch creates/reuses a normal saved color group behind the scenes.
            button.addEventListener('click', async () => {
                // Alan 7/3/26 - Ignore disabled or stale swatch clicks during processing/view-only states.
                if (!viewer || button.disabled) return;
                // Alan 7/3/26 - Read the swatch metadata from the template.
                const presetName = button.dataset.presetName || 'Color';
                // Alan 7/3/26 - Read the preset hex value from the template.
                const presetColor = button.dataset.presetColor || '#1f77b4';
                // Alan 7/3/26 - Apply the selected swatch to the current selection.
                await applyQuickColorToSelection(presetName, presetColor);
            });
        });

        // Alan 7/3/26 - Let preset swatches inside the creation popover fill color and name fields.
        colorPresetCreateButtons.forEach((button) => {
            // Alan 7/3/26 - Popover swatches stay local to manual color-group creation.
            button.addEventListener('click', () => {
                // Alan 7/3/26 - Ignore stale popover clicks before controls exist.
                if (!groupNewColorInput) return;
                // Alan 7/3/26 - Copy the swatch color into the native color field.
                groupNewColorInput.value = normalizeQuickColor(button.dataset.presetColor || '#1f77b4');
                // Alan 7/3/26 - Prefill the group name when it is blank so presets avoid unnecessary typing.
                if (groupNameInput && !groupNameInput.value.trim()) groupNameInput.value = nextAvailableColorGroupName(button.dataset.presetName || 'Color');
                // Alan 7/3/26 - Keep keyboard focus near the field a user may still want to customize.
                groupNameInput?.focus();
            });
        });

        // New Set button
        btnNewSet?.addEventListener('click', () => {
            // Alan 5/12/26 - Do not open creation UI before the viewer is ready.
            if (!viewer || !colorGroupPopover) return;
            // Alan 5/12/26 - Persisted color-group creation is not available in view-only mode.
            if (window.VIEW_ONLY || isProcessing) return;
            // Alan 5/12/26 - Start with a blank group name for each creation.
            if (groupNameInput) groupNameInput.value = '';
            // Alan 5/12/26 - Suggest the next palette color while still allowing any native color choice.
            if (groupNewColorInput) groupNewColorInput.value = viewer.suggestSelectionSetColor?.() || '#1f77b4';
            // Alan 5/12/26 - Show the compact creation popover.
            colorGroupPopover.classList.remove('hidden');
            // Alan 5/12/26 - Focus the name field for keyboard flow.
            groupNameInput?.focus();
        });

        // Alan 5/12/26 - Cancel color-group creation without changing selections.
        btnCancelColorGroup?.addEventListener('click', hideColorGroupPopover);

        // Alan 5/12/26 - Create a user-named color group with the chosen color.
        btnCreateColorGroup?.addEventListener('click', async () => {
            // Alan 5/12/26 - Guard against missing viewer/popover state.
            if (!viewer || window.VIEW_ONLY || isProcessing) return;
            // Alan 5/12/26 - Read and trim the requested group name.
            const name = groupNameInput?.value.trim() || '';
            // Alan 5/12/26 - Read the native color picker value.
            const color = groupNewColorInput?.value || '#1f77b4';
            // Alan 5/12/26 - Require a usable group name.
            if (!name) {
                showStatus("Enter a color group name.", "warning", 2000);
                groupNameInput?.focus();
                return;
            }
            // Alan 5/12/26 - Create the group with the user's chosen color.
            if (!viewer.createSelectionSet(name, color)) {
                showStatus(`Color group "${name}" already exists or is invalid.`, "warning", 2500);
                return;
            }
            // Alan 5/12/26 - Make the new group active so selected tips apply to it.
            viewer.setActiveSelectionSet(name);
            // Alan 5/12/26 - Close the popover after a successful create.
            hideColorGroupPopover();
            // Alan 5/12/26 - Creating a group while tips are selected applies it immediately.
            if (getCurrentSelectionCount() > 0) {
                await applyActiveColorGroupToSelection(name);
                return;
            }
            // Alan 5/12/26 - Refresh chips for the new group.
            updateSelectionSetUI();
            // Alan 5/12/26 - Persist new group metadata even when no tips are colored yet.
            await saveSelectionSetsNow();
            // Alan 5/12/26 - Report group creation.
            showStatus(`Created color group "${name}".`, "success", 2000);
        });

        // Alan 5/12/26 - Let users change the active color group's color with a native color picker.
        btnEditSetColor?.addEventListener('click', () => {
            // Alan 5/12/26 - Ignore color edits before the viewer is ready.
            if (!viewer || !colorInput || window.VIEW_ONLY || isProcessing) return;
            // Alan 7/3/26 - When tips are selected, the palette picker should apply a custom quick color.
            colorInputAppliesToSelection = getCurrentSelectionCount() > 0;
            // Alan 5/12/26 - Sync the picker to the active group before opening it.
            colorInput.value = viewer.getSelectionSetColor(viewer.getActiveSelectionSet()) || '#1f77b4';
            // Alan 5/12/26 - Open the browser-native color picker.
            colorInput.click();
        });

        // Alan 5/12/26 - Persist native color picker changes to the active group.
        colorInput?.addEventListener('change', async () => {
            // Alan 5/12/26 - Guard against stale input events.
            if (!viewer || window.VIEW_ONLY || isProcessing) return;
            // Alan 7/3/26 - Normalize the chosen color before either quick-apply or group-edit paths.
            const chosenColor = normalizeQuickColor(colorInput.value);
            // Alan 7/3/26 - Apply native picker colors directly when the palette was opened with tips selected.
            if (colorInputAppliesToSelection && getCurrentSelectionCount() > 0) {
                // Alan 7/3/26 - Reset the one-shot mode before the async save path can be re-entered.
                colorInputAppliesToSelection = false;
                // Alan 7/3/26 - Use a readable chip name for arbitrary custom colors.
                await applyQuickColorToSelection(`Custom ${chosenColor.toUpperCase()}`, chosenColor);
                // Alan 7/3/26 - Stop before editing the previously active group color.
                return;
            }
            // Alan 7/3/26 - Reset one-shot custom coloring when the picker is used for active-group editing.
            colorInputAppliesToSelection = false;
            // Alan 5/12/26 - Resolve the active group at the time of color edit.
            const active = viewer.getActiveSelectionSet();
            // Alan 5/12/26 - Save the chosen color to the active group.
            if (!viewer.setSelectionSetColor(active, chosenColor)) return;
            // Alan 5/12/26 - Re-render chips with the new color.
            updateSelectionSetUI();
            // Alan 5/12/26 - Persist color metadata.
            await saveSelectionSetsNow();
            // Alan 5/12/26 - Confirm the color edit.
            showStatus(`Updated "${active}" color.`, "success", 1500);
        });

        // Delete Set button
        btnDeleteSet?.addEventListener('click', () => {
            // Alan 5/12/26 - Do not delete persisted color groups while edits are unavailable.
            if (window.VIEW_ONLY || isProcessing) return;
            if (!viewer) return;
            const active = viewer.getActiveSelectionSet();
            if (active === 'Default') {
                // Alan 5/12/26 - Explain why the protected default color group cannot be deleted.
                showStatus("Cannot delete the Default color group.", "warning", 2000);
                return;
            }
            // Alan 5/12/26 - Confirm color-group deletion without implying it controls current action selection.
            if (!confirm(`Delete color group "${active}"? This will clear its colors.`)) return;

            if (viewer.deleteSelectionSet(active)) {
                updateSelectionSetUI();
                saveSelectionSets();
                // Alan 5/12/26 - Report color-group deletion using the new user-facing terminology.
                showStatus(`Deleted color group "${active}".`, "success", 2000);
            }
        });

        // Alan 5/12/26 - Clear color from the current temporary selection across all color groups.
        btnUncolorSelection?.addEventListener('click', async () => {
            // Alan 5/12/26 - Ignore uncolor actions before the viewer is ready.
            if (!viewer?.clearCurrentSelectionColorGroups || window.VIEW_ONLY || isProcessing) return;
            // Alan 5/12/26 - Remove persistent color membership from every color group.
            const changed = viewer.clearCurrentSelectionColorGroups();
            // Alan 5/12/26 - Clear-color consumes the temporary selection for the same reason color application does.
            viewer.clearActiveSelection();
            // Alan 5/12/26 - Refresh buttons after clearing the consumed temporary selection.
            updateButtons();
            // Alan 5/12/26 - Persist color groups after deliberate color removal.
            await saveSelectionSetsNow();
            // Alan 5/12/26 - Report both no-op and changed color removal clearly.
            showStatus(changed > 0 ? `Cleared color from ${changed} sequence${changed === 1 ? '' : 's'}.` : "Selected sequences had no color.", changed > 0 ? "success" : "info", 1800);
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
                // Alan 5/12/26 - Prune selected names without clearing unrelated selection-set colors.
                await pruneTaxaPreservingSelectionColors(names);
            // Alan 5/12/26 - Selection-set cleanup is handled inside pruneTaxaPreservingSelectionColors.
            }, { clearSelections: false });
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

        // Alan 5/29/26 - Rooting-mode dropdown drives the unified rooting API; "manual" defers to existing Reroot-Here click flow.
        if (rootingModeSelect) rootingModeSelect.addEventListener('change', () => {
            if (isProcessing) return;
            const mode = rootingModeSelect.value;
            if (mode === 'manual') {
                rerootMode = true;
                if (viewer) viewer.clearSelection();
                installRerootCapture();
                showStatus("Click a node to reroot.", "info");
                updateButtons();
                return;
            }
            runBackendAction(`Applying ${mode} rooting`, async () => {
                const result = await TreeEditActions.setRootingMode(JOB_ID, mode);
                // Alan 5/31/26 - Return the chosen-tip status so it survives the reload.
                return { finalStatus: rootingFinalStatus(mode, result) };
            });
        });

        // Alan 5/29/26 - Persist focal/sequence-of-interest tip from the current viewer selection.
        if (btnSetSoi) btnSetSoi.addEventListener('click', () => {
            if (!viewer || typeof viewer.getSelectedTipNames !== 'function') {
                showStatus("Select one tip first.", "warning", 2500);
                return;
            }
            const selected = viewer.getSelectedTipNames();
            if (!selected || selected.length !== 1) {
                showStatus("Select exactly one tip to mark as the sequence of interest.", "warning", 3000);
                return;
            }
            const tipName = selected[0];
            runBackendAction("Setting sequence of interest", async () => {
                await TreeEditActions.setSequenceOfInterest(JOB_ID, tipName, "user_selected");
                // Alan 5/29/26 - Sequence of interest is now known; clear the prompt flag and re-apply auto root.
                needsSequenceOfInterest = false;
                if (rootingModeSelect && rootingModeSelect.value === 'auto') {
                    const result = await TreeEditActions.setRootingMode(JOB_ID, 'auto');
                    // Alan 5/31/26 - Persist the chosen-tip status past the reload.
                    return { finalStatus: rootingFinalStatus('auto', result) };
                }
            });
        });

        // Alan 7/20/26 - Route button clicks through the same reliable deselect action used by the D hotkey.
        if (btnDeselect) btnDeselect.addEventListener('click', deselectCurrentTreeSelection);

        if (btnRecompute) btnRecompute.addEventListener('click', () => {
            if (!confirm("Recompute tree?")) return;
            runBackendAction("Recomputing", async () => {
                await TreeEditActions.recomputeTree(JOB_ID);
            });
        });

        // Alan 8/4/26 - Rebuild the tree with the deduped duplicate records added back in.
        // Creates a separate job so the current tree keeps its URL.
        const btnRebuildDupes = getEl('btn-rebuild-with-duplicates');
        if (btnRebuildDupes) btnRebuildDupes.addEventListener('click', async () => {
            if (!confirm("Start a new job with the removed duplicate sequences added back in?\n\nThis tree is left unchanged.")) return;
            btnRebuildDupes.disabled = true;
            btnRebuildDupes.classList.add('opacity-50', 'cursor-not-allowed');
            try {
                // Alan 8/4/26 - Reuse TreeEditActions' CSRF header builder; a bare fetch
                // here is rejected by CSRFProtect with "CSRF token missing".
                const csrf = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
                const headers = { 'Content-Type': 'application/json' };
                if (csrf) headers['X-CSRFToken'] = csrf;
                const resp = await fetch(`/api/job/${JOB_ID}/rebuild-with-duplicates`, {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers
                });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) {
                    showStatus(data.error || "Could not start the rebuild.", "danger", 5000);
                    btnRebuildDupes.disabled = false;
                    btnRebuildDupes.classList.remove('opacity-50', 'cursor-not-allowed');
                    return;
                }
                showStatus(
                    `Queued a new tree with ${data.restored_count} duplicate record(s) restored. Opening it now...`,
                    "success", 0
                );
                setTimeout(() => { window.location.href = data.status_url || `/job/${data.job_id}`; }, 1200);
            } catch (err) {
                showStatus("Could not start the rebuild.", "danger", 5000);
                btnRebuildDupes.disabled = false;
                btnRebuildDupes.classList.remove('opacity-50', 'cursor-not-allowed');
            }
        });

        // Alan 5/13/26 - Open the full-screen Alignment Viewer for selected/visible tree tips.
        if (btnAlignmentViewer) btnAlignmentViewer.addEventListener('click', () => {
            if (!viewer || typeof viewer.getVisibleTipOrder !== 'function') {
                showStatus("Alignment is not available yet.", "warning", 2500);
                return;
            }
            const visible = viewer.getVisibleTipOrder();
            const selected = typeof viewer.getSelectedTipNames === 'function' ? viewer.getSelectedTipNames() : [];
            if (visible.length === 0 && selected.length === 0) {
                showStatus("No visible sequences to display in the Alignment Viewer.", "warning", 2500);
                return;
            }
            if (!window.DikaryaAlignmentViewer || typeof window.DikaryaAlignmentViewer.open !== 'function') {
                showStatus("Alignment Viewer is not available on this page.", "danger", 3000);
                return;
            }
            // Alan 5/13/26 - Expose the viewer instance so the alignment viewer can read tree state during refresh.
            window.dikaryaViewer = viewer;
            window.DikaryaAlignmentViewer.open({
                jobId: JOB_ID,
                treeOrder: visible,
                selectedNames: selected,
                // Alan 5/29/26 - Prefer the persisted focal tip, but fall back to the saved one-member Default set.
                preferredName: getPreferredAlignmentTip(treeState),
            });
        });



        document.addEventListener('keydown', (e) => {
            // Alan 7/20/26 - Close keyboard help before handling viewer modes or other Escape behavior.
            if (e.key === 'Escape' && shortcutHelpModal && !shortcutHelpModal.classList.contains('hidden')) {
                closeShortcutHelp();
                return;
            }
            // Alan 7/20/26 - Let Escape dismiss the advanced selection menu without changing tree state.
            if (e.key === 'Escape' && btnSelectionMore?.getAttribute('aria-expanded') === 'true') {
                setSelectionMoreMenuOpen(false);
                btnSelectionMore.focus();
                return;
            }
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
                return;
            }

            // Alan 7/20/26 - Handle safe viewer hotkeys only outside text controls, dialogs, and modified browser shortcuts.
            const target = e.target;
            const isTyping = target instanceof HTMLElement
                && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName));
            const modalOpen = Boolean(document.querySelector('[role="dialog"][aria-modal="true"]:not(.hidden)'));
            if (e.repeat || e.ctrlKey || e.metaKey || e.altKey || isTyping || modalOpen) return;

            const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
            let handled = false;
            // Alan 7/20/26 - D clears the visible current selection directly, independent of button disabled-state timing.
            if (key === 'd' && viewer && !isProcessing) {
                deselectCurrentTreeSelection();
                handled = true;
            // Alan 7/20/26 - V opens the existing Alignment Viewer through its established click handler.
            } else if (key === 'v' && viewer && btnAlignmentViewer && !btnAlignmentViewer.disabled) {
                btnAlignmentViewer.click();
                handled = true;
            // Alan 7/20/26 - S cycles the existing original, ascending, and descending node sort modes.
            } else if (key === 's' && viewer) {
                getEl('btn-ladderize')?.click();
                handled = true;
            // Alan 7/20/26 - B toggles the existing Box Select mode and its normal status feedback.
            } else if (key === 'b' && viewer) {
                handled = toggleBoxSelectMode();
            // Alan 7/20/26 - Question mark opens the shortcut reference without requiring a loaded tree.
            } else if (e.key === '?') {
                openShortcutHelp();
                handled = true;
            }
            if (handled) e.preventDefault();
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
        // Alan 5/12/26 - Keep color chips, tooltips, and edit disabled states in sync with selection state.
        updateSelectionSetUI();

        // Alan 5/11/26 - Keep Deselect tied to visible active selections, not saved selection sets.
        const updateDeselectButton = (forceDisabled = false) => {
            if (!btnDeselect) return;
            const disabled = forceDisabled || selCount === 0;
            btnDeselect.disabled = disabled;
            // Alan 7/20/26 - Advertise the D shortcut in both enabled and empty-selection button states.
            btnDeselect.title = selCount > 0 ? `Deselect ${selCount} sequence${selCount === 1 ? '' : 's'} (D)` : "No current selection (D)";
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
            // Alan 5/12/26 - Color clearing is a persisted edit, so disable it in view-only mode.
            disableBtn(btnUncolorSelection);
            // btnMakeCopy remains enabled
            // Alan 5/11/26 - Leave Deselect available in view-only mode because it only changes local highlighting.
            updateDeselectButton(false);
            // Alan 5/14/26 - Keep the read-only Alignment Viewer enabled once rendered tips are available.
            updateAlignmentViewerButton();
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
            // Alan 5/29/26 - Freeze rooting controls while a backend rooting action is in flight.
            if (btnSetSoi) btnSetSoi.disabled = true;
            // Alan 5/29/26 - SOI button only ever appears when auto root has no resolvable focal tip.
            if (btnSetSoi) btnSetSoi.classList.toggle('hidden', !(rootingModeSelect && rootingModeSelect.value === 'auto' && needsSequenceOfInterest));
            if (rootingModeSelect) rootingModeSelect.disabled = true;
            if (btnRecompute) btnRecompute.disabled = true;
            if (btnMakeCopy) btnMakeCopy.disabled = true;
            // Alan 5/12/26 - Prevent color clearing while backend actions are refreshing the tree.
            if (btnUncolorSelection) btnUncolorSelection.disabled = true;
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
        // Alan 5/29/26 - Show Set Sequence of Interest only when auto root needs help; require exactly one tip selected to enable it.
        if (btnSetSoi) {
            const showSoi = rootingModeSelect && rootingModeSelect.value === 'auto' && needsSequenceOfInterest;
            btnSetSoi.classList.toggle('hidden', !showSoi);
            btnSetSoi.disabled = !showSoi || selCount !== 1;
        }
        if (rootingModeSelect) rootingModeSelect.disabled = false;
        if (btnRecompute) btnRecompute.disabled = false;
        // Alan 5/12/26 - Enable clear color only when there is a temporary current selection.
        if (btnUncolorSelection) btnUncolorSelection.disabled = selCount === 0;
        // Alan 5/13/26 - Keep the Alignment Viewer count and disabled state in sync with the tree.
        updateAlignmentViewerButton();
    }

    // Alan 5/13/26 - Refresh the Alignment Viewer button label/state based on the current tree.
    function updateAlignmentViewerButton() {
        if (!btnAlignmentViewer) return;
        if (!viewer || typeof viewer.getVisibleTipOrder !== 'function') {
            btnAlignmentViewer.disabled = true;
            btnAlignmentViewer.classList.add('opacity-50', 'cursor-not-allowed');
            btnAlignmentViewer.innerHTML = '<i class="fa fa-stream"></i> Alignment Viewer';
            return;
        }
        const visible = viewer.getVisibleTipOrder();
        const selected = typeof viewer.getSelectedTipNames === 'function' ? viewer.getSelectedTipNames() : [];
        const count = selected.length > 0 ? selected.length : visible.length;
        const disabled = count === 0;
        btnAlignmentViewer.disabled = disabled;
        btnAlignmentViewer.classList.toggle('opacity-50', disabled);
        btnAlignmentViewer.classList.toggle('cursor-not-allowed', disabled);
        // Alan 7/20/26 - Preserve V shortcut discoverability when the count-aware Alignment Viewer tooltip refreshes.
        btnAlignmentViewer.title = selected.length > 0
            ? `Open Alignment Viewer for ${selected.length} selected sequence${selected.length === 1 ? '' : 's'} (V)`
            : `Open Alignment Viewer for ${visible.length} visible sequence${visible.length === 1 ? '' : 's'} (V)`;
        btnAlignmentViewer.innerHTML = count > 0
            ? `<i class="fa fa-stream"></i> Alignment Viewer (${count})`
            : '<i class="fa fa-stream"></i> Alignment Viewer';
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
            // Alan 7/20/26 - Identity displays its direct maximum while coverage sliders retain their inverted minimum scale.
            ['slider-identity', 'identity-value', 'identityMaximum', false]
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

        // Alan 7/20/26 - Let each metric declare whether its renderer threshold is inverted for display.
        sliderConfig.forEach(([sliderId, valueId, statKey, invert = true]) => {
            const slider = getEl(sliderId);
            const valueEl = getEl(valueId);
            const rawValue = Number(stats?.[statKey]);
            const value = Number.isFinite(rawValue) ? Math.max(0, Math.min(100, Math.round(rawValue))) : 0;
            // Alan 7/20/26 - Display identity directly so 99% immediately removes hits above 99% identity.
            const displayValue = invert ? 100 - value : value;
            if (slider) {
                // Alan 7/20/26 - Put the thumb at the computed direct or inverted display percentage.
                slider.value = String(displayValue);
                slider.disabled = !metricsAvailable;
                slider.classList.toggle('opacity-40', !metricsAvailable);
                slider.classList.toggle('cursor-not-allowed', !metricsAvailable);
            }
            if (valueEl) {
                // Alan 7/20/26 - Label the same percentage represented by the slider thumb.
                valueEl.textContent = metricsAvailable ? `${displayValue}%` : '--';
            }
        });
    }

    // Alan 5/9/26 - Read current metric slider values into renderer filter options.
    function getSequenceFilterSliderOptions() {
        // Alan 7/20/26 - Read either an inverted minimum threshold or a direct maximum cutoff from a slider.
        const readSlider = (id, invert = true) => {
            const slider = getEl(id);
            const fallback = invert ? 0 : 100;
            return slider && !slider.disabled
                ? (invert ? 100 - parseInt(slider.value, 10) : parseInt(slider.value, 10))
                : fallback;
        };
        return {
            queryCoverThreshold: readSlider('slider-query-cover'),
            subjectCoverThreshold: readSlider('slider-subject-cover'),
            // Alan 7/20/26 - Send identity as the maximum percentage that remains visible.
            identityMaximum: readSlider('slider-identity', false)
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
            // Alan 8/4/26 - Pull the label and hover note from the shared SUPPORT_TYPE_INFO map
            // (tree_viewer_phylotree_v2.js) so the badge and the on-tree labels stay in sync.
            const info = (window.SUPPORT_TYPE_INFO || {})[stats.supportType]
                || (window.SUPPORT_TYPE_INFO || {}).none
                || { label: 'None', tooltip: '' };
            const label = info.label;

            badge.textContent = `Support: ${label}`;
            // Alan 8/4/26 - Reviewers were misreading FastTree's 0-1 SH-like values as Bayesian
            // posteriors. Give the badge a ring + tooltip so the scale is legible at a glance.
            badge.title = info.tooltip;
            badge.className = "px-2 py-0.5 text-xs font-semibold rounded shrink-0 transition-colors ring-1 cursor-help";

            // Alan 8/4/26 - Added matching ring-* colors so the ring-1 outline above tracks
            // each support scale instead of falling back to Tailwind's default blue ring.
            if (stats.supportType === 'BS') {
                badge.classList.add('text-blue-800', 'bg-blue-100', 'ring-blue-400/60', 'dark:text-blue-200', 'dark:bg-blue-900/40');
            }
            else if (stats.supportType === 'PP') {
                badge.classList.add('text-purple-800', 'bg-purple-100', 'ring-purple-400/60', 'dark:text-purple-200', 'dark:bg-purple-900/40');
            }
            else if (stats.supportType === 'SH') {
                // Teal for FastTree SH
                badge.classList.add('text-teal-800', 'bg-teal-100', 'ring-teal-400/60', 'dark:text-teal-200', 'dark:bg-teal-900/40');
            }
            else if (stats.supportType === 'mixed') {
                badge.classList.add('text-amber-800', 'bg-amber-100', 'ring-amber-400/60', 'dark:text-amber-200', 'dark:bg-amber-900/40');
            }
            else {
                badge.classList.add('text-gray-800', 'bg-gray-100', 'ring-gray-400/60', 'dark:text-gray-200', 'dark:bg-gray-700/40');
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

        // Alan 8/4/26 - Dual SH-aLRT/UFBoot trees threshold on the UFBoot half, so the
        // bootstrap input stays live and is relabelled to say which half it filters.
        if (bsInput) {
            const bsLabel = bsInput.parentElement && bsInput.parentElement.querySelector('span');
            if (bsLabel) bsLabel.textContent = (s === 'ALRT_UFBOOT') ? "UFBoot >" : "BS >";
            setInput(bsInput, globalEnable && (s === 'BS' || s === 'mixed' || s === 'ALRT_UFBOOT'));
        }
    }

    // START
    loadTree();
});
