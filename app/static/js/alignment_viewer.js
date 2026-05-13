/**
 * Dikarya Alignment Viewer
 *
 * Loads aligned FASTA from the backend and renders an interactive, polished
 * alignment grid in a full-screen modal, driven by the current tree viewer
 * selection / visible tip order. Phase 1 implementation.
 */
(function () {
    'use strict';

    // Alan 5/13/26 - Bail if the modal partial is not present (e.g., non-viewer page).
    const modal = document.getElementById('modal-alignment-viewer');
    if (!modal) return;

    // Alan 5/13/26 - Cache controls so callbacks don't repeatedly query the DOM.
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
    const copyBtn = $('alignment-copy-fasta');
    const downloadBtn = $('alignment-download-fasta');

    const NUC_CLASS = {
        A: 'av-a', C: 'av-c', G: 'av-g', T: 'av-t', U: 'av-u',
        '-': 'av-gap', '.': 'av-gap'
    };
    const AMBIG = new Set(['N', 'R', 'Y', 'S', 'W', 'K', 'M', 'B', 'D', 'H', 'V']);

    // Alan 5/13/26 - Persistent state for the currently open viewer session.
    const state = {
        jobId: null,
        sequences: [],          // [{name, sequence}]
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
        consensus: '',
        displayedRows: [],      // sorted/filtered rows actually rendered
        visibleColumns: null,   // null = all
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

    function computeConsensus(rows) {
        if (!rows.length) return '';
        const len = state.alignmentLength;
        const out = new Array(len);
        for (let i = 0; i < len; i++) {
            const counts = {};
            for (const r of rows) {
                const c = (r.sequence[i] || '-').toUpperCase();
                if (c === '-' || c === '.' || c === 'N') continue;
                counts[c] = (counts[c] || 0) + 1;
            }
            let best = '-';
            let bestN = 0;
            for (const k in counts) {
                if (counts[k] > bestN) { best = k; bestN = counts[k]; }
            }
            out[i] = best;
        }
        return out.join('');
    }

    function computeVariableColumns(rows) {
        const len = state.alignmentLength;
        const visible = new Uint8Array(len);
        for (let i = 0; i < len; i++) {
            let firstChar = null;
            let varied = false;
            for (const r of rows) {
                const c = (r.sequence[i] || '-').toUpperCase();
                if (firstChar === null) firstChar = c;
                else if (c !== firstChar) { varied = true; break; }
            }
            visible[i] = varied ? 1 : 0;
        }
        return visible;
    }

    function percentIdentity(seqA, seqB) {
        const len = Math.min(seqA.length, seqB.length);
        let comparable = 0;
        let matches = 0;
        for (let i = 0; i < len; i++) {
            const a = seqA.charCodeAt(i);
            const b = seqB.charCodeAt(i);
            // Skip gaps in either sequence (45 = '-', 46 = '.')
            if (a === 45 || a === 46 || b === 45 || b === 46) continue;
            // Skip N/ambiguous on either side when convenient (78 = 'N')
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
        while (i < seq.length && (seq[i] === '-' || seq[i] === '.')) i++;
        return i;
    }

    function sortRows(rows) {
        const mode = state.sortMode;
        if (mode === 'tree') {
            // Preserve the order provided by the server (which matches tree order).
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
            // Attach computed score for display
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
        // Default: exactly one selected sequence → use it; otherwise first displayed.
        if (state.selectedNames.length === 1) {
            state.referenceName = state.selectedNames[0];
        } else if (state.sequences.length) {
            state.referenceName = state.sequences[0].name;
        } else {
            state.referenceName = null;
        }
    }

    function buildRuler(length, visibleMask) {
        const frag = document.createDocumentFragment();
        for (let i = 0; i < length; i++) {
            const span = document.createElement('span');
            span.className = 'av-ruler-tick';
            const pos = i + 1;
            if (pos === 1 || pos % 10 === 0) {
                span.classList.add('av-major');
                span.textContent = String(pos);
            } else {
                span.textContent = '';
            }
            if (visibleMask && !visibleMask[i]) span.classList.add('av-hidden-col');
            frag.appendChild(span);
        }
        return frag;
    }

    function cellClass(ch, consensusCh) {
        const up = ch.toUpperCase();
        let cls = 'av-cell ';
        cls += NUC_CLASS[up] || (AMBIG.has(up) ? 'av-amb' : 'av-amb');
        if (consensusCh && up === consensusCh.toUpperCase() && up !== '-') cls += ' av-match';
        return cls;
    }

    function buildCellRow(seq, consensus, visibleMask) {
        const frag = document.createDocumentFragment();
        for (let i = 0; i < seq.length; i++) {
            const ch = seq[i] || '-';
            const span = document.createElement('span');
            span.className = cellClass(ch, consensus ? consensus[i] : null);
            span.textContent = ch;
            if (visibleMask && !visibleMask[i]) span.classList.add('av-hidden-col');
            frag.appendChild(span);
        }
        return frag;
    }

    function renderGrid() {
        gridEl.innerHTML = '';
        bodyEl.classList.toggle('av-highlight-diffs', state.highlightDiffs);
        emptyEl.classList.add('hidden');

        let rows = state.sequences.slice();
        const filter = (state.filterText || '').trim().toLowerCase();
        if (filter) rows = rows.filter(r => r.name.toLowerCase().includes(filter));

        // Score handling: clear stale scores before sort
        for (const r of rows) r.__score = undefined;
        rows = sortRows(rows);
        state.displayedRows = rows;

        if (rows.length === 0) {
            emptyEl.textContent = 'No sequences match the current filter.';
            emptyEl.classList.remove('hidden');
            updateStats();
            return;
        }

        const consensus = computeConsensus(rows);
        state.consensus = consensus;

        let visibleMask = null;
        if (state.variableOnly) {
            visibleMask = computeVariableColumns(rows);
        }
        state.visibleColumns = visibleMask;

        // Build DOM: 2 columns (names | cells) × 2 rows (ruler/header | content)
        // Header corner + ruler
        const corner = document.createElement('div');
        corner.className = 'av-names-header';
        corner.textContent = `${rows.length} sequences × ${state.alignmentLength} bp`;
        gridEl.appendChild(corner);

        const ruler = document.createElement('div');
        ruler.className = 'av-ruler';
        const rulerInner = document.createElement('div');
        rulerInner.style.whiteSpace = 'nowrap';
        rulerInner.appendChild(buildRuler(state.alignmentLength, visibleMask));
        ruler.appendChild(rulerInner);
        gridEl.appendChild(ruler);

        // Names column (with consensus row pinned at top)
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
        consCellRow.appendChild(buildCellRow(consensus, null, visibleMask));
        cellsCol.appendChild(consCellRow);

        // Sync vertical scroll between names and cells.
        cellsCol.addEventListener('scroll', () => {
            namesCol.scrollTop = cellsCol.scrollTop;
            ruler.scrollLeft = cellsCol.scrollLeft;
        });

        const showScores = state.sortMode === 'similar' || state.sortMode === 'different';

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
            namesCol.appendChild(nameRow);

            const cellRow = document.createElement('div');
            cellRow.className = 'av-cell-row';
            cellRow.appendChild(buildCellRow(row.sequence, consensus, visibleMask));
            cellsCol.appendChild(cellRow);
        }

        gridEl.appendChild(namesCol);
        gridEl.appendChild(cellsCol);

        updateStats();
    }

    function updateStats() {
        if (!statsEl) return;
        const shown = state.displayedRows.length;
        const total = state.sequences.length;
        let txt = `${shown}/${total} sequences · ${state.alignmentLength} bp`;
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
        const viewer = window.dikaryaViewer;
        const treeOrder = state.treeOrder;
        const tipNames = state.selectedNames.length ? state.selectedNames : [];

        const payload = {
            tip_names: tipNames,
            tree_order: treeOrder,
            include_pruned: state.includePruned,
            pruned_tip_names: [],
        };

        const headers = { 'Content-Type': 'application/json' };
        // Alan 5/13/26 - Send the CSRF token directly so the POST isn't rejected by Flask-WTF.
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
            renderWarnings();
            pickInitialReference();
            populateReferenceSelector();
            renderGrid();
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
        state.referenceName = null;

        // Reset controls
        if (sortSel) sortSel.value = 'tree';
        if (filterInp) filterInp.value = '';
        if (diffsChk) diffsChk.checked = false;
        if (variableChk) variableChk.checked = false;
        if (includeChk) includeChk.checked = false;

        statsEl.textContent = 'Loading alignment…';
        warningsEl.classList.add('hidden');
        gridEl.innerHTML = '';
        emptyEl.classList.add('hidden');
        openModal();
        await refresh();
    }

    function downloadFasta() {
        const rows = state.displayedRows;
        if (!rows || rows.length === 0) {
            showStatusMsg('No sequences to download.', 'warning', 2500);
            return;
        }
        const lines = [];
        for (const r of rows) {
            const safeHeader = (r.name || 'sequence').replace(/[\r\n]/g, ' ');
            lines.push(`>${safeHeader}`);
            lines.push(r.sequence);
        }
        const blob = new Blob([lines.join('\n') + '\n'], { type: 'text/plain' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `alignment_view_${state.jobId || 'job'}.fasta`;
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
        const text = rows.map(r => `>${(r.name || '').replace(/[\r\n]/g, ' ')}\n${r.sequence}`).join('\n') + '\n';
        try {
            await navigator.clipboard.writeText(text);
            showStatusMsg(`Copied ${rows.length} sequence${rows.length === 1 ? '' : 's'} as FASTA.`, 'success', 2000);
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
        state.sortMode = sortSel.value;
        populateReferenceSelector();
        renderGrid();
    });
    refSel.addEventListener('change', () => {
        state.referenceName = refSel.value;
        renderGrid();
    });
    let filterDeb = null;
    filterInp.addEventListener('input', () => {
        state.filterText = filterInp.value;
        clearTimeout(filterDeb);
        filterDeb = setTimeout(renderGrid, 80);
    });
    diffsChk.addEventListener('change', () => {
        state.highlightDiffs = diffsChk.checked;
        bodyEl.classList.toggle('av-highlight-diffs', state.highlightDiffs);
    });
    variableChk.addEventListener('change', () => {
        state.variableOnly = variableChk.checked;
        renderGrid();
    });
    copyBtn.addEventListener('click', copyFasta);
    downloadBtn.addEventListener('click', downloadFasta);

    // Expose a minimal public API for the controller to drive.
    window.DikaryaAlignmentViewer = {
        open,
        close: closeModal,
    };
})();
