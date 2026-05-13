/**
 * Dikarya Alignment Viewer
 *
 * Loads aligned FASTA from the backend and renders an interactive, polished
 * alignment grid in a full-screen modal, driven by the current tree viewer
 * selection / visible tip order.
 */
(function () {
    'use strict';

    // Alan 5/12/26 - Bail if the modal partial is not present (e.g., non-viewer page).
    const modal = document.getElementById('modal-alignment-viewer');
    if (!modal) return;

    // Alan 5/12/26 - Optional perf instrumentation for Highlight Differences toggling.
    const DEBUG_ALIGNMENT_PERF = false;
    // Alan 5/12/26 - Threshold above which we switch to the cheapest .is-diff styling.
    const LARGE_GRID_CELLS = 50000;

    // Alan 5/12/26 - Cache controls so callbacks don't repeatedly query the DOM.
    const $ = (id) => document.getElementById(id);
    const backdrop = $('alignment-viewer-backdrop');
    const closeBtn = $('alignment-viewer-close');
    const statsEl = $('alignment-viewer-stats');
    const warningsEl = $('alignment-warnings');
    const bodyEl = $('alignment-viewer-body');
    const emptyEl = bodyEl.querySelector('.alignment-viewer-empty');
    const gridEl = bodyEl.querySelector('.alignment-viewer-grid');
    const includeWrap = $('alignment-include-pruned-wrap');
    const includeChk = $('alignment-include-pruned');
    const includeLabel = $('alignment-include-pruned-label');
    const sortSel = $('alignment-sort-mode');
    const refSel = $('alignment-reference');
    const filterInp = $('alignment-filter');
    const diffsChk = $('alignment-highlight-diffs');
    const variableChk = $('alignment-variable-only');
    // Alan 5/12/26 - Cache the new gap-compaction control (default ON).
    const compactChk = $('alignment-compact-gaps');
    const copyBtn = $('alignment-copy-fasta');
    const downloadBtn = $('alignment-download-fasta');

    const NUC_CLASS = {
        A: 'av-a', C: 'av-c', G: 'av-g', T: 'av-t', U: 'av-u',
        '-': 'av-gap', '.': 'av-gap'
    };
    const AMBIG = new Set(['N', 'R', 'Y', 'S', 'W', 'K', 'M', 'B', 'D', 'H', 'V']);

    // Alan 5/12/26 - Persistent state for the currently open viewer session.
    const state = {
        jobId: null,
        sequences: [],          // [{name, sequence}] (server order = tree order)
        allCount: 0,
        availablePruned: 0,
        includedPruned: 0,
        alignmentLength: 0,
        warnings: [],
        treeOrder: [],
        selectedNames: [],
        includePruned: false,
        sortMode: 'tree',
        referenceName: null,
        filterText: '',
        highlightDiffs: false,
        variableOnly: false,
        compactGaps: true,      // Alan 5/12/26 - Default-on gap compaction.
        displayedRows: [],      // sorted/filtered rows actually rendered
        visibleColumnIndexes: null, // null = all original columns
        gapOnlyHidden: 0,       // count of gap-only columns hidden
        consensus: '',          // consensus over displayedRows (full length)
        // Alan 5/12/26 - Cache the row set fingerprint so we know when to recompute consensus/diffs.
        _rowFingerprint: '',
        _diffComputedFor: '',   // fingerprint last used for consensus/diff masks
    };

    function showStatusMsg(msg, type, timeout = 0) {
        if (typeof window.showStatus === 'function') {
            window.showStatus(msg, type, timeout);
        } else {
            console.log(`[alignment-viewer] ${type}: ${msg}`);
        }
    }

    function openModal() {
        modal.classList.remove('hidden');
        document.body.classList.add('overflow-hidden');
    }
    function closeModal() {
        modal.classList.add('hidden');
        document.body.classList.remove('overflow-hidden');
    }

    function renderWarnings() {
        if (!warningsEl) return;
        if (!state.warnings || state.warnings.length === 0) {
            warningsEl.classList.add('hidden');
            warningsEl.textContent = '';
            return;
        }
        warningsEl.classList.remove('hidden');
        warningsEl.textContent = state.warnings.join(' • ');
    }

    // Alan 5/12/26 - Treat both '-' and '.' as gap so dot-gap alignments compact too.
    function isGap(ch) { return ch === '-' || ch === '.'; }

    // Alan 5/12/26 - Build the list of original alignment indexes to render, applying compaction then variable-only.
    function buildVisibleColumnIndexes(rows) {
        const len = state.alignmentLength;
        const indexes = [];
        let gapOnly = 0;
        for (let i = 0; i < len; i++) {
            let hasNonGap = false;
            let firstChar = null;
            let varied = false;
            for (let r = 0; r < rows.length; r++) {
                const ch = rows[r].sequence[i] || '-';
                if (!hasNonGap && !isGap(ch)) hasNonGap = true;
                if (state.variableOnly) {
                    const up = ch.toUpperCase();
                    if (firstChar === null) firstChar = up;
                    else if (up !== firstChar) varied = true;
                }
            }
            if (state.compactGaps && !hasNonGap) { gapOnly++; continue; }
            if (state.variableOnly && !varied) continue;
            indexes.push(i);
        }
        state.gapOnlyHidden = gapOnly;
        return indexes;
    }

    // Alan 5/12/26 - Compute consensus across displayed rows for the visible columns only.
    function computeConsensus(rows, visibleIdx) {
        if (!rows.length) return '';
        const out = new Array(state.alignmentLength).fill('-');
        for (let k = 0; k < visibleIdx.length; k++) {
            const i = visibleIdx[k];
            const counts = {};
            for (let r = 0; r < rows.length; r++) {
                const c = (rows[r].sequence[i] || '-').toUpperCase();
                if (c === '-' || c === '.' || c === 'N') continue;
                counts[c] = (counts[c] || 0) + 1;
            }
            let best = '-';
            let bestN = 0;
            for (const k2 in counts) {
                if (counts[k2] > bestN) { best = k2; bestN = counts[k2]; }
            }
            out[i] = best;
        }
        return out.join('');
    }

    function percentIdentity(seqA, seqB) {
        const len = Math.min(seqA.length, seqB.length);
        let comparable = 0;
        let matches = 0;
        for (let i = 0; i < len; i++) {
            const a = seqA.charCodeAt(i);
            const b = seqB.charCodeAt(i);
            if (a === 45 || a === 46 || b === 45 || b === 46) continue;
            const ac = String.fromCharCode(a).toUpperCase();
            const bc = String.fromCharCode(b).toUpperCase();
            if (ac === 'N' || bc === 'N') continue;
            comparable++;
            if (ac === bc) matches++;
        }
        if (!comparable) return 0;
        return matches / comparable;
    }

    function leadingGapCount(seq) {
        let i = 0;
        while (i < seq.length && isGap(seq[i])) i++;
        return i;
    }

    function sortRows(rows) {
        const mode = state.sortMode;
        if (mode === 'tree') {
            const indexMap = new Map(state.sequences.map((r, i) => [r, i]));
            return rows.slice().sort((a, b) => (indexMap.get(a) ?? 0) - (indexMap.get(b) ?? 0));
        }
        if (mode === 'name_asc') return rows.slice().sort((a, b) => a.name.localeCompare(b.name));
        if (mode === 'name_desc') return rows.slice().sort((a, b) => b.name.localeCompare(a.name));
        if (mode === 'length') {
            return rows.slice().sort((a, b) => {
                const la = a.sequence.replace(/[-.]/g, '').length;
                const lb = b.sequence.replace(/[-.]/g, '').length;
                return lb - la;
            });
        }
        if (mode === 'leading_gaps') {
            return rows.slice().sort((a, b) => leadingGapCount(a.sequence) - leadingGapCount(b.sequence));
        }
        if (mode === 'similar' || mode === 'different') {
            const ref = rows.find(r => r.name === state.referenceName) || rows[0];
            if (!ref) return rows.slice();
            const scored = rows.map(r => ({ row: r, score: r === ref ? 1 : percentIdentity(ref.sequence, r.sequence) }));
            scored.sort((a, b) => (mode === 'similar' ? b.score - a.score : a.score - b.score));
            for (const s of scored) s.row.__score = s.score;
            return scored.map(s => s.row);
        }
        return rows.slice();
    }

    function populateReferenceSelector() {
        if (!refSel) return;
        const showRef = state.sortMode === 'similar' || state.sortMode === 'different';
        refSel.classList.toggle('hidden', !showRef);
        if (!showRef) return;
        refSel.innerHTML = '';
        for (const row of state.sequences) {
            const opt = document.createElement('option');
            opt.value = row.name;
            opt.textContent = row.name.length > 50 ? row.name.slice(0, 47) + '…' : row.name;
            refSel.appendChild(opt);
        }
        if (state.referenceName && state.sequences.some(r => r.name === state.referenceName)) {
            refSel.value = state.referenceName;
        } else if (state.sequences.length) {
            refSel.value = state.sequences[0].name;
            state.referenceName = refSel.value;
        }
    }

    function pickInitialReference() {
        if (state.selectedNames.length === 1) {
            state.referenceName = state.selectedNames[0];
        } else if (state.sequences.length) {
            state.referenceName = state.sequences[0].name;
        } else {
            state.referenceName = null;
        }
    }

    function cellClass(ch, consensusCh) {
        const up = ch.toUpperCase();
        let cls = 'av-cell ';
        cls += NUC_CLASS[up] || (AMBIG.has(up) ? 'av-amb' : 'av-amb');
        if (consensusCh) {
            const consUp = consensusCh.toUpperCase();
            // Alan 5/12/26 - Distinguish "matches consensus" from "differs from consensus" so the root class can toggle styles cheaply.
            if (up === consUp && up !== '-') cls += ' av-match';
            else if (up !== consUp && !isGap(ch)) cls += ' is-diff';
        }
        return cls;
    }

    // Alan 5/12/26 - Build only the visible columns (compacted), letting toggles render orders of magnitude fewer cells.
    function buildCellRow(seq, consensus, visibleIdx) {
        const frag = document.createDocumentFragment();
        for (let k = 0; k < visibleIdx.length; k++) {
            const i = visibleIdx[k];
            const ch = seq[i] || '-';
            const span = document.createElement('span');
            span.className = cellClass(ch, consensus ? consensus[i] : null);
            span.textContent = ch;
            frag.appendChild(span);
        }
        return frag;
    }

    // Alan 5/12/26 - Build ruler ticks only for visible columns; labels still show original alignment coordinates.
    function buildRuler(visibleIdx) {
        const frag = document.createDocumentFragment();
        for (let k = 0; k < visibleIdx.length; k++) {
            const i = visibleIdx[k];
            const pos = i + 1;
            const span = document.createElement('span');
            span.className = 'av-ruler-tick';
            if (pos === 1 || pos % 10 === 0) {
                span.classList.add('av-major');
                span.textContent = String(pos);
            } else {
                span.textContent = '';
            }
            span.title = `Original alignment position ${pos}`;
            frag.appendChild(span);
        }
        return frag;
    }

    // Alan 5/12/26 - Stable fingerprint identifying the displayed row set plus column-affecting toggles.
    function rowSetFingerprint(rows) {
        return rows.map(r => r.name).join('')
            + '|cg=' + (state.compactGaps ? 1 : 0)
            + '|vo=' + (state.variableOnly ? 1 : 0)
            + '|ip=' + (state.includePruned ? 1 : 0);
    }

    function renderAlignmentGrid() {
        gridEl.innerHTML = '';
        emptyEl.classList.add('hidden');

        let rows = state.sequences.slice();
        const filter = (state.filterText || '').trim().toLowerCase();
        if (filter) rows = rows.filter(r => r.name.toLowerCase().includes(filter));

        for (const r of rows) r.__score = undefined;
        rows = sortRows(rows);
        state.displayedRows = rows;

        if (rows.length === 0) {
            state.visibleColumnIndexes = [];
            state.gapOnlyHidden = 0;
            state.consensus = '';
            emptyEl.textContent = 'No sequences match the current filter.';
            emptyEl.classList.remove('hidden');
            updateStats();
            return;
        }

        // Alan 5/12/26 - Recompute visible columns + consensus only when the row set or column toggles change.
        const fp = rowSetFingerprint(rows);
        if (fp !== state._diffComputedFor) {
            state.visibleColumnIndexes = buildVisibleColumnIndexes(rows);
            state.consensus = computeConsensus(rows, state.visibleColumnIndexes);
            state._diffComputedFor = fp;
        }
        const visibleIdx = state.visibleColumnIndexes;
        const consensus = state.consensus;

        // Header corner + ruler
        const corner = document.createElement('div');
        corner.className = 'av-names-header';
        corner.textContent = `${rows.length} sequences · ${visibleIdx.length}/${state.alignmentLength} cols`;
        gridEl.appendChild(corner);

        const ruler = document.createElement('div');
        ruler.className = 'av-ruler';
        const rulerInner = document.createElement('div');
        rulerInner.style.whiteSpace = 'nowrap';
        rulerInner.appendChild(buildRuler(visibleIdx));
        ruler.appendChild(rulerInner);
        gridEl.appendChild(ruler);

        const namesCol = document.createElement('div');
        namesCol.className = 'av-names';
        const cellsCol = document.createElement('div');
        cellsCol.className = 'av-cells';

        // Consensus row
        const consNameRow = document.createElement('div');
        consNameRow.className = 'av-name-row av-consensus-row';
        const consName = document.createElement('span');
        consName.className = 'av-name-text';
        consName.textContent = 'Consensus';
        consNameRow.appendChild(consName);
        namesCol.appendChild(consNameRow);

        const consCellRow = document.createElement('div');
        consCellRow.className = 'av-cell-row av-consensus-row';
        consCellRow.appendChild(buildCellRow(consensus, null, visibleIdx));
        cellsCol.appendChild(consCellRow);

        cellsCol.addEventListener('scroll', () => {
            namesCol.scrollTop = cellsCol.scrollTop;
            ruler.scrollLeft = cellsCol.scrollLeft;
        });

        const showScores = state.sortMode === 'similar' || state.sortMode === 'different';

        // Alan 5/12/26 - Batch all rows into one fragment so the browser does a single layout pass.
        const namesFrag = document.createDocumentFragment();
        const cellsFrag = document.createDocumentFragment();
        for (const row of rows) {
            const nameRow = document.createElement('div');
            nameRow.className = 'av-name-row';
            const nameText = document.createElement('span');
            nameText.className = 'av-name-text';
            nameText.textContent = row.name;
            nameText.title = row.name;
            nameRow.appendChild(nameText);
            if (showScores && typeof row.__score === 'number') {
                const score = document.createElement('span');
                score.className = 'av-name-score';
                score.textContent = (row.__score * 100).toFixed(1) + '%';
                nameRow.appendChild(score);
            }
            namesFrag.appendChild(nameRow);

            const cellRow = document.createElement('div');
            cellRow.className = 'av-cell-row';
            cellRow.appendChild(buildCellRow(row.sequence, consensus, visibleIdx));
            cellsFrag.appendChild(cellRow);
        }
        namesCol.appendChild(namesFrag);
        cellsCol.appendChild(cellsFrag);

        gridEl.appendChild(namesCol);
        gridEl.appendChild(cellsCol);

        // Alan 5/12/26 - Apply the highlight state via the root class only — never per-cell on toggle.
        bodyEl.classList.toggle('alignment-highlight-differences', state.highlightDiffs);
        // Alan 5/12/26 - Mark big grids so CSS can drop to the cheapest .is-diff styling.
        const cellCount = rows.length * visibleIdx.length;
        bodyEl.classList.toggle('alignment-large-grid', cellCount > LARGE_GRID_CELLS);
        if (DEBUG_ALIGNMENT_PERF) {
            console.log(`[alignment-viewer] rendered cells=${cellCount} rows=${rows.length} cols=${visibleIdx.length}`);
        }

        updateStats();
    }

    // Alan 5/12/26 - Toggle differences purely via a root class. No DOM rebuild, no per-cell touches.
    function setHighlightDifferences(enabled) {
        // Alan 5/12/26 - Guarded timing so we can confirm the toggle itself is cheap.
        const t0 = DEBUG_ALIGNMENT_PERF ? performance.now() : 0;
        state.highlightDiffs = !!enabled;
        bodyEl.classList.toggle('alignment-highlight-differences', state.highlightDiffs);
        if (DEBUG_ALIGNMENT_PERF) {
            console.log(`[alignment-viewer] setHighlightDifferences(${enabled}) took ${(performance.now() - t0).toFixed(2)}ms`);
        }
    }

    function updateStats() {
        if (!statsEl) return;
        const shown = state.displayedRows.length;
        const total = state.sequences.length;
        const visibleCols = state.visibleColumnIndexes ? state.visibleColumnIndexes.length : state.alignmentLength;
        let txt = `${shown}/${total} sequences · ${visibleCols}/${state.alignmentLength} columns`;
        if (state.compactGaps && state.gapOnlyHidden > 0) {
            txt += ` · ${state.gapOnlyHidden} gap-only columns hidden`;
        }
        if (state.includedPruned > 0) {
            txt += ` · ${state.includedPruned} pruned included`;
        }
        statsEl.textContent = txt;

        if (state.availablePruned > 0) {
            includeWrap.classList.remove('hidden');
            includeWrap.classList.add('inline-flex');
            includeLabel.textContent = state.includePruned
                ? `Including ${state.availablePruned} Pruned Sequences (from alignment file)`
                : `Include Pruned Sequences (${state.availablePruned})`;
        } else {
            includeWrap.classList.add('hidden');
            includeWrap.classList.remove('inline-flex');
        }
    }

    async function fetchAlignment() {
        const treeOrder = state.treeOrder;
        const tipNames = state.selectedNames.length ? state.selectedNames : [];

        const payload = {
            tip_names: tipNames,
            tree_order: treeOrder,
            include_pruned: state.includePruned,
            pruned_tip_names: [],
        };

        const headers = { 'Content-Type': 'application/json' };
        const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
        if (csrfToken) headers['X-CSRFToken'] = csrfToken;

        const resp = await fetch(`/api/job/${state.jobId}/alignment/view`, {
            method: 'POST',
            headers,
            body: JSON.stringify(payload),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || data.status !== 'success') {
            const err = data && data.error ? data.error : `HTTP ${resp.status}`;
            throw new Error(err);
        }
        return data;
    }

    async function refresh() {
        try {
            const data = await fetchAlignment();
            state.sequences = Array.isArray(data.sequences) ? data.sequences : [];
            state.allCount = data.total_alignment_count || state.sequences.length;
            state.availablePruned = data.available_pruned_count || 0;
            state.includedPruned = data.included_pruned_count || 0;
            state.alignmentLength = data.alignment_length || (state.sequences[0]?.sequence.length || 0);
            state.warnings = data.warnings || [];
            // Alan 5/12/26 - Force recompute of consensus/diff masks when row set or include-pruned changes.
            state._diffComputedFor = '';
            renderWarnings();
            pickInitialReference();
            populateReferenceSelector();
            renderAlignmentGrid();
        } catch (e) {
            console.error('Alignment Viewer failed:', e);
            showStatusMsg(`Alignment Viewer failed: ${e.message}`, 'danger', 4000);
            emptyEl.textContent = `Could not load alignment: ${e.message}`;
            emptyEl.classList.remove('hidden');
            gridEl.innerHTML = '';
        }
    }

    async function open(opts) {
        opts = opts || {};
        state.jobId = opts.jobId || window.JOB_ID;
        state.treeOrder = opts.treeOrder || [];
        state.selectedNames = opts.selectedNames || [];
        state.includePruned = false;
        state.sortMode = 'tree';
        state.filterText = '';
        state.highlightDiffs = false;
        state.variableOnly = false;
        // Alan 5/12/26 - Reset gap compaction to default-on on every open.
        state.compactGaps = true;
        state.referenceName = null;
        state._diffComputedFor = '';

        if (sortSel) sortSel.value = 'tree';
        if (filterInp) filterInp.value = '';
        if (diffsChk) diffsChk.checked = false;
        if (variableChk) variableChk.checked = false;
        if (compactChk) compactChk.checked = true;
        if (includeChk) includeChk.checked = false;
        // Alan 5/12/26 - Clear the highlight class so reopening doesn't carry over the previous state.
        bodyEl.classList.remove('alignment-highlight-differences');

        statsEl.textContent = 'Loading alignment…';
        warningsEl.classList.add('hidden');
        gridEl.innerHTML = '';
        emptyEl.classList.add('hidden');
        openModal();
        await refresh();
    }

    // Alan 5/12/26 - FASTA export uses the currently displayed rows; if compacted, uses compacted columns.
    function exportFastaText() {
        const rows = state.displayedRows;
        const idx = state.visibleColumnIndexes;
        const useCompact = state.compactGaps || state.variableOnly;
        const lines = [];
        for (const r of rows) {
            const safeHeader = (r.name || 'sequence').replace(/[\r\n]/g, ' ');
            lines.push(`>${safeHeader}`);
            if (useCompact && idx && idx.length !== state.alignmentLength) {
                let out = '';
                for (let k = 0; k < idx.length; k++) out += (r.sequence[idx[k]] || '-');
                lines.push(out);
            } else {
                lines.push(r.sequence);
            }
        }
        return { text: lines.join('\n') + '\n', compact: useCompact && idx && idx.length !== state.alignmentLength };
    }

    function downloadFasta() {
        const rows = state.displayedRows;
        if (!rows || rows.length === 0) {
            showStatusMsg('No sequences to download.', 'warning', 2500);
            return;
        }
        const { text, compact } = exportFastaText();
        const blob = new Blob([text], { type: 'text/plain' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        // Alan 5/12/26 - Suffix the filename so users can tell compacted exports apart from full ones.
        const suffix = compact ? '_compact' : '';
        a.download = `alignment_view_${state.jobId || 'job'}${suffix}.fasta`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    }

    async function copyFasta() {
        const rows = state.displayedRows;
        if (!rows || rows.length === 0) {
            showStatusMsg('No sequences to copy.', 'warning', 2500);
            return;
        }
        const { text, compact } = exportFastaText();
        try {
            await navigator.clipboard.writeText(text);
            const note = compact ? ' (compacted columns)' : '';
            showStatusMsg(`Copied ${rows.length} sequence${rows.length === 1 ? '' : 's'} as FASTA${note}.`, 'success', 2000);
        } catch (e) {
            showStatusMsg('Copy failed: ' + e.message, 'danger', 3000);
        }
    }

    // --- Event wiring ---
    closeBtn.addEventListener('click', closeModal);
    backdrop.addEventListener('click', closeModal);
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && !modal.classList.contains('hidden')) closeModal();
    });

    includeChk.addEventListener('change', () => {
        state.includePruned = includeChk.checked;
        refresh();
    });
    sortSel.addEventListener('change', () => {
        // Alan 5/12/26 - Sorting only re-orders the same row set, so we can skip consensus/diff recompute.
        state.sortMode = sortSel.value;
        populateReferenceSelector();
        renderAlignmentGrid();
    });
    refSel.addEventListener('change', () => {
        state.referenceName = refSel.value;
        renderAlignmentGrid();
    });
    let filterDeb = null;
    filterInp.addEventListener('input', () => {
        state.filterText = filterInp.value;
        clearTimeout(filterDeb);
        // Alan 5/12/26 - Filter changes the displayed row set, so let the fingerprint trigger recompute.
        filterDeb = setTimeout(renderAlignmentGrid, 80);
    });
    diffsChk.addEventListener('change', () => {
        // Alan 5/12/26 - Highlight Differences toggle now only flips a root class — no rebuild, no recompute.
        setHighlightDifferences(diffsChk.checked);
    });
    variableChk.addEventListener('change', () => {
        state.variableOnly = variableChk.checked;
        renderAlignmentGrid();
    });
    if (compactChk) compactChk.addEventListener('change', () => {
        // Alan 5/12/26 - Toggle gap-only compaction and rebuild the visible columns.
        state.compactGaps = compactChk.checked;
        renderAlignmentGrid();
    });
    copyBtn.addEventListener('click', copyFasta);
    downloadBtn.addEventListener('click', downloadFasta);

    // Expose a minimal public API for the controller to drive.
    window.DikaryaAlignmentViewer = {
        open,
        close: closeModal,
        setHighlightDifferences,
    };
})();
