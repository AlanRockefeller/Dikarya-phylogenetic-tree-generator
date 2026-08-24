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
    // Alan 8/17/26 - Cache the "Analyze with Claude" modal's elements alongside the other
    // toolbar controls. Claude review controls. Absent when the server has no API key configured,
    // in which case every reference below no-ops via optional chaining.
    const btnClaudeReview = getEl('btn-claude-review');
    const claudeReviewModal = getEl('modal-claude-review');
    const claudeReviewBackdrop = getEl('claude-review-backdrop');
    const claudeReviewBody = getEl('claude-review-body');
    const claudeReviewSubtitle = getEl('claude-review-subtitle');
    const btnClaudeReviewClose = getEl('btn-claude-review-close');
    const btnClaudeReviewDone = getEl('btn-claude-review-done');
    const btnClaudeReviewRefresh = getEl('btn-claude-review-refresh');

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

    // Alan 8/17/26 - New section: the "Analyze with Claude" review modal and its renderer.
    // --- CLAUDE REVIEW ---
    // Claude's review is prose plus a few short lists, all of it model output, so
    // everything below is escaped before it reaches the DOM and only a tiny,
    // fixed subset of Markdown is turned back into markup.
    let claudeReviewInFlight = false;

    function escapeHtml(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    // Alan 8/17/26 - Tiny Markdown subset for Claude's review prose, so model output never
    // reaches the DOM as markup. Deliberately minimal: paragraphs, `code`, **bold**, *italic*,
    // and dash bullets. Anything else stays literal text, which is the safe failure.
    function renderSimpleMarkdown(text) {
        const inline = (chunk) => escapeHtml(chunk)
            .replace(/`([^`]+)`/g, '<code class="px-1 py-0.5 rounded bg-gray-100 dark:bg-journal-dark font-mono text-[0.85em]">$1</code>')
            .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
            .replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>');

        return String(text ?? '').split(/\n{2,}/).map((block) => {
            const lines = block.split('\n').map(l => l.trim()).filter(Boolean);
            if (!lines.length) return '';
            if (lines.every(l => /^[-*]\s+/.test(l))) {
                const items = lines.map(l => `<li>${inline(l.replace(/^[-*]\s+/, ''))}</li>`).join('');
                return `<ul class="list-disc pl-5 space-y-1">${items}</ul>`;
            }
            return `<p>${inline(lines.join(' '))}</p>`;
        }).join('');
    }

    const CLAUDE_RATING_STYLES = {
        strong: { label: 'Strong', classes: 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-200' },
        usable: { label: 'Usable', classes: 'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-200' },
        caution: { label: 'Caution', classes: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/40 dark:text-yellow-200' },
        unreliable: { label: 'Unreliable', classes: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-200' }
    };
    // Alan 8/21/26 - Defence in depth only: the server rejects a rating outside the schema
    // enum before it can be cached. If one ever reaches here it must not land on the
    // "Usable" styling, which would dress a malformed answer up as a favourable one.
    // Not a rating the model may produce -- it is not in RESPONSE_SCHEMA.
    const CLAUDE_RATING_UNKNOWN = {
        label: 'Unrated',
        classes: 'bg-gray-100 text-gray-700 dark:bg-gray-700/50 dark:text-gray-200'
    };
    const CLAUDE_SEVERITY_STYLES = {
        high: 'border-red-400 dark:border-red-500',
        medium: 'border-yellow-400 dark:border-yellow-500',
        low: 'border-gray-300 dark:border-gray-600'
    };

    function setClaudeReviewBody(html) {
        if (claudeReviewBody) claudeReviewBody.innerHTML = html;
    }

    function renderClaudeReviewLoading() {
        if (claudeReviewSubtitle) claudeReviewSubtitle.textContent = 'Reading the alignment and tree statistics…';
        setClaudeReviewBody(`
            <div class="flex items-center gap-3 text-gray-600 dark:text-gray-300 py-8 justify-center">
                <i class="fas fa-circle-notch fa-spin text-journal-gold text-xl"></i>
                <span class="text-sm">This usually takes under a minute.</span>
            </div>`);
    }

    function renderClaudeReviewError(message) {
        if (claudeReviewSubtitle) claudeReviewSubtitle.textContent = 'Review unavailable';
        setClaudeReviewBody(`
            <div class="rounded-lg border border-red-300 dark:border-red-700 bg-red-50 dark:bg-red-900/20 p-4">
                <p class="text-sm font-semibold text-red-800 dark:text-red-200 mb-1">Could not complete the review</p>
                <p class="text-sm text-red-700 dark:text-red-300">${escapeHtml(message)}</p>
            </div>`);
    }

    // Alan 8/22/26 - "Well supported: 62%" told the user nothing about what counted as
    // well supported, and the scale itself was shown as its raw code ("SH", "UFBOOT").
    // Both now say what they mean, using the same support-type information the badge uses.
    function claudeSupportLabel(supportType) {
        const info = (window.SUPPORT_TYPE_INFO || {})[supportType];
        return info ? info.label : supportType;
    }

    function claudeSupportCriterion(supportType, threshold) {
        if (threshold === null || threshold === undefined) return '';
        const value = Number(threshold);
        const shown = Number.isInteger(value) ? String(value) : value.toFixed(2);
        switch (supportType) {
            case 'BS': return `BS ≥${shown}`;
            case 'UFBOOT': return `UFBoot ≥${shown}`;
            case 'ALRT': return `SH-aLRT ≥${shown}`;
            // The single-value percentage thresholds the UFBoot half; the joint rule is
            // reported separately from dual_support_summary so neither is overstated.
            case 'ALRT_UFBOOT': return `UFBoot ≥${shown}`;
            case 'SH': return `≥${shown} SH-like`;
            case 'PP': return `PP ≥${shown}`;
            default: return `≥${shown}`;
        }
    }

    // Same loose matching the backend uses to line tip labels up with FASTA headers.
    function claudeNormalizeName(value) {
        return String(value ?? '').replace(/_/g, ' ').replace(/\s+/g, ' ').trim().toLowerCase();
    }

    // Alan 8/22/26 - Claude's prose must not be the authority for a branch length or a gap
    // percentage. Every deterministic per-sequence number the backend already computed is
    // indexed here and rendered beside the model's reason.
    function claudeSequenceFacts(metrics) {
        const index = new Map();
        const merge = (rows) => {
            (Array.isArray(rows) ? rows : []).forEach(row => {
                if (!row || !row.name) return;
                const key = claudeNormalizeName(row.name);
                index.set(key, Object.assign({}, index.get(key) || {}, row));
            });
        };
        const tree = metrics.tree || {};
        const alignment = metrics.alignment || {};
        merge(tree.longest_terminal_branches);
        merge(tree.outlier_long_branch_tips);
        merge(alignment.gappiest_sequences);
        merge(alignment.most_internally_gapped_sequences);
        merge(alignment.most_ambiguous_sequences);
        merge(alignment.shortest_sequences);
        return index;
    }

    function claudeFactChips(row) {
        if (!row) return '';
        const chips = [];
        const num = (value, digits) => (typeof value === 'number'
            ? (digits === undefined ? String(value) : value.toFixed(digits))
            : null);
        if (typeof row.branch_length === 'number') chips.push(`branch ${num(row.branch_length)}`);
        if (typeof row.ungapped_length === 'number') chips.push(`${row.ungapped_length} bp`);
        if (typeof row.gap_percent === 'number') chips.push(`gaps ${num(row.gap_percent, 1)}%`);
        if (typeof row.terminal_gap_percent === 'number' && typeof row.internal_gap_percent === 'number') {
            chips.push(`terminal ${num(row.terminal_gap_percent, 1)}% / internal ${num(row.internal_gap_percent, 1)}%`);
        }
        if (typeof row.ambiguity_percent === 'number') chips.push(`ambiguity ${num(row.ambiguity_percent, 1)}%`);
        if (typeof row.parent_support === 'number') chips.push(`parent support ${num(row.parent_support)}`);
        if (!chips.length) return '';
        return `<div class="mt-1 flex flex-wrap gap-1">${chips.map(chip => `
            <span class="px-1.5 py-0.5 rounded bg-gray-100 dark:bg-journal-dark text-[10px] font-mono text-gray-600 dark:text-gray-300">${escapeHtml(chip)}</span>`).join('')}</div>`;
    }

    function renderClaudeReview(payload) {
        const review = payload?.review || {};
        const rating = CLAUDE_RATING_STYLES[review.overall_rating] || CLAUDE_RATING_UNKNOWN;
        const metrics = payload?.metrics || {};
        const alignment = metrics.alignment || {};
        const tree = metrics.tree || {};

        if (claudeReviewSubtitle) {
            const when = payload?.generated_at ? new Date(payload.generated_at * 1000).toLocaleString() : '';
            claudeReviewSubtitle.textContent = payload?.cached
                ? `Saved review${when ? ` from ${when}` : ''} — use Re-run for a fresh one.`
                : `Reviewed by ${payload?.model || 'Claude'}${when ? ` at ${when}` : ''}.`;
        }

        const sections = [];

        sections.push(`
            <div class="flex flex-wrap items-center gap-3">
                <span class="px-3 py-1 rounded-full text-xs font-bold uppercase tracking-wide ${rating.classes}">${escapeHtml(rating.label)}</span>
                <p class="text-base font-semibold text-journal-dark dark:text-gray-100 flex-1 min-w-[16rem]">${escapeHtml(review.headline || '')}</p>
            </div>`);

        // A compact strip of the numbers Claude was actually given, so any claim
        // in the prose can be checked against its source without leaving the modal.
        // Alan 8/21/26 - Column tallies are exact only when every column was scored. On a
        // sampled run the backend publishes `parsimony_informative_columns_estimated`
        // instead of the bare count, so a figure extrapolated from a fraction of the
        // alignment can never be shown as the count for the whole alignment.
        const sampled = alignment.column_metrics_are_estimates === true;
        const informativeLabel = sampled ? 'Parsimony-informative (est.)' : 'Parsimony-informative';
        const informativeValue = sampled
            ? (alignment.parsimony_informative_columns_estimated != null
                ? `≈${alignment.parsimony_informative_columns_estimated}`
                : (alignment.parsimony_informative_percent != null
                    ? `≈${alignment.parsimony_informative_percent}% of columns`
                    : null))
            : alignment.parsimony_informative_columns;

        // Alan 8/22/26 - Show the human-readable scale name rather than the backend's code,
        // and say what "well supported" was measured against. A bare percentage under an
        // unexplained "SH" is exactly the misreading the support work was meant to end.
        const supportType = tree.support_type;
        const supportCriterion = claudeSupportCriterion(supportType, tree.strong_support_threshold);
        const dual = tree.dual_support_summary;

        // Alan 8/22/26 - Rooting is never inferred from topology, and "unknown" is a real
        // answer the user needs to see rather than an absent row.
        const rooting = tree.rooting || {};
        const rootingValue = rooting.rooting_known
            ? (rooting.description || rooting.root_mode)
            : 'Unknown (not recorded)';

        const facts = [
            ['Sequences', alignment.sequences],
            ['Alignment columns', alignment.columns],
            [informativeLabel, informativeValue],
            sampled
                ? ['Columns scored', `${alignment.columns_scored} of ${alignment.columns} (sampled)`]
                : null,
            ['Gaps', alignment.overall_gap_percent != null ? `${alignment.overall_gap_percent}%` : null],
            ['Tips', tree.tips],
            ['Support scale', supportType && supportType !== 'none' ? claudeSupportLabel(supportType) : null],
            [
                supportCriterion ? `Well supported (${supportCriterion})` : 'Well supported',
                tree.strongly_supported_percent != null ? `${tree.strongly_supported_percent}%` : null
            ],
            (dual && dual.nodes_scored)
                ? ['SH-aLRT ≥80 + UFBoot ≥95', `${dual.nodes_meeting_both_thresholds} of ${dual.nodes_scored} nodes`]
                : null,
            ['Rooting', rootingValue]
        ].filter(entry => entry && entry[1] !== null && entry[1] !== undefined);

        if (facts.length) {
            sections.push(`
                <div class="grid grid-cols-2 sm:grid-cols-4 gap-3">
                    ${facts.map(([label, value]) => `
                        <div class="rounded-lg border border-gray-200 dark:border-journal-dark px-3 py-2">
                            <div class="text-[10px] font-bold uppercase tracking-wide text-gray-500 dark:text-gray-400">${escapeHtml(label)}</div>
                            <div class="text-sm font-semibold text-journal-dark dark:text-gray-100">${escapeHtml(value)}</div>
                        </div>`).join('')}
                </div>`);
        }

        // Alan 8/22/26 - When the review describes a pruned or recomputed tree it is NOT
        // describing every sequence the tree builder saw, and the user has no way to know
        // that from the prose. Say so above the summary.
        const builderSequences = alignment.sequences_in_builder_alignment
            ?? alignment.sequences_in_source_file;
        const scopeNotes = [];
        if (alignment.alignment_restricted_to_current_tips && builderSequences) {
            scopeNotes.push(`Reviewed the currently displayed tree (${alignment.sequences} of ${builderSequences} sequences in the alignment the tree builder used). Branch support was estimated on the full alignment; the alignment statistics above describe only the displayed tips.`);
        } else if (alignment.alignment_is_tree_builder_input === false) {
            scopeNotes.push(`Alignment statistics come from ${alignment.source_file}, which is not the alignment this tree's builder consumed.`);
        }
        if (alignment.tree_tips_unmatched_in_alignment) {
            scopeNotes.push(`${alignment.tree_tips_unmatched_in_alignment} tip(s) in this tree had no matching alignment row, so no alignment statistics could be computed for them.`);
        }
        if (scopeNotes.length) {
            sections.push(`
                <div class="rounded-lg border border-gray-200 dark:border-journal-dark bg-gray-50 dark:bg-journal-dark/40 px-3 py-2">
                    ${scopeNotes.map(note => `<p class="text-xs text-gray-600 dark:text-gray-300">${escapeHtml(note)}</p>`).join('')}
                </div>`);
        }

        if (review.summary) {
            sections.push(`
                <div class="text-sm text-gray-700 dark:text-gray-200 space-y-3 leading-relaxed">
                    ${renderSimpleMarkdown(review.summary)}
                </div>`);
        }

        const concerns = Array.isArray(review.concerns) ? review.concerns : [];
        if (concerns.length) {
            sections.push(`
                <div>
                    <h4 class="font-semibold text-journal-gold mb-2 uppercase text-xs tracking-wider">Concerns</h4>
                    <div class="space-y-2">
                        ${concerns.map(item => `
                            <div class="border-l-4 ${CLAUDE_SEVERITY_STYLES[item?.severity] || CLAUDE_SEVERITY_STYLES.low} pl-3 py-1">
                                <p class="text-sm font-semibold text-journal-dark dark:text-gray-100">
                                    ${escapeHtml(item?.title || '')}
                                    <span class="ml-1 text-[10px] font-bold uppercase text-gray-500 dark:text-gray-400">${escapeHtml(item?.severity || '')}</span>
                                </p>
                                <p class="text-sm text-gray-700 dark:text-gray-300">${escapeHtml(item?.detail || '')}</p>
                            </div>`).join('')}
                    </div>
                </div>`);
        }

        const strengths = Array.isArray(review.strengths) ? review.strengths : [];
        if (strengths.length) {
            sections.push(`
                <div>
                    <h4 class="font-semibold text-journal-gold mb-2 uppercase text-xs tracking-wider">Strengths</h4>
                    <ul class="list-disc pl-5 space-y-1 text-sm text-gray-700 dark:text-gray-300">
                        ${strengths.map(item => `<li>${escapeHtml(item)}</li>`).join('')}
                    </ul>
                </div>`);
        }

        const recommendations = Array.isArray(review.recommendations) ? review.recommendations : [];
        if (recommendations.length) {
            sections.push(`
                <div>
                    <h4 class="font-semibold text-journal-gold mb-2 uppercase text-xs tracking-wider">Suggested next steps</h4>
                    <ol class="list-decimal pl-5 space-y-1 text-sm text-gray-700 dark:text-gray-300">
                        ${recommendations.map(item => `<li>${escapeHtml(item)}</li>`).join('')}
                    </ol>
                </div>`);
        }

        const suspects = Array.isArray(review.sequences_to_inspect) ? review.sequences_to_inspect : [];
        if (suspects.length) {
            // Alan 8/22/26 - Join the backend's own numbers onto each named sequence, and flag
            // a name that is not a tip of the tree on screen instead of presenting the model's
            // prose as authoritative. A cached review can also outlive a rename.
            const factIndex = claudeSequenceFacts(metrics);
            const currentTips = (viewer && typeof viewer.getTipNames === 'function')
                ? new Set(viewer.getTipNames().map(claudeNormalizeName))
                : null;

            sections.push(`
                <div>
                    <h4 class="font-semibold text-journal-gold mb-2 uppercase text-xs tracking-wider">Sequences worth a look</h4>
                    <div class="overflow-x-auto">
                        <table class="min-w-full text-xs text-left text-gray-600 dark:text-gray-300">
                            <thead class="text-gray-500 dark:text-gray-400 uppercase border-b border-gray-200 dark:border-gray-700">
                                <tr><th class="py-2 pr-4 font-semibold">Sequence</th><th class="py-2 font-semibold">Why</th></tr>
                            </thead>
                            <tbody class="divide-y divide-gray-100 dark:divide-gray-800">
                                ${suspects.map(item => {
                                    const key = claudeNormalizeName(item?.name || '');
                                    const facts = factIndex.get(key);
                                    const missing = currentTips && key && !currentTips.has(key);
                                    return `
                                    <tr>
                                        <td class="py-2 pr-4 font-mono break-all max-w-sm align-top">
                                            ${escapeHtml(item?.name || '')}
                                            ${missing ? `<div class="mt-1 inline-block px-1.5 py-0.5 rounded bg-yellow-100 text-yellow-800 dark:bg-yellow-900/40 dark:text-yellow-200 text-[10px] font-sans font-semibold">not a tip on this tree</div>` : ''}
                                            ${claudeFactChips(facts)}
                                        </td>
                                        <td class="py-2 align-top">${escapeHtml(item?.reason || '')}</td>
                                    </tr>`;
                                }).join('')}
                            </tbody>
                        </table>
                    </div>
                    <p class="mt-2 text-[11px] text-gray-500 dark:text-gray-400">Values in grey are computed from the alignment and tree, not written by Claude.</p>
                </div>`);
        }

        setClaudeReviewBody(sections.join(''));
    }

    function closeClaudeReview() {
        if (!claudeReviewModal) return;
        claudeReviewModal.classList.add('hidden');
        document.body.classList.remove('overflow-hidden');
    }

    async function runClaudeReview({ refresh = false } = {}) {
        if (!claudeReviewModal || claudeReviewInFlight) return;
        claudeReviewInFlight = true;
        claudeReviewModal.classList.remove('hidden');
        document.body.classList.add('overflow-hidden');
        btnClaudeReview?.setAttribute('disabled', 'disabled');
        if (btnClaudeReviewRefresh) btnClaudeReviewRefresh.disabled = true;
        renderClaudeReviewLoading();
        btnClaudeReviewClose?.focus();

        try {
            const payload = await TreeEditActions.claudeReview(window.JOB_ID, { refresh });
            renderClaudeReview(payload);
        } catch (error) {
            renderClaudeReviewError(error?.message || 'Unknown error.');
        } finally {
            claudeReviewInFlight = false;
            btnClaudeReview?.removeAttribute('disabled');
            if (btnClaudeReviewRefresh) btnClaudeReviewRefresh.disabled = false;
        }
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

    // Alan 8/14/26 - Resolve a clicked node into a backend reroot target. Tips keep their
    // original name; internal nodes use the stable descendant-tip ID rather than their
    // rendered support label, which the backend cannot match back to a clade.
    function getRerootTargetForNode(node) {
        const data = node?.data || node || {};
        const children = node?.children || data.children || [];
        if (!children || children.length === 0) {
            return data.__original_name || data.original_name || data.name || node?.name || null;
        }
        const tipNames = getDescendantTipNames(node);
        if (!tipNames.length) return null;
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
        // Alan 8/15/26 - The prune response is the saved tree state, so enable Edited FASTA now
        // instead of waiting for the tree reload.
        updateEditedFastaAvailability(pruneResult);
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

    // ======================================================================
    // Alan 8/15/26 - LAYERED CLADE ANNOTATIONS
    //
    // Local mirror of the persisted `annotation_layers` / `clade_annotations`
    // arrays. Every mutation goes through saveAnnotationsNow(), which posts the
    // WHOLE configuration to one atomic endpoint and then adopts the server's
    // normalized reply (so layer order is always the server's, never ours).
    // ======================================================================
    let annotationLayers = [];
    let cladeAnnotations = [];
    let annotationSaveDebounce = null;
    // Alan 8/15/26 - Serialize annotation saves: at most one POST in flight per tab, with
    // everything requested meanwhile coalesced into one follow-up send of the LATEST state.
    // `annotationSaveChain` is that in-flight cycle, `annotationSaveQueued` says another send
    // is owed, and `annotationRevision` counts local edits so a reply that is already stale
    // (the user kept typing/dragging while it was in flight) is never applied over them.
    let annotationSaveChain = null;
    let annotationSaveQueued = false;
    let annotationRevision = 0;
    // Alan 8/15/26 - Editor session: what is being added/edited and for which tips.
    let annotationEditorState = null;
    const ANNOTATION_STYLE_FIELDS = [
        { field: 'font_family', label: 'Font' },
        { field: 'font_size', label: 'Size' },
        { field: 'font_style', label: 'Style' },
        { field: 'font_weight', label: 'Weight' },
        { field: 'text_color', label: 'Text color' },
        // Alan 8/17/26 - Expose bubble fill controls alongside the shared line and text styles.
        { field: 'line_color', label: 'Line / border color' },
        { field: 'fill_color', label: 'Bubble fill' },
        { field: 'fill_opacity', label: 'Bubble opacity' },
        // Alan 8/24/26 - Clade highlights get their own fill fields; sharing the bubble's
        // would give every band the bubble default of near-opaque white.
        { field: 'highlight_color', label: 'Highlight color' },
        { field: 'highlight_opacity', label: 'Highlight opacity' }
    ];
    // Alan 8/24/26 - "Automatic" is a real, stored choice, not the absence of one: an
    // automatic highlight takes the colour of the clade's persistent colour group when it has
    // one and a palette colour otherwise. Keeping it as an explicit mode is what lets a user
    // deliberately pick the historical gold #c9a962 as a fixed colour and keep it.
    const HIGHLIGHT_COLOR_MODE_FIELD = 'highlight_color_mode';
    const LEGACY_AUTO_HIGHLIGHT_COLOR = '#c9a962';

    // Alan 8/24/26 - Carried alongside highlight_color but never given a row of its own; the
    // highlight-color row's Auto/Fixed select writes it.
    const ANNOTATION_HIDDEN_STYLE_FIELDS = [HIGHLIGHT_COLOR_MODE_FIELD];

    // Alan 8/24/26 - The bounded 0..1 style fields, so an opacity control is never built
    // with font-size bounds. Mirrors the renderer's ANNOTATION_NUMERIC_STYLE_FIELDS.
    const ANNOTATION_OPACITY_FIELDS = new Set(['fill_opacity', 'highlight_opacity']);
    const isNumericAnnotationField = (field) =>
        field === 'font_size' || ANNOTATION_OPACITY_FIELDS.has(field);
    // Alan 8/24/26 - Types that annotate a clade as a whole. They occupy a right-hand lane
    // and stay valid on the root, which has no incoming branch to hang a label on.
    const CLADE_ANNOTATION_TYPES = ['clade_line', 'clade_highlight'];
    const ALL_ANNOTATION_TYPES = CLADE_ANNOTATION_TYPES.concat(['branch_text', 'branch_bubble']);

    // Alan 8/24/26 - Read the effective mode for a layer, inferring it for layers saved
    // before the field existed: those said "automatic" by still carrying the shared default.
    function layerHighlightColorMode(layer) {
        const stored = layer ? layer['default_' + HIGHLIGHT_COLOR_MODE_FIELD] : null;
        if (stored === 'auto' || stored === 'fixed') return stored;
        const color = layer ? layer.default_highlight_color : null;
        return (!color || color === LEGACY_AUTO_HIGHLIGHT_COLOR) ? 'auto' : 'fixed';
    }

    // Alan 8/24/26 - Map the short-lived old aliases onto the canonical type in one place.
    function canonicalAnnotationType(value) {
        if (value === 'line') return 'clade_line';
        if (value === 'bubble') return 'branch_bubble';
        return ALL_ANNOTATION_TYPES.includes(value) ? value : 'clade_line';
    }

    const annotationsEditable = () => !window.VIEW_ONLY;
    const annotationConfig = () => window.DikaryaCladeAnnotations || {
        FONT_FAMILIES: ['Arial'], MIN_FONT_SIZE: 6, MAX_FONT_SIZE: 72,
        // Alan 8/17/26 - Keep the controller fallback complete when server configuration is absent.
        DEFAULTS: { font_family: 'Arial', font_size: 12, font_style: 'normal', font_weight: 'normal', text_color: '#1f2937', line_color: '#1f2937', fill_color: '#ffffff', fill_opacity: 0.9,
            highlight_color: '#c9a962', highlight_opacity: 0.2 }
    };

    // Alan 8/15/26 - Short, URL/attribute-safe IDs inside the server's 64-char limit.
    function newAnnotationId(prefix) {
        return `${prefix}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
    }

    function findAnnotationLayer(layerId) {
        return annotationLayers.find(layer => layer && layer.id === layerId) || null;
    }

    // Alan 8/15/26 - Effective value for one style property: annotation override, then layer
    // default, then the shared default. Mirrors the renderer so the editor previews the truth.
    function effectiveAnnotationStyle(annotation, layer, field) {
        const own = annotation ? annotation[field] : null;
        if (own !== null && own !== undefined && own !== '') return own;
        const inherited = layer ? layer['default_' + field] : null;
        const defaults = annotationConfig().DEFAULTS;
        // Alan 8/24/26 - highlight_color deliberately does NOT pass through here: its three
        // states (inherit / automatic / fixed) cannot be expressed as one value, so it has its
        // own control in highlightColorRow() and layerHighlightColorControl().
        if (inherited !== null && inherited !== undefined && inherited !== '') return inherited;
        return defaults[field];
    }

    // Alan 8/17/26 - Render the same plain text, line breaks, type, and resolved whole-label
    // style that the SVG renderer will use. textContent rather than HTML, so pasted markup is
    // displayed literally and can never execute; the CSS carries white-space: pre-line so the
    // preview shows the same line breaks the tree will draw. The font is mapped through the
    // shared fallback stack so the preview and the figure use the same typeface.
    function renderAnnotationLivePreview() {
        const preview = getEl('annotation-live-preview');
        if (!preview || !annotationEditorState) return;
        const label = String(getEl('input-annotation-label')?.value || '') || 'Annotation preview';
        // Alan 8/17/26 - Hand the complete typed draft to the renderer's shared SVG preview.
        const type = getEl('select-annotation-type')?.value || 'clade_line';
        const layer = findAnnotationLayer(getEl('select-annotation-layer')?.value);
        // Alan 8/17/26 - Package label, type, and nullable style overrides for the SVG renderer.
        const draft = { label, annotation_type: type };
        ANNOTATION_STYLE_FIELDS.forEach(({ field }) => {
            // Alan 8/17/26 - Keep inheritance explicit by passing null for unchecked overrides.
            draft[field] = annotationEditorState.style[field] ?? null;
        });
        // Alan 8/24/26 - The preview resolves the highlight colour exactly as the tree does,
        // so it needs the mode and the saved palette slot as well as the colour itself.
        ANNOTATION_HIDDEN_STYLE_FIELDS.forEach(field => {
            draft[field] = annotationEditorState.style[field] ?? null;
        });
        draft.automatic_highlight_slot = Number.isInteger(annotationEditorState.automaticSlot)
            ? annotationEditorState.automaticSlot : null;
        // Alan 8/17/26 - Replace the old CSS-only preview with the shared SVG primitive.
        // Alan 8/24/26 - Hand it the session's membership and id so an unstyled clade highlight
        // previews the automatic colour it will really be assigned, not a placeholder.
        if (viewer?.renderAnnotationPreview) {
            viewer.renderAnnotationPreview(preview, draft, layer, {
                memberIds: annotationEditorState.memberIds,
                annotationId: annotationEditorState.annotationId
                    || annotationEditorState.pendingId
            });
        }
    }

    // Alan 8/15/26 - Adopt a configuration (ours or the server's) into state, renderer and UI.
    // Reuse matching object identities: a focused layer control has handlers closed over its
    // layer object, and renderAnnotationManager deliberately leaves that control in place.
    // Replacing the object here would make the next change mutate a detached stale object.
    function adoptAnnotationObjects(current, incoming) {
        const byId = new Map(
            (Array.isArray(current) ? current : [])
                .filter(item => item && item.id)
                .map(item => [item.id, item])
        );
        return (Array.isArray(incoming) ? incoming : []).map(next => {
            if (!next || !next.id || !byId.has(next.id)) return next;
            const existing = byId.get(next.id);
            Object.keys(existing).forEach(key => {
                if (!Object.prototype.hasOwnProperty.call(next, key)) delete existing[key];
            });
            Object.assign(existing, next);
            return existing;
        });
    }

    function applyAnnotationState(layers, annotations) {
        // Alan 8/15/26 - A layer belonging to an open editor session is deliberately not sent
        // to the server yet, so no reply can mention it. Carry it across the adoption instead
        // of letting an unrelated round trip delete the layer the user is currently filling in.
        const pendingLayers = annotationLayers.filter(layer => layer && layer.__editorSession);
        annotationLayers = adoptAnnotationObjects(annotationLayers, layers);
        if (pendingLayers.length) {
            const known = new Set(annotationLayers.map(layer => layer && layer.id));
            pendingLayers.forEach(layer => { if (!known.has(layer.id)) annotationLayers.push(layer); });
        }
        cladeAnnotations = adoptAnnotationObjects(cladeAnnotations, annotations);
        if (viewer && typeof viewer.setCladeAnnotations === 'function') {
            viewer.setCladeAnnotations(annotationLayers, cladeAnnotations);
        }
        renderAnnotationManager();
    }

    // Alan 8/15/26 - Re-read the authoritative persisted configuration. Used after a failed
    // save so the UI never keeps showing changes that were rejected.
    async function reloadAnnotationsFromServer() {
        try {
            const state = await TreeEditActions.getTreeState(JOB_ID);
            applyAnnotationState(state.annotation_layers, state.clade_annotations);
        } catch (e) {
            console.warn('Could not reload annotations:', e);
        }
    }

    // Alan 8/15/26 - Every local annotation mutation bumps this before asking for a save, so
    // the save cycle can tell "this reply describes exactly what I sent" from "the user has
    // edited since, do not overwrite them with the server's older normalization".
    function touchAnnotations() {
        annotationRevision += 1;
    }

    // Alan 8/15/26 - Layers still owned by an open annotation editor are held back from every
    // save until that editor commits, so an unrelated save (a colour tweak, a visibility
    // toggle) can never persist a layer the user is about to cancel out of.
    function annotationLayersForSave() {
        return annotationLayers.filter(layer => layer && !layer.__editorSession);
    }

    // Alan 8/15/26 - The one place that talks to the annotation endpoint. It loops rather
    // than recursing so several requests can never overlap: whatever was asked for while a
    // POST was in flight is coalesced into a single follow-up send of the current state.
    async function runAnnotationSaveCycle() {
        let ok = true;
        try {
            do {
                annotationSaveQueued = false;
                const sentRevision = annotationRevision;
                try {
                    // Serialized at call time, so this is a snapshot of the state as it is now.
                    const data = await TreeEditActions.saveCladeAnnotations(
                        JOB_ID, annotationLayersForSave(), cladeAnnotations
                    );
                    ok = true;
                    // Adopt the server's normalization only while it is still the newest
                    // thing anyone has said. If the user edited during the round trip, their
                    // state wins and the next pass (or the pending debounce) sends it.
                    if (annotationRevision === sentRevision) {
                        applyAnnotationState(data.layers, data.annotations);
                    }
                } catch (err) {
                    const msg = (err.details && err.details.error) ? err.details.error : err.message;
                    showStatus(`Could not save annotations: ${msg}`, 'danger', 6000);
                    // Stop the cycle instead of retrying the same rejected payload forever,
                    // and fall back to the authoritative persisted configuration so nothing
                    // local-only keeps being displayed as if it had been saved.
                    annotationSaveQueued = false;
                    if (annotationSaveDebounce) {
                        clearTimeout(annotationSaveDebounce);
                        annotationSaveDebounce = null;
                    }
                    await reloadAnnotationsFromServer();
                    ok = false;
                    break;
                }
            } while (annotationSaveQueued);
            return ok;
        } finally {
            // Cleared synchronously as the loop exits, so a caller can never join a cycle
            // that has already decided it has nothing left to send.
            annotationSaveChain = null;
        }
    }

    // Alan 8/15/26 - One atomic save of the complete configuration. Last-write-wins across
    // tabs by design; the endpoint replaces only the annotation keys of tree_state.json.
    // Callers await the outcome of the coalesced cycle rather than starting a second POST.
    function saveAnnotationsNow() {
        if (annotationSaveDebounce) {
            clearTimeout(annotationSaveDebounce);
            annotationSaveDebounce = null;
        }
        if (!annotationsEditable() || JOB_ID === 'unknown') return Promise.resolve(false);
        touchAnnotations();
        if (annotationSaveChain) {
            annotationSaveQueued = true;
            return annotationSaveChain;
        }
        annotationSaveChain = runAnnotationSaveCycle();
        return annotationSaveChain;
    }

    // Alan 8/15/26 - Debounce rapid edits, especially the colour inputs, into one request.
    // The revision is bumped now, not when the timer fires, so an in-flight reply that lands
    // in between cannot repaint the UI with the colour the user has already moved past.
    function debouncedSaveAnnotations() {
        touchAnnotations();
        if (annotationSaveDebounce) clearTimeout(annotationSaveDebounce);
        annotationSaveDebounce = setTimeout(() => {
            annotationSaveDebounce = null;
            saveAnnotationsNow();
        }, 500);
    }

    // Alan 8/15/26 - Give a first-time user a layer without making them go find the Layers tab.
    // A layer created from inside the annotation editor is part of that editor's transaction:
    // it is tagged `__editorSession`, which holds it back from every save (see
    // annotationLayersForSave) and lets a cancel roll it back. Saving the annotation clears
    // the tag, so the layer goes to the server in the same atomic request as the annotation.
    function createAnnotationLayer(name, fromEditorSession = false) {
        const defaults = annotationConfig().DEFAULTS;
        const layer = {
            id: newAnnotationId('layer'),
            name: name,
            order: annotationLayers.length + 1,
            visible: true,
            default_font_family: defaults.font_family,
            default_font_size: defaults.font_size,
            default_font_style: defaults.font_style,
            default_font_weight: defaults.font_weight,
            default_text_color: defaults.text_color,
            // Alan 8/17/26 - New layers inherit bubble fill styling as concrete defaults.
            default_line_color: defaults.line_color,
            default_fill_color: defaults.fill_color,
            default_fill_opacity: defaults.fill_opacity,
            // Alan 8/24/26 - Seed the highlight defaults too, so a new layer's bands are
            // the subtle gold tint rather than the bubble's near-opaque white.
            default_highlight_color: defaults.highlight_color,
            // Alan 8/24/26 - A fresh layer is Automatic, stated outright rather than implied
            // by the colour it happens to carry.
            default_highlight_color_mode: 'auto',
            // Alan 8/24/26 - Deliberately NOT seeded: an untouched highlight opacity has to
            // be absent, not a copy of the shared default, or the layer's opacity control
            // would be inert at exactly the value it displays. The renderer resolves the
            // absent value to the theme-aware automatic opacity.
            default_highlight_opacity: null
        };
        if (fromEditorSession) layer.__editorSession = true;
        annotationLayers.push(layer);
        if (fromEditorSession && annotationEditorState) {
            annotationEditorState.createdLayerIds.push(layer.id);
        }
        return layer;
    }

    // Alan 8/17/26 - New section: the Add/Edit clade-annotation dialog and its transaction.
    // --- Annotation editor ------------------------------------------------

    function setAnnotationEditorError(message) {
        const el = getEl('annotation-editor-error');
        if (!el) return;
        el.textContent = message || '';
        el.classList.toggle('hidden', !message);
    }

    /**
     * Alan 8/15/26 - Close the editor, rolling back anything it created but never persisted.
     * Layers made inside an editor session used to survive a cancel as local-only "ghost"
     * state, and the next unrelated save would then persist a layer the user had thrown
     * away. Layers that existed before the editor opened are deliberately left alone.
     *
     * Committing (the save path) simply clears the session tag, which is what releases those
     * layers into the very next save alongside the annotation that references them.
     */
    function closeAnnotationEditor(commit) {
        // Strict comparison because this is also wired straight to click handlers, which
        // would otherwise pass an Event object here and read as "committed".
        const committed = commit === true;
        const sessionIds = new Set(
            (annotationEditorState && annotationEditorState.createdLayerIds) || []
        );
        if (committed) {
            annotationLayers.forEach(layer => {
                if (layer && sessionIds.has(layer.id)) delete layer.__editorSession;
            });
        }
        const createdIds = committed ? null : sessionIds;
        annotationEditorState = null;
        getEl('modal-annotation-editor')?.classList.add('hidden');
        getEl('annotation-inline-layer-row')?.classList.add('hidden');
        setAnnotationEditorError('');
        if (!createdIds || !createdIds.size) return;
        const before = annotationLayers.length;
        annotationLayers = annotationLayers.filter(
            layer => !(layer && layer.__editorSession && createdIds.has(layer.id))
        );
        if (annotationLayers.length === before) return;
        // Nothing was ever sent for these, so there is nothing to save -- just resync the UI.
        if (viewer?.setCladeAnnotations) viewer.setCladeAnnotations(annotationLayers, cladeAnnotations);
        renderAnnotationManager();
    }

    // Alan 8/15/26 - Build the compact per-property override rows. An unchecked box means the
    // stored value stays null, i.e. the annotation keeps inheriting that property from its layer.
    function renderAnnotationStyleOverrides() {
        const host = getEl('annotation-style-overrides');
        if (!host || !annotationEditorState) return;
        const cfg = annotationConfig();
        const layer = findAnnotationLayer(getEl('select-annotation-layer')?.value);
        const draft = annotationEditorState.style;
        host.textContent = '';

        ANNOTATION_STYLE_FIELDS.forEach(({ field, label }) => {
            // Alan 8/24/26 - Highlight colour is not a plain override: its three states are
            // "inherit from the layer", "work it out from the tree" and "this exact colour",
            // and showing a gold swatch for the first two while drawing blue was misleading.
            if (field === 'highlight_color') {
                host.appendChild(highlightColorRow(layer, draft, label));
                return;
            }

            const row = document.createElement('div');
            row.className = 'annotation-style-row';

            const toggle = document.createElement('input');
            toggle.type = 'checkbox';
            toggle.className = 'rounded border-gray-300 text-journal-gold focus:ring-journal-gold';
            toggle.checked = draft[field] !== null && draft[field] !== undefined;
            toggle.title = `Override the layer's ${label.toLowerCase()}`;

            const caption = document.createElement('label');
            caption.className = 'text-xs text-gray-600 dark:text-gray-300';
            caption.textContent = label;

            let control;
            const effective = effectiveAnnotationStyle(
                { [field]: draft[field] }, layer, field
            );

            if (field === 'font_family' || field === 'font_style' || field === 'font_weight') {
                control = document.createElement('select');
                control.className = 'rounded border border-gray-300 bg-white px-2 py-1 text-xs text-gray-900 dark:border-gray-700 dark:bg-journal-dark dark:text-gray-100';
                const options = field === 'font_family'
                    ? cfg.FONT_FAMILIES
                    : (field === 'font_style' ? ['normal', 'italic'] : ['normal', 'bold']);
                options.forEach(value => {
                    const option = document.createElement('option');
                    option.value = value;
                    option.textContent = value;
                    control.appendChild(option);
                });
                control.value = effective;
            // Alan 8/17/26 - Treat opacity as a bounded numeric override like font size.
            } else if (isNumericAnnotationField(field)) {
                const isOpacity = ANNOTATION_OPACITY_FIELDS.has(field);
                control = document.createElement('input');
                control.type = 'number';
                // Alan 8/17/26 - Bound opacity to 0–1 while retaining configured font-size bounds.
                control.min = isOpacity ? '0' : String(cfg.MIN_FONT_SIZE);
                control.max = isOpacity ? '1' : String(cfg.MAX_FONT_SIZE);
                control.step = isOpacity ? '0.05' : '1';
                control.className = 'w-24 rounded border border-gray-300 bg-white px-2 py-1 text-xs text-gray-900 dark:border-gray-700 dark:bg-journal-dark dark:text-gray-100';
                control.value = String(effective);
            } else {
                control = document.createElement('input');
                control.type = 'color';
                control.className = 'h-7 w-14 rounded border border-gray-300 bg-transparent p-0 dark:border-gray-700';
                control.value = effective;
            }

            control.disabled = !toggle.checked;
            const commit = () => {
                if (!toggle.checked) { draft[field] = null; return; }
                // Alan 8/17/26 - Store numeric opacity without string coercion in the draft.
                draft[field] = isNumericAnnotationField(field)
                    ? Number(control.value) : control.value;
                renderAnnotationLivePreview();
            };
            toggle.addEventListener('change', () => {
                control.disabled = !toggle.checked;
                commit();
                renderAnnotationLivePreview();
            });
            control.addEventListener('change', commit);
            control.addEventListener('input', commit);

            row.appendChild(toggle);
            row.appendChild(caption);
            row.appendChild(control);
            host.appendChild(row);
        });
    }

    /**
     * Alan 8/24/26 - The annotation-level highlight colour control.
     *
     * One select for the mode and one picker for the colour. The picker stays visible while
     * the mode is Inherit or Automatic, showing the colour that will actually be drawn, but
     * is disabled so it reads as a preview rather than a choice.
     */
    function highlightColorRow(layer, draft, labelText) {
        const row = document.createElement('div');
        row.className = 'annotation-style-row';

        const caption = document.createElement('label');
        caption.className = 'text-xs text-gray-600 dark:text-gray-300';
        caption.textContent = labelText;

        const mode = document.createElement('select');
        mode.className = 'rounded border border-gray-300 bg-white px-2 py-1 text-xs text-gray-900 dark:border-gray-700 dark:bg-journal-dark dark:text-gray-100';
        [
            ['inherit', 'Layer default'],
            ['auto', 'Automatic'],
            ['fixed', 'Fixed color']
        ].forEach(([value, text]) => {
            const option = document.createElement('option');
            option.value = value;
            option.textContent = text;
            mode.appendChild(option);
        });

        const picker = document.createElement('input');
        picker.type = 'color';
        picker.className = 'h-7 w-14 rounded border border-gray-300 bg-transparent p-0 dark:border-gray-700';

        // Inherit is "no annotation-level opinion at all": neither a colour nor a mode.
        const current = () => {
            if (draft.highlight_color) return 'fixed';
            if (draft[HIGHLIGHT_COLOR_MODE_FIELD] === 'auto') return 'auto';
            if (draft[HIGHLIGHT_COLOR_MODE_FIELD] === 'fixed') return 'fixed';
            return 'inherit';
        };
        mode.value = current();

        // Alan 8/24/26 - Ask with the DRAFT, not with the annotation id alone. While an
        // existing Fixed highlight is being switched to Automatic the saved annotation still
        // says fixed and still carries its palette slot, and resolving against that stale
        // object showed a swatch the save would not reproduce. The draft below is exactly what
        // saving would write for an Automatic highlight, so the swatch and the saved figure
        // agree -- including the stored slot, which the save keeps.
        const automaticColor = () => {
            if (!viewer?.resolveDraftHighlightColor || !annotationEditorState) {
                return annotationConfig().DEFAULTS.highlight_color;
            }
            return viewer.resolveDraftHighlightColor({
                id: annotationEditorState.annotationId || annotationEditorState.pendingId,
                layer_id: layer?.id || null,
                annotation_type: 'clade_highlight',
                member_tip_ids: annotationEditorState.memberIds,
                highlight_color: null,
                [HIGHLIGHT_COLOR_MODE_FIELD]: 'auto',
                automatic_highlight_slot: Number.isInteger(annotationEditorState.automaticSlot)
                    ? annotationEditorState.automaticSlot : null
            }, layer);
        };

        const sync = () => {
            const selected = mode.value;
            picker.disabled = selected !== 'fixed' || !annotationsEditable();
            picker.title = selected === 'fixed'
                ? 'Highlight color for this annotation'
                : 'The color this highlight will be drawn with';
            if (selected === 'fixed') {
                picker.value = draft.highlight_color
                    || (layerHighlightColorMode(layer) === 'fixed'
                        && layer?.default_highlight_color)
                    || automaticColor();
            } else if (selected === 'auto') {
                picker.value = automaticColor();
            } else {
                picker.value = layerHighlightColorMode(layer) === 'fixed'
                    ? (layer?.default_highlight_color
                        || annotationConfig().DEFAULTS.highlight_color)
                    : automaticColor();
            }
        };

        const commit = () => {
            const selected = mode.value;
            if (selected === 'fixed') {
                draft.highlight_color = picker.value;
                draft[HIGHLIGHT_COLOR_MODE_FIELD] = 'fixed';
            } else if (selected === 'auto') {
                // No colour, and an explicit auto, so an Auto annotation stays automatic even
                // on a layer whose default is a fixed colour.
                draft.highlight_color = null;
                draft[HIGHLIGHT_COLOR_MODE_FIELD] = 'auto';
            } else {
                draft.highlight_color = null;
                draft[HIGHLIGHT_COLOR_MODE_FIELD] = null;
            }
            renderAnnotationLivePreview();
            // The automatic colour is only known after the draft settles, so refresh the
            // swatch from the value the renderer just resolved.
            sync();
        };

        mode.disabled = !annotationsEditable();
        mode.addEventListener('change', commit);
        picker.addEventListener('change', commit);
        picker.addEventListener('input', commit);
        sync();

        row.appendChild(mode);
        row.appendChild(caption);
        row.appendChild(picker);
        return row;
    }

    function populateAnnotationLayerSelect(selectedLayerId) {
        const select = getEl('select-annotation-layer');
        if (!select) return;
        select.textContent = '';
        annotationLayers
            .slice()
            .sort((a, b) => (a.order || 0) - (b.order || 0))
            .forEach(layer => {
                const option = document.createElement('option');
                option.value = layer.id;
                option.textContent = `${layer.order}. ${layer.name}`;
                select.appendChild(option);
            });
        if (selectedLayerId) select.value = selectedLayerId;
    }

    /**
     * Alan 8/15/26 - Open the Add/Edit dialog.
     * `memberIds` are canonical leaf IDs already resolved from the tree; the editor never
     * derives membership from labels or positions.
     */
    function openAnnotationEditor(mode, options = {}) {
        if (!annotationsEditable()) return;
        const modal = getEl('modal-annotation-editor');
        if (!modal) return;

        const existing = mode === 'edit'
            ? cladeAnnotations.find(a => a.id === options.annotationId)
            : null;
        if (mode === 'edit' && !existing) return;

        // Alan 8/15/26 - Open the transaction BEFORE creating the first-use layer, so that
        // layer is recorded as this session's and is rolled back on cancel like any other
        // layer made inside the editor.
        annotationEditorState = {
            mode,
            annotationId: existing ? existing.id : null,
            memberIds: existing
                ? (existing.member_tip_ids || []).slice()
                : (Array.isArray(options.memberIds) ? options.memberIds.slice() : []),
            style: {},
            // Layers this editor session created; everything that existed beforehand is
            // untouched by a cancel.
            createdLayerIds: []
        };
        // Alan 8/24/26 - Reserve the id for a new annotation now rather than at save time. The
        // automatic highlight colour is assigned per annotation id, so the preview and the saved
        // annotation have to be talking about the same one.
        if (mode !== 'edit') annotationEditorState.pendingId = newAnnotationId('annotation');
        // Alan 8/17/26 - Whole-tree clades may use a bracket, but the root has no incoming
        // segment on which branch text or a branch bubble could be placed.
        annotationEditorState.hasIncomingBranch = viewer?.hasIncomingBranchForMemberIds
            ? viewer.hasIncomingBranchForMemberIds(annotationEditorState.memberIds)
            : true;
        annotationEditorState.defaultType = options.defaultType || 'clade_line';

        if (!annotationLayers.length) {
            // First use: give them a sensible layer rather than a dead-end dropdown.
            createAnnotationLayer('Annotations', true);
        }

        ANNOTATION_STYLE_FIELDS.forEach(({ field }) => {
            annotationEditorState.style[field] = existing ? (existing[field] ?? null) : null;
        });
        // Alan 8/24/26 - Carried but not shown as its own row; the highlight-color row owns it.
        ANNOTATION_HIDDEN_STYLE_FIELDS.forEach(field => {
            annotationEditorState.style[field] = existing ? (existing[field] ?? null) : null;
        });
        // Alan 8/24/26 - An automatic highlight keeps the palette slot it was saved with, so
        // deleting or adding OTHER annotations never recolours it. Editing preserves it; a new
        // annotation reserves one now so the preview shows the colour it will really get.
        annotationEditorState.automaticSlot = existing
            ? (Number.isInteger(existing.automatic_highlight_slot)
                ? existing.automatic_highlight_slot : null)
            : null;

        // Alan 8/17/26 - Use branch-oriented copy and restrict root annotations to clade lines.
        getEl('annotation-editor-title').textContent = mode === 'edit'
            // Alan 8/17/26 - Shorten the modal title now that it supports branch annotations.
            ? 'Edit Annotation' : 'Add Annotation';
        const count = annotationEditorState.memberIds.length;
        // Alan 8/17/26 - Describe saved membership as descendants of the annotated branch.
        getEl('annotation-editor-subtitle').textContent =
            // Alan 8/17/26 - Use branch-relative descendant wording for the membership count.
            `${count} descendant tip${count === 1 ? '' : 's'} on this branch.`;
        getEl('input-annotation-label').value = existing ? existing.label : '';
        // Alan 8/17/26 - Map short-lived aliases and disable branch-only root choices.
        const savedType = existing ? canonicalAnnotationType(existing.annotation_type) : null;
        const typeSelect = getEl('select-annotation-type');
        typeSelect.querySelectorAll('option').forEach(option => {
            // Alan 8/24/26 - Clade line AND clade highlight annotate the clade itself, so both
            // remain available on the root; only the branch types need an incoming branch.
            option.disabled = !annotationEditorState.hasIncomingBranch
                && !CLADE_ANNOTATION_TYPES.includes(option.value);
        });
        typeSelect.value = savedType || annotationEditorState.defaultType;
        getEl('btn-annotation-save-text').textContent = mode === 'edit' ? 'Save changes' : 'Save';
        getEl('btn-annotation-delete').classList.toggle('hidden', mode !== 'edit');
        getEl('annotation-style-details').open = false;

        populateAnnotationLayerSelect(existing ? existing.layer_id : annotationLayers[0]?.id);
        renderAnnotationStyleOverrides();
        renderAnnotationLivePreview();
        setAnnotationEditorError('');
        modal.classList.remove('hidden');
        getEl('input-annotation-label')?.focus();
    }

    async function submitAnnotationEditor() {
        if (!annotationEditorState) return;
        // Alan 8/17/26 - Normalize textarea newlines and tabs before client validation and saving.
        const label = String(getEl('input-annotation-label')?.value || '')
            .replace(/\r\n/g, '\n').replace(/\r/g, '\n').replace(/\t/g, '    ').trim();
        if (!label) {
            setAnnotationEditorError('Enter a label for this annotation.');
            return;
        }
        // Alan 8/17/26 - Match the server's ten-line annotation-label limit in the editor.
        if (label.split('\n').length > 10) {
            setAnnotationEditorError('Use no more than 10 lines.');
            return;
        }
        const layerId = getEl('select-annotation-layer')?.value;
        if (!layerId || !findAnnotationLayer(layerId)) {
            setAnnotationEditorError('Choose a layer for this annotation.');
            return;
        }
        if (!annotationEditorState.memberIds.length) {
            setAnnotationEditorError('This annotation has no member tips.');
            return;
        }
        // Alan 8/17/26 - Refuse branch-only types when the selected membership resolves to the root.
        const requestedType = getEl('select-annotation-type')?.value || 'clade_line';
        if (!annotationEditorState.hasIncomingBranch
            && !CLADE_ANNOTATION_TYPES.includes(requestedType)) {
            setAnnotationEditorError(
                'The whole-tree root has no incoming branch. Use Clade line or Clade highlight.'
            );
            return;
        }

        const payload = {
            // Alan 8/24/26 - Reuse the id the preview reserved, so the automatic colour the
            // user just approved is the one this annotation is assigned once saved.
            id: annotationEditorState.annotationId || annotationEditorState.pendingId
                || newAnnotationId('annotation'),
            layer_id: layerId,
            label,
            // Alan 8/24/26 - Save only the supported canonical annotation types.
            annotation_type: canonicalAnnotationType(requestedType),
            member_tip_ids: annotationEditorState.memberIds.slice()
        };
        ANNOTATION_STYLE_FIELDS.forEach(({ field }) => {
            payload[field] = annotationEditorState.style[field] ?? null;
        });
        ANNOTATION_HIDDEN_STYLE_FIELDS.forEach(field => {
            payload[field] = annotationEditorState.style[field] ?? null;
        });
        // Alan 8/24/26 - Pin the automatic palette slot on first save (and backfill it for
        // annotations saved before slots existed), so this highlight keeps the colour it was
        // published with no matter what happens to the annotations around it. It stays an
        // Automatic highlight: it still follows a colour group if its clade joins one, and it
        // still takes the theme-aware automatic opacity.
        if (payload.annotation_type === 'clade_highlight') {
            let slot = annotationEditorState.automaticSlot;
            if (!Number.isInteger(slot) && viewer?.reserveAutomaticHighlightSlot) {
                slot = viewer.reserveAutomaticHighlightSlot(payload.id, payload.member_tip_ids);
            }
            payload.automatic_highlight_slot = Number.isInteger(slot) ? slot : null;
        }

        if (annotationEditorState.mode === 'edit') {
            const index = cladeAnnotations.findIndex(a => a.id === payload.id);
            if (index >= 0) cladeAnnotations[index] = payload;
        } else {
            cladeAnnotations.push(payload);
        }

        // Alan 8/15/26 - Commit the editor transaction: any layer created in this session is
        // kept and goes to the server in the same atomic save as the annotation that uses it.
        // If that save fails, saveAnnotationsNow() reloads the persisted configuration, which
        // drops the local-only layer rather than leaving it behind as a fake.
        closeAnnotationEditor(true);
        const saved = await saveAnnotationsNow();
        if (saved) showStatus(`Annotation "${label}" saved.`, 'success', 2000);
    }

    async function deleteCurrentAnnotation() {
        if (!annotationEditorState || annotationEditorState.mode !== 'edit') return;
        const id = annotationEditorState.annotationId;
        cladeAnnotations = cladeAnnotations.filter(a => a.id !== id);
        closeAnnotationEditor();
        const saved = await saveAnnotationsNow();
        if (saved) showStatus('Annotation deleted.', 'success', 2000);
    }

    // Alan 8/17/26 - New section: the Annotation Manager list, layer cards and validity warnings.
    // --- Annotation manager ------------------------------------------------

    function openAnnotationManager() {
        const modal = getEl('modal-clade-annotations');
        if (!modal) return;
        renderAnnotationManager();
        modal.classList.remove('hidden');
        document.body.classList.add('overflow-hidden');
    }

    function closeAnnotationManager() {
        getEl('modal-clade-annotations')?.classList.add('hidden');
        document.body.classList.remove('overflow-hidden');
    }

    function setAnnotationManagerTab(tab) {
        const isLayers = tab === 'layers';
        getEl('annotations-tab-annotations')?.classList.toggle('hidden', isLayers);
        getEl('annotations-tab-layers')?.classList.toggle('hidden', !isLayers);
        document.querySelectorAll('.annotation-tab').forEach(button => {
            const active = button.getAttribute('data-annotation-tab') === tab;
            button.classList.toggle('border-journal-gold', active);
            button.classList.toggle('border-transparent', !active);
            button.classList.toggle('text-journal-dark', active);
            button.classList.toggle('dark:text-gray-100', active);
            button.classList.toggle('text-gray-500', !active);
            button.classList.toggle('dark:text-gray-400', !active);
        });
    }

    // Alan 8/15/26 - Small helper for the manager's compact icon buttons.
    function annotationIconButton(iconClass, title, onClick, extraClass = '') {
        const button = document.createElement('button');
        button.type = 'button';
        button.title = title;
        button.setAttribute('aria-label', title);
        button.className = 'px-2 py-1 text-xs rounded border border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-journal-dark disabled:opacity-40 disabled:cursor-not-allowed ' + extraClass;
        const icon = document.createElement('i');
        icon.className = iconClass;
        button.appendChild(icon);
        button.addEventListener('click', onClick);
        return button;
    }

    function renderAnnotationList() {
        const host = getEl('annotation-list');
        if (!host) return;
        host.textContent = '';

        if (!cladeAnnotations.length) {
            const empty = document.createElement('p');
            empty.className = 'annotation-empty text-gray-500 dark:text-gray-400';
            // Alan 8/17/26 - Describe the branch-first creation workflow in the empty state.
            empty.textContent = 'No annotations yet. Right-click any non-root branch and choose "Add annotation…", or select a complete clade and use the button above.';
            host.appendChild(empty);
            return;
        }

        const validity = (viewer && typeof viewer.getCladeAnnotationValidity === 'function')
            ? viewer.getCladeAnnotationValidity() : new Map();

        cladeAnnotations.forEach(annotation => {
            const layer = findAnnotationLayer(annotation.layer_id);
            const row = document.createElement('div');
            row.className = 'annotation-row';

            const main = document.createElement('div');
            main.className = 'annotation-row-main';

            const title = document.createElement('div');
            title.className = 'annotation-row-title text-gray-900 dark:text-gray-100';
            // textContent, never innerHTML: a label such as "<svg onload=alert(1)>" must
            // appear literally, exactly as it does on the tree.
            title.textContent = annotation.label;
            main.appendChild(title);

            const meta = document.createElement('div');
            meta.className = 'annotation-row-meta text-gray-500 dark:text-gray-400';
            const memberCount = (annotation.member_tip_ids || []).length;
            // Alan 8/17/26 - Show canonical branch and clade type names in the manager.
            const type = canonicalAnnotationType(annotation.annotation_type);
            const typeLabel = {
                branch_text: 'Branch text',
                branch_bubble: 'Branch bubble',
                clade_highlight: 'Clade highlight'
            }[type] || 'Clade line';
            meta.textContent = `${typeLabel} · ${layer ? layer.name : 'Unknown layer'} · ${memberCount} tip${memberCount === 1 ? '' : 's'}`;
            main.appendChild(meta);

            const status = validity.get(annotation.id);
            if (status && !status.valid) {
                const warning = document.createElement('div');
                warning.className = 'annotation-row-warning';
                warning.textContent = status.present === 0
                    ? 'None of its tips are in the current tree.'
                    : 'No longer forms one clade in the current rooting';
                main.appendChild(warning);
            }
            if (layer && layer.visible === false) {
                const hidden = document.createElement('div');
                hidden.className = 'annotation-row-meta text-gray-400 dark:text-gray-500';
                hidden.textContent = 'Its layer is hidden.';
                main.appendChild(hidden);
            }

            row.appendChild(main);

            row.appendChild(annotationIconButton('fas fa-crosshairs', 'Select this annotation’s tips', () => {
                if (!viewer?.selectLeafIds) return;
                const matched = viewer.selectLeafIds(annotation.member_tip_ids || []);
                updateButtons();
                showStatus(`Selected ${matched} tip${matched === 1 ? '' : 's'}.`, 'info', 1800);
            }));

            // Mutating controls simply are not rendered for read-only viewers; the server
            // rejects the requests regardless, so this is presentation only.
            if (annotationsEditable()) {
                row.appendChild(annotationIconButton('fas fa-pen', 'Edit annotation', () => {
                    openAnnotationEditor('edit', { annotationId: annotation.id });
                }));
                row.appendChild(annotationIconButton('fas fa-trash', 'Delete annotation', async () => {
                    cladeAnnotations = cladeAnnotations.filter(a => a.id !== annotation.id);
                    const saved = await saveAnnotationsNow();
                    if (saved) showStatus('Annotation deleted.', 'success', 2000);
                }, 'text-red-600 dark:text-red-300'));
            }

            host.appendChild(row);
        });
    }

    // Alan 8/15/26 - Build one labelled control for a layer's default style.
    /**
     * Alan 8/24/26 - A layer's default highlight colour: Automatic, or one fixed colour.
     *
     * Same reasoning as the annotation-level row. A layer set to Automatic hands each of its
     * highlights its own colour, so there is no single swatch to show for it and the picker is
     * disabled until the user says Fixed.
     */
    function layerHighlightColorControl(layer) {
        const wrap = document.createElement('label');
        wrap.className = 'annotation-layer-style text-gray-600 dark:text-gray-300';
        const caption = document.createElement('span');
        caption.textContent = 'Highlight';
        wrap.appendChild(caption);

        const mode = document.createElement('select');
        mode.className = 'rounded border border-gray-300 bg-white px-1 py-0.5 text-xs text-gray-900 dark:border-gray-700 dark:bg-journal-dark dark:text-gray-100';
        [['auto', 'Auto'], ['fixed', 'Fixed']].forEach(([value, text]) => {
            const option = document.createElement('option');
            option.value = value;
            option.textContent = text;
            mode.appendChild(option);
        });
        mode.value = layerHighlightColorMode(layer);

        const picker = document.createElement('input');
        picker.type = 'color';
        picker.className = 'h-6 w-10 rounded border border-gray-300 bg-transparent p-0 dark:border-gray-700';
        picker.value = layer.default_highlight_color
            || annotationConfig().DEFAULTS.highlight_color;

        const sync = () => {
            picker.disabled = mode.value !== 'fixed' || !annotationsEditable();
            picker.title = mode.value === 'fixed'
                ? 'Default highlight color for this layer'
                : 'Each highlight picks its own color from its clade or the palette';
        };
        mode.disabled = !annotationsEditable();

        const commit = () => {
            layer['default_' + HIGHLIGHT_COLOR_MODE_FIELD] = mode.value;
            // The colour is kept even while Automatic is selected, so switching back to Fixed
            // returns the colour the user last chose rather than a reset one.
            layer.default_highlight_color = picker.value;
            sync();
            if (viewer?.setCladeAnnotations) viewer.setCladeAnnotations(annotationLayers, cladeAnnotations);
            debouncedSaveAnnotations();
        };
        mode.addEventListener('change', commit);
        picker.addEventListener('change', commit);
        picker.addEventListener('input', commit);
        sync();

        wrap.appendChild(mode);
        wrap.appendChild(picker);
        return wrap;
    }

    function layerStyleControl(layer, field, labelText) {
        const cfg = annotationConfig();
        const wrap = document.createElement('label');
        wrap.className = 'annotation-layer-style text-gray-600 dark:text-gray-300';
        const caption = document.createElement('span');
        caption.textContent = labelText;
        wrap.appendChild(caption);

        let control;
        if (field === 'font_family' || field === 'font_style' || field === 'font_weight') {
            control = document.createElement('select');
            control.className = 'rounded border border-gray-300 bg-white px-1 py-0.5 text-xs text-gray-900 dark:border-gray-700 dark:bg-journal-dark dark:text-gray-100';
            const options = field === 'font_family'
                ? cfg.FONT_FAMILIES
                : (field === 'font_style' ? ['normal', 'italic'] : ['normal', 'bold']);
            options.forEach(value => {
                const option = document.createElement('option');
                option.value = value;
                option.textContent = value;
                control.appendChild(option);
            });
        // Alan 8/17/26 - Layer opacity uses the same bounded numeric control as font size.
        } else if (isNumericAnnotationField(field)) {
            control = document.createElement('input');
            control.type = 'number';
            // Alan 8/17/26 - Bound layer opacity to 0–1 while retaining font-size bounds.
            const isOpacity = ANNOTATION_OPACITY_FIELDS.has(field);
            control.min = isOpacity ? '0' : String(cfg.MIN_FONT_SIZE);
            control.max = isOpacity ? '1' : String(cfg.MAX_FONT_SIZE);
            control.step = isOpacity ? '0.05' : '1';
            control.className = 'w-16 rounded border border-gray-300 bg-white px-1 py-0.5 text-xs text-gray-900 dark:border-gray-700 dark:bg-journal-dark dark:text-gray-100';
        } else {
            control = document.createElement('input');
            control.type = 'color';
            control.className = 'h-6 w-10 rounded border border-gray-300 bg-transparent p-0 dark:border-gray-700';
        }
        // Alan 8/17/26 - Resolve absent newer layer fields through shared defaults for old state.
        control.value = String(effectiveAnnotationStyle(null, layer, field));
        // Left visible but inert for read-only viewers, so they can still see what a layer's
        // style actually is without being offered an edit that the server would refuse.
        control.disabled = !annotationsEditable();

        const commit = () => {
            // Alan 8/17/26 - Persist opacity as a number so layer defaults round-trip cleanly.
            layer['default_' + field] = isNumericAnnotationField(field)
                ? Number(control.value) : control.value;
            // Repaint immediately so inheriting annotations restyle without waiting for the
            // debounced save round-trip.
            if (viewer?.setCladeAnnotations) viewer.setCladeAnnotations(annotationLayers, cladeAnnotations);
            debouncedSaveAnnotations();
        };
        control.addEventListener('change', commit);
        if (control.type === 'color') control.addEventListener('input', commit);

        wrap.appendChild(control);
        return wrap;
    }

    function renderAnnotationLayerList() {
        const host = getEl('annotation-layer-list');
        if (!host) return;
        host.textContent = '';

        if (!annotationLayers.length) {
            const empty = document.createElement('p');
            empty.className = 'annotation-empty text-gray-500 dark:text-gray-400';
            empty.textContent = 'No layers yet. Add one, or just create an annotation and a default layer will be made for you.';
            host.appendChild(empty);
            return;
        }

        const ordered = annotationLayers.slice().sort((a, b) => (a.order || 0) - (b.order || 0));
        ordered.forEach((layer, index) => {
            const card = document.createElement('div');
            card.className = 'annotation-layer-card';

            const top = document.createElement('div');
            top.className = 'annotation-layer-head';

            const badge = document.createElement('span');
            badge.className = 'annotation-layer-order text-gray-500 dark:text-gray-400';
            badge.textContent = String(layer.order);
            badge.title = layer.order === 1 ? 'Innermost layer, closest to the tips' : 'Higher numbers sit further from the tree';
            top.appendChild(badge);

            if (annotationsEditable()) {
                const name = document.createElement('input');
                name.type = 'text';
                name.maxLength = 80;
                name.value = layer.name;
                name.className = 'annotation-layer-name rounded border border-gray-300 bg-white px-2 py-1 text-sm text-gray-900 dark:border-gray-700 dark:bg-journal-dark dark:text-gray-100';
                name.addEventListener('change', () => {
                    const next = name.value.trim();
                    if (!next) { name.value = layer.name; return; }
                    layer.name = next;
                    debouncedSaveAnnotations();
                });
                top.appendChild(name);
            } else {
                const name = document.createElement('span');
                name.className = 'annotation-layer-name annotation-row-title text-gray-900 dark:text-gray-100';
                name.textContent = layer.name;
                top.appendChild(name);
            }

            const visible = layer.visible !== false;

            if (annotationsEditable()) {
                // Simple up/down controls rather than drag-and-drop: the project has no
                // reusable sortable-list pattern, and order here is a small integer anyway.
                const up = annotationIconButton('fas fa-arrow-up', 'Move layer inward (closer to the tips)', async () => {
                    if (index === 0) return;
                    const previous = ordered[index - 1];
                    const swap = layer.order; layer.order = previous.order; previous.order = swap;
                    await saveAnnotationsNow();
                });
                up.disabled = index === 0;
                top.appendChild(up);

                const down = annotationIconButton('fas fa-arrow-down', 'Move layer outward (further from the tree)', async () => {
                    if (index === ordered.length - 1) return;
                    const next = ordered[index + 1];
                    const swap = layer.order; layer.order = next.order; next.order = swap;
                    await saveAnnotationsNow();
                });
                down.disabled = index === ordered.length - 1;
                top.appendChild(down);

                top.appendChild(annotationIconButton(visible ? 'fas fa-eye' : 'fas fa-eye-slash',
                    visible ? 'Hide this layer (its annotations are kept)' : 'Show this layer', async () => {
                        layer.visible = !visible;
                        await saveAnnotationsNow();
                    }));

                // Deleting a layer never silently discards the annotations inside it.
                top.appendChild(annotationIconButton('fas fa-trash', 'Delete layer', async () => {
                    const used = cladeAnnotations.filter(a => a.layer_id === layer.id).length;
                    if (used > 0) {
                        showStatus(
                            `"${layer.name}" still holds ${used} annotation${used === 1 ? '' : 's'}. Move them to another layer or delete them first.`,
                            'warning', 5000
                        );
                        return;
                    }
                    annotationLayers = annotationLayers.filter(l => l.id !== layer.id);
                    const saved = await saveAnnotationsNow();
                    if (saved) showStatus('Layer deleted.', 'success', 2000);
                }, 'text-red-600 dark:text-red-300'));
            } else if (!visible) {
                const hidden = document.createElement('span');
                hidden.className = 'text-xs text-gray-400 dark:text-gray-500';
                hidden.textContent = 'hidden';
                top.appendChild(hidden);
            }

            card.appendChild(top);

            const styles = document.createElement('div');
            styles.className = 'annotation-layer-styles';
            styles.appendChild(layerStyleControl(layer, 'font_family', 'Font'));
            styles.appendChild(layerStyleControl(layer, 'font_size', 'Size'));
            styles.appendChild(layerStyleControl(layer, 'font_style', 'Style'));
            styles.appendChild(layerStyleControl(layer, 'font_weight', 'Weight'));
            styles.appendChild(layerStyleControl(layer, 'text_color', 'Text'));
            styles.appendChild(layerStyleControl(layer, 'line_color', 'Line'));
            // Alan 8/17/26 - Let managers edit the default fill and opacity for bubble annotations.
            styles.appendChild(layerStyleControl(layer, 'fill_color', 'Fill'));
            styles.appendChild(layerStyleControl(layer, 'fill_opacity', 'Opacity'));
            // Alan 8/24/26 - Layer-level defaults for the clade-highlight band.
            styles.appendChild(layerHighlightColorControl(layer));
            styles.appendChild(layerStyleControl(layer, 'highlight_opacity', 'Highlight opacity'));
            card.appendChild(styles);

            host.appendChild(card);
        });
    }

    function renderAnnotationManager() {
        // Rebuilding the rows replaces the very control the user is holding, which would
        // close an open colour picker mid-drag. The viewer still repaints, so the tree is
        // always current; the list catches up on the next render.
        const active = document.activeElement;
        if (active && active.closest && active.closest('#modal-clade-annotations')
            && ['INPUT', 'SELECT'].includes(active.tagName)) {
            return;
        }
        renderAnnotationList();
        renderAnnotationLayerList();
        const status = getEl('annotation-manager-status');
        if (status) {
            status.textContent = annotationsEditable()
                ? `${cladeAnnotations.length} annotation${cladeAnnotations.length === 1 ? '' : 's'} across ${annotationLayers.length} layer${annotationLayers.length === 1 ? '' : 's'}.`
                : 'Read-only view. Make an editable copy of this tree to add annotations.';
        }
        // Authorization is enforced server-side by check_job_access(mode="edit"); hiding the
        // static add buttons here only avoids offering a read-only user an action that fails.
        if (!annotationsEditable()) {
            document.querySelectorAll('#modal-clade-annotations .annotation-edit-control')
                .forEach(el => { el.disabled = true; el.classList.add('hidden'); });
        }
    }

    // Alan 8/17/26 - Open Add, Edit, or the manager from one exact canonical membership set,
    // while allowing explicit Add entry points to create supported same-branch stacks.
    function openAnnotationForMembership(memberIds, options = {}) {
        const suppliedIds = Array.isArray(options.annotationIds)
            ? options.annotationIds.filter(Boolean) : null;
        const annotationIds = suppliedIds || (
            viewer?.getAnnotationsForMemberIds
                ? viewer.getAnnotationsForMemberIds(memberIds)
                    .map(annotation => annotation.id).filter(Boolean)
                : []
        );
        // Alan 8/17/26 - An explicit Add action must remain Add even when this branch already
        // has annotations; editing stays available from the branch shortcut and manager.
        if (options.forceAdd) {
            openAnnotationEditor('add', {
                memberIds,
                defaultType: options.defaultType || 'clade_line'
            });
            return;
        }
        if (annotationIds.length === 1) {
            openAnnotationEditor('edit', { annotationId: annotationIds[0] });
            return;
        }
        if (annotationIds.length > 1) {
            openAnnotationManager();
            setAnnotationManagerTab('annotations');
            showStatus(
                `This clade has ${annotationIds.length} annotations. Choose one from the annotation list to edit.`,
                'info', 5000
            );
            return;
        }
        openAnnotationEditor('add', {
            memberIds,
            defaultType: options.defaultType || 'clade_line'
        });
    }

    // Alan 8/15/26 - Secondary workflow: annotate whatever is selected, but only when the
    // selection is exactly one clade. Anything else would draw a bracket across taxa that
    // do not belong to it, so refuse with an explanation instead.
    function annotateCurrentSelection() {
        if (!annotationsEditable() || !viewer?.getSelectedCladeLeafIds) return;
        const memberIds = viewer.getSelectedCladeLeafIds();
        if (!memberIds) {
            showStatus(
                'The selected sequences do not form a single clade. Select a complete clade or right-click its branch.',
                'warning', 6000
            );
            return;
        }
        // Alan 8/17/26 - The button says Annotate, so keep it as an Add path for stacked labels.
        openAnnotationForMembership(memberIds, { defaultType: 'clade_line', forceAdd: true });
    }

    function saveDisplayPrefs() {
        if (JOB_ID === 'unknown') return;
        try {
            localStorage.setItem(`dikarya_tree_${JOB_ID}`, JSON.stringify({
                supportFont: Number(getEl('input-support-font')?.value) || 9,
                tipFont: Number(getEl('input-tip-font')?.value) || 12,
                spacingX: viewer?.spacingState?.x || 0,
                spacingY: viewer?.spacingState?.y || 0,
                tipLabelGap: viewer?.tipLabelGap ?? 2,
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
            viewer.setTipLabelGap(prefs.tipLabelGap ?? 2);
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
                const renameResult = await TreeEditActions.renameTip(JOB_ID, change.oldName, change.newName);
                // Alan 8/15/26 - The rename response is the saved tree state, so enable Edited
                // FASTA as soon as the first rename lands.
                updateEditedFastaAvailability(renameResult);
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
                // Alan 8/16/26 - Treat Alt/left-drag boxes as direct prune requests, matching Select + Prune.
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
            treeMethod: window.TREE_METHOD || '',
            // Alan 8/22/26 - IQ-TREE run with -alrt but no ultrafast bootstrap writes single
            // SH-aLRT percentages, not UFBoot ones.
            alrtOnly: !!(window.TREE_SUPPORT_CONTEXT && window.TREE_SUPPORT_CONTEXT.alrt_only)
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
                // Alan 8/15/26 - Reevaluate Edited FASTA from the authoritative persisted state
                // on initial load and after every backend action that reloads the tree.
                updateEditedFastaAvailability(loadedTreeState);
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
                // Alan 8/15/26 - Restore layered clade annotations. Old tree state has neither
                // key, which simply means "no layers, no annotations" and is left alone on disk.
                applyAnnotationState(
                    loadedTreeState.annotation_layers,
                    loadedTreeState.clade_annotations
                );
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
            window.reportClientError?.('tree_viewer.load_newick', err);
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
        setupLink('fasta-original', `/api/job/${JOB_ID}/download/fasta/original`);
    }

    // Alan 8/15/26 - Edited FASTA only exists once the tree state prunes or renames
    // something; rerooting, rotating and selecting leave the sequences untouched.
    function treeStateHasFastaEdits(state) {
        if (!state) return false;
        const pruned = Array.isArray(state.pruned_taxa)
            ? state.pruned_taxa.some(name => String(name || '').trim())
            : false;
        if (pruned) return true;
        const renames = state.renames || {};
        return Object.keys(renames).some(original => {
            const from = String(original || '').trim();
            const to = String(renames[original] || '').trim();
            return from && to && from !== to;
        });
    }

    // Alan 8/15/26 - Single place that enables/disables the Edited FASTA download, driven
    // by the persisted tree state so undone edits disable it again without a page reload.
    function updateEditedFastaAvailability(state) {
        const link = getEl('fasta-edited');
        if (!link) return;
        const available = JOB_ID !== "unknown" && treeStateHasFastaEdits(state);
        link.classList.toggle('is-available', available);
        link.classList.toggle('is-unavailable', !available);
        link.setAttribute('aria-disabled', available ? 'false' : 'true');
        if (available) {
            link.href = `/api/job/${JOB_ID}/download/fasta/edited`;
            link.title = "Download the current unaligned sequences with pruning and renaming applied.";
            link.removeAttribute('tabindex');
        } else {
            link.removeAttribute('href');
            link.title = "Available after pruning or renaming sequences.";
            link.setAttribute('tabindex', '-1');
        }
    }

    // Alan 8/15/26 - Keep the disabled state unclickable even where the cursor style is ignored.
    getEl('fasta-edited')?.addEventListener('click', (e) => {
        if (getEl('fasta-edited')?.getAttribute('aria-disabled') === 'true') {
            e.preventDefault();
            showStatus("Edited FASTA is available after you prune or rename sequences.", "info", 3000);
        }
    });

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
            let newickStr;
            try {
                newickStr = viewer.getNewickString();
            } catch (err) {
                showStatus(err?.message || "Tree export failed.", "warning", 3000);
                return;
            }
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
        // Alan 8/15/26 - Wire the clade-annotation manager and editor using the same modal
        // interaction pattern as Rename (button, backdrop, close, Esc handled globally).
        getEl('btn-annotations')?.addEventListener('click', openAnnotationManager);
        getEl('btn-clade-annotations-close')?.addEventListener('click', closeAnnotationManager);
        getEl('btn-clade-annotations-done')?.addEventListener('click', closeAnnotationManager);
        getEl('clade-annotations-backdrop')?.addEventListener('click', closeAnnotationManager);
        document.querySelectorAll('.annotation-tab').forEach(button => {
            button.addEventListener('click', () => setAnnotationManagerTab(button.getAttribute('data-annotation-tab')));
        });
        getEl('btn-annotation-add-selected')?.addEventListener('click', annotateCurrentSelection);
        getEl('btn-annotation-add-layer')?.addEventListener('click', async () => {
            if (!annotationsEditable()) return;
            createAnnotationLayer(`Layer ${annotationLayers.length + 1}`);
            await saveAnnotationsNow();
            setAnnotationManagerTab('layers');
        });
        getEl('btn-annotation-editor-close')?.addEventListener('click', closeAnnotationEditor);
        getEl('btn-annotation-cancel')?.addEventListener('click', closeAnnotationEditor);
        getEl('annotation-editor-backdrop')?.addEventListener('click', closeAnnotationEditor);
        getEl('btn-annotation-save')?.addEventListener('click', submitAnnotationEditor);
        getEl('btn-annotation-delete')?.addEventListener('click', deleteCurrentAnnotation);
        // Alan 8/15/26 - Switching layer changes what the override rows inherit, so re-render them.
        getEl('select-annotation-layer')?.addEventListener('change', renderAnnotationStyleOverrides);
        // Alan 8/17/26 - The live preview only refreshed when a style override changed, so
        // typing a label or switching Line/Bubble left it showing the previous annotation.
        // Wire the three controls that own the preview's content directly.
        getEl('input-annotation-label')?.addEventListener('input', renderAnnotationLivePreview);
        getEl('select-annotation-type')?.addEventListener('change', renderAnnotationLivePreview);
        getEl('select-annotation-layer')?.addEventListener('change', renderAnnotationLivePreview);
        getEl('btn-annotation-inline-new-layer')?.addEventListener('click', () => {
            const row = getEl('annotation-inline-layer-row');
            row?.classList.toggle('hidden');
            if (row && !row.classList.contains('hidden')) getEl('input-annotation-new-layer-name')?.focus();
        });
        // Alan 8/15/26 - Create a layer inline so the user never abandons a half-written annotation.
        getEl('btn-annotation-inline-layer-create')?.addEventListener('click', () => {
            const input = getEl('input-annotation-new-layer-name');
            const name = String(input?.value || '').trim();
            if (!name) { setAnnotationEditorError('Enter a name for the new layer.'); return; }
            // Alan 8/15/26 - Part of the editor transaction, not an independent change: it is
            // persisted with the annotation on save and rolled back if the editor is cancelled.
            const layer = createAnnotationLayer(name, true);
            if (input) input.value = '';
            getEl('annotation-inline-layer-row')?.classList.add('hidden');
            populateAnnotationLayerSelect(layer.id);
            renderAnnotationStyleOverrides();
            setAnnotationEditorError('');
        });

        // Alan 7/20/26 - Wire keyboard-help close controls using the same modal interaction pattern as Rename.
        btnShortcutHelpClose?.addEventListener('click', closeShortcutHelp);
        shortcutHelpBackdrop?.addEventListener('click', closeShortcutHelp);
        // Alan 7/21/26 - Open the same complete shortcut reference from the new toolbar button and question-mark hotkey.
        btnShortcutHelpOpen?.addEventListener('click', openShortcutHelp);
        // Alan 8/17/26 - Wire the new "Analyze with Claude" toolbar button and its modal controls.
        // Claude review: opening uses the cached review when the tree has not
        // changed, so only Re-run ever spends a fresh call.
        btnClaudeReview?.addEventListener('click', () => runClaudeReview());
        btnClaudeReviewRefresh?.addEventListener('click', () => runClaudeReview({ refresh: true }));
        btnClaudeReviewClose?.addEventListener('click', closeClaudeReview);
        btnClaudeReviewDone?.addEventListener('click', closeClaudeReview);
        claudeReviewBackdrop?.addEventListener('click', closeClaudeReview);
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

        // Alan 8/17/26 - Adjust and persist the independent tip-label gap from toolbar buttons.
        const changeTipLabelGap = (delta) => {
            if (!viewer?.setTipLabelGap) return;
            const next = viewer.setTipLabelGap((viewer.tipLabelGap ?? 2) + delta);
            showStatus(`Tip label gap: ${next} px`, 'info', 1200);
            saveDisplayPrefs();
        };
        getEl('btn-tip-label-gap-inc')?.addEventListener('click', () => changeTipLabelGap(2));
        getEl('btn-tip-label-gap-dec')?.addEventListener('click', () => changeTipLabelGap(-2));

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

        // Alan 8/15/26 - Primary annotation workflow: the viewer hands over the clicked clade's
        // canonical descendant leaf IDs, so the user never selects every tip by hand.
        if (viewer && typeof viewer.setAddCladeAnnotationHandler === 'function') {
            // Alan 8/17/26 - Accept exact-match metadata and explicit stacked-annotation requests.
            viewer.setAddCladeAnnotationHandler((node, memberIds = [], annotationOptions = {}) => {
                if (window.VIEW_ONLY || isProcessing) return;
                if (!Array.isArray(memberIds) || !memberIds.length) {
                    showStatus("Can't annotate: this branch has no tips.", "warning", 2500);
                    return;
                }
                // Alan 8/17/26 - Preserve whether the clicked menu action meant Edit or Add another.
                const annotationIds = Array.isArray(annotationOptions.annotationIds)
                    ? annotationOptions.annotationIds.filter(Boolean) : [];
                openAnnotationForMembership(memberIds, {
                    annotationIds,
                    defaultType: annotationOptions.defaultType || 'clade_line',
                    forceAdd: annotationOptions.forceAdd === true
                });
            });
        }

        // Alan 8/21/26 - Right-clicking a drawn annotation edits that exact annotation, which is
        // the only unambiguous path when several are stacked on one clade or branch.
        if (viewer && typeof viewer.setEditCladeAnnotationHandler === 'function') {
            viewer.setEditCladeAnnotationHandler((annotationId) => {
                if (window.VIEW_ONLY || isProcessing || !annotationId) return;
                openAnnotationEditor('edit', { annotationId });
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
                window.reportClientError?.('tree_viewer.export_svg', err);
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
                window.reportClientError?.('tree_viewer.export_jpg', err);
                const msg = err instanceof Error ? err.message : String(err);
                showStatus("JPG export failed: " + msg, "danger", 5000);
            }
        });

        // Tree Edit Actions (Backend)
        wireBackendActions();
    }

    // Alan 8/24/26 - A function declaration, not a const arrow, so it hoists: it is
    // wired to the zoom buttons ~800 lines above this point. The body uses no `this`,
    // so the arrow form bought nothing.
    function triggerZoom(delta) {
        const svg = container?.querySelector('svg');
        if (!svg) return;
        const rect = svg.getBoundingClientRect();
        const cx = rect.left + rect.width / 2;
        const cy = rect.top + rect.height / 2;
        svg.dispatchEvent(new WheelEvent('wheel', {
            clientX: cx, clientY: cy, deltaY: delta,
            bubbles: true, cancelable: true, view: window
        }));
    }

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
            // Alan 8/23/26 - Rooting is a persisted edit; view-only trees must not reach
            // it. The backend enforces this too (check_job_access mode="edit"), so this
            // only keeps the UI from offering an action that can only 403.
            if (window.VIEW_ONLY || isProcessing) return;
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

        if (btnRecompute) btnRecompute.addEventListener('click', async () => {
            if (!confirm("Recompute tree?")) return;
            btnRecompute.disabled = true;
            try {
                const result = await TreeEditActions.recomputeTree(JOB_ID);
                showStatus(result.message || "Tree recompute queued.", "success", 2500);
                setTimeout(() => {
                    window.location.href = result.redirect_url || `/job/${JOB_ID}`;
                }, 500);
            } catch (error) {
                showStatus(error.message || "Could not queue the recompute.", "danger", 5000);
                btnRecompute.disabled = false;
            }
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
            // Alan 8/17/26 - Escape now also closes the Claude review modal, matching Rename.
            // Escape dismisses Claude's review the same way it dismisses Rename.
            // The request keeps running; the result is cached either way.
            if (e.key === 'Escape' && claudeReviewModal && !claudeReviewModal.classList.contains('hidden')) {
                closeClaudeReview();
                return;
            }
            // Alan 7/20/26 - Let Escape dismiss the advanced selection menu without changing tree state.
            if (e.key === 'Escape' && btnSelectionMore?.getAttribute('aria-expanded') === 'true') {
                setSelectionMoreMenuOpen(false);
                btnSelectionMore.focus();
                return;
            }
            // Alan 8/15/26 - Escape closes the annotation editor first, then the manager behind it,
            // matching how the Rename modal already behaves.
            if (e.key === 'Escape') {
                const editorModal = getEl('modal-annotation-editor');
                if (editorModal && !editorModal.classList.contains('hidden')) {
                    closeAnnotationEditor();
                    return;
                }
                const managerModal = getEl('modal-clade-annotations');
                if (managerModal && !managerModal.classList.contains('hidden')) {
                    closeAnnotationManager();
                    return;
                }
            }
            // Alan 5/11/26 - Escape closes the rename modal before handling reroot cancellation.
            if (e.key === "Escape" && renameModal && !renameModal.classList.contains('hidden')) {
                closeRenameModal();
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
            // Alan 8/24/26 - A opens the Alignment Viewer; V stays as the original alias.
            } else if ((key === 'a' || key === 'v') && viewer && btnAlignmentViewer && !btnAlignmentViewer.disabled) {
                btnAlignmentViewer.click();
                handled = true;
            // Alan 7/20/26 - S cycles the existing original, ascending, and descending node sort modes.
            } else if (key === 's' && viewer) {
                getEl('btn-ladderize')?.click();
                handled = true;
            // Alan 8/16/26 - The B hotkey and its Box Select mode are gone; left-drag always draws the box.
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

                // Alan 8/14/26 - Resolve the clicked node to a stable ID for internal nodes
                // instead of its rendered label. The viewer parses raw Newick in the browser,
                // so an internal node's label is its support value ("1.000"); the backend
                // reads the same file with BioPython, which moves that label into .confidence,
                // so every internal-node reroot failed with "Root target not found: 1.000".
                // getRerootTargetForNode returns the tip name for leaves and the shared
                // stable ID (same scheme rotate/prune use) for internal nodes.
                let nodeName = null;
                const g = e.target.closest('g.node, g.internal-node');
                if (g && window.d3v7) {
                    try {
                        nodeName = getRerootTargetForNode(window.d3v7.select(g).datum());
                    } catch (err) { nodeName = null; }
                }
                if (!nodeName && typeof viewer.getNodeIdFromEvent === 'function') {
                    nodeName = viewer.getNodeIdFromEvent(e);
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
            // Alan 8/23/26 - Rooting mode and sequence-of-interest both persist edits, so
            // they belong with the disabled controls rather than beside them.
            if (rootingModeSelect) {
                rootingModeSelect.disabled = true;
                rootingModeSelect.title = "View Only - Make an editable copy to use this feature";
            }
            disableBtn(btnSetSoi);
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
            ? `Open Alignment Viewer for ${selected.length} selected sequence${selected.length === 1 ? '' : 's'} (A)`
            : `Open Alignment Viewer for ${visible.length} visible sequence${visible.length === 1 ? '' : 's'} (A)`;
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
            // Alan 8/22/26 - IQ-TREE's own scales get their own colours so they are not read
            // as the classical bootstrap sitting next to them in blue.
            else if (stats.supportType === 'UFBOOT') {
                badge.classList.add('text-indigo-800', 'bg-indigo-100', 'ring-indigo-400/60', 'dark:text-indigo-200', 'dark:bg-indigo-900/40');
            }
            else if (stats.supportType === 'ALRT') {
                badge.classList.add('text-cyan-800', 'bg-cyan-100', 'ring-cyan-400/60', 'dark:text-cyan-200', 'dark:bg-cyan-900/40');
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
            if (bsLabel) {
                if (s === 'ALRT_UFBOOT' || s === 'UFBOOT') bsLabel.textContent = "UFBoot >";
                else if (s === 'ALRT') bsLabel.textContent = "SH-aLRT >";
                else bsLabel.textContent = "BS >";
            }
            setInput(bsInput, globalEnable && (
                s === 'BS' || s === 'mixed' || s === 'ALRT_UFBOOT' || s === 'UFBOOT' || s === 'ALRT'
            ));
        }
    }

    // START
    loadTree();
});
