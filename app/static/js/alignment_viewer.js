/**
 * Dikarya Alignment Viewer
 *
 * Loads aligned FASTA from the backend and renders an interactive, polished
 * alignment grid in a full-screen modal, driven by the current tree viewer
 * selection / visible tip order.
 *
 * Phase 2: viewport virtualization. Only the visible row × column window
 * (plus a small buffer) is kept in the DOM; everything else is sizers + a
 * translated inner layer per strip (ruler, consensus, names) so scrolling
 * the cells viewport keeps every strip aligned.
 */
(function () {
    'use strict';

    // Alan 5/12/26 - Bail if the modal partial is not present (e.g., non-viewer page).
    const modal = document.getElementById('modal-alignment-viewer');
    if (!modal) return;

    // Alan 5/12/26 - Optional perf instrumentation; flip locally when triaging.
    const DEBUG_ALIGNMENT_PERF = false;

    // Alan 5/12/26 - Fixed geometry: must match CSS .av-cell width and row heights.
    const CELL_WIDTH = 12;
    const ROW_HEIGHT = 18;
    // Alan 5/12/26 - Overscan around the visible viewport when (re)building the window.
    const ROW_BUFFER = 12;
    const COL_BUFFER = 40;
    // Alan 5/12/26 - Hysteresis: keep the current rendered window while the viewport stays this far inside it.
    const ROW_REBUILD_MARGIN = 4;
    const COL_REBUILD_MARGIN = 18;
    // Alan 5/12/26 - Switch to the cheapest .is-diff styling once the LIVE rendered cell count exceeds this.
    const LARGE_LIVE_CELLS = 8000;

    // Alan 8/4/26 - Draggable names/alignment splitter. Width persists across sessions like the color scheme.
    const NAMES_MIN_WIDTH = 110;
    const NAMES_DEFAULT_WIDTH = 352; // Matches the old 22rem CSS max.
    const CELLS_MIN_WIDTH = 160;
    const NAMES_WIDTH_STORAGE_KEY = 'av-names-width';

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
    const compactChk = $('alignment-compact-gaps');
    // Alan 5/13/26 - Uppercase nucleotide toggle; pure CSS class flip on the viewer root.
    const uppercaseChk = $('alignment-uppercase');
    const copyBtn = $('alignment-copy-fasta');
    const downloadBtn = $('alignment-download-fasta');
    // Alan 5/13/26 - Color scheme picker; persists in localStorage and drives data-color-scheme on the viewer root.
    const schemeSel = $('alignment-color-scheme');
    const SCHEME_STORAGE_KEY = 'av-color-scheme';
    // Alan 5/13/26 - Removed 'charcoal' (Warm charcoal) from valid schemes.
    const VALID_SCHEMES = new Set(['moss', 'arctic', 'forest', 'solarized']);

    const NUC_CLASS = {
        A: 'av-a', C: 'av-c', G: 'av-g', T: 'av-t', U: 'av-u',
        '-': 'av-gap', '.': 'av-gap'
    };
    const AMBIG = new Set(['N', 'R', 'Y', 'S', 'W', 'K', 'M', 'B', 'D', 'H', 'V']);

    // Alan 5/12/26 - Persistent state for the currently open viewer session.
    const state = {
        jobId: null,
        sequences: [],
        allCount: 0,
        availablePruned: 0,
        includedPruned: 0,
        alignmentLength: 0,
        warnings: [],
        treeOrder: [],
        selectedNames: [],
        // Alan 5/29/26 - Keep the focal tip pinned to the top when the tree has a sequence of interest.
        preferredName: null,
        includePruned: false,
        sortMode: 'tree',
        referenceName: null,
        filterText: '',
        highlightDiffs: false,
        variableOnly: false,
        compactGaps: true,
        displayedRows: [],
        visibleColumnIndexes: null,
        gapOnlyHidden: 0,
        consensus: '',
        // Alan 8/4/26 - Baseline string every cell is compared against: the reference sequence when one is
        // chosen, otherwise the computed consensus. Drives diff highlighting and Variable Columns Only.
        baselineSeq: '',
        // Alan 5/12/26 - Fingerprint identifies the row SET + column-affecting toggles, not row order.
        _diffComputedFor: '',
        // Alan 5/12/26 - Virtualization scroll/window cache.
        _scrollTop: 0,
        _scrollLeft: 0,
        _frame: null,
        // Alan 5/12/26 - Split window cache so row-only and column-only changes can rebuild independently.
        _rowWindow: { rs: -1, re: -1 },
        _colWindow: { cs: -1, ce: -1 },
        _dom: null,
        _resizeObs: null,
        // Alan 8/4/26 - Current width of the names column, in px; driven by the splitter.
        namesWidth: NAMES_DEFAULT_WIDTH,
    };

    // Alan 8/4/26 - Read the persisted names-column width; ignore anything absurd.
    function loadNamesWidth() {
        try {
            const v = parseInt(localStorage.getItem(NAMES_WIDTH_STORAGE_KEY), 10);
            if (Number.isFinite(v) && v >= NAMES_MIN_WIDTH && v <= 4000) return v;
        } catch (_) { /* ignore storage errors */ }
        return NAMES_DEFAULT_WIDTH;
    }

    // Alan 8/4/26 - Apply a names-column width, clamped so both columns stay usable at the current modal size.
    // A role="separator" that is focusable is a range widget: without these it is
    // announced with no position and no bounds.
    function updateResizerAriaValues(resizer, width, max) {
        resizer.setAttribute('aria-valuemin', String(NAMES_MIN_WIDTH));
        resizer.setAttribute('aria-valuemax', String(Math.round(max)));
        resizer.setAttribute('aria-valuenow', String(width));
        resizer.setAttribute('aria-valuetext', `${width} pixels`);
    }

    function applyNamesWidth(px, persist) {
        const total = gridEl.clientWidth || 0;
        const max = total > 0 ? Math.max(NAMES_MIN_WIDTH, total - CELLS_MIN_WIDTH) : 4000;
        const w = Math.round(Math.min(Math.max(px, NAMES_MIN_WIDTH), max));
        state.namesWidth = w;
        gridEl.style.gridTemplateColumns = `${w}px 1fr`;
        if (state._dom && state._dom.resizer) {
            state._dom.resizer.style.left = `${w}px`;
            // Every path that changes the width -- drag, arrow keys, double-click,
            // the restored preference, a container resize -- comes through here, so
            // this is the one place the announced range can be kept true.
            updateResizerAriaValues(state._dom.resizer, w, max);
        }
        if (persist) {
            try { localStorage.setItem(NAMES_WIDTH_STORAGE_KEY, String(w)); } catch (_) { /* ignore */ }
        }
    }

    // Alan 8/4/26 - Double-clicking the splitter fits the column to the longest displayed name. Measured on a
    // canvas with the same font as .av-names rather than by laying out the (virtualized) rows.
    function measureLongestNameWidth() {
        const rows = state.displayedRows || [];
        if (!rows.length) return NAMES_DEFAULT_WIDTH;
        const canvas = measureLongestNameWidth._canvas
            || (measureLongestNameWidth._canvas = document.createElement('canvas'));
        const ctx = canvas.getContext('2d');
        if (!ctx) return NAMES_DEFAULT_WIDTH;
        ctx.font = "12px 'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
        let max = 0;
        for (let i = 0; i < rows.length; i++) {
            const label = (state.referenceName === rows[i].name ? '◆ ' : '') + rows[i].name;
            const w = ctx.measureText(label).width;
            if (w > max) max = w;
        }
        // 8px padding each side, the score badge when a similarity sort is active, plus a little slack.
        const scoreRoom = (state.sortMode === 'similar' || state.sortMode === 'different') ? 52 : 0;
        return Math.ceil(max + 16 + scoreRoom + 6);
    }

    // Alan 5/13/26 - Read persisted color scheme; fall back to "moss" default if missing or invalid.
    function loadColorScheme() {
        try {
            const v = localStorage.getItem(SCHEME_STORAGE_KEY);
            if (v && VALID_SCHEMES.has(v)) return v;
        } catch (_) { /* ignore storage errors */ }
        return 'arctic';
    }
    // Alan 5/13/26 - Apply a scheme by setting data-color-scheme; CSS variables handle the rest.
    function applyColorScheme(name) {
        const scheme = VALID_SCHEMES.has(name) ? name : 'arctic';
        bodyEl.dataset.colorScheme = scheme;
        try { localStorage.setItem(SCHEME_STORAGE_KEY, scheme); } catch (_) { /* ignore */ }
        if (schemeSel && schemeSel.value !== scheme) schemeSel.value = scheme;
    }

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

    function isGap(ch) { return ch === '-' || ch === '.'; }

    // Alan 5/13/26 - Build the corner label shown above the names column: sort mode + variable/conserved column counts.
    const SORT_MODE_LABELS = {
        tree: 'Tree order',
        name_asc: 'Name A–Z',
        name_desc: 'Name Z–A',
        similar: 'Similarity to ref',
        different: 'Most different',
        length: 'Length',
        leading_gaps: 'Leading gaps',
    };
    function cornerLabel(visibleColCount) {
        const sortLabel = SORT_MODE_LABELS[state.sortMode] || state.sortMode || 'Tree order';
        const variable = state.variableColumnCount || 0;
        const conserved = Math.max(0, (visibleColCount || 0) - variable);
        return `Sort: ${sortLabel} · ${variable} var · ${conserved} cons`;
    }

    // Alan 8/4/26 - Resolve the chosen reference row from the full fetched set (not displayedRows) so it
    // stays the baseline even when the text filter hides it. null means "compare to consensus".
    function getReferenceRow() {
        if (!state.referenceName) return null;
        return state.sequences.find(r => r.name === state.referenceName) || null;
    }

    // Alan 5/13/26 - Variability ignores gaps and N so a column with one base plus gaps is not "variable".
    // Operates on the rows passed in (the filter-aware, pruned-respecting displayedRows), never on DOM.
    // Alan 8/4/26 - With a reference sequence chosen, "variable" means "some displayed row differs from the
    // reference here" rather than "more than one base is present"; that is what makes a holotype comparison
    // useful. Columns where the reference itself is a gap or N fall back to the distinct-base rule.
    function buildVisibleColumnIndexes(rows, refSeq) {
        const len = state.alignmentLength;
        const indexes = [];
        let gapOnly = 0;
        for (let i = 0; i < len; i++) {
            let hasNonGap = false;
            let firstBase = null;
            let varied = false;
            const refCh = refSeq ? (refSeq[i] || '-').toUpperCase() : null;
            const useRef = !!refCh && !isGap(refCh) && refCh !== 'N';
            if (useRef) firstBase = refCh;
            for (let r = 0; r < rows.length; r++) {
                const ch = rows[r].sequence[i] || '-';
                const gap = isGap(ch);
                if (!hasNonGap && !gap) hasNonGap = true;
                if (state.variableOnly && !varied && !gap) {
                    const up = ch.toUpperCase();
                    if (up === 'N') continue;
                    if (firstBase === null) firstBase = up;
                    else if (up !== firstBase) varied = true;
                }
            }
            if (state.compactGaps && !hasNonGap) { gapOnly++; continue; }
            if (state.variableOnly && !varied) continue;
            indexes.push(i);
        }
        state.gapOnlyHidden = gapOnly;
        return indexes;
    }

    function computeConsensus(rows, visibleIdx, refSeq) {
        if (!rows.length) { state.variableColumnCount = 0; return ''; }
        const out = new Array(state.alignmentLength).fill('-');
        // Alan 5/13/26 - Count variable columns (>1 distinct non-gap/non-N base) while computing consensus for the corner label.
        let variable = 0;
        for (let k = 0; k < visibleIdx.length; k++) {
            const i = visibleIdx[k];
            const counts = {};
            let distinct = 0;
            // Alan 8/4/26 - Against a reference, a column counts as variable when any row differs from the
            // reference base, so the corner label matches what Variable Columns Only actually shows.
            const refCh = refSeq ? (refSeq[i] || '-').toUpperCase() : null;
            const useRef = !!refCh && refCh !== '-' && refCh !== '.' && refCh !== 'N';
            let differsFromRef = false;
            for (let r = 0; r < rows.length; r++) {
                const c = (rows[r].sequence[i] || '-').toUpperCase();
                if (c === '-' || c === '.' || c === 'N') continue;
                if (counts[c] === undefined) { counts[c] = 1; distinct++; }
                else counts[c]++;
                if (useRef && c !== refCh) differsFromRef = true;
            }
            let best = '-';
            let bestN = 0;
            for (const k2 in counts) {
                if (counts[k2] > bestN) { best = k2; bestN = counts[k2]; }
            }
            out[i] = best;
            if (useRef ? differsFromRef : distinct > 1) variable++;
        }
        state.variableColumnCount = variable;
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

    // Alan 5/29/26 - Normalize tip names so we can match the persisted focal tip against FASTA headers.
    function normalizeTipName(name) {
        return String(name || '').trim();
    }

    // Alan 5/29/26 - Reuse the persisted focal tip as a visual marker without changing row order.
    function isPreferredRow(row) {
        if (!state.preferredName || !row) return false;
        const preferred = normalizeTipName(state.preferredName);
        const rowName = normalizeTipName(row.name);
        if (!preferred || !rowName) return false;
        if (rowName === preferred) return true;
        // Alan 6/2/26 - Bridge an accession-only alignment id and a fuller tree label by
        // matching only at a token boundary, so a shared leading genus token (e.g.
        // "Amanita muscaria" vs "Amanita phalloides") no longer over-highlights every row.
        const shorter = preferred.length <= rowName.length ? preferred : rowName;
        const longer = preferred.length <= rowName.length ? rowName : preferred;
        return longer.startsWith(shorter + ' ');
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
            // Alan 8/4/26 - Score against the same baseline the cells are colored against: the chosen
            // reference sequence, or the consensus when none is selected.
            const refSeq = state.baselineSeq || (rows[0] && rows[0].sequence);
            if (!refSeq) return rows.slice();
            const scored = rows.map(r => ({ row: r, score: percentIdentity(refSeq, r.sequence) }));
            scored.sort((a, b) => (mode === 'similar' ? b.score - a.score : a.score - b.score));
            for (const s of scored) s.row.__score = s.score;
            return scored.map(s => s.row);
        }
        return rows.slice();
    }

    // Alan 8/4/26 - The reference selector is now always visible and drives diffs, variable columns and
    // similarity sorts alike. The empty value means "compare to the computed consensus" (the old behavior).
    function populateReferenceSelector() {
        if (!refSel) return;
        refSel.innerHTML = '';
        const consensusOpt = document.createElement('option');
        consensusOpt.value = '';
        consensusOpt.textContent = 'Compare to: Consensus';
        refSel.appendChild(consensusOpt);
        for (const row of state.sequences) {
            const opt = document.createElement('option');
            opt.value = row.name;
            opt.textContent = 'Compare to: ' + (row.name.length > 50 ? row.name.slice(0, 47) + '…' : row.name);
            opt.title = row.name;
            refSel.appendChild(opt);
        }
        if (state.referenceName && !state.sequences.some(r => r.name === state.referenceName)) {
            state.referenceName = null;
        }
        refSel.value = state.referenceName || '';
    }

    // Alan 8/4/26 - Set the reference sequence (null / '' restores the consensus baseline) and rerender.
    function setReferenceName(name) {
        const next = name || null;
        if (next === state.referenceName) return;
        state.referenceName = next;
        if (refSel) refSel.value = next || '';
        // Column set and baseline both change; force the consensus/visible-column recompute.
        state._diffComputedFor = '';
        renderAlignmentGrid();
    }

    function pickInitialReference() {
        // Alan 8/4/26 - Keep an explicitly chosen reference across refetches (e.g. the Include Pruned toggle)
        // as long as it is still in the alignment; open() clears it so each session starts fresh.
        if (state.referenceName && state.sequences.some(r => r.name === state.referenceName)) return;
        // Alan 8/4/26 - A single selected tip still seeds the reference; otherwise start from the consensus.
        if (state.selectedNames.length === 1) {
            state.referenceName = state.selectedNames[0];
        } else {
            state.referenceName = null;
        }
    }

    function cellClass(ch, consensusCh) {
        const up = ch.toUpperCase();
        let cls = 'av-cell ';
        cls += NUC_CLASS[up] || (AMBIG.has(up) ? 'av-amb' : 'av-amb');
        // Alan 8/4/26 - An N in the baseline is unknown, not a mismatch, so it never marks a whole column diff.
        if (consensusCh && consensusCh.toUpperCase() !== 'N') {
            const consUp = consensusCh.toUpperCase();
            if (up === consUp && up !== '-') cls += ' av-match';
            else if (up !== consUp && !isGap(ch)) cls += ' is-diff';
        }
        return cls;
    }

    // Alan 5/12/26 - Sorted-name fingerprint so sort-only changes don't invalidate consensus/diff caches.
    function rowSetFingerprint(rows) {
        const names = rows.map(r => r.name).slice().sort();
        return names.join('\x1f')
            + '|cg=' + (state.compactGaps ? 1 : 0)
            + '|vo=' + (state.variableOnly ? 1 : 0)
            + '|ip=' + (state.includePruned ? 1 : 0)
            // Alan 8/4/26 - The reference changes both the baseline and the variable-column set.
            + '|ref=' + (state.referenceName || '');
    }

    // ============================================================
    // Virtualized rendering
    // ============================================================

    // Alan 8/4/26 - Label the baseline strip with the reference name (or "Consensus") plus the ref marker.
    function setBaselineLabel(el) {
        if (!el) return;
        const ref = getReferenceRow();
        el.classList.toggle('av-ref-header', !!ref);
        if (ref) {
            el.textContent = '◆ ' + ref.name;
            el.title = `Reference sequence: ${ref.name}\nEvery row is compared to this sequence. Click a name to change it.`;
        } else {
            el.textContent = 'Consensus';
            el.title = 'Most frequent non-gap, non-N base in each column. Click a sequence name to compare against that sequence instead.';
        }
    }

    // Alan 5/12/26 - Build the persistent skeleton (sizers + strips); only rerun when the row/column set changes.
    function buildSkeleton(rows, visibleIdx) {
        gridEl.innerHTML = '';
        gridEl.style.gridTemplateRows = 'auto auto 1fr';
        const totalW = visibleIdx.length * CELL_WIDTH;
        const totalH = rows.length * ROW_HEIGHT;

        const corner = document.createElement('div');
        corner.className = 'av-names-header';
        // Alan 5/13/26 - Show sort mode + variable/conserved column counts instead of duplicating the header stats.
        corner.textContent = cornerLabel(visibleIdx.length);

        const ruler = document.createElement('div');
        ruler.className = 'av-ruler';
        const rulerInner = document.createElement('div');
        rulerInner.className = 'av-ruler-inner';
        rulerInner.style.cssText = `position:relative;width:${totalW}px;height:${ROW_HEIGHT}px;will-change:transform;`;
        ruler.appendChild(rulerInner);

        const consensusName = document.createElement('div');
        consensusName.className = 'av-consensus-name';
        // Alan 8/4/26 - The top strip shows whichever baseline is active: consensus or a chosen sequence.
        setBaselineLabel(consensusName);

        const consensusStrip = document.createElement('div');
        consensusStrip.className = 'av-consensus-strip';
        const consensusInner = document.createElement('div');
        consensusInner.className = 'av-consensus-inner';
        consensusInner.style.cssText = `position:relative;width:${totalW}px;height:${ROW_HEIGHT}px;will-change:transform;`;
        consensusStrip.appendChild(consensusInner);

        const namesCol = document.createElement('div');
        namesCol.className = 'av-names';
        const namesInner = document.createElement('div');
        namesInner.className = 'av-names-inner';
        namesInner.style.cssText = `position:relative;width:100%;height:${totalH}px;will-change:transform;`;
        namesCol.appendChild(namesInner);

        const cellsCol = document.createElement('div');
        cellsCol.className = 'av-cells';
        const cellsSizer = document.createElement('div');
        cellsSizer.className = 'av-cells-sizer';
        cellsSizer.style.cssText = `position:relative;width:${totalW}px;height:${totalH}px;`;
        cellsCol.appendChild(cellsSizer);

        // Alan 8/4/26 - Splitter overlay on the names/cells boundary. Absolutely positioned so it claims no
        // grid track; the grid itself is position:relative in CSS.
        const resizer = document.createElement('div');
        resizer.className = 'av-col-resizer';
        resizer.title = 'Drag to resize the sequence name column (double-click to fit the longest name)';
        resizer.setAttribute('role', 'separator');
        resizer.setAttribute('aria-orientation', 'vertical');
        resizer.setAttribute('tabindex', '0');
        resizer.setAttribute('aria-label', 'Resize the sequence name column');
        // Seeded here so the control is never exposed without a value; the real
        // bounds land as soon as applyNamesWidth() runs against the built grid.
        updateResizerAriaValues(resizer, state.namesWidth, state.namesWidth);
        resizer.addEventListener('keydown', (e) => {
            const step = e.shiftKey ? 40 : 8;
            if (e.key === 'ArrowLeft') {
                applyNamesWidth(state.namesWidth - step, true);
            } else if (e.key === 'ArrowRight') {
                applyNamesWidth(state.namesWidth + step, true);
            } else if (e.key === 'Home') {
                applyNamesWidth(NAMES_MIN_WIDTH, true);
            } else if (e.key === 'Enter') {
                // Same as the double-click shortcut: fit the longest name.
                applyNamesWidth(measureLongestNameWidth(), true);
            } else {
                return;
            }
            e.preventDefault();
        });

        gridEl.appendChild(corner);
        gridEl.appendChild(ruler);
        gridEl.appendChild(consensusName);
        gridEl.appendChild(consensusStrip);
        gridEl.appendChild(namesCol);
        gridEl.appendChild(cellsCol);
        gridEl.appendChild(resizer);

        // Alan 8/4/26 - Pointer events (not mouse) so the splitter works with touch and pen too.
        let dragStartX = 0;
        let dragStartW = 0;
        resizer.addEventListener('pointerdown', (e) => {
            e.preventDefault();
            dragStartX = e.clientX;
            dragStartW = state.namesWidth;
            resizer.classList.add('is-dragging');
            document.body.classList.add('av-col-resizing');
            resizer.setPointerCapture(e.pointerId);
        });
        resizer.addEventListener('pointermove', (e) => {
            if (!resizer.classList.contains('is-dragging')) return;
            applyNamesWidth(dragStartW + (e.clientX - dragStartX), false);
        });
        const endDrag = (e) => {
            if (!resizer.classList.contains('is-dragging')) return;
            resizer.classList.remove('is-dragging');
            document.body.classList.remove('av-col-resizing');
            try { resizer.releasePointerCapture(e.pointerId); } catch (_) { /* already released */ }
            // Only persist on release so a drag in progress doesn't spam localStorage.
            applyNamesWidth(state.namesWidth, true);
        };
        resizer.addEventListener('pointerup', endDrag);
        resizer.addEventListener('pointercancel', endDrag);
        resizer.addEventListener('dblclick', () => applyNamesWidth(measureLongestNameWidth(), true));

        // Alan 5/12/26 - Detach the previous resize observer so it doesn't fire on torn-down DOM.
        if (state._resizeObs) { state._resizeObs.disconnect(); state._resizeObs = null; }
        if (window.ResizeObserver) {
            state._resizeObs = new ResizeObserver(() => scheduleRender());
            state._resizeObs.observe(cellsCol);
        }

        // Alan 5/12/26 - Single scroll listener on cells viewport; everything else follows via transform.
        cellsCol.addEventListener('scroll', onScroll, { passive: true });

        // Alan 8/4/26 - Click a sequence name to make it the reference; click it again to go back to consensus.
        // Delegated on the names column so virtualized rows need no per-row listeners.
        namesCol.addEventListener('click', (e) => {
            const rowEl = e.target.closest('.av-name-row');
            if (!rowEl || !rowEl.dataset.name) return;
            setReferenceName(rowEl.dataset.name === state.referenceName ? null : rowEl.dataset.name);
        });

        state._dom = { corner, ruler, rulerInner, consensusName, consensusStrip, consensusInner, namesCol, namesInner, cellsCol, cellsSizer, resizer };

        // Alan 8/4/26 - Re-apply the stored width to the fresh skeleton (clamped to the current modal size).
        applyNamesWidth(state.namesWidth, false);
    }

    function onScroll() {
        const dom = state._dom;
        if (!dom) return;
        state._scrollTop = dom.cellsCol.scrollTop;
        state._scrollLeft = dom.cellsCol.scrollLeft;
        scheduleRender();
    }

    function scheduleRender() {
        // Alan 5/12/26 - Coalesce scroll/resize triggers into a single rAF.
        if (state._frame) return;
        state._frame = requestAnimationFrame(() => {
            state._frame = null;
            renderWindow(false);
        });
    }

    // Alan 5/12/26 - Decide whether the row window must rebuild based on hysteresis margins.
    function rowsNeedRebuild(force, visRowStart, visRowEnd, rowCount) {
        if (force) return true;
        const rw = state._rowWindow;
        if (rw.rs < 0) return true;
        if (rw.rs > 0 && visRowStart < rw.rs + ROW_REBUILD_MARGIN) return true;
        if (rw.re < rowCount && visRowEnd > rw.re - ROW_REBUILD_MARGIN) return true;
        return false;
    }

    // Alan 5/12/26 - Decide whether the column window must rebuild based on hysteresis margins.
    function colsNeedRebuild(force, visColStart, visColEnd, colCount) {
        if (force) return true;
        const cw = state._colWindow;
        if (cw.cs < 0) return true;
        if (cw.cs > 0 && visColStart < cw.cs + COL_REBUILD_MARGIN) return true;
        if (cw.ce < colCount && visColEnd > cw.ce - COL_REBUILD_MARGIN) return true;
        return false;
    }

    function renderWindow(force) {
        const dom = state._dom;
        if (!dom) return;
        const t0 = DEBUG_ALIGNMENT_PERF ? performance.now() : 0;
        // Alan 5/12/26 - Translate follower strips first; this is the cheap path used on every scroll frame.
        dom.namesInner.style.transform = `translateY(${-state._scrollTop}px)`;
        dom.rulerInner.style.transform = `translateX(${-state._scrollLeft}px)`;
        dom.consensusInner.style.transform = `translateX(${-state._scrollLeft}px)`;

        const rows = state.displayedRows;
        const cols = state.visibleColumnIndexes || [];
        const vh = Math.max(0, dom.cellsCol.clientHeight);
        const vw = Math.max(0, dom.cellsCol.clientWidth);
        const visRowStart = Math.floor(state._scrollTop / ROW_HEIGHT);
        const visRowEnd = Math.ceil((state._scrollTop + vh) / ROW_HEIGHT);
        const visColStart = Math.floor(state._scrollLeft / CELL_WIDTH);
        const visColEnd = Math.ceil((state._scrollLeft + vw) / CELL_WIDTH);

        const rowsRebuild = rowsNeedRebuild(force, visRowStart, visRowEnd, rows.length);
        const colsRebuild = colsNeedRebuild(force, visColStart, visColEnd, cols.length);

        if (!rowsRebuild && !colsRebuild) {
            if (DEBUG_ALIGNMENT_PERF) {
                console.log(`[alignment-viewer] transform-only frame (no rebuild) took ${(performance.now() - t0).toFixed(2)}ms`);
            }
            return;
        }

        if (rowsRebuild) {
            const rs = Math.max(0, visRowStart - ROW_BUFFER);
            const re = Math.min(rows.length, visRowEnd + ROW_BUFFER);
            state._rowWindow = { rs, re };
        }
        if (colsRebuild) {
            const cs = Math.max(0, visColStart - COL_BUFFER);
            const ce = Math.min(cols.length, visColEnd + COL_BUFFER);
            state._colWindow = { cs, ce };
        }
        const { rs, re } = state._rowWindow;
        const { cs, ce } = state._colWindow;

        // Alan 5/12/26 - Names only need to rebuild when the row window changes.
        if (rowsRebuild) renderNamesWindow(rs, re);
        // Alan 5/12/26 - Ruler/consensus only need to rebuild when the column window changes.
        if (colsRebuild) {
            renderRulerWindow(cs, ce);
            renderConsensusWindow(cs, ce);
        }
        // Alan 5/12/26 - Cells rebuild if either dimension changed; uses the union of both windows.
        renderCellsWindow(rs, re, cs, ce);

        // Alan 5/12/26 - Large-window class is based on LIVE rendered cell count, not logical alignment size.
        const liveCellCount = (re - rs) * (ce - cs);
        bodyEl.classList.toggle('alignment-large-grid', liveCellCount > LARGE_LIVE_CELLS);

        if (DEBUG_ALIGNMENT_PERF) {
            const liveCells = bodyEl.querySelectorAll('.av-cell').length;
            console.log(`[alignment-viewer] rebuild rows=${rowsRebuild} cols=${colsRebuild} window rows=[${rs},${re}) cols=[${cs},${ce}) liveCells=${liveCells} (computed=${liveCellCount}) took ${(performance.now() - t0).toFixed(2)}ms`);
        }
    }

    function renderRulerWindow(cs, ce) {
        const idx = state.visibleColumnIndexes;
        const inner = state._dom.rulerInner;
        const frag = document.createDocumentFragment();
        for (let k = cs; k < ce; k++) {
            const orig = idx[k] + 1;
            const tick = document.createElement('span');
            tick.className = 'av-ruler-tick';
            tick.style.cssText = `position:absolute;left:${k * CELL_WIDTH}px;width:${CELL_WIDTH}px;`;
            if (orig === 1 || orig % 10 === 0) {
                tick.classList.add('av-major');
                tick.textContent = String(orig);
            }
            tick.title = `Original alignment position ${orig}`;
            frag.appendChild(tick);
        }
        inner.replaceChildren(frag);
    }

    function renderConsensusWindow(cs, ce) {
        const idx = state.visibleColumnIndexes;
        // Alan 8/4/26 - Strip shows the active baseline (reference sequence or consensus).
        const consensus = state.baselineSeq || state.consensus;
        const inner = state._dom.consensusInner;
        const frag = document.createDocumentFragment();
        for (let k = cs; k < ce; k++) {
            const i = idx[k];
            const ch = consensus[i] || '-';
            const span = document.createElement('span');
            span.className = cellClass(ch, null) + ' av-consensus-cell';
            span.style.cssText = `position:absolute;left:${k * CELL_WIDTH}px;width:${CELL_WIDTH}px;`;
            span.textContent = ch;
            frag.appendChild(span);
        }
        inner.replaceChildren(frag);
    }

    function renderNamesWindow(rs, re) {
        const rows = state.displayedRows;
        const inner = state._dom.namesInner;
        const frag = document.createDocumentFragment();
        const showScores = state.sortMode === 'similar' || state.sortMode === 'different';
        // Alan 8/4/26 - Mark the reference row and expose the name for the delegated click handler.
        const refName = state.referenceName;
        for (let r = rs; r < re; r++) {
            const row = rows[r];
            const div = document.createElement('div');
            // Alan 5/13/26 - Tag every odd logical row with av-row-alt so CSS can apply alternating shading.
            const isRef = !!refName && row.name === refName;
            div.className = 'av-name-row av-name-row-clickable' + (r % 2 === 1 ? ' av-row-alt' : '')
                + (isPreferredRow(row) ? ' av-focus-row' : '') + (isRef ? ' av-ref-row' : '');
            div.style.cssText = `position:absolute;top:${r * ROW_HEIGHT}px;left:0;right:0;height:${ROW_HEIGHT}px;`;
            div.dataset.name = row.name;
            const nameText = document.createElement('span');
            nameText.className = 'av-name-text';
            nameText.textContent = isRef ? '◆ ' + row.name : row.name;
            nameText.title = isRef
                ? `${row.name}\n(reference — click to compare to the consensus again)`
                : `${row.name}\nClick to use as the reference sequence`;
            div.appendChild(nameText);
            if (showScores && typeof row.__score === 'number') {
                const score = document.createElement('span');
                score.className = 'av-name-score';
                score.textContent = (row.__score * 100).toFixed(1) + '%';
                div.appendChild(score);
            }
            frag.appendChild(div);
        }
        inner.replaceChildren(frag);
    }

    function renderCellsWindow(rs, re, cs, ce) {
        const rows = state.displayedRows;
        const idx = state.visibleColumnIndexes;
        // Alan 8/4/26 - Compare against the active baseline (reference sequence or consensus).
        const consensus = state.baselineSeq || state.consensus;
        const refName = state.referenceName;
        const sizer = state._dom.cellsSizer;
        const frag = document.createDocumentFragment();
        // Alan 5/13/26 - In diff mode, av-match cells render as a middle dot; new cells from scroll inherit this.
        const diffMode = state.highlightDiffs;
        for (let r = rs; r < re; r++) {
            const rowDiv = document.createElement('div');
            // Alan 5/13/26 - Tag odd logical rows so the schemed alt-row tint shows through.
            // Alan 8/4/26 - The reference row keeps its literal bases in diff mode; dotting it would hide the
            // very sequence everything else is being read against.
            const isRef = !!refName && rows[r].name === refName;
            rowDiv.className = 'av-cell-row' + (r % 2 === 1 ? ' av-row-alt' : '')
                + (isPreferredRow(rows[r]) ? ' av-focus-row' : '') + (isRef ? ' av-ref-row' : '');
            rowDiv.style.cssText = `position:absolute;top:${r * ROW_HEIGHT}px;left:${cs * CELL_WIDTH}px;height:${ROW_HEIGHT}px;`;
            const seq = rows[r].sequence;
            for (let k = cs; k < ce; k++) {
                const i = idx[k];
                const ch = seq[i] || '-';
                const span = document.createElement('span');
                const cls = cellClass(ch, consensus[i]);
                span.className = cls;
                // Alan 5/13/26 - Replace matches with · only when highlight-differences is on; gaps are unaffected.
                if (diffMode && !isRef && cls.indexOf('av-match') !== -1) {
                    span.dataset.orig = ch;
                    span.textContent = '·';
                } else {
                    span.textContent = ch;
                }
                rowDiv.appendChild(span);
            }
            frag.appendChild(rowDiv);
        }
        sizer.replaceChildren(frag);
    }

    // ============================================================
    // Public render entry point that preserves all pre-virtualization behavior.
    // ============================================================

    function renderAlignmentGrid() {
        emptyEl.classList.add('hidden');

        let rows = state.sequences.slice();
        const filter = (state.filterText || '').trim().toLowerCase();
        if (filter) rows = rows.filter(r => r.name.toLowerCase().includes(filter));
        for (const r of rows) r.__score = undefined;
        state.displayedRows = rows;

        if (rows.length === 0) {
            state.visibleColumnIndexes = [];
            state.gapOnlyHidden = 0;
            state.consensus = '';
            state.baselineSeq = '';
            gridEl.innerHTML = '';
            state._dom = null;
            emptyEl.textContent = 'No sequences match the current filter.';
            emptyEl.classList.remove('hidden');
            updateStats();
            return;
        }

        // Alan 5/12/26 - Recompute visible columns + consensus only when the row SET or column toggles change.
        // Alan 8/4/26 - Runs before sorting now: the similarity sorts score against the resolved baseline,
        // and neither the column set nor the consensus depends on row order.
        const refRow = getReferenceRow();
        const refSeq = refRow ? refRow.sequence : null;
        const fp = rowSetFingerprint(rows);
        const rowSetChanged = fp !== state._diffComputedFor;
        if (rowSetChanged) {
            state.visibleColumnIndexes = buildVisibleColumnIndexes(rows, refSeq);
            state.consensus = computeConsensus(rows, state.visibleColumnIndexes, refSeq);
            state._diffComputedFor = fp;
        }
        state.baselineSeq = refSeq || state.consensus;
        const visibleIdx = state.visibleColumnIndexes;

        // Alan 8/4/26 - Sort after the baseline exists so "Similarity to reference" uses the same yardstick.
        rows = sortRows(rows);
        state.displayedRows = rows;

        // Alan 5/12/26 - Rebuild the skeleton when row count or column count changes; otherwise just rerender the window.
        const needSkeleton = !state._dom
            || state._dom.cellsSizer.style.width !== (visibleIdx.length * CELL_WIDTH) + 'px'
            || state._dom.cellsSizer.style.height !== (rows.length * ROW_HEIGHT) + 'px';
        if (needSkeleton) {
            buildSkeleton(rows, visibleIdx);
            state._scrollTop = 0;
            state._scrollLeft = 0;
            state._dom.cellsCol.scrollTop = 0;
            state._dom.cellsCol.scrollLeft = 0;
            // Alan 5/12/26 - Invalidate split window cache so the next render rebuilds both axes.
            state._rowWindow = { rs: -1, re: -1 };
            state._colWindow = { cs: -1, ce: -1 };
        } else {
            // Alan 5/12/26 - Same skeleton, but row order/window contents may have changed; force a window rerender.
            // Alan 5/12/26 - Invalidate split window cache so the next render rebuilds both axes.
            state._rowWindow = { rs: -1, re: -1 };
            state._colWindow = { cs: -1, ce: -1 };
            // Update header text in case col/row counts displayed there changed.
            // Alan 5/13/26 - Refresh corner with sort mode + variable/conserved column counts on rerender.
            state._dom.corner.textContent = cornerLabel(visibleIdx.length);
            // Alan 8/4/26 - Keep the baseline strip label in sync when only the reference changed.
            setBaselineLabel(state._dom.consensusName);
        }

        bodyEl.classList.toggle('alignment-highlight-differences', state.highlightDiffs);
        // Alan 5/12/26 - The large-grid class is now decided inside renderWindow based on live rendered cells.

        // Alan 5/12/26 - Defer the initial render one frame so clientWidth/clientHeight are known after layout.
        requestAnimationFrame(() => renderWindow(true));
        updateStats();
    }

    function setHighlightDifferences(enabled) {
        const t0 = DEBUG_ALIGNMENT_PERF ? performance.now() : 0;
        state.highlightDiffs = !!enabled;
        bodyEl.classList.toggle('alignment-highlight-differences', state.highlightDiffs);
        // Alan 5/13/26 - Swap visible av-match cell text between original base and · without a full skeleton rebuild.
        const sizer = state._dom && state._dom.cellsSizer;
        if (sizer) {
            // Alan 8/4/26 - Never dot the reference row itself; it stays readable as the comparison baseline.
            const matches = sizer.querySelectorAll('.av-cell-row:not(.av-ref-row) .av-cell.av-match');
            if (state.highlightDiffs) {
                for (let m = 0; m < matches.length; m++) {
                    const el = matches[m];
                    if (!el.dataset.orig) el.dataset.orig = el.textContent;
                    el.textContent = '·';
                }
            } else {
                for (let m = 0; m < matches.length; m++) {
                    const el = matches[m];
                    if (el.dataset.orig) {
                        el.textContent = el.dataset.orig;
                        delete el.dataset.orig;
                    }
                }
            }
        }
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
        // Alan 8/4/26 - Make the active comparison baseline visible in the header stats.
        if (state.referenceName) {
            const short = state.referenceName.length > 32 ? state.referenceName.slice(0, 29) + '…' : state.referenceName;
            txt += ` · vs ${short}`;
        }
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

    // Alignment requests are bounded and singular: the Include Pruned toggle (and a
    // reopen) can re-enter refresh() while a request is still running, and without
    // this a slow earlier response could overwrite a newer alignment.
    let inFlightRequest = null;
    const FETCH_TIMEOUT_MS = 60000;

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

        if (inFlightRequest) {
            inFlightRequest.superseded = true;
            inFlightRequest.controller.abort();
        }
        const controller = new AbortController();
        const request = { controller, superseded: false, timedOut: false };
        inFlightRequest = request;
        const timer = setTimeout(() => {
            request.timedOut = true;
            controller.abort();
        }, FETCH_TIMEOUT_MS);

        try {
            const resp = await fetch(`/api/job/${state.jobId}/alignment/view`, {
                method: 'POST',
                headers,
                body: JSON.stringify(payload),
                signal: controller.signal,
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok || data.status !== 'success') {
                const err = data && data.error ? data.error : `HTTP ${resp.status}`;
                throw new Error(err);
            }
            return data;
        } catch (e) {
            if (e && e.name === 'AbortError') {
                const abortError = new Error(
                    request.timedOut ? 'The request timed out.' : 'Superseded by a newer request.'
                );
                abortError.name = 'AbortError';
                abortError.superseded = request.superseded;
                throw abortError;
            }
            throw e;
        } finally {
            clearTimeout(timer);
            // Cleared either way, so a failed load stays retryable.
            if (inFlightRequest === request) inFlightRequest = null;
        }
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
            // Alan 5/12/26 - Row set changed; force consensus/visible-column recompute.
            state._diffComputedFor = '';
            // Alan 5/12/26 - Reset skeleton so dimensions match the new data.
            state._dom = null;
            renderWarnings();
            pickInitialReference();
            populateReferenceSelector();
            renderAlignmentGrid();
        } catch (e) {
            // A request we cancelled ourselves in favour of a newer one is not a
            // failure the user should see; the newer one owns the UI now.
            if (e && e.name === 'AbortError' && e.superseded) return;
            console.error('Alignment Viewer failed:', e);
            showStatusMsg(`Alignment Viewer failed: ${e.message}`, 'danger', 4000);
            emptyEl.textContent = `Could not load alignment: ${e.message}`;
            emptyEl.classList.remove('hidden');
            gridEl.innerHTML = '';
            state._dom = null;
        }
    }

    async function open(opts) {
        opts = opts || {};
        state.jobId = opts.jobId || window.JOB_ID;
        state.treeOrder = opts.treeOrder || [];
        state.selectedNames = opts.selectedNames || [];
        // Alan 5/29/26 - Accept the persisted focal tip from the tree page when available.
        state.preferredName = opts.preferredName || null;
        state.includePruned = false;
        state.sortMode = 'tree';
        state.filterText = '';
        // Alan 8/4/26 - Open with difference highlighting and variable-columns-only already on; that is the
        // view people actually want first, and both are one click away from off.
        state.highlightDiffs = true;
        state.variableOnly = true;
        state.compactGaps = true;
        state.referenceName = null;
        // Alan 8/4/26 - Start each session on the consensus baseline.
        state.baselineSeq = '';
        state._diffComputedFor = '';
        state._dom = null;

        if (sortSel) sortSel.value = 'tree';
        if (filterInp) filterInp.value = '';
        // Alan 8/4/26 - Checkbox state must mirror the defaults above.
        if (diffsChk) diffsChk.checked = true;
        if (variableChk) variableChk.checked = true;
        if (compactChk) compactChk.checked = true;
        if (includeChk) includeChk.checked = false;
        bodyEl.classList.add('alignment-highlight-differences');
        bodyEl.classList.remove('alignment-large-grid');
        // Alan 5/13/26 - Reset uppercase mode on each open so it starts in the default (mixed-case) state.
        if (uppercaseChk) uppercaseChk.checked = false;
        bodyEl.classList.remove('alignment-uppercase');

        statsEl.textContent = 'Loading alignment…';
        warningsEl.classList.add('hidden');
        gridEl.innerHTML = '';
        emptyEl.classList.add('hidden');
        openModal();
        await refresh();
    }

    // Alan 5/12/26 - Copy/download still export the FULL displayed rows × visibleColumns, not the screen window.
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
    // Alan 5/13/26 - Persist scheme choice and apply instantly; pure CSS swap, no cell rebuild needed.
    if (schemeSel) {
        schemeSel.addEventListener('change', () => applyColorScheme(schemeSel.value));
    }
    // Alan 5/13/26 - Apply persisted scheme once at startup so the viewer is correctly styled before first open.
    applyColorScheme(loadColorScheme());
    // Alan 8/4/26 - Seed the names-column width from storage; the skeleton re-applies it once it exists.
    state.namesWidth = loadNamesWidth();
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && !modal.classList.contains('hidden')) closeModal();
    });

    includeChk.addEventListener('change', () => {
        state.includePruned = includeChk.checked;
        refresh();
    });
    sortSel.addEventListener('change', () => {
        // Alan 5/12/26 - Sorting only reorders rows; row set fingerprint stays the same so consensus is reused.
        // Alan 8/4/26 - The reference selector is always visible now, so it no longer needs rebuilding here.
        state.sortMode = sortSel.value;
        renderAlignmentGrid();
    });
    refSel.addEventListener('change', () => {
        // Alan 8/4/26 - Empty value restores the consensus baseline.
        setReferenceName(refSel.value);
    });
    let filterDeb = null;
    filterInp.addEventListener('input', () => {
        state.filterText = filterInp.value;
        clearTimeout(filterDeb);
        filterDeb = setTimeout(renderAlignmentGrid, 80);
    });
    diffsChk.addEventListener('change', () => {
        setHighlightDifferences(diffsChk.checked);
    });
    variableChk.addEventListener('change', () => {
        state.variableOnly = variableChk.checked;
        // Alan 5/12/26 - Column set changed; force consensus/visible-column recompute.
        state._diffComputedFor = '';
        renderAlignmentGrid();
    });
    // Alan 5/13/26 - Uppercase toggle: pure class flip, no rerender required since CSS handles the display.
    if (uppercaseChk) uppercaseChk.addEventListener('change', () => {
        bodyEl.classList.toggle('alignment-uppercase', uppercaseChk.checked);
    });
    if (compactChk) compactChk.addEventListener('change', () => {
        state.compactGaps = compactChk.checked;
        // Alan 5/12/26 - Column set changed; force consensus/visible-column recompute.
        state._diffComputedFor = '';
        renderAlignmentGrid();
    });
    copyBtn.addEventListener('click', copyFasta);
    downloadBtn.addEventListener('click', downloadFasta);

    // Alan 5/12/26 - Re-render the visible window on window resize as a fallback for browsers without ResizeObserver.
    window.addEventListener('resize', () => {
        if (modal.classList.contains('hidden')) return;
        // Alan 8/4/26 - Re-clamp the names column so a narrower window can't squeeze out the alignment.
        // Not persisted: shrinking the window shouldn't overwrite the width the user chose.
        if (state._dom) applyNamesWidth(state.namesWidth, false);
        scheduleRender();
    });

    window.DikaryaAlignmentViewer = {
        open,
        close: closeModal,
        setHighlightDifferences,
        // Alan 8/4/26 - Allow the tree viewer (or the console) to pin a reference sequence programmatically.
        setReferenceName,
    };

    // Alan 5/12/26 - Lightweight debug surface for triaging perf in the console.
    window.DikaryaAlignmentViewerDebug = {
        cellCount() { return bodyEl.querySelectorAll('.av-cell').length; },
        windowInfo() {
            return {
                rowWindow: { ...state._rowWindow },
                colWindow: { ...state._colWindow },
                displayedRows: state.displayedRows.length,
                visibleColumns: (state.visibleColumnIndexes || []).length,
                scrollTop: state._scrollTop,
                scrollLeft: state._scrollLeft,
                largeGrid: bodyEl.classList.contains('alignment-large-grid'),
            };
        },
        timeHighlightToggle() {
            const t0 = performance.now();
            setHighlightDifferences(!state.highlightDiffs);
            return performance.now() - t0;
        },
        // Alan 5/13/26 - Verify Variable Columns Only is filter-aware: reports the active row set used to compute variability.
        variableInfo() {
            const fetched = state.sequences.length;
            const active = state.displayedRows.length;
            const excludedByFilter = fetched - active;
            return {
                activeRowsUsedForVariability: active,
                totalFetchedRows: fetched,
                excludedByTextFilter: excludedByFilter,
                includePruned: state.includePruned,
                availablePruned: state.availablePruned,
                includedPruned: state.includedPruned,
                prunedRowsExcludedFromFetch: state.includePruned ? 0 : state.availablePruned,
                variableOnlyEnabled: state.variableOnly,
                // Alan 8/4/26 - null baseline name means the computed consensus is in use.
                referenceName: state.referenceName,
                baselineIsReference: !!state.referenceName,
                variableColumnCount: state.variableColumnCount || 0,
                visibleColumns: (state.visibleColumnIndexes || []).length,
                alignmentLength: state.alignmentLength,
                computedFromDOM: false,
                gapsIgnored: true,
                nIgnored: true,
            };
        },
    };
})();
