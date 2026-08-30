/**
 * DikaryaTreeViewer - Advanced Phylotree Wrapper
 * Adds selection management, incremental updates, and state persistence.
 */
(function () {
    'use strict';

    const DEBUG_MODE = new URLSearchParams(window.location.search).has('debug');
    // FastTree writes numerical stand-ins such as 6e-9 and 1.2e-8 for branches whose
    // optimized length is effectively zero. Treating only literal 0 as unresolved would
    // split one biological polytomy into arbitrary binary nodes in the viewer.
    const ZERO_LENGTH_POLYTOMY_EPSILON = 1e-7;

    // Alan 8/24/26 - Say why a phylotree instance is not usable, or null when it is.
    // phylotree.js does not throw on a truncated or otherwise unparseable Newick: its
    // parser hands back a plain array instead of a d3 hierarchy, and the constructor
    // now tolerates that rather than dying inside .links(). That tolerance is what
    // keeps the bundle from taking the page down, but on its own it would turn a
    // corrupt tree artifact into a blank viewer with no explanation. Every caller
    // must run the model past this before treating it as renderable.
    function describeTreeParseFailure(tree) {
        const root = tree && (typeof tree.getNodes === 'function' ? tree.getNodes() : tree.nodes);
        if (!root || Array.isArray(root) || typeof root.descendants !== 'function') {
            return 'The tree file could not be parsed as Newick (it looks truncated or malformed).';
        }
        const tips = typeof tree.getTips === 'function' ? tree.getTips() : [];
        if (!tips || tips.length === 0) {
            return 'The tree file parsed but contains no sequences.';
        }
        return null;
    }

    // Exported so the Node regression harness can drive it without a DOM.
    window.describeTreeParseFailure = describeTreeParseFailure;

    // Alan 8/24/26 - The same stable internal-node ID the backend computes in
    // tree_edit_service._stable_internal_node_id_from_names() and the controller computes in
    // stableInternalNodeIdFromTipNames(): FNV-1a over the sorted descendant tip names. Two
    // distinct clades can only collide by having identical tip sets, which in a tree means a
    // unary chain -- and those are excluded from collapse targets below. Sorting is by Unicode
    // code point (not UTF-16 unit) so it agrees with Python's sorted()/ord() on non-BMP names.
    function stableCladeIdFromTipNames(tipNames) {
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
        let hash = 2166136261;
        for (const ch of names.join('\x1f')) {
            hash ^= ch.codePointAt(0);
            hash = Math.imul(hash, 16777619) >>> 0;
        }
        return `internal:${hash.toString(16).padStart(8, '0')}`;
    }

    // Alan 7/15/26 - Hide pipeline-only MAFFT and RiC annotations from tip labels while preserving stable tree IDs.
    function cleanTipDisplayName(name) {
        if (typeof name !== 'string') return name;
        return name.replace(/^_R_/, '').replace(/\s+RiC(?:\s+\d+)?\s*$/i, '').trim();
    }

    // Alan 8/15/26 - Curated font list for clade annotations, shared with the controller's
    // editor UI and mirrored by ALLOWED_FONT_FAMILIES in tree_annotation_service.py. Using a
    // fixed list plus a fixed fallback stack means a font value can never carry a CSS fragment
    // into the SVG.
    window.DikaryaCladeAnnotations = {
        FONT_FAMILIES: [
            'Inter', 'Arial', 'Helvetica', 'Times New Roman', 'Georgia',
            'Verdana', 'Courier New', 'serif', 'sans-serif', 'monospace'
        ],
        FONT_STACKS: {
            'Inter': "Inter, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
            'Arial': "Arial, Helvetica, sans-serif",
            'Helvetica': "Helvetica, Arial, sans-serif",
            'Times New Roman': "'Times New Roman', Times, serif",
            'Georgia': "Georgia, 'Times New Roman', serif",
            'Verdana': "Verdana, Geneva, sans-serif",
            'Courier New': "'Courier New', Courier, monospace",
            'serif': "serif",
            'sans-serif': "sans-serif",
            'monospace': "monospace"
        },
        MIN_FONT_SIZE: 6,
        MAX_FONT_SIZE: 72,
        DEFAULTS: {
            font_family: 'Arial',
            font_size: 12,
            font_style: 'normal',
            font_weight: 'normal',
            text_color: '#1f2937',
            // Alan 8/17/26 - Supply default border, fill, and opacity for branch bubbles.
            line_color: '#1f2937',
            fill_color: '#ffffff',
            fill_opacity: 0.9,
            // Alan 8/24/26 - Clade highlights get their OWN fill fields. Reusing the bubble's
            // fill_color/fill_opacity would paint every highlight near-opaque white and hide
            // the tree underneath it. journal-gold at 0.15 tints both themes without dominating.
            highlight_color: '#c9a962',
            // Alan 8/24/26 - Automatic means "work the colour out from the tree": the clade's
            // own persistent colour group when it has one, otherwise the next palette colour.
            highlight_color_mode: 'auto',
            // Alan 8/24/26 - Equal to AUTO_HIGHLIGHT_OPACITY.light below, and it must stay
            // equal: an untouched highlight is drawn with the automatic, theme-aware opacity,
            // so a shared default that differed from it made the layer card report a number
            // the figure never used. Mirrors DEFAULT_HIGHLIGHT_OPACITY in the service.
            highlight_opacity: 0.2
        }
    };

    // Alan 8/24/26 - The style properties every annotation resolves, in one place: the live
    // renderer and the editor preview used to carry two copies of this list, so adding the
    // highlight fields to one and not the other would have silently unstyled the preview.
    // Mirrors _LAYER_STYLE_FIELDS in tree_annotation_service.py.
    const ANNOTATION_STYLE_FIELDS = [
        'font_family', 'font_size', 'font_style', 'font_weight', 'text_color',
        'line_color', 'fill_color', 'fill_opacity', 'highlight_color',
        'highlight_color_mode', 'highlight_opacity'
    ];
    const ANNOTATION_NUMERIC_STYLE_FIELDS = new Set([
        'font_size', 'fill_opacity', 'highlight_opacity'
    ]);
    // Alan 8/24/26 - Layer fields the server leaves absent rather than backfilling with the
    // shared default, so absence really means "nobody chose this". Mirrors
    // _LAYER_OPTIONAL_STYLE_FIELDS in tree_annotation_service.py.
    const ANNOTATION_OPTIONAL_LAYER_STYLE_FIELDS = new Set(['highlight_opacity']);

    // Alan 8/24/26 - Curated fills for LARGE translucent clade backgrounds, which is a very
    // different job from the categorical line colours in _selectionColors: those are chosen to
    // be legible as a 1px stroke and read as neon poster paint when spread over a third of a
    // figure. These are mid-tone and slightly desaturated so that, at the automatic opacities
    // below, a light theme gets a pale tint that black tip labels stay readable on and a dark
    // theme gets a deep tint that white ones do. Order is the assignment order for successive
    // unstyled highlights, arranged so the first four are maximally distinct.
    const HIGHLIGHT_PALETTE = [
        '#3b6fb6',  // blue
        '#7a5aa6',  // purple
        '#2f8f83',  // teal
        '#b8536b',  // rose
        '#c08a2e',  // amber
        '#4a8b4a',  // green
        '#a8503c',  // muted red
        '#2b7f9e'   // cyan
    ];

    // Alan 8/24/26 - The opacity an UNSTYLED highlight is drawn at. A pale wash that reads
    // clearly on white is nearly invisible on black, so the automatic value differs by theme --
    // but it is resolved here and written inline, never applied as a CSS override, so the
    // exported SVG carries exactly what was on screen. An opacity the user chose is used
    // verbatim in both themes.
    const AUTO_HIGHLIGHT_OPACITY = { light: 0.2, dark: 0.26 };

    // Alan 8/26/26 - Minimum HSL lightness a colour-group / marker colour is DRAWN at in dark
    // mode. The categorical group colours are picked to read against white and were then used
    // verbatim in both themes, but the dark tree panel is #2d4a3e: the Blue group (#2563eb)
    // sat at 1.88:1 against bare canvas, and 1.06:1 where it crossed a light user-picked clade
    // highlight -- text that is effectively not on the page. 0.76 roughly doubles that worst
    // case while leaving the closest pair of group colours about 7 CIE76 units apart, so the
    // groups stay tellable apart. See _displayColorForTheme().
    const DARK_LABEL_MIN_LIGHTNESS = 0.76;

    // Alan 8/24/26 - Published on the shared config so the editor and the regression harness
    // resolve styles through the same list the renderer uses, instead of their own copies.
    window.DikaryaCladeAnnotations.STYLE_FIELDS = ANNOTATION_STYLE_FIELDS;
    window.DikaryaCladeAnnotations.NUMERIC_STYLE_FIELDS = Array.from(
        ANNOTATION_NUMERIC_STYLE_FIELDS
    );
    window.DikaryaCladeAnnotations.HIGHLIGHT_PALETTE = HIGHLIGHT_PALETTE;
    window.DikaryaCladeAnnotations.AUTO_HIGHLIGHT_OPACITY = AUTO_HIGHLIGHT_OPACITY;

    // Alan 8/15/26 - Map a validated family name onto its fallback stack; anything unknown
    // falls back to the default rather than reaching the SVG verbatim.
    function annotationFontStack(family) {
        const stacks = window.DikaryaCladeAnnotations.FONT_STACKS;
        return stacks[family] || stacks[window.DikaryaCladeAnnotations.DEFAULTS.font_family];
    }

    // Alan 8/4/26 - Shared descriptions of what each support scale actually means, so the
    // badge and the on-tree node labels can both explain that SH-like local supports are
    // neither bootstrap proportions nor Bayesian posterior probabilities. Reviewers were
    // reading FastTree's 0-1 values as posteriors from a Bayesian run.
    window.SUPPORT_TYPE_INFO = {
        BS: {
            label: 'Bootstrap',
            tooltip: 'Bootstrap support (0-100): percentage of pseudoreplicate trees containing this clade.'
        },
        PP: {
            label: 'Posterior',
            tooltip: 'Bayesian posterior probability (0-1): probability of this clade given the data and model.'
        },
        SH: {
            label: 'FastTree SH-like',
            tooltip: 'FastTree SH-like local support (0-1). NOT a bootstrap proportion and NOT a Bayesian posterior probability. '
                + 'It only tests this node against its two nearest-neighbour-interchange alternatives, so it is local, known to be '
                + 'anti-conservative, and cannot detect long-branch attraction. High values on long branches deserve scepticism.'
        },
        // Alan 8/22/26 - IQ-TREE's single-value support is the ultrafast bootstrap, which
        // is not the classical bootstrap and does not share its 70 "moderate" convention.
        // Labelling it "Bootstrap" told users a UFBoot of 80 was moderately supported.
        UFBOOT: {
            label: 'UFBoot',
            tooltip: 'IQ-TREE ultrafast bootstrap (0-100). NOT the classical bootstrap scale: UFBoot is '
                + 'markedly less conservative, so the conventional cutoff is 95, and the 70 "moderate" '
                + 'convention from standard bootstrap does not apply to it.'
        },
        ALRT: {
            label: 'SH-aLRT',
            tooltip: 'IQ-TREE SH-aLRT branch test (0-100), written when -alrt ran without ultrafast '
                + 'bootstrap. A likelihood-ratio test of the branch, not a bootstrap proportion; the '
                + 'conventional cutoff is 80.'
        },
        ALRT_UFBOOT: {
            label: 'SH-aLRT / UFBoot',
            tooltip: 'IQ-TREE dual support, shown as SH-aLRT/UFBoot. Both are percentages (0-100). '
                + 'A clade is normally called well supported when SH-aLRT is at least 80 AND UFBoot is at least 95. '
                + 'The threshold filter applies to the UFBoot half.'
        },
        mixed: {
            label: 'Mixed',
            tooltip: 'Mixed support scales detected in this tree. Check the tree source before comparing values across nodes.'
        },
        none: {
            label: 'None',
            tooltip: 'This tree carries no node support values.'
        }
    };

    // Alan 8/21/26 - Support scale is decided by which program built the tree, not by how
    // big the numbers are. RAxML-NG happily writes bootstrap values of 0, 1 and 0.95 for
    // poorly supported clades, and the old "everything <= 1 must be posterior" rule
    // relabelled those trees as Bayesian. Mirrors _classify_support() /
    // _normalize_tree_method() in app/services/tree_analysis_service.py -- the cases in
    // tests/fixtures/support_classification_cases.json are run against both.
    const SUPPORT_METHOD_ALIASES = {
        'raxml-ng': 'raxml', 'raxmlng': 'raxml', 'raxml_ng': 'raxml', 'raxml8': 'raxml',
        'iq-tree': 'iqtree', 'iqtree2': 'iqtree', 'iq-tree2': 'iqtree',
        'mr_bayes': 'mrbayes', 'mrbayes3': 'mrbayes',
        'fasttree2': 'fasttree',
        'neighbor-joining': 'nj', 'neighbour-joining': 'nj'
    };
    const SUPPORT_METHOD_TYPES = {
        fasttree: 'SH',
        raxml: 'BS',
        iqtree: 'UFBOOT',
        mrbayes: 'PP'
    };

    // Alan 8/22/26 - Whitespace is removed rather than trimmed, so "IQ-TREE 2", "RAxML NG"
    // and "Fast Tree" normalize onto the same builder as their hyphenated spellings instead
    // of falling through to the value-shape fallback.
    window.normalizeTreeMethod = function (treeMethod) {
        const method = String(treeMethod == null ? '' : treeMethod).toLowerCase().replace(/\s+/g, '');
        return SUPPORT_METHOD_ALIASES[method] || method;
    };

    // Alan 8/22/26 - `options.alrtOnly` marks an IQ-TREE run that used -alrt without
    // ultrafast bootstrap: the node labels are then single SH-aLRT percentages, and reading
    // them as bootstrap gives both the wrong test and the wrong threshold. The flag is
    // resolved server-side (tree_analysis_service.resolve_tree_support_context) so the badge
    // and the Claude review cannot disagree about the same tree.
    window.classifySupportType = function (values, hasDual, treeMethod, options) {
        if (hasDual) return 'ALRT_UFBOOT';
        if (!values || !values.length) return 'none';

        const declared = SUPPORT_METHOD_TYPES[window.normalizeTreeMethod(treeMethod)];
        if (declared === 'UFBOOT' && options && options.alrtOnly) return 'ALRT';
        if (declared) return declared;

        // Genuinely unknown or legacy builder: the numbers are all there is to go on.
        if (values.some(v => v > 1.0)) {
            return values.some(v => v > 0 && v < 1.0) ? 'mixed' : 'BS';
        }
        return 'PP';
    };

    // --- ZOOM PANIC STOP ---
    if (!window.__dikarya_zoom_panic_stop_attached) {
        window.__dikarya_zoom_panic_stop_attached = true;
        window.addEventListener('pointerup', () => {
            if (window.d3v7) {
                window.d3v7.select(window).on('mousemove.zoom', null).on('mouseup.zoom', null);
            }
        }, true);
    }

    /**
     * TreeViewerBase - Abstract base class defining the selection interface.
     * All tree viewer implementations should extend this class to ensure
     * a consistent API for the controller. Methods provide sensible no-op
     * defaults so the controller doesn't need defensive checks.
     * 
     * @interface
     */
    class TreeViewerBase {
        // =========== SELECTION STATE MANAGEMENT ===========

        /**
         * Get all currently selected nodes.
         * @returns {Array} Array of selected node objects
         */
        getSelectedNodes() { return []; }

        /**
         * Get the count of selected nodes.
         * @returns {number}
         */
        getSelectionCount() { return 0; }

        /**
         * Clear ALL selections across ALL selection sets.
         * Use after backend mutations (prune, rename, reroot) when the tree
         * structure changes and all selected node references become stale.
         */
        clearSelection() { }

        /**
         * Clear only the current temporary action selection.
         * Use before applying a new selection action to replace (not append)
         * current action selections.
         */
        clearActiveSelection() { }

        // Alan 5/11/26 - Clear visible active selections without mutating saved selection sets.
        deselectCurrentSelection() { return 0; }

        // Alan 8/24/26 - Viewer-only clade collapse. Non-destructive: no tips are removed, no
        // topology or branch length changes, nothing is persisted and nothing is recomputed.
        getSelectedCladeNodes() { return []; }
        getSelectedCladeCount() { return 0; }
        getBulkCollapseTargets() { return []; }
        getBulkExpandTargets() { return []; }
        collapseSelectedClades() { return 0; }
        expandSelectedClades() { return 0; }
        hasCollapseUndo() { return false; }
        getCollapseUndoLabel() { return null; }
        undoLastCollapseChange() { return null; }
        clearCollapseUndo() { }

        // Alan 5/12/26 - Remove pruned IDs from saved selection sets without clearing unrelated colors.
        removeIdsFromSelectionSets(ids) { return 0; }

        // Alan 5/12/26 - Apply current temporary selections to the active persistent color group.
        addCurrentSelectionToActiveColorGroup() { return 0; }

        // Alan 5/12/26 - Remove current temporary selections from the active persistent color group.
        removeCurrentSelectionFromActiveColorGroup() { return 0; }

        // Alan 5/12/26 - Clear current temporary selections from every persistent color group.
        clearCurrentSelectionColorGroups() { return 0; }

        // Alan 6/4/26 - Let implementations expose a controller-owned context-menu prune action.
        setPruneNodeHandler(fn) { }

        // Alan 8/13/26 - Let implementations expose a controller-owned context-menu rename action.
        setRenameNodeHandler(fn) { }

        // Alan 6/4/26 - Let implementations expose a controller-owned context-menu copy-name action.
        setCopySequenceNameHandler(fn) { }

        // Alan 6/25/26 - Let implementations expose a controller-owned iNaturalist-number copy action.
        setCopyInaturalistNumbersHandler(fn) { }

        /**
         * Perform a bulk selection action.
         * @param {string} action - One of: 'all', 'none', 'inverse', 'all-leaves', 'all-internal', 'select-filtered'
         * @param {Function|null} predicate - Optional filter function for 'select-filtered'
         * @returns {boolean} True if action was handled
         */
        selectionAction(action, predicate = null) { return false; }

        // =========== SELECTION SET MANAGEMENT ===========

        /**
         * Get names of all available selection sets.
         * @returns {string[]}
         */
        getSelectionSetNames() { return []; }

        /**
         * Set the active selection set for editing.
         * @param {string} name - Name of set to activate
         * @returns {boolean} True if set was activated
         */
        setActiveSelectionSet(name) { return false; }

        /**
         * Get the currently active selection set name.
         * @returns {string}
         */
        getActiveSelectionSet() { return 'Default'; }

        /**
         * Create a new named selection set.
         * @param {string} name - Name for the new set
         * @returns {boolean} True if created
         */
        createSelectionSet(name) { return false; }

        /**
         * Delete a named selection set.
         * @param {string} name - Name of set to delete
         * @returns {boolean} True if deleted
         */
        deleteSelectionSet(name) { return false; }

        /**
         * Get the color assigned to a selection set.
         * @param {string} name - Set name
         * @returns {string|null} CSS color or null
         */
        getSelectionSetColor(name) { return null; }

        // Alan 5/12/26 - Let callers persist user-chosen colors for color groups.
        setSelectionSetColor(name, color) { return false; }

        // Alan 5/12/26 - Let the toolbar suggest a distinct default color for new groups.
        suggestSelectionSetColor() { return '#1f77b4'; }

        // =========== RENDERING & LAYOUT ===========

        /**
         * Render the tree from Newick string.
         * @param {string} newick - Newick format tree string
         */
        async render(newick) { }

        /**
         * Update the tree layout.
         * @param {string|null} layout - 'linear' or 'radial'
         * @param {boolean|null} alignTips - Whether to align tip labels
         */
        updateLayout(layout, alignTips) { }

        /**
         * Update tree spacing.
         * @param {number} xDelta - Horizontal spacing delta
         * @param {number} yDelta - Vertical spacing delta
         */
        updateSpacing(xDelta, yDelta) { }

        // Alan 8/28/26 - Camera zoom is separate from figure spacing and shares D3 touch state.
        zoomCamera(factor) { return false; }

        // Alan 7/17/26 - Let saved display preferences replace spacing instead of accumulating it across tree redraws.
        setSpacingState(x, y) { }

        // Alan 8/28/26 - Expose the mobile mode and node-menu bridge through the common viewer contract.
        /** Switch touch interaction between camera navigation and deliberate selection. */
        setMobileInteractionMode(mode) { }

        /** Open the existing node action menu for the most recently tapped touch target. */
        openMobileNodeActions() { return false; }

        /** Close the touch-oriented presentation of the existing node action menu. */
        closeMobileNodeActions() { }

        /**
         * Set viewer options.
         * @param {Object} newOpts - Options to merge
         */
        setOptions(newOpts) { }

        /**
         * Get tree statistics.
         * @returns {Object} Stats object with supportType, maxSupport, etc.
         */
        getStats() { return { supportType: 'none', maxSupport: 0 }; }

        // Alan 5/9/26 - Add a public metric-filter stats hook so the controller can size sequence filter sliders from the loaded tree.
        getSequenceFilterStats() { return { totalTips: 0, visibleTips: 0, hiddenTips: 0, metricTips: 0, metricsAvailable: false }; }

        // Alan 5/9/26 - Add a public metric-filter setter so toolbar sliders can hide tips without mutating the saved tree.
        setSequenceFilterOptions(newOpts) { return this.getSequenceFilterStats(); }

        // Alan 5/9/26 - Add a public reset hook for restoring all metric-filtered tips in the viewer.
        resetSequenceFilters() { return this.getSequenceFilterStats(); }

        /**
         * Fit the tree to the viewport.
         */
        fitToView() { }

        /**
         * Toggle support value display.
         * @param {boolean} show - Whether to show support values
         */
        toggleSupport(show) { }

        /**
         * Apply text filter to highlight matching nodes.
         * @param {string} text - Filter text
         */
        applyFilter(text) { }

        /**
         * Apply text sizing based on zoom level.
         */
        applyTextSizing() { }
        // Alan 8/17/26 - Let implementations expose the independent tip-label offset.
        setTipLabelGap(value) { }

        /**
         * Sort/ladderize nodes.
         * @param {string} mode - 'asc', 'desc', or 'original'
         */
        sortNodes(mode) { }

        /**
         * Get node ID from a DOM event.
         * @param {Event} event - Click or similar event
         * @returns {string|null} Node ID or null
         */
        getNodeIdFromEvent(event) { return null; }

        /**
         * Export current tree as Newick string.
         * @returns {string} Newick format string
         */
        getNewickString() { return ''; }

        /**
         * Export tree visualization as SVG.
         */
        exportSVG() { }

        /**
         * Export tree visualization as PNG.
         */
        exportPNG() { }
    }

    // Expose the base class globally for potential extension
    window.TreeViewerBase = TreeViewerBase;

    class DikaryaTreeViewer extends TreeViewerBase {
        constructor(elementId, callbacks, initialOptions) {
            super(); // Call base class constructor
            this.elementId = elementId;
            this.container = document.getElementById(elementId);
            this.callbacks = callbacks || {};
            this.options = Object.assign({
                showSupport: true,
                ppThreshold: 0.9,
                bootstrapThreshold: 70,
                minTips: 0,
                // Alan 5/9/26 - Track view-only MycoMap metric filters separately from destructive prune actions.
                queryCoverThreshold: 0,
                // Alan 5/9/26 - Track view-only MycoMap subject coverage filters separately from destructive prune actions.
                subjectCoverThreshold: 0,
                // Alan 7/20/26 - Treat identity as a maximum so lowering the slider removes the most similar hits first.
                identityMaximum: 100,
                // Alan 5/9/26 - Store per-sequence BLAST metrics passed from the job metadata.
                sequenceMetrics: [],
                supportBasePx: 9,
                tipBasePx: 12,
                layout: 'linear',
                alignTips: false
            }, initialOptions);
            // Alan 8/17/26 - Track tip-label spacing independently from branch spacing.
            this.tipLabelGap = 2;
            // Alan 5/9/26 - Build a lookup once so tree tips can be matched to stored BLAST metrics quickly.
            this.sequenceMetricMap = this._buildSequenceMetricMap(this.options.sequenceMetrics);

            this.tree = null;
            this.newick = null;
            this.allNodes = []; // Node Cache

            // State - Persistent Color Groups
            // Alan 5/12/26 - selectionSets is retained as the saved storage name, but now means color groups.
            this.selectionSets = { 'Default': new Set() };
            // Alan 5/12/26 - Persist user-selected colors separately from group membership.
            this.selectionSetColors = { 'Default': '#1f77b4' };
            this.activeSelectionSet = 'Default';
            // Alan 6/2/26 - Focal/sequence-of-interest tip, highlighted directly from durable
            // state (not via the user-editable Default color group). Set via setFocalTip().
            this.focalTipName = null;
            // Alan 5/12/26 - Track temporary action selection separately from persistent color groups.
            this.currentSelectionIds = new Set();
            // Alan 5/11/26 - Track locally hidden current selections so Deselect does not mutate color groups.
            this.hiddenSelectionIds = new Set();
            // Alan 8/24/26 - Selected INTERNAL nodes are tracked separately, by the same stable
            // descendant-tip ID the backend uses for rotate/prune/reroot. currentSelectionIds keys
            // on _getNodeId(), which for an internal node is its support label ("100") and is
            // therefore shared by dozens of unrelated clades -- fine for highlighting, useless as
            // an identity for "collapse exactly the clades I picked".
            this.selectedCladeIds = new Set();
            // Alan 8/24/26 - Resolving those IDs back to nodes costs a traversal that hashes
            // each clade's descendant tips, and the context menu asks for the target set once
            // per label and per enabled/disabled check. The revision counter lets one menu
            // open share a single resolution; it is bumped wherever the selection or the
            // parsed tree changes.
            this._cladeSelectionRevision = 0;
            this._cladeNodeCache = null;
            // Alan 8/24/26 - One-slot record of the last collapse/expand so the viewer can undo it.
            // Collapse is transient display state (node.collapsed), so this never touches the server.
            this._collapseUndo = null;
            // Alan 8/24/26 - Set while a bulk collapse/expand runs so phylotree's per-node
            // 'collapsed' event does not record N single-node undo entries over the bulk one.
            this._bulkCollapseInProgress = false;
            // Color palette from d3.schemeCategory10 for persistent color groups
            this._selectionColors = [
                '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
                '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf'
            ];
            // Alan 5/12/26 - Backward-compatible selectedIds now means temporary action selection.
            Object.defineProperty(this, 'selectedIds', {
                get: () => this.currentSelectionIds,
                set: (val) => { this.currentSelectionIds = val instanceof Set ? val : new Set(val || []); }
            });

            // Spacing State (relative to default)
            this.spacingState = { x: 0, y: 0 };
            // Alan 7/17/26 - Apply automatic large-tree spacing only once so backend redraws retain the current viewer spacing.
            this.automaticSpacingInitialized = false;
            this.spacingTimeout = null;

            // Zoom/UI state
            this.cachedZoomNode = null;
            this.zoomObserver = null;
            this.zoomObservedNodes = [];
            this.rafPending = false;
            this.supportLabelsTimer = null;
            this.lastStats = { supportType: 'none', maxSupport: 0 };
            this.baseSpacing = { x: 20, y: 20 }; // Phylotree fixed_width values (per-node/per-level)
            // Alan 8/16/26 - Track active box-drag state, modifier memory, and removable listeners in one place.
            // The old toggleable "mode" flag is gone: left-drag on empty background always draws the box.
            this.boxSelectState = { drag: null, pointerDownListener: null, contextMenuListener: null, modifierKeyDownListener: null, modifierKeyUpListener: null, modifiers: { alt: false, ctrl: false, meta: false, lastAltDownAt: 0, lastCtrlDownAt: 0, lastMetaDownAt: 0 }, suppressContextMenuUntil: 0 };
            // Alan 8/28/26 - Touch defaults to map-like navigation; explicit Select mode owns
            // selection gestures without feeding camera movement back into spacingState.
            this.mobileInteractionMode = 'navigate';
            this.mobileTouchState = {
                pointers: new Map(), pointerDownListener: null, pointerMoveListener: null,
                pointerUpListener: null, pointerCancelListener: null,
                suppressClickUntil: 0, suppressClickTarget: null, lastTargetNode: null,
                menuDismissListener: null,
                menuActionListener: null
            };
            // Alan 6/2/26 - Controller-supplied handler invoked by the native menu's "Rotate node" item.
            this._onRotateNode = null;
            // Alan 7/17/26 - Controller-supplied handler receives the clicked node and the exact context-menu prune targets.
            this._onPruneNode = null;
            // Alan 8/13/26 - Controller-supplied handler opens Rename for a single selected tip.
            this._onRenameNode = null;
            // Alan 6/4/26 - Controller-supplied handler invoked by the native menu's "Copy sequence name" item.
            this._onCopySequenceName = null;
            // Alan 6/25/26 - Controller-supplied handler invoked by the native menu's iNaturalist-number copy item.
            this._onCopyInaturalistNumbers = null;
            // Alan 7/16/26 - Controller-supplied handler refreshes selected source observations through Mycomap.
            this._onRefreshMycomapRecords = null;
            // Alan 8/15/26 - Persistent layered clade annotations. Membership is stored by
            // canonical leaf ID only; every coordinate is recomputed from the live SVG on redraw.
            this.annotationLayers = [];
            this.cladeAnnotations = [];
            // Alan 8/15/26 - Trailing-debounce handle so a burst of layout/zoom changes redraws once.
            this.annotationRedrawTimer = null;
            // Alan 8/15/26 - Cache measured label widths across redraws, keyed by text plus font.
            this.annotationTextWidthCache = new Map();
            // Alan 8/15/26 - Last computed per-annotation clade validity, read by the manager.
            this.annotationValidity = new Map();
            // Alan 8/15/26 - True only while an annotation group is present in the SVG, so the
            // redraw scheduler can no-op entirely on trees that have never had annotations.
            this._annotationsDrawn = false;
            // Alan 8/15/26 - Controller-supplied handler opens the annotation editor for a clade.
            this._onAddCladeAnnotation = null;
            // Alan 8/21/26 - Controller-supplied handler opens the editor for one drawn annotation,
            // so a right-click on the bracket or label itself edits exactly that annotation.
            this._onEditCladeAnnotation = null;
        }

        // Alan 8/15/26 - Let the controller own the annotation editor while the item lives in
        // phylotree's native internal-node menu, rather than adding a second context-menu system.
        setAddCladeAnnotationHandler(fn) {
            this._onAddCladeAnnotation = typeof fn === 'function' ? fn : null;
        }

        // Alan 8/21/26 - Same arrangement for editing a drawn annotation directly: the viewer
        // knows which annotation was clicked, the controller owns the editor modal.
        setEditCladeAnnotationHandler(fn) {
            this._onEditCladeAnnotation = typeof fn === 'function' ? fn : null;
        }

        // Alan 6/2/26 - Let the controller own the rotate action while the item lives in phylotree's native node menu.
        setRotateNodeHandler(fn) {
            this._onRotateNode = typeof fn === 'function' ? fn : null;
        }

        // Alan 6/4/26 - Let the controller own pruning while the item lives in phylotree's native node menu.
        setPruneNodeHandler(fn) {
            this._onPruneNode = typeof fn === 'function' ? fn : null;
        }

        // Alan 8/13/26 - Let the controller reuse its existing rename modal from the native tip menu.
        setRenameNodeHandler(fn) {
            this._onRenameNode = typeof fn === 'function' ? fn : null;
        }

        // Alan 6/4/26 - Let the controller own clipboard copying while the item lives in phylotree's native node menu.
        setCopySequenceNameHandler(fn) {
            this._onCopySequenceName = typeof fn === 'function' ? fn : null;
        }

        // Alan 6/25/26 - Let the controller own iNaturalist-number clipboard copying from the native node menu.
        setCopyInaturalistNumbersHandler(fn) {
            this._onCopyInaturalistNumbers = typeof fn === 'function' ? fn : null;
        }

        // Alan 7/16/26 - Let the controller own API-backed Mycomap refreshes from the native tip menu.
        setRefreshMycomapRecordsHandler(fn) {
            this._onRefreshMycomapRecords = typeof fn === 'function' ? fn : null;
        }

        // Alan 8/28/26 - Coordinate the wrapper touch mode and reuse phylotree's existing node action menu.
        setMobileInteractionMode(mode) {
            const next = mode === 'select' ? 'select' : 'navigate';
            this.mobileInteractionMode = next;
            this._cancelBoxSelectDrag();
            this.mobileTouchState?.pointers?.clear();
            this.container?.classList.toggle('mobile-select-mode', next === 'select');
            return next;
        }

        openMobileNodeActions() {
            const node = this.mobileTouchState?.lastTargetNode;
            if (!node || !this.tree?.display?.handle_node_click || !this.container) return false;
            const rect = this.container.getBoundingClientRect();
            document.body.classList.add('tree-mobile-node-menu-open');
            this.tree.display.handle_node_click(node, {
                clientX: rect.left + Math.min(rect.width * 0.5, 240),
                clientY: rect.top + Math.min(rect.height * 0.5, 240)
            });
            this._attachMobileMenuDismissListener();
            return true;
        }

        closeMobileNodeActions() {
            this._closeMobileNodeMenu();
        }

        _attachMobileMenuDismissListener() {
            const state = this.mobileTouchState;
            if (!state || state.menuDismissListener) return;
            state.menuDismissListener = (event) => {
                const menu = this.container?.querySelector('.phylotree-context-menu');
                if (menu?.contains(event.target)) return;
                this._closeMobileNodeMenu();
            };
            state.menuActionListener = (event) => {
                const menu = this.container?.querySelector('.phylotree-context-menu');
                if (!menu?.contains(event.target)) return;
                // Let phylotree's action handler finish before removing the shared mobile state.
                setTimeout(() => this._closeMobileNodeMenu(), 0);
            };
            setTimeout(() => document.addEventListener('pointerdown', state.menuDismissListener, true), 0);
            document.addEventListener('click', state.menuActionListener);
        }

        _closeMobileNodeMenu() {
            const menu = this.container?.querySelector('.phylotree-context-menu');
            if (menu) menu.style.display = 'none';
            document.body.classList.remove('tree-mobile-node-menu-open');
            const state = this.mobileTouchState;
            if (state?.menuDismissListener) {
                document.removeEventListener('pointerdown', state.menuDismissListener, true);
                state.menuDismissListener = null;
            }
            if (state?.menuActionListener) {
                document.removeEventListener('click', state.menuActionListener);
                state.menuActionListener = null;
            }
        }

        // Alan 6/2/26 - Highlight the focal/sequence-of-interest tip directly from durable state,
        // independent of the user-editable color groups. Re-styles in place (no full redraw).
        setFocalTip(name) {
            const next = (typeof name === 'string' && name) ? name : null;
            if (this.focalTipName === next) return;
            this.focalTipName = next;
            if (this.tree && typeof this._updateNodeStylesOnly === 'function') this._updateNodeStylesOnly();
        }

        async render(newick) {
            this.newick = newick;
            if (!this.container) return;

            // 1. CLEAR & SETUP
            this.container.innerHTML = '';

            if (!window.d3v7) {
                this.container.innerHTML = '<div class="p-4 bg-red-50 text-red-800 rounded">D3 v7 is required.</div>';
                return;
            }
            const phylotreeLib = window.phylotree;
            if (!phylotreeLib) {
                this.container.innerHTML = '<div class="p-4 bg-red-50 text-red-800 rounded">Phylotree library not loaded.</div>';
                return;
            }

            // 2. CREATE TREE
            this.tree = new phylotreeLib.phylotree(newick);

            // Alan 8/24/26 - Refuse to render a model the parser never actually built.
            // Throwing here reaches loadTreeNow()'s catch in tree_viewer_controller.js,
            // which prints the reason into the container and ships it to the server log,
            // so a corrupt tree/tree_original.newick is a legible failure rather than an
            // empty canvas that looks like a tree with nothing in it.
            const parseFailure = describeTreeParseFailure(this.tree);
            if (parseFailure) {
                this.tree = null;
                this.allNodes = [];
                throw new Error(parseFailure);
            }

            // FastTree resolves zero-length polytomies into arbitrary binary ladders. Compact
            // each complete zero-length tip component before recording "original" order, so
            // Sort -> Original and later reloads both retain the useful grouped presentation.
            this._groupZeroLengthPolytomies();

            // Tag original order per-parent for correct restoration
            this.tree.traverse_and_compute(n => {
                // Alan 7/15/26 - Hide MAFFT and RiC annotations while retaining the saved tip ID for tree edits.
                const data = n.data || n;
                const isTip = !n.children || n.children.length === 0;
                const displayName = isTip ? cleanTipDisplayName(data.name) : data.name;
                if (typeof data.name === 'string' && displayName !== data.name) {
                    const originalName = data.name;
                    if (!data.__original_name) data.__original_name = originalName;
                    if (!n.__original_name) n.__original_name = originalName;
                    data.name = displayName;
                    if (n.name !== undefined) n.name = displayName;
                }
                // Alan 7/17/26 - Register a count-aware prune item that passes the same resolved nodes used by its label.
                if (n.parent) {
                    n.menu_items = n.menu_items || [];
                    // Alan 8/17/26 - Priority custom item: phylotree renders the optional fifth-field items
                    // before its built-in collapse/selection actions.
                    n.menu_items.unshift([
                        (node) => {
                            // Alan 8/21/26 - Label the item for the membership it will actually
                            // annotate, which may be the selected clade rather than the clicked tip.
                            const memberIds = this.getAnnotationTargetLeafIds(node);
                            const count = this.getAnnotationsForMemberIds(memberIds).length;
                            if (count === 1) return 'Edit annotation…';
                            if (count > 1) return `Edit annotations… (${count})`;
                            return this._isSelectionAnnotationTarget(node)
                                // Alan 8/21/26 - Say so when the action will use the selection.
                                ? `Add annotation to ${memberIds.length} selected…`
                                : 'Add annotation…';
                        },
                        (node) => {
                            // Alan 8/21/26 - Annotate the selected clade when the clicked tip is part of it.
                            const memberIds = this.getAnnotationTargetLeafIds(node);
                            const annotationIds = this.getAnnotationsForMemberIds(memberIds)
                                .map((annotation) => annotation.id).filter(Boolean);
                            if (typeof this._onAddCladeAnnotation === 'function') {
                                this._onAddCladeAnnotation(node, memberIds, {
                                    annotationIds,
                                    // Alan 8/21/26 - Let a multi-tip selection default to Clade line.
                                    defaultType: this.getDefaultAnnotationType(node, memberIds)
                                });
                            }
                        },
                        (node) => !window.VIEW_ONLY && Boolean(node.parent)
                            && this.getDescendantLeafIds(node).length > 0,
                        false,
                        true
                    ], [
                        // Alan 8/17/26 - Keep an explicit creation path once one or more
                        // annotations already occupy this incoming branch.
                        () => 'Add another annotation…',
                        (node) => {
                            // Alan 8/21/26 - Stack onto the same membership the primary item uses.
                            const memberIds = this.getAnnotationTargetLeafIds(node);
                            if (typeof this._onAddCladeAnnotation === 'function') {
                                this._onAddCladeAnnotation(node, memberIds, {
                                    forceAdd: true,
                                    // Alan 8/21/26 - Let a multi-tip selection default to Clade line.
                                    defaultType: this.getDefaultAnnotationType(node, memberIds)
                                });
                            }
                        },
                        (node) => !window.VIEW_ONLY && Boolean(node.parent)
                            // Alan 8/21/26 - Offer stacking against the resolved membership.
                            && this.getAnnotationsForMemberIds(
                                this.getAnnotationTargetLeafIds(node)
                            ).length > 0,
                        false,
                        true
                    ]);
                    n.menu_items.push([
                        // Alan 7/17/26 - Show how many terminal sequences the context action will remove.
                        (node) => this._getPruneMenuLabel(node),
                        // Alan 7/17/26 - Prune all selected tips when multiple tips are selected, otherwise prune the clicked node.
                        (node) => {
                            // Alan 7/17/26 - Resolve targets at click time so the action remains aligned with the displayed menu count.
                            const nodes = this._getContextPruneNodes(node);
                            // Alan 7/17/26 - Give the controller the resolved bulk target list for the backend prune request.
                            if (typeof this._onPruneNode === 'function') this._onPruneNode(node, nodes);
                        },
                        () => !window.VIEW_ONLY
                    ]);
                    // Alan 8/13/26 - Put Rename below Prune for an unselected tip or the sole selected tip.
                    if (!n.children || n.children.length === 0) {
                        n.menu_items.push([
                            () => 'Rename 1 node',
                            (node) => {
                                const renameNode = this._getContextRenameNode(node);
                                if (renameNode && typeof this._onRenameNode === 'function') this._onRenameNode(renameNode);
                            },
                            (node) => !window.VIEW_ONLY && Boolean(this._getContextRenameNode(node))
                        ]);
                    }
                }
                // Alan 7/14/26 - Copy the clicked sequence name or all visible selected sequence names from terminal nodes.
                if (!n.children || n.children.length === 0) {
                    n.menu_items = n.menu_items || [];
                    n.menu_items.push([
                        (node) => this._getSequenceNameCopyMenuLabel(node),
                        (node) => {
                            const nodes = this._getContextSequenceNameNodes(node);
                            if (typeof this._onCopySequenceName === 'function') this._onCopySequenceName(node, nodes);
                        },
                        (node) => this._getContextSequenceNameNodes(node).length > 0
                    ]);
                    // Alan 6/25/26 - Offer iNaturalist-number copying when the clicked tip or selected tips expose observation IDs.
                    n.menu_items.push([
                        (node) => this._getInaturalistCopyMenuLabel(node),
                        (node) => {
                            const numbers = this._getContextInaturalistNumbers(node);
                            if (typeof this._onCopyInaturalistNumbers === 'function') this._onCopyInaturalistNumbers(node, numbers);
                        },
                        (node) => this._getContextInaturalistNumbers(node).length > 0
                    ]);
                    // Alan 7/16/26 - Refresh one clicked or multiple selected iNaturalist/Mushroom Observer records in Mycomap.
                    n.menu_items.push([
                        (node) => this._getMycomapRefreshMenuLabel(node),
                        (node) => {
                            const nodes = this._getContextMycomapRecordNodes(node);
                            if (typeof this._onRefreshMycomapRecords === 'function') this._onRefreshMycomapRecords(node, nodes);
                        },
                        (node) => !window.VIEW_ONLY && this._getContextMycomapRecordReferences(node).length > 0,
                        // Alan 8/13/26 - Keep the currently broken refresh action visible but unavailable.
                        true
                    ]);
                }
                // Alan 8/17/26 - Annotation actions are registered for every parented node above;
                // internal-node-specific registration here now only adds rotation.
                if (n.children) {
                    n.children.forEach((c, i) => c.__original_index = i);
                    // Alan 6/2/26 - Register "Rotate node" as a native phylotree internal-node menu item so it
                    // coexists with Collapse/Select instead of being shadowed by a capture-phase handler.
                    if (n.children.length >= 2) {
                        n.menu_items = n.menu_items || [];
                        n.menu_items.push([
                            () => 'Rotate node',
                            (node) => { if (typeof this._onRotateNode === 'function') this._onRotateNode(node); },
                            (node) => Boolean(node.children && node.children.length >= 2)
                        ]);
                        // Alan 8/24/26 - Bulk collapse/expand of the SELECTED clades. These act on
                        // the selection, not on the clicked node, so they are registered on every
                        // internal node and only appear once two or more clades are selected -- a
                        // single clade is already served by phylotree's own Collapse Clade item.
                        // They are priority items so they sit next to it rather than at the bottom.
                        //
                        // Three items rather than one toggle: when the selected clades are all
                        // expanded (or all collapsed) one unambiguous counted action is clearer,
                        // and when the selection is MIXED a toggle would have to guess, so both
                        // directions are offered explicitly.
                        n.menu_items.push([
                            () => {
                                const collapseCount = this.getBulkCollapseTargets().length;
                                if (collapseCount) {
                                    return `Collapse ${collapseCount} Clade${collapseCount === 1 ? '' : 's'}`;
                                }
                                const expandCount = this.getBulkExpandTargets().length;
                                return `Expand ${expandCount} Clade${expandCount === 1 ? '' : 's'}`;
                            },
                            () => {
                                if (this.getBulkCollapseTargets().length) this.collapseSelectedClades();
                                else this.expandSelectedClades();
                            },
                            () => this.getSelectedCladeCount() >= 2
                                && !(this.getBulkCollapseTargets().length && this.getBulkExpandTargets().length),
                            false,
                            true
                        ], [
                            () => 'Collapse Selected Clades',
                            () => this.collapseSelectedClades(),
                            () => this.getSelectedCladeCount() >= 2
                                && Boolean(this.getBulkCollapseTargets().length)
                                && Boolean(this.getBulkExpandTargets().length),
                            () => this.getBulkCollapseTargets().length === 0,
                            true
                        ], [
                            () => 'Expand Selected Clades',
                            () => this.expandSelectedClades(),
                            () => this.getSelectedCladeCount() >= 2
                                && Boolean(this.getBulkCollapseTargets().length)
                                && Boolean(this.getBulkExpandTargets().length),
                            () => this.getBulkExpandTargets().length === 0,
                            true
                        ]);
                        // Alan 8/17/26 - Annotation creation now stays in the shared priority menu above.
                    }
                }
            });

            // 2b. Cache Nodes & Compute Metadata (Flatten operations)
            this._cacheNodes();

            // Alan 7/17/26 - Give large trees more initial vertical room so tip labels do not appear crowded.
            this._initializeAutomaticSpacing();

            // 2c. Initial Selection Processing
            // Alan 5/12/26 - Full tree reload clears only temporary action selection; color groups restore below.
            this.currentSelectionIds.clear();
            // Alan 5/12/26 - Reset transient hidden state tied to temporary action selection.
            this.hiddenSelectionIds.clear();
            // Alan 8/24/26 - A reload replaces every node object, so both the clade registry and
            // the collapse undo entry (which holds node references) describe a tree that is gone.
            this.selectedCladeIds.clear();
            this._collapseUndo = null;
            this._invalidateCladeSelection();

            // 3. COMPUTE STATS
            this.lastStats = this._computeSupportStats();
            if (this.container) this.container.__treeStats = this.lastStats;

            // Alan 5/9/26 - Apply any existing sequence metric filter state before the first draw.
            this._applySequenceFilters({ updateDisplay: false });

            // 4. DRAW
            this._draw();
        }

        /**
         * Traverses tree once to populate flat cache and pre-compute metadata.
         * Replaces multiple ad-hoc traversals.
         */
        _cacheNodes() {
            if (!this.tree) {
                this.allNodes = [];
                return;
            }

            // ALWAYS use manual traversal to ensure we get a proper array
            // this.tree.get_nodes() might return a generator or non-array
            this.allNodes = [];
            this.tree.traverse_and_compute(n => this.allNodes.push(n));

            // Post-order helper for leaf count
            // We can't rely on allNodes order being post-order, so we recurse from roots.
            const compute = (n) => {
                n.__leafCount = 0;
                // Store original index for sibling restoration
                if (n.children) {
                    n.children.forEach((c, i) => c.__original_index = i);
                }

                if (!n.children || !n.children.length) {
                    n.__leafCount = 1;
                    return 1;
                }
                let c = 0;
                n.children.forEach(kid => c += compute(kid));
                n.__leafCount = c;
                return c;
            };

            // Find roots (nodes without parents)
            const roots = this.allNodes.filter(n => !n.parent);
            if (roots.length === 0 && this.allNodes.length > 0) {
                // Should not happen for valid tree, but fallback:
                try { compute(this.allNodes[0]); } catch (e) { }
            } else {
                roots.forEach(r => compute(r));
            }
            // Alan 5/9/26 - Attach stored BLAST metrics to leaf nodes after names are available.
            this._attachSequenceMetricsToLeaves();
        }

        _branchLength(node) {
            const raw = node?.data?.attribute ?? node?.attribute ?? node?.branch_length;
            if (raw === null || raw === undefined || raw === '') return null;
            const value = Number(raw);
            return Number.isFinite(value) ? value : null;
        }

        _hasZeroLengthIncomingBranch(node) {
            const length = this._branchLength(node);
            return length !== null && Math.abs(length) <= ZERO_LENGTH_POLYTOMY_EPSILON;
        }

        /**
         * Return the zero-length connected component containing one tip. Contracting these
         * edges is the viewer's model of a soft polytomy: positive-length side clades are not
         * members even when FastTree happened to attach them inside its binary resolution.
         */
        _zeroLengthPolytomyForTip(tip) {
            if (!tip) return null;
            const component = new Set([tip]);
            const pending = [tip];
            while (pending.length) {
                const node = pending.pop();
                if (node.parent && this._hasZeroLengthIncomingBranch(node)
                    && !component.has(node.parent)) {
                    component.add(node.parent);
                    pending.push(node.parent);
                }
                for (const child of (node.children || node.data?.children || [])) {
                    if (this._hasZeroLengthIncomingBranch(child) && !component.has(child)) {
                        component.add(child);
                        pending.push(child);
                    }
                }
            }
            const tips = Array.from(component).filter((node) => {
                const children = node.children || node.data?.children || [];
                return !children.length;
            });
            if (tips.length < 2) return null;
            const memberIds = tips.map((node) => this._getNodeId(node)).filter(Boolean);
            if (memberIds.length !== tips.length) return null;
            const anchor = Array.from(component).find((node) => !component.has(node.parent)) || null;
            return { nodes: component, tips, memberIds, anchor };
        }

        _zeroLengthPolytomyForMemberIds(memberIds) {
            const wanted = new Set(Array.isArray(memberIds) ? memberIds.filter(Boolean) : []);
            if (wanted.size < 2) return null;
            const tip = this.allNodes.find((node) => {
                const children = node.children || node.data?.children || [];
                return !children.length && wanted.has(this._getNodeId(node));
            });
            const polytomy = this._zeroLengthPolytomyForTip(tip);
            if (!polytomy || polytomy.memberIds.length !== wanted.size
                || !polytomy.memberIds.every((id) => wanted.has(id))) return null;
            return polytomy;
        }

        _tipOrderFromModel() {
            const ids = [];
            if (!this.tree) return ids;
            this.tree.traverse_and_compute((node) => {
                const children = node.children || node.data?.children || [];
                if (children.length) return;
                const id = this._getNodeId(node);
                if (id) ids.push(id);
            });
            return ids;
        }

        _selectionRunCount(order, members) {
            const wanted = members instanceof Set ? members : new Set(members || []);
            let runs = 0;
            let inside = false;
            for (const id of order) {
                const selected = wanted.has(id);
                if (selected && !inside) runs += 1;
                inside = selected;
            }
            return runs;
        }

        /**
         * Find the best combination of ordinary node rotations for one member set. The dynamic
         * program retains only the cheapest ordering for each selected/unselected boundary
         * state, so a binary FastTree with hundreds of tips stays linear in practice.
         */
        _bestRotationsForLeafSet(root, memberIds) {
            const wanted = memberIds instanceof Set ? memberIds : new Set(memberIds || []);
            const better = (candidate, current) => !current
                || candidate.transitions < current.transitions
                || (candidate.transitions === current.transitions
                    && candidate.rotations.length < current.rotations.length);

            const solve = (node) => {
                const children = node.children || node.data?.children || [];
                if (!children.length) {
                    const selected = wanted.has(this._getNodeId(node));
                    return new Map([[`${Number(selected)}:${Number(selected)}`, {
                        first: selected, last: selected, transitions: 0, rotations: []
                    }]]);
                }
                const childOptions = children.map((child) => Array.from(solve(child).values()));
                const combinations = [];
                const collect = (index, chosen) => {
                    if (index === childOptions.length) {
                        combinations.push(chosen.slice());
                        return;
                    }
                    childOptions[index].forEach((option) => {
                        chosen.push(option); collect(index + 1, chosen); chosen.pop();
                    });
                };
                collect(0, []);
                const result = new Map();
                const orientations = children.length > 1 ? [false, true] : [false];
                combinations.forEach((chosen) => orientations.forEach((reversed) => {
                    const ordered = reversed ? chosen.slice().reverse() : chosen;
                    let transitions = ordered.reduce((sum, item) => sum + item.transitions, 0);
                    for (let i = 1; i < ordered.length; i += 1) {
                        if (ordered[i - 1].last !== ordered[i].first) transitions += 1;
                    }
                    const rotations = ordered.flatMap((item) => item.rotations);
                    if (reversed) rotations.push(node);
                    const candidate = {
                        first: ordered[0].first,
                        last: ordered[ordered.length - 1].last,
                        transitions,
                        rotations
                    };
                    const key = `${Number(candidate.first)}:${Number(candidate.last)}`;
                    if (better(candidate, result.get(key))) result.set(key, candidate);
                }));
                return result;
            };

            const options = Array.from(solve(root).values());
            return options.reduce((best, option) => better(option, best) ? option : best, null);
        }

        _groupZeroLengthPolytomies() {
            if (!this.tree) return 0;
            const nodes = [];
            this.tree.traverse_and_compute((node) => nodes.push(node));
            const root = nodes.find((node) => !node.parent);
            if (!root) return 0;

            const components = new Map();
            nodes.forEach((node) => {
                const children = node.children || node.data?.children || [];
                if (children.length) return;
                const polytomy = this._zeroLengthPolytomyForTip(node);
                if (!polytomy) return;
                const key = this._annotationMembershipKey(polytomy.memberIds);
                if (key && !components.has(key)) components.set(key, new Set(polytomy.memberIds));
            });
            const groups = Array.from(components.values()).sort((a, b) => b.size - a.size);
            const totalRuns = () => {
                const order = this._tipOrderFromModel();
                return groups.reduce((sum, group) => sum + this._selectionRunCount(order, group), 0);
            };

            let score = totalRuns();
            let grouped = 0;
            groups.forEach((group) => {
                const order = this._tipOrderFromModel();
                if (this._selectionRunCount(order, group) <= 1) return;
                const plan = this._bestRotationsForLeafSet(root, group);
                if (!plan || !plan.rotations.length) return;
                plan.rotations.forEach((node) => node.children.reverse());
                const nextScore = totalRuns();
                if (nextScore < score && this._selectionRunCount(this._tipOrderFromModel(), group) === 1) {
                    score = nextScore;
                    grouped += 1;
                } else {
                    // Reversing the same nodes again restores the exact prior order.
                    plan.rotations.slice().reverse().forEach((node) => node.children.reverse());
                }
            });
            return grouped;
        }

        _draw() {
            if (!this.tree) return;

            // Cleanup observers
            this._cleanupZoomObserver();
            this.cachedZoomNode = null; // Clear cached node on redraw
            // Alan 8/28/26 - A node object belongs to one render, so clear the mobile action
            // target and close its menu before replacing the topology/layout DOM.
            if (this.mobileTouchState) {
                this.mobileTouchState.lastTargetNode = null;
                this.container.dispatchEvent(new CustomEvent('dikarya:mobile-target-change', {
                    detail: { available: false }
                }));
            }
            this._closeMobileNodeMenu();
            if (this.supportLabelsTimer) {
                clearTimeout(this.supportLabelsTimer);
                this.supportLabelsTimer = null;
            }

            this.container.innerHTML = '';

            // Phylotree uses 'fixed-step' mode where spacing_x/spacing_y set the per-node/per-level pixel values
            // The render options should use 'fixed-step' and we apply spacing post-render
            const renderOpts = {
                container: "#" + this.elementId,
                'is-radial': (this.options.layout === 'radial'),
                'align-tips': this.options.alignTips,
                'draw-size-bubbles': false,
                'zoom': true,
                // Alan 8/16/26 - Keep phylotree's built-in brush off; our own left-drag box select replaces it.
                'brush': false,
                // Alan 8/17/26 - Include custom tip-label spacing in phylotree's SVG padding.
                'tip-label-gap': this.tipLabelGap,
                'left-right-spacing': 'fixed-step',
                'top-bottom-spacing': 'fixed-step',
                'node-styler': (element, node) => this._styleNode(element, node)
            };

            // D3 Version Lock
            if (!window.__dikarya_d3_locked_to_v7) {
                window.__dikarya_d3_locked_to_v7 = true;
                window.d3 = window.d3v7;
            }

            try {
                const renderer = this.tree.render(renderOpts);

                // Alan 7/14/26 - Expose visible selected tips to the native source-record menu for bulk observation links.
                if (renderer) renderer.recordTargetNodesProvider = () => this.getSelectedNodes();

                if (this.container.children.length === 0 && renderer) {
                    if (renderer.element) this.container.appendChild(renderer.element);
                    else if (renderer.svg) this.container.appendChild(renderer.svg.node());
                }

                // Apply spacing via phylotree's native API
                this._applySpacingViaAPI();

                this.supportLabelsTimer = setTimeout(() => this._addSupportLabels(), 150);
                this._attachEventHandlers();

                // Hook into phylotree's native selection system
                this._hookPhylotreeSelection();

                // Alan 8/24/26 - Watch the native Collapse/Expand Clade menu item so a single
                // collapse is undoable and so Dikarya's support labels repaint with it.
                this._hookNativeCollapseEvents();

                // Override click behavior: left-click = select, shift/right = menu
                this._overrideClickBehavior();
                // Alan 5/11/26 - Attach background box-select after phylotree handlers so it can suppress pan only for box gestures.
                this._attachBoxSelectHandlers();
                // Alan 8/28/26 - Reattach the removable touch wrapper after phylotree replaces the SVG.
                this._attachTouchInteractionHandlers();
                // Alan 5/12/26 - Repaint labels immediately so selection-set colors beat base light/dark label CSS.
                this._updateNodeStylesOnly();

                // Alan 5/8/26 - Disable wheel-to-zoom so mouse wheel scrolls the page instead of zooming the tree.
                // Intercept wheel events before D3 and let the page scroll instead.
                const svgEl = this.container.querySelector('svg');
                if (svgEl) {
                    // Alan 7/17/26 - Keep double-clicks on the tree from triggering D3's default zoom-in gesture.
                    window.d3v7.select(svgEl).on('dblclick.zoom', null);
                    svgEl.addEventListener('wheel', (e) => {
                        if (!e.ctrlKey) {
                            e.stopImmediatePropagation();
                        }
                    }, { capture: true, passive: true });
                }

            } catch (e) {
                console.error("Render error:", e);
                this.container.innerHTML = `<div class="p-4 bg-red-50 text-red-800 dark:bg-red-900/30 dark:text-red-200 rounded">Render Error: ${e.message}</div>`;
            }
        }

        _applySpacingViaAPI() {
            const display = this.tree?.display;
            if (!display) return;

            try {
                // Calculate absolute spacing values (base + accumulated state)
                // Phylotree constraints: X: 2-100, Y: 10-100
                const targetX = Math.max(2, Math.min(100, (this.baseSpacing?.x || 20) + (this.spacingState?.x || 0)));
                const targetY = Math.max(10, Math.min(100, (this.baseSpacing?.y || 20) + (this.spacingState?.y || 0)));

                if (DEBUG_MODE) console.log('Applying spacing via API:', { targetX, targetY, stateX: this.spacingState.x, stateY: this.spacingState.y });

                // Set absolute values (not relative/delta)
                // Note: phylotree uses inverted conventions - spacing_x affects vertical, spacing_y affects horizontal
                if (typeof display.spacing_x === 'function') {
                    display.spacing_x(targetY, true); // Y state -> spacing_x (vertical)
                }
                if (typeof display.spacing_y === 'function') {
                    display.spacing_y(targetX, true); // X state -> spacing_y (horizontal)
                }

                // Now trigger layout recalculation
                if (typeof display.update === 'function') {
                    display.update();
                }
            } catch (e) {
                console.error('Error applying spacing via API:', e);
            }
        }

        // Alan 7/18/26 - Keep all trees at the compact established vertical spacing regardless of tip count.
        _recommendedVerticalSpacingDelta() {
            // Alan 7/18/26 - Let users expand spacing manually instead of adding automatic gaps to large trees.
            return 0;
        }

        // Alan 7/17/26 - Set the automatic spacing before the first draw without overwriting later user adjustments.
        _initializeAutomaticSpacing() {
            // Alan 7/17/26 - Skip repeat initialization when the tree reloads after an edit.
            if (this.automaticSpacingInitialized) return;
            // Alan 7/17/26 - Store the recommendation as the same relative vertical value used by toolbar controls.
            this.spacingState.y = this._recommendedVerticalSpacingDelta();
            // Alan 7/17/26 - Remember initialization so reroot, prune, and rename redraws keep the current spacing.
            this.automaticSpacingInitialized = true;
        }





        // --- PUBLIC API FOR CONTROLLER ---

        setOptions(newOpts) {
            Object.assign(this.options, newOpts);
            // Decide if we need full redraw or implemented partial update
            // For thresholds, we can just re-run support labels
            this._addSupportLabels();
            this._updateNodeStylesOnly(); // in case filter logic uses options
        }

        updateLayout(layout, alignTips) {
            let changed = false;
            // Handle toggle logic safely
            if (layout && layout !== this.options.layout) {
                this.options.layout = layout;
                changed = true;
            }
            if (typeof alignTips === 'boolean' && alignTips !== this.options.alignTips) {
                this.options.alignTips = alignTips;
                changed = true;
            }
            if (changed) {
                // Reset spacing state on layout change prevents weird accumulations
                // Alan 7/17/26 - Retain the large-tree vertical recommendation when a layout change resets manual spacing.
                this.spacingState = { x: 0, y: this._recommendedVerticalSpacingDelta() };
                this._draw();
            }
        }

        updateSpacing(xDelta, yDelta) {
            // Update accumulated state
            this.spacingState.x += xDelta;
            this.spacingState.y += yDelta;

            if (DEBUG_MODE) console.log('Spacing update:', { x: this.spacingState.x, y: this.spacingState.y, xDelta, yDelta });

            // Try incremental update via phylotree's native API (faster than full redraw)
            const display = this.tree?.display;
            if (display && typeof display.spacing_x === 'function' && typeof display.spacing_y === 'function') {
                try {
                    // Calculate new absolute values
                    const targetX = Math.max(2, Math.min(100, (this.baseSpacing?.x || 20) + this.spacingState.x));
                    const targetY = Math.max(10, Math.min(100, (this.baseSpacing?.y || 20) + this.spacingState.y));

                    if (DEBUG_MODE) console.log('Incremental spacing update:', { targetX, targetY });

                    // Note: phylotree uses inverted conventions - spacing_x affects vertical, spacing_y affects horizontal
                    display.spacing_x(targetY, true); // Y state -> spacing_x (vertical)
                    display.spacing_y(targetX, true); // X state -> spacing_y (horizontal)

                    if (typeof display.update === 'function') {
                        display.update();
                        // Re-add support labels after layout change
                        if (this.spacingTimeout) clearTimeout(this.spacingTimeout);
                        this.spacingTimeout = setTimeout(() => {
                            this._addSupportLabels();
                            this._applyTextSizingFromZoom();
                        }, 100);
                        return; // Success!
                    }
                } catch (e) {
                    if (DEBUG_MODE) console.log('Incremental spacing failed, falling back to full redraw:', e);
                }
            }

            // Fallback: full redraw
            this._draw();
        }

        // Alan 8/28/26 - Keep +/- on phylotree's D3 behavior so buttons and pinch update the
        // same __zoom transform; no spacing state or redraw participates in camera zoom.
        zoomCamera(factor) {
            const display = this.tree?.display;
            return Boolean(display && typeof display.zoom_by === 'function' && display.zoom_by(factor));
        }

        // Alan 7/17/26 - Replace the relative spacing state so persisted preferences do not compound after redraws.
        setSpacingState(x, y) {
            // Alan 7/17/26 - Normalize malformed stored horizontal values to the established zero offset.
            const nextX = Number.isFinite(Number(x)) ? Number(x) : 0;
            // Alan 7/17/26 - Normalize malformed stored vertical values to the established zero offset.
            const nextY = Number.isFinite(Number(y)) ? Number(y) : 0;
            // Alan 7/17/26 - Reuse incremental rendering with only the difference from the current state.
            this.updateSpacing(nextX - this.spacingState.x, nextY - this.spacingState.y);
        }

        applyTextSizing() {
            this._applyTextSizingFromZoom();
        }

        // Alan 8/17/26 - Keep the independent tip-label offset in the requested 0-80 px,
        // two-pixel-step range and repaint without changing branch geometry.
        setTipLabelGap(value) {
            const numeric = Number(value);
            const clamped = Number.isFinite(numeric)
                ? Math.max(0, Math.min(80, Math.round(numeric / 2) * 2))
                : 2;
            this.tipLabelGap = clamped;
            const output = document.getElementById('tip-label-gap-value');
            if (output) output.textContent = String(clamped);
            // Alan 8/17/26 - Ask phylotree to recompute its label padding immediately so a
            // large gap cannot push labels beyond the fixed-step SVG viewport.
            const display = this.tree?.display;
            if (display) {
                display.options['tip-label-gap'] = clamped;
                // Alan 8/17/26 - Reset phylotree's raw size before resizeSvg adds padding,
                // preventing repeated gap clicks from compounding the current SVG dimensions.
                if (typeof display.placenodes === 'function') display.placenodes();
                if (typeof display.resizeSvg === 'function') {
                    display.resizeSvg(display.phylotree, display.svg, false);
                }
            }
            this._applyTextSizingFromZoom();
            return clamped;
        }

        // Alan 8/17/26 - Convert the saved screen-space gap into an outward SVG offset.
        _tipLabelDx(node, zoomScale = 1) {
            const direction = node?.text_align === 'end' ? -1 : 1;
            return direction * this.tipLabelGap / (zoomScale || 1);
        }

        sortNodes(mode) {
            if (!this.tree) return;

            // Metric: Total Descendants (clade size)
            const countDescendants = (node) => {
                if (node.__total_descendants !== undefined) return node.__total_descendants;
                if (!node.children || !node.children.length) return 0;
                let c = 0;
                node.children.forEach(child => c += 1 + countDescendants(child));
                node.__total_descendants = c;
                return c;
            };

            // This method exists in most v2 builds. If not, we can't sort easily without re-implementing.
            // Check for both camelCase and snake_case versions (phylotree builds vary)
            const resortFn = typeof this.tree.resortChildren === 'function'
                ? this.tree.resortChildren.bind(this.tree)
                : (typeof this.tree.resort_children === 'function'
                    ? this.tree.resort_children.bind(this.tree)
                    : null);

            if (resortFn) {
                resortFn((a, b) => {
                    if (mode === 'original') return (a.__original_index || 0) - (b.__original_index || 0);
                    const valA = countDescendants(a);
                    const valB = countDescendants(b);
                    return (mode === 'asc') ? valA - valB : valB - valA;
                });

                // Incremental update if possible
                if (this.tree.display && typeof this.tree.display.update === 'function') {
                    // Alan 5/9/26 - Reapply sequence metric filters after ladderizing so hidden tips stay hidden.
                    this._applySequenceFilters({ updateDisplay: false });
                    this.tree.display.update();
                    this._addSupportLabels(); // Re-position labels
                } else {
                    this._draw();
                }
            } else {
                console.warn("tree.resortChildren / tree.resort_children not found");
            }
        }

        fitToView() {
            // Ideally: manipulate zoom transform.
            // Phylotree v2 usually resets zoom on redraw.
            this._draw();
        }

        // Alan 5/11/26 - Apply persisted tip renames after loading Newick while preserving original IDs for edit actions.
        applyRenames(renames = {}) {
            if (!this.tree || !renames || typeof renames !== 'object') return;
            let changed = false;
            this.tree.traverse_and_compute(node => {
                const data = node.data || node;
                const originalName = data.__original_name || node.__original_name || data.name || node.name;
                if (!originalName) return;
                if (!data.__original_name) data.__original_name = originalName;
                if (!node.__original_name) node.__original_name = originalName;
                if (Object.prototype.hasOwnProperty.call(renames, originalName)) {
                    // Alan 7/15/26 - Keep persisted rename labels free of pipeline-only MAFFT and RiC annotations too.
                    const displayName = cleanTipDisplayName(renames[originalName]);
                    if (data.name !== displayName) {
                        data.name = displayName;
                        if (node.name !== undefined) node.name = displayName;
                        changed = true;
                    }
                }
            });
            if (changed) {
                this._cacheNodes();
                this._applySequenceFilters({ updateDisplay: false });
                this._draw();
            }
        }

        toggleSupport(show) {
            this.options.showSupport = show;
            this._addSupportLabels(); // Just add/remove text (no massive redraw)
        }

        applyFilter(text) {
            if (!text) {
                // FAST: Iterate Array
                for (const n of this.allNodes) { delete n.__search_match; }
                this._updateNodeStylesOnly();
                return;
            }

            const term = text.toLowerCase();
            // FAST: Iterate Array
            for (const n of this.allNodes) {
                const name = n.data.name || "";
                n.__search_match = name.toLowerCase().includes(term);
            }

            this._updateNodeStylesOnly();
        }

        getNodeIdFromEvent(event) {
            if (!event || !event.target) return null;
            const g = event.target.closest('g.node, g.internal-node');
            if (!g || !window.d3v7) return null;
            try {
                const d = window.d3v7.select(g).datum();
                return d?.data?.__original_name || d?.data?.name || d?.name || null;
            } catch (e) { return null; }
        }



        getStats() {
            return this.lastStats;
        }

        // Alan 5/9/26 - Let the controller update sequence metric filters from live slider input.
        setSequenceFilterOptions(newOpts = {}) {
            // Alan 7/20/26 - Accept minimum coverage thresholds plus the independent maximum-identity cutoff.
            ['queryCoverThreshold', 'subjectCoverThreshold', 'identityMaximum'].forEach(key => {
                if (Object.prototype.hasOwnProperty.call(newOpts, key)) {
                    const nextValue = Number(newOpts[key]);
                    this.options[key] = Number.isFinite(nextValue)
                        ? Math.max(0, Math.min(100, nextValue))
                        : 0;
                }
            });
            return this._applySequenceFilters({ updateDisplay: true });
        }

        // Alan 5/9/26 - Restore all tips by clearing view-only sequence metric filters.
        resetSequenceFilters() {
            this.options.queryCoverThreshold = 0;
            this.options.subjectCoverThreshold = 0;
            // Alan 7/20/26 - Reset maximum identity to 100 so all identity values remain visible.
            this.options.identityMaximum = 100;
            return this._applySequenceFilters({ updateDisplay: true });
        }

        // Alan 5/9/26 - Expose current sequence metric filter values and counts for slider labels.
        getSequenceFilterStats() {
            return this._getSequenceFilterStats();
        }

        /**
         * Export the current tree as a Newick string with selected nodes annotated.
         * Selected nodes receive a {Selected} tag comment.
         * @returns {string} Newick string representation of the current tree state.
         */
        getNewickString() {
            if (!this.tree) return "";
            return this.tree.getNewick((node) => {
                // The callback determines what annotation gets appended to the node name
                const id = this._getNodeId(node);
                // Alan 5/11/26 - Export only visible active selections after local Deselect has been used.
                if (id && this._isVisibleSelection(id)) {
                    // Appends {Selected} to the node, recognized as a comment/tag by most tree viewers
                    return "{Selected}";
                }
                return "";
            });
        }

        exportSVG() {
            const svg = this.container.querySelector('svg');
            if (!svg) throw new Error('No SVG found in tree container.');

            const { clone } = this._buildExportClone(svg);

            // Serialize and download
            const data = (new XMLSerializer()).serializeToString(clone);
            const blob = new Blob([data], { type: "image/svg+xml;charset=utf-8" });
            const url = URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.href = url;
            link.download = `tree_${Date.now()}.svg`;
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            URL.revokeObjectURL(url);
        }

        /**
         * Shared helper: clones the live SVG and prepares it for export.
         *
         * Handles:
         *  - Dark Reader artifact removal
         *  - Embedding relevant document CSS
         *  - Merging selection/search inline styles (preserving existing)
         *  - Copying support-label positioning attributes
         *  - Resolving pixel dimensions via getBoundingClientRect (% attrs ignored)
         *  - Preserving / synthesising the viewBox
         *
         * @param {SVGSVGElement} svg
         * @returns {{ clone: SVGSVGElement, width: number, height: number }}
         */
        _buildExportClone(svg) {
            const clone = svg.cloneNode(true);

            // 1. Strip Dark Reader artifacts (recursive)
            const removeDarkReader = (el) => {
                if (!el || !el.getAttribute) return;
                [...el.attributes].forEach(attr => {
                    if (attr.name.startsWith('data-darkreader')) el.removeAttribute(attr.name);
                });
                const style = el.getAttribute('style');
                if (style) {
                    const clean = style.replace(/--darkreader-[^;]+;?/g, '').trim();
                    if (clean) el.setAttribute('style', clean);
                    else el.removeAttribute('style');
                }
                for (const child of el.children) removeDarkReader(child);
            };
            removeDarkReader(clone);
            // Alan 8/21/26 - The invisible right-click targets behind each annotation are UI
            // only; keep them out of exported SVG/JPG figures.
            clone.querySelectorAll('.clade-annotation-hit').forEach(hit => hit.remove());
            clone.querySelectorAll('style').forEach(s => {
                if ((s.textContent || '').toLowerCase().includes('darkreader')) s.remove();
            });

            // 2. Extract and embed relevant CSS rules
            const relevantPatterns = [
                /\.node\b/, /\.branch\b/, /\.phylotree/, /\.internal-node/,
                /\.tree-/, /circle/, /path/, /text/, /line/,
                /\.node-support-value/, /\.selected/
            ];
            let cssText = '';
            try {
                for (const sheet of document.styleSheets) {
                    let rules;
                    try { rules = sheet.cssRules || sheet.rules; } catch (_) { continue; }
                    if (!rules) continue;
                    for (const rule of rules) {
                        if (rule.type !== CSSRule.STYLE_RULE) continue;
                        const sel = rule.selectorText || '';
                        if (relevantPatterns.some(p => p.test(sel))) {
                            cssText += rule.cssText.replace(/--darkreader-[^;:]+:[^;]+;?/g, '') + '\n';
                        }
                    }
                }
            } catch (e) {
                if (typeof DEBUG_MODE !== 'undefined' && DEBUG_MODE)
                    console.warn('Could not extract all stylesheets:', e);
            }
            if (cssText) {
                let defs = clone.querySelector('defs');
                if (!defs) {
                    defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
                    clone.insertBefore(defs, clone.firstChild);
                }
                const styleEl = document.createElementNS('http://www.w3.org/2000/svg', 'style');
                styleEl.setAttribute('type', 'text/css');
                styleEl.textContent = cssText;
                defs.appendChild(styleEl);
            }

            // 3. Merge selection/search inline styles (prepend so they win; preserve rest)
            const origShapes = svg.querySelectorAll('.node circle, .node path, .node rect');
            const cloneShapes = clone.querySelectorAll('.node circle, .node path, .node rect');
            const sn = Math.min(origShapes.length, cloneShapes.length);
            for (let i = 0; i < sn; i++) {
                const os = origShapes[i].getAttribute('style') || '';
                if (os.includes('fill') || os.includes('stroke')) {
                    const fm = os.match(/fill\s*:\s*([^;]+)/);
                    const sm = os.match(/stroke\s*:\s*([^;]+)/);
                    let ov = '';
                    if (fm) ov += `fill:${fm[1].trim()};`;
                    if (sm) ov += `stroke:${sm[1].trim()};`;
                    if (ov) {
                        const existing = cloneShapes[i].getAttribute('style') || '';
                        cloneShapes[i].setAttribute('style', ov + existing);
                    }
                }
            }

            // 4. Copy support-label positioning + essential styles
            const origTexts = svg.querySelectorAll('text.node-support-value');
            const cloneTexts = clone.querySelectorAll('text.node-support-value');
            const tn = Math.min(origTexts.length, cloneTexts.length);
            for (let i = 0; i < tn; i++) {
                ['x', 'y', 'text-anchor', 'dominant-baseline'].forEach(attr => {
                    const val = origTexts[i].getAttribute(attr);
                    if (val) cloneTexts[i].setAttribute(attr, val);
                });
                const os = origTexts[i].getAttribute('style') || '';
                const fsm = os.match(/font-size\s*:\s*([^;]+)/);
                const swm = os.match(/stroke-width\s*:\s*([^;]+)/);
                let es = '';
                if (fsm) es += `font-size:${fsm[1].trim()};`;
                if (swm) es += `stroke-width:${swm[1].trim()};`;
                if (es) {
                    const existing = cloneTexts[i].getAttribute('style') || '';
                    cloneTexts[i].setAttribute('style', es + existing);
                }
            }

            // 5. Resolve pixel dimensions.
            //    Prefer getBoundingClientRect because it is reliable even when SVG attrs use "%" or are unset.
            //    Only fall back to attribute value if it parses as a plain number (no % unit).
            const rect = svg.getBoundingClientRect();
            let width = rect.width || 0;
            let height = rect.height || 0;
            if (!width) {
                const raw = svg.getAttribute('width') || '';
                if (/^\d+(\.\d+)?(px)?$/.test(raw.trim())) width = parseFloat(raw);
            }
            if (!height) {
                const raw = svg.getAttribute('height') || '';
                if (/^\d+(\.\d+)?(px)?$/.test(raw.trim())) height = parseFloat(raw);
            }
            if (!width) width = 1200;
            if (!height) height = 800;

            clone.setAttribute('width', width);
            clone.setAttribute('height', height);

            // 6. Preserve viewBox (ensures correct scaling in rasterisers)
            const vb = svg.getAttribute('viewBox');
            if (vb) clone.setAttribute('viewBox', vb);
            else clone.setAttribute('viewBox', `0 0 ${width} ${height}`);

            return { clone, width, height };
        }

        /**
         * Export the current tree visualization as a JPG image.
         * Returns a Promise that resolves when the download is triggered, or
         * rejects with a descriptive Error on any failure.
         *
         * @param {number} [quality=0.95] - JPEG quality (0–1)
         * @returns {Promise<void>}
         */
        exportJPG(quality = 0.95) {
            return this._exportRasterImage('image/jpeg', 'jpg', quality);
        }

        /**
         * Export the current tree visualization as a PNG image.
         *
         * @returns {Promise<void>}
         */
        exportPNG() {
            return this._exportRasterImage('image/png', 'png');
        }

        /**
         * Rasterize the prepared SVG clone and download it in the requested format.
         *
         * @param {string} mimeType
         * @param {string} extension
         * @param {number|undefined} quality
         * @returns {Promise<void>}
         */
        _exportRasterImage(mimeType, extension, quality) {
            return new Promise((resolve, reject) => {
                const svg = this.container.querySelector('svg');
                if (!svg) { reject(new Error('No SVG found in tree container.')); return; }

                let clone, width, height;
                try {
                    ({ clone, width, height } = this._buildExportClone(svg));
                } catch (err) { reject(err); return; }

                let svgUrl;
                try {
                    const svgData = (new XMLSerializer()).serializeToString(clone);
                    const svgBlob = new Blob([svgData], { type: 'image/svg+xml;charset=utf-8' });
                    svgUrl = URL.createObjectURL(svgBlob);
                } catch (err) { reject(err); return; }

                const img = new Image();

                img.onerror = () => {
                    URL.revokeObjectURL(svgUrl);
                    reject(new Error('Failed to render SVG as image (check for unsupported CSS or external resources).'));
                };

                img.onload = () => {
                    URL.revokeObjectURL(svgUrl);
                    try {
                        const scale = window.devicePixelRatio || 1;
                        const canvas = document.createElement('canvas');
                        canvas.width = Math.round(width * scale);
                        canvas.height = Math.round(height * scale);

                        const ctx = canvas.getContext('2d');
                        if (!ctx) {
                            reject(new Error('Could not obtain 2D canvas context.'));
                            return;
                        }

                        // JPEG has no alpha channel; PNG deliberately retains transparency.
                        if (mimeType === 'image/jpeg') {
                            ctx.fillStyle = '#ffffff';
                            ctx.fillRect(0, 0, canvas.width, canvas.height);
                        }
                        ctx.scale(scale, scale);
                        ctx.drawImage(img, 0, 0, width, height);

                        canvas.toBlob(imageBlob => {
                            if (!imageBlob) {
                                reject(new Error(`canvas.toBlob() returned null, so ${extension.toUpperCase()} encoding failed.`));
                                return;
                            }
                            let imageUrl;
                            try {
                                imageUrl = URL.createObjectURL(imageBlob);
                                const link = document.createElement('a');
                                link.href = imageUrl;
                                link.download = `tree_${Date.now()}.${extension}`;
                                document.body.appendChild(link);
                                link.click();
                                document.body.removeChild(link);
                                resolve();
                            } catch (err) {
                                reject(err);
                            } finally {
                                if (imageUrl) URL.revokeObjectURL(imageUrl);
                            }
                        }, mimeType, quality);

                    } catch (err) { reject(err); }
                };

                img.src = svgUrl;
            });
        }

        // --- INTERNAL HELPERS ---

        // Alan 5/9/26 - Normalize names from FASTA headers and Newick tips for metric lookups.
        _normalizeMetricKey(value) {
            return String(value || '')
                .replace(/^>/, '')
                .trim()
                .replace(/\s+/g, ' ')
                .toLowerCase();
        }

        // Alan 5/9/26 - Generate forgiving lookup keys for full labels and accession-like first tokens.
        _metricKeysForName(value) {
            const full = this._normalizeMetricKey(value);
            if (!full) return [];
            const firstToken = full.split(' ')[0] || '';
            const accessionRoot = firstToken.split('.')[0] || '';
            return Array.from(new Set([full, firstToken, accessionRoot].filter(Boolean)));
        }

        // Alan 5/9/26 - Parse optional metric numbers from stored job metadata.
        _metricNumber(value) {
            if (value === null || value === undefined || value === '') return null;
            const num = Number(value);
            return Number.isFinite(num) ? num : null;
        }

        // Alan 5/9/26 - Build a name-keyed lookup from saved sequence metadata for fast tip matching.
        _buildSequenceMetricMap(records) {
            const map = new Map();
            if (!Array.isArray(records)) return map;
            for (const record of records) {
                if (!record || typeof record !== 'object') continue;
                // Alan 7/19/26 - Parse subject coverage once so invalid saved values remain missing instead of becoming zero.
                const subjectCover = this._metricNumber(record.subject_cover);
                const metric = {
                    query_cover: this._metricNumber(record.query_cover),
                    // Alan 7/19/26 - Treat MycoMap's negative reverse-strand subject coverage as a positive coverage magnitude for existing jobs.
                    subject_cover: subjectCover === null ? null : Math.abs(subjectCover),
                    identity: this._metricNumber(record.identity),
                    blast_metrics_available: Boolean(record.blast_metrics_available)
                };
                metric.blast_metrics_available = metric.blast_metrics_available ||
                    metric.query_cover !== null ||
                    metric.subject_cover !== null ||
                    metric.identity !== null;
                if (!metric.blast_metrics_available) continue;

                [record.fasta_header, record.name].forEach(name => {
                    this._metricKeysForName(name).forEach(key => map.set(key, metric));
                });
            }
            return map;
        }

        // Alan 5/9/26 - Attach stored BLAST metrics to matching tips in the phylotree node cache.
        _attachSequenceMetricsToLeaves() {
            for (const node of this.allNodes) {
                node.__sequenceMetrics = null;
                if (node.children && node.children.length) continue;
                const names = [
                    node?.data?.__original_name,
                    node?.data?.name,
                    node?.name
                ];
                for (const name of names) {
                    const metric = this._metricKeysForName(name)
                        .map(key => this.sequenceMetricMap.get(key))
                        .find(Boolean);
                    if (metric) {
                        node.__sequenceMetrics = metric;
                        break;
                    }
                }
            }
        }

        // Alan 5/9/26 - Return leaf nodes so sequence metric filters only hide terminal tips.
        _getLeafNodes() {
            return (this.allNodes || []).filter(node => !node.children || !node.children.length);
        }

        // Alan 8/22/26 - Display names of the tips currently on screen (renames applied), so
        // the Claude review renderer can tell a sequence the user can actually find from one
        // the model named but the tree does not contain.
        getTipNames() {
            return this._getLeafNodes()
                .map(node => String((node.data && node.data.name) || node.name || ''))
                .filter(Boolean);
        }

        // Alan 5/9/26 - Compare one metric against its current threshold while allowing missing metric fields.
        _passesMetricThreshold(metric, field, threshold) {
            if (!metric || metric[field] === null || metric[field] === undefined) return true;
            return Number(metric[field]) + 1e-9 >= threshold;
        }

        // Alan 7/20/26 - Keep hits at or below the chosen identity maximum, preserving tips with missing metrics.
        _passesMetricMaximum(metric, field, maximum) {
            if (!metric || metric[field] === null || metric[field] === undefined) return true;
            return Number(metric[field]) <= maximum + 1e-9;
        }

        // Alan 5/9/26 - Decide whether a leaf should remain visible under the active MycoMap metric sliders.
        _passesSequenceFilters(node) {
            const metric = node.__sequenceMetrics;
            if (!metric || !metric.blast_metrics_available) return true;
            return this._passesMetricThreshold(metric, 'query_cover', this.options.queryCoverThreshold || 0) &&
                this._passesMetricThreshold(metric, 'subject_cover', this.options.subjectCoverThreshold || 0) &&
                // Alan 7/20/26 - Lower identity values hide highly similar hits instead of imposing a reversed minimum.
                this._passesMetricMaximum(metric, 'identity', this.options.identityMaximum ?? 100);
        }

        // Alan 5/9/26 - Compute counts and current thresholds for the sequence metric filter UI.
        _getSequenceFilterStats(leaves = null) {
            const leafNodes = leaves || this._getLeafNodes();
            const totalTips = leafNodes.length;
            const hiddenTips = leafNodes.filter(node => node.notshown).length;
            const metricTips = leafNodes.filter(node => node.__sequenceMetrics?.blast_metrics_available).length;

            return {
                totalTips,
                visibleTips: Math.max(0, totalTips - hiddenTips),
                hiddenTips,
                metricTips,
                metricsAvailable: metricTips > 0,
                queryCoverThreshold: this.options.queryCoverThreshold || 0,
                subjectCoverThreshold: this.options.subjectCoverThreshold || 0,
                // Alan 7/20/26 - Report the direct maximum value displayed by the identity slider.
                identityMaximum: this.options.identityMaximum ?? 100
            };
        }

        // Alan 5/9/26 - Apply the active MycoMap metric sliders to phylotree visibility.
        _applySequenceFilters({ updateDisplay = true } = {}) {
            if (!this.tree) return this._getSequenceFilterStats([]);

            const leaves = this._getLeafNodes();
            for (const node of this.allNodes) {
                node.notshown = false;
            }
            leaves.forEach(leaf => {
                leaf.notshown = !this._passesSequenceFilters(leaf);
            });

            const stats = this._getSequenceFilterStats(leaves);
            if (updateDisplay) {
                this._refreshSequenceFilterDisplay();
            }
            return stats;
        }

        // Alan 5/9/26 - Refresh the rendered SVG after sequence metric filters change.
        _refreshSequenceFilterDisplay() {
            const display = this.tree?.display;
            if (display && typeof display.update === 'function') {
                display.update();
                this._addSupportLabels();
                this._attachEventHandlers();
                this._updateNodeStylesOnly();
                this._applyTextSizingFromZoom();
            } else {
                this._draw();
            }
        }

        // Alan 5/12/26 - Treat hidden current-selection members as deselected without touching color groups.
        _isVisibleSelection(id) {
            return Boolean(id && this.currentSelectionIds.has(id) && !this.hiddenSelectionIds.has(id));
        }

        // Alan 5/12/26 - Toggle clicks through temporary action selection without changing color groups.
        _toggleVisibleSelection(id) {
            if (!id) return false;
            if (this.hiddenSelectionIds.has(id)) {
                this.hiddenSelectionIds.delete(id);
                return true;
            }
            if (this.currentSelectionIds.has(id)) {
                this.currentSelectionIds.delete(id);
                return false;
            }
            this.currentSelectionIds.add(id);
            return true;
        }

        // Alan 5/12/26 - Return visible temporary selections for action buttons and local Deselect behavior.
        _getVisibleSelectionIds() {
            this._trimSelectionSetsToCurrentTree();
            return Array.from(this.currentSelectionIds).filter(id => !this.hiddenSelectionIds.has(id));
        }

        // Alan 7/17/26 - Use multiple selected tips as context-menu prune targets, falling back to the right-clicked node.
        _getContextPruneNodes(node) {
            // Alan 7/17/26 - Limit bulk context pruning to terminal sequences, as requested for multi-tip selections.
            const selectedLeaves = this.getSelectedNodes().filter((selectedNode) => {
                // Alan 7/17/26 - A selected node is a pruneable sequence tip only when it has no children.
                const children = selectedNode?.children || selectedNode?.data?.children || [];
                // Alan 7/17/26 - Keep only leaf nodes in the multi-tip target list.
                return !children || children.length === 0;
            });
            // Alan 7/17/26 - Preserve the existing clicked-node behavior unless more than one tip is selected.
            return selectedLeaves.length > 1 ? selectedLeaves : [node];
        }

        // Alan 7/17/26 - Count terminal sequences beneath resolved prune targets so the label reflects actual removals.
        _getContextPruneCount(node) {
            // Alan 7/17/26 - Track visited tips to avoid double-counting overlapping targets.
            const tipIds = new Set();
            // Alan 7/17/26 - Walk each target subtree and collect its terminal node IDs.
            const visit = (current) => {
                // Alan 7/17/26 - Ignore missing nodes defensively when building the context-menu count.
                if (!current) return;
                // Alan 7/17/26 - Read children from either the rendered node or its wrapped data object.
                const children = current.children || current.data?.children || [];
                // Alan 7/17/26 - Count only terminal sequences because those are what pruning removes.
                if (!children || children.length === 0) {
                    // Alan 7/17/26 - Prefer the stable viewer ID, with the object as a safe last-resort identity.
                    const id = this._getNodeId(current) || current;
                    // Alan 7/17/26 - Deduplicate each terminal sequence across all resolved targets.
                    tipIds.add(id);
                    // Alan 7/17/26 - Stop descending after reaching a terminal sequence.
                    return;
                }
                // Alan 7/17/26 - Include every terminal descendant of an internal prune target.
                children.forEach(visit);
            };
            // Alan 7/17/26 - Count the exact target set that the click handler will send to the controller.
            this._getContextPruneNodes(node).forEach(visit);
            // Alan 7/17/26 - Return the unique terminal-sequence count for label pluralization.
            return tipIds.size;
        }

        // Alan 7/17/26 - Render singular or plural prune wording from the exact context-menu target count.
        _getPruneMenuLabel(node) {
            // Alan 7/17/26 - Calculate the removal count when the menu opens so current selections are reflected.
            const count = this._getContextPruneCount(node);
            // Alan 7/17/26 - Match the requested "Prune 1 node" and "Prune 28 nodes" wording.
            return `Prune ${count} node${count === 1 ? '' : 's'}`;
        }

        // Alan 8/13/26 - Rename the right-clicked tip with no selection, or the sole selected tip when it is clicked.
        _getContextRenameNode(node) {
            const selectedNodes = this.getSelectedNodes();
            if (selectedNodes.length === 0) return node;
            if (selectedNodes.length !== 1) return null;
            const selectedNode = selectedNodes[0];
            const children = selectedNode?.children || selectedNode?.data?.children || [];
            if (children && children.length > 0) return null;
            return this._getNodeId(selectedNode) === this._getNodeId(node) ? selectedNode : null;
        }

        // Alan 7/14/26 - Use visible selected leaf sequences for name copying, falling back to the right-clicked tip.
        _getContextSequenceNameNodes(node) {
            const selectedLeaves = this.getSelectedNodes().filter((selectedNode) => {
                const children = selectedNode?.children || selectedNode?.data?.children || [];
                return !children || children.length === 0;
            });
            const candidates = selectedLeaves.length ? selectedLeaves : [node];
            return candidates.filter((candidate) => {
                const children = candidate?.children || candidate?.data?.children || [];
                return (!children || children.length === 0) && Boolean(candidate?.data?.name || candidate?.name);
            });
        }

        // Alan 7/14/26 - Pluralize the copy-name menu item when it targets multiple selected sequence names.
        _getSequenceNameCopyMenuLabel(node) {
            return this._getContextSequenceNameNodes(node).length > 1 ? 'Copy sequence names' : 'Copy sequence name';
        }

        // Alan 6/25/26 - Extract an iNaturalist observation number through phylotree's shared label parser.
        _getInaturalistObservationNumber(node) {
            const parser = window.phylotree?.extractINaturalistObservationNumber;
            return typeof parser === 'function' ? parser(node) : null;
        }

        // Alan 6/25/26 - Keep selected iNaturalist copy targets limited to visible selected leaf sequences.
        _getSelectedLeafInaturalistNumbers() {
            const numbers = [];
            const seen = new Set();
            this.getSelectedNodes().forEach((node) => {
                const children = node?.children || node?.data?.children || [];
                if (children && children.length) return;
                const number = this._getInaturalistObservationNumber(node);
                if (number && !seen.has(number)) {
                    seen.add(number);
                    numbers.push(number);
                }
            });
            return numbers;
        }

        // Alan 6/25/26 - Count selected leaf sequences so active selections can take precedence over a clicked tip.
        _getSelectedLeafCount() {
            return this.getSelectedNodes().filter((node) => {
                const children = node?.children || node?.data?.children || [];
                return !children || children.length === 0;
            }).length;
        }

        // Alan 6/25/26 - Use selected iNaturalist tips when present, otherwise fall back to the right-clicked tip.
        _getContextInaturalistNumbers(node) {
            const selectedNumbers = this._getSelectedLeafInaturalistNumbers();
            if (selectedNumbers.length) return selectedNumbers;
            if (this._getSelectedLeafCount() > 0) return [];
            const number = this._getInaturalistObservationNumber(node);
            return number ? [number] : [];
        }

        // Alan 7/14/26 - Pluralize the menu label from the unique iNaturalist numbers the action will copy.
        _getInaturalistCopyMenuLabel(node) {
            const numberCount = this._getContextInaturalistNumbers(node).length;
            return numberCount > 1 ? 'Copy iNaturalist numbers' : 'Copy iNaturalist number';
        }

        // Alan 7/16/26 - Parse either supported observation platform through phylotree's shared label parser.
        _getMycomapRecordReference(node) {
            const parser = window.phylotree?.extractObservationRecordReference;
            return typeof parser === 'function' ? parser(node) : null;
        }

        // Alan 7/16/26 - Target eligible highlighted tips, falling back to the clicked tip only when nothing is highlighted.
        _getContextMycomapRecordNodes(node) {
            const selectedLeaves = this.getSelectedNodes().filter((selectedNode) => {
                const children = selectedNode?.children || selectedNode?.data?.children || [];
                return (!children || children.length === 0) && Boolean(this._getMycomapRecordReference(selectedNode));
            });
            if (selectedLeaves.length) return selectedLeaves;
            if (this._getSelectedLeafCount() > 0) return [];
            return this._getMycomapRecordReference(node) ? [node] : [];
        }

        // Alan 7/16/26 - Deduplicate references so the menu count matches the Mycomap API work requested.
        _getContextMycomapRecordReferences(node) {
            const references = this._getContextMycomapRecordNodes(node)
                .map((recordNode) => this._getMycomapRecordReference(recordNode)?.reference)
                .filter(Boolean);
            return Array.from(new Set(references));
        }

        // Alan 7/16/26 - Use the requested singular/plural Mycomap wording from the highlighted record count.
        _getMycomapRefreshMenuLabel(node) {
            return this._getContextMycomapRecordNodes(node).length > 1
                ? 'Refresh Mycomap records'
                : 'Refresh Mycomap record';
        }

        // Alan 5/12/26 - Clear temporary current selection without changing persistent color groups.
        deselectCurrentSelection() {
            const visibleIds = this._getVisibleSelectionIds();
            this.currentSelectionIds.clear();
            this.hiddenSelectionIds.clear();
            // Alan 8/24/26 - Deselect drops selected clades along with selected tips. Collapsed
            // clades stay collapsed: this clears the selection, not the display state.
            this.selectedCladeIds.clear();
            this._invalidateCladeSelection();
            // Alan 5/11/26 - Clear phylotree's native selected styling so deselected labels stop looking selected.
            this._clearNativeSelectionForIds(visibleIds);
            this._updateStats();
            this._updateNodeStylesOnly();
            return visibleIds.length;
        }

        // Alan 5/11/26 - Remove native phylotree selected flags/classes for locally deselected nodes only.
        _clearNativeSelectionForIds(ids) {
            const hiddenIds = new Set(ids || []);
            if (!this.tree || hiddenIds.size === 0) return;
            // Alan 5/11/26 - Use phylotree's active selection attribute instead of assuming one fixed property.
            const selectionAttr = this.tree.display?.selection_attribute_name || 'selected';

            this.tree.traverse_and_compute(node => {
                const id = this._getNodeId(node);
                if (!hiddenIds.has(id)) return;
                node[selectionAttr] = false;
                if (node.data) node.data[selectionAttr] = false;
            });

            const links = this.tree.display?.links || [];
            links.forEach(edge => {
                const id = this._getNodeId(edge?.target);
                if (hiddenIds.has(id)) edge[selectionAttr] = false;
            });

            const svg = window.d3v7.select(this.container).select("svg");
            if (svg.empty()) return;
            const self = this;
            svg.selectAll("g.node, g.internal-node").each(function (d) {
                const id = self._getNodeId(d);
                if (hiddenIds.has(id)) window.d3v7.select(this).classed("node-selected", false);
            });
            svg.selectAll(".branch-selected").each(function (d) {
                const id = self._getNodeId(d?.target || d);
                if (hiddenIds.has(id)) window.d3v7.select(this).classed("branch-selected", false);
            });
        }

        _updateNodeStylesOnly() {
            if (!this.tree) return;
            const svg = window.d3v7.select(this.container).select("svg");
            if (svg.empty()) return;
            const self = this;

            svg.selectAll("g.node, g.internal-node").each(function (d) {
                const el = window.d3v7.select(this);
                const id = self._getNodeId(d);
                // Alan 5/11/26 - Restyle the group itself because tip label text inherits stale selected fill from it.
                self._styleNode(el, d);
                if (id && self.hiddenSelectionIds.has(id)) el.classed("node-selected", false);
                // Alan 5/11/26 - Restyle node shapes but clear label inline color so text follows the refreshed group state.
                el.selectAll("circle,path,rect").each(function () {
                    self._styleNode(window.d3v7.select(this), d);
                });
                // Alan 5/12/26 - Track temporary action selection separately from persistent color groups.
                const isCurrentSelection = self._isVisibleSelection(id);
                // Alan 5/12/26 - Mark current action selection on the node group for CSS/debug hooks.
                el.classed("node-current-selected", isCurrentSelection);
                // Alan 5/12/26 - Compute label color explicitly because base CSS no longer lets text inherit group fill.
                const labelColor = self._getNodeDisplayColor(id, d) || (isCurrentSelection ? "#c9a962" : null);
                // Alan 5/12/26 - Keep selected labels colored without adding weight or SVG stroke.
                const labelText = el.selectAll("text.phylotree-node-text")
                    .style("font-weight", "400", "important")
                    .style("stroke", "none", "important")
                    .style("stroke-width", "0", "important")
                    .style("paint-order", "normal", "important");
                // Alan 5/12/26 - Underline temporary selection so actions are visible without changing color groups.
                labelText.style("text-decoration", isCurrentSelection ? "underline" : null);
                // Alan 5/12/26 - Use important inline fill so selection colors beat dark-mode base label CSS.
                if (labelColor) labelText.style("fill", labelColor, "important");
                // Alan 5/12/26 - Remove inline fill for ordinary labels so light/dark base CSS controls them.
                else labelText.style("fill", null);
            });
        }

        /**
         * Alan 8/24/26 - Repaint everything a persistent colour group controls.
         *
         * Group membership, group precedence and group colour all feed BOTH the tip/node
         * styling and the automatic clade-highlight colour. Only the first was being
         * repainted, so recolouring a group turned its labels blue immediately while the
         * band behind them kept its old palette colour until some unrelated redraw happened.
         *
         * The annotation redraw is the existing debounced one and no-ops entirely on trees
         * that have no annotations, so this stays as cheap as the label repaint it replaces.
         * Nothing here rebuilds the tree.
         */
        _refreshPersistentColorVisuals() {
            this._updateNodeStylesOnly();
            this._scheduleAnnotationRedraw();
        }

        // Alan 5/12/26 - Normalize user-provided group colors before saving them.
        _normalizeColor(color, fallback = '#1f77b4') {
            // Alan 5/12/26 - Lowercase string colors so persisted values compare predictably.
            const raw = typeof color === 'string' ? color.trim().toLowerCase() : '';
            // Alan 5/12/26 - Expand shorthand colors accepted by native color inputs in some browsers.
            if (/^#[0-9a-f]{3}$/.test(raw)) return `#${raw[1]}${raw[1]}${raw[2]}${raw[2]}${raw[3]}${raw[3]}`;
            // Alan 5/12/26 - Accept only full hex colors for safe inline SVG styling.
            if (/^#[0-9a-f]{6}$/.test(raw)) return raw;
            // Alan 5/12/26 - Fall back to a known-safe color when restored state is malformed.
            return fallback;
        }

        // Alan 5/12/26 - Ensure every restored color group has a stable editable color.
        _ensureSelectionSetColors() {
            // Alan 5/12/26 - Iterate current groups so old saved states get palette-backed colors.
            Object.keys(this.selectionSets).forEach((name, index) => {
                // Alan 5/12/26 - Pick the saved color first, otherwise use the old deterministic palette.
                const fallback = this._selectionColors[index % this._selectionColors.length] || '#1f77b4';
                // Alan 5/12/26 - Store normalized colors for every known group.
                this.selectionSetColors[name] = this._normalizeColor(this.selectionSetColors[name], fallback);
            });
            // Alan 5/12/26 - Drop colors for deleted groups so the payload stays compact.
            Object.keys(this.selectionSetColors).forEach(name => {
                // Alan 5/12/26 - Remove stale color metadata when its group no longer exists.
                if (!this.selectionSets[name]) delete this.selectionSetColors[name];
            });
        }

        // Alan 5/12/26 - Keep each tip in at most one persistent color group.
        _enforceSingleColorMembership() {
            // Alan 5/12/26 - Prefer the active group, then existing object order, when old data has duplicates.
            const orderedNames = [this.activeSelectionSet, ...Object.keys(this.selectionSets).filter(name => name !== this.activeSelectionSet)];
            // Alan 5/12/26 - Track the first group that claims each tip.
            const ownerById = new Map();
            // Alan 5/12/26 - Remove duplicate memberships from lower-priority groups.
            orderedNames.forEach(name => {
                // Alan 5/12/26 - Skip missing or malformed groups defensively.
                const memberSet = this.selectionSets[name];
                // Alan 5/12/26 - Ignore invalid restored values that are not Sets.
                if (!(memberSet instanceof Set)) return;
                // Alan 5/12/26 - Review a copy so deletion during iteration stays predictable.
                Array.from(memberSet).forEach(id => {
                    // Alan 5/12/26 - First owner keeps the visible color.
                    if (!ownerById.has(id)) {
                        ownerById.set(id, name);
                        return;
                    }
                    // Alan 5/12/26 - Later duplicate owners are removed.
                    memberSet.delete(id);
                });
            });
        }

        // Alan 5/12/26 - Remove selected IDs from all color groups before assigning or clearing color.
        _removeIdsFromAllColorGroups(ids) {
            // Alan 5/12/26 - Normalize the caller's IDs once for consistent membership cleanup.
            const targetIds = new Set(Array.isArray(ids) ? ids.filter(Boolean) : []);
            // Alan 5/12/26 - Track how many actual memberships were removed.
            let removed = 0;
            // Alan 5/12/26 - Visit every persistent color group.
            for (const memberSet of Object.values(this.selectionSets)) {
                // Alan 5/12/26 - Ignore malformed restored groups defensively.
                if (!(memberSet instanceof Set)) continue;
                // Alan 5/12/26 - Delete each selected tip from this group.
                for (const id of targetIds) {
                    // Alan 5/12/26 - Count real removals for status messaging.
                    if (memberSet.delete(id)) removed += 1;
                }
            }
            // Alan 5/12/26 - Return the number of removed memberships.
            return removed;
        }

        // Alan 5/12/26 - Resolve the display color for selected/search-matched nodes without changing text weight.
        _getNodeDisplayColor(id, node) {
            // Alan 5/12/26 - Missing IDs cannot belong to selection sets.
            if (!id) return null;
            // Alan 8/24/26 - Group membership now resolves in one place, shared with the
            // automatic clade-highlight colour, so a highlight and the labels it sits behind
            // can never disagree about which group a tip belongs to.
            const matchingSetName = this._persistentGroupNameForTip(id);
            // Alan 8/26/26 - Every colour leaving here is a DRAWN colour, so each one goes
            // through the dark-mode lift. This is the one place both the node marks and the
            // label text resolve through, so they cannot disagree about the adjustment.
            // Alan 5/12/26 - Return selection-set color when membership is visible.
            if (matchingSetName) {
                return this._displayColorForTheme(this.getSelectionSetColor(matchingSetName));
            }
            // Alan 5/12/26 - Search matches remain blue when no selection set controls the color.
            if (node?.__search_match) return this._displayColorForTheme("#0EA5E9");
            // Alan 6/2/26 - Focal/sequence-of-interest tip highlights blue directly from state,
            // so a user-set SOI shows without mutating the user's Default color group.
            if (this.focalTipName && id === this.focalTipName) {
                return this._displayColorForTheme("#1f77b4");
            }
            // Alan 5/12/26 - Ordinary labels should use CSS light/dark colors.
            return null;
        }

        _styleNode(element, node) {
            // Logic: Check all selection sets for membership, apply set-specific color
            // Search Match = Blue highlight (lower priority than selection)
            const id = this._getNodeId(node);
            if (!id) {
                element.style("fill", "").style("stroke", "");
                return;
            }

            // Alan 5/12/26 - Use one shared color resolver for node marks and label text.
            const color = this._getNodeDisplayColor(id, node);
            // Alan 5/12/26 - Apply selected/search colors to node marks and groups.
            if (color) {
                element.style("fill", color).style("stroke", color);
            } else {
                element.style("fill", "").style("stroke", "");
            }
        }

        // Alan 8/4/26 - IQ-TREE run with both -alrt and -B labels nodes "SH-aLRT/UFBoot"
        // (e.g. "82.7/87"). Every existing extractor here expects a bare number, so such
        // trees previously rendered with no support values at all. Returns {alrt, ufboot}
        // or null. Both halves are percentages on a 0-100 scale.
        _extractDualSupport(node) {
            const candidates = [
                node?.data?.confidence, node?.data?.bootstrap, node?.data?.support,
                node?.confidence, node?.bootstrap, node?.support,
                node?.data?.bootstrap_values, node?.bootstrap_values,
                node?.data?.name, node?.name,
            ];

            for (const v of candidates) {
                if (v === undefined || v === null || v === "") continue;
                const s = String(v).trim();
                // Tolerate a "Node_12_" prefix, as the single-value path below does.
                const m = s.match(/^(?:Node_\d+_)?(\d+(?:\.\d+)?)\/(\d+(?:\.\d+)?)$/);
                if (m) {
                    return { alrt: Number(m[1]), ufboot: Number(m[2]) };
                }
            }
            return null;
        }

        _extractSupportValue(node) {
            // Alan 8/4/26 - Dual SH-aLRT/UFBoot labels resolve to the UFBoot half so the
            // existing 0-100 bootstrap thresholding keeps working unchanged; the label
            // renderer shows both numbers.
            const dual = this._extractDualSupport(node);
            if (dual) return dual.ufboot;

            // 1) Direct numeric fields (preferred)
            const direct = [
                node?.data?.confidence, node?.data?.bootstrap, node?.data?.support,
                node?.confidence, node?.bootstrap, node?.support,
            ];

            for (const v of direct) {
                if (v === 0) return 0;
                if (v === undefined || v === null || v === "") continue;
                const num = Number(v);
                if (!Number.isNaN(num)) return num;
            }

            // 2) bootstrap_values ONLY if it is numeric (FastTree/phylotree often puts "Node_..." here)
            const bv = node?.data?.bootstrap_values ?? node?.bootstrap_values;
            if (bv !== undefined && bv !== null) {
                const s = String(bv).trim();
                if (/^\d+(\.\d+)?$/.test(s)) return Number(s);
            }

            // 3) Fallback: parse from name ("0.97" OR "Node_12_0.97")
            const name = node?.data?.name ?? node?.name;
            if (name !== undefined && name !== null) {
                const s = String(name).trim();
                if (/^\d+(\.\d+)?$/.test(s)) return Number(s);

                const m = s.match(/^Node_\d+_(\d+(?:\.\d+)?)$/);
                if (m) return Number(m[1]);
            }

            return null;
        }

        _computeSupportStats() {
            if (!this.allNodes || !this.allNodes.length) return { maxSupport: 0, supportType: 'none' };

            let maxSupport = 0;
            let supportValues = [];

            // Fast Iteration over Cached Nodes
            // Leaf counts are already pre-computed in _cacheNodes()

            // Support value extraction
            for (const node of this.allNodes) {
                if (!node.children || node.children.length === 0) continue; // Skip tips for support

                const val = this._extractSupportValue(node);
                if (val !== null) {
                    supportValues.push(val);
                    maxSupport = Math.max(maxSupport, val);
                }
            }

            // Alan 8/4/26 - Dual SH-aLRT/UFBoot labels take priority: they are their own
            // scale (two 0-100 percentages) and must not be reported as plain bootstrap.
            const hasDual = this.allNodes.some(
                n => n.children && n.children.length > 0 && this._extractDualSupport(n)
            );

            // Alan 8/21/26 - Shared method-first rules, kept in one place so the badge, the
            // node labels and the backend review cannot drift apart.
            const supportType = window.classifySupportType(
                supportValues, hasDual, this.options.treeMethod,
                { alrtOnly: !!this.options.alrtOnly }
            );
            return { maxSupport, supportType };
        }

        _addSupportLabels() {
            const svg = window.d3v7.select(this.container).select("svg");
            if (svg.empty()) return;

            // Find zoom group
            const zoomGroup = this._findZoomGroup(svg);
            this.cachedZoomNode = zoomGroup.node() || svg.node();
            this._attachZoomObserverTo(this.cachedZoomNode);

            if (!this.options.showSupport) {
                svg.selectAll("text.node-support-value").remove();
                this._applyTextSizingFromZoom();
                // Alan 8/15/26 - Support labels are re-added after every layout change, so this
                // is also the right moment to recompute annotation geometry.
                this._scheduleAnnotationRedraw();
                return;
            }

            const { maxSupport, supportType } = this.lastStats;
            // Alan 8/4/26 - Resolve the support-scale tooltip once per redraw so every node
            // label can carry a hover note distinguishing SH-like / bootstrap / posterior.
            const supportInfo = (window.SUPPORT_TYPE_INFO || {})[supportType] || null;
            const supportTooltip = supportInfo ? supportInfo.tooltip : '';
            const ppThreshold = this.options.ppThreshold;
            const bootThreshold = this.options.bootstrapThreshold;
            const minTips = this.options.minTips;

            // Tip Zone calculation
            // In radial layout, Y is radius/angle and 'tip zone' concept based on max depth doesn't apply linearly.
            let tipZoneStart = Infinity; // Default: disable tip zone

            if (this.options.layout !== 'radial') {
                let maxY = 0;
                svg.selectAll("g.node, g.internal-node").each(d => {
                    if (d && typeof d.y === "number" && d.y > maxY) maxY = d.y;
                });
                tipZoneStart = 0.85 * maxY;
            }

            const self = this;
            svg.selectAll("g.node, g.internal-node").filter(d => d && d.children && d.children.length > 0)
                .each(function (d) {
                    const group = window.d3v7.select(this);

                    // Min Tips (Optimized)
                    if (minTips > 0) {
                        // Use precomputed __leafCount if available, else 0 (safeguard)
                        const count = (typeof d.__leafCount === 'number') ? d.__leafCount : 9999;
                        if (count < minTips) {
                            group.select("text.node-support-value").remove();
                            return;
                        }
                    }

                    const numVal = self._extractSupportValue(d);
                    if (numVal === null) {
                        group.select("text.node-support-value").remove();
                        return;
                    }
                    let rawLabel = "";

                    // Threshold Filter - decide PP vs bootstrap vs SH by value magnitude / type
                    const EPS = 1e-9;

                    // Alan 8/4/26 - Dual SH-aLRT/UFBoot nodes render both halves ("83/97").
                    // numVal is the UFBoot half, so the existing 0-100 bootstrap threshold
                    // applies to it directly.
                    const dualVal = (supportType === 'ALRT_UFBOOT') ? self._extractDualSupport(d) : null;
                    if (dualVal) {
                        if (numVal + EPS < bootThreshold) { group.select("text.node-support-value").remove(); return; }
                        const fmt = v => (Number.isInteger(v) ? String(v) : v.toFixed(1));
                        rawLabel = `${fmt(dualVal.alrt)}/${fmt(dualVal.ufboot)}`;
                    }
                    // Alan 8/21/26 - Threshold and formatting now follow the declared scale
                    // rather than the magnitude of the value. A RAxML tree whose bootstraps
                    // are all 0 or 1 is a tree with 0-1% support, and filtering it against
                    // the 0-1 posterior threshold displayed those as well-supported nodes.
                    else if (supportType === 'SH' || supportType === 'PP') {
                        // 0-1 scales: filter and format as a proportion.
                        if (numVal + EPS < ppThreshold) { group.select("text.node-support-value").remove(); return; }
                        rawLabel = numVal.toFixed(2);
                    }
                    // Alan 8/22/26 - UFBoot and SH-aLRT are 0-100 scales like bootstrap, so they
                    // format and threshold the same way even though they mean different things.
                    else if (supportType === 'BS' || supportType === 'UFBOOT' || supportType === 'ALRT') {
                        if (numVal + EPS < bootThreshold) { group.select("text.node-support-value").remove(); return; }
                        rawLabel = Math.round(numVal).toString();
                    }
                    else if (numVal <= 1.0) {
                        // Unknown or mixed scale: the value's own magnitude is all there is.
                        if (numVal + EPS < ppThreshold) { group.select("text.node-support-value").remove(); return; }
                        rawLabel = numVal.toFixed(2);
                    } else {
                        if (numVal + EPS < bootThreshold) { group.select("text.node-support-value").remove(); return; }
                        rawLabel = Math.round(numVal).toString();
                    }

                    // Append Text
                    let text = group.select("text.node-support-value");
                    if (text.empty()) text = group.append("text").attr("class", "node-support-value");

                    // Vector Math logic ...
                    // Re-calculate vector info if missing (e.g. after sort)
                    let ux = 0, uy = 0, px = 0, py = 1;
                    let textAnchor = "end";
                    // Alan 8/4/26 - Rectangular ("linear") trees are drawn by phylotree with a
                    // stepBefore curve, so the branch arriving at a node is always a HORIZONTAL
                    // segment. The old code aimed the label along the straight diagonal to the
                    // parent, which for a node whose parent sits far away vertically flung the
                    // number well off its own branch and on top of a neighbouring tip label.
                    // Aim along the drawn segment instead: toward the root is exactly -x, and
                    // the label sits just above that segment.
                    const isRadialLayout = (self.options.layout === 'radial');
                    if (!isRadialLayout) {
                        ux = -1; uy = 0;
                        px = 0; py = -1;
                        textAnchor = 'end';
                    } else if (d.parent) {
                        const vx = d.parent.y - d.y;
                        const vy = d.parent.x - d.x;
                        const len = Math.hypot(vx, vy);
                        if (len > 0) {
                            ux = vx / len; uy = vy / len;
                            px = -uy; py = ux;
                            if (py < 0) { px = -px; py = -py; }
                            if (ux < -0.2) textAnchor = 'end';
                            else if (ux > 0.2) textAnchor = 'start';
                            else textAnchor = 'middle';
                        }
                    }
                    d.__supportVec = {
                        ux, uy, px, py, textAnchor,
                        isTipZone: (d.y > tipZoneStart),
                        linear: !isRadialLayout
                    };

                    text.attr("text-anchor", textAnchor)
                        // Alan 8/4/26 - Alphabetic baseline in linear layout so the number rests
                        // on top of the horizontal branch instead of hanging down across it.
                        .attr("dominant-baseline", isRadialLayout ? "hanging" : "auto")
                        // Alan 8/4/26 - Was pointer-events:none, which blocked hover. Labels need
                        // to be hoverable to surface the support-scale note below. Clicks still
                        // bubble to the parent node <g>, so selection and zoom/pan are unchanged.
                        .style("pointer-events", supportTooltip ? "auto" : "none")
                        .text(rawLabel);

                    // Alan 8/4/26 - Appended after .text(), which replaces child nodes and would
                    // otherwise wipe the <title>. Explains which scale the number is on so a
                    // FastTree 1.00 is not misread as a Bayesian posterior probability.
                    if (supportTooltip) {
                        text.append("title")
                            .text(`${supportInfo.label}: ${rawLabel}\n\n${supportTooltip}`);
                    }
                });

            this._applyTextSizingFromZoom();
            // Alan 8/15/26 - Recompute annotation geometry after the tip labels have their
            // final size, since the bracket is placed just right of the drawn tip labels.
            this._scheduleAnnotationRedraw();
        }

        _applyTextSizingFromZoom() {
            if (!this.cachedZoomNode) return;
            const { k } = this._getSvgAndZoomGroup();
            // Alan 8/28/26 - Coarse-pointer users expect the complete tree to zoom like one
            // image. Desktop retains its established counter-scaled labels and annotations.
            const visualK = window.matchMedia?.('(pointer: coarse)')?.matches ? 1 : k;

            // Base sizes
            let supportBase = this.options.supportBasePx;
            let tipBase = this.options.tipBasePx;
            // Let inputs override if present in DOM (live update)
            const sIn = document.getElementById("input-support-font");
            const tIn = document.getElementById("input-tip-font");
            if (sIn && !isNaN(sIn.value)) supportBase = Number(sIn.value);
            if (tIn && !isNaN(tIn.value)) tipBase = Number(tIn.value);

            // Limit text size to be readable
            const tipFontSvgPx = Math.max(1, tipBase / visualK);
            const supportFontSvgPx = Math.max(1, supportBase / visualK);
            const haloSvgPx = Math.max(0.75, 3 / visualK);

            const svg = window.d3v7.select(this.container).select("svg");

            svg.selectAll("text.node-support-value").each(function (d) {
                if (!d || !d.__supportVec) return;
                const { ux, uy, px, py, isTipZone, linear } = d.__supportVec;

                let offRoot = 10, offPerp = 6;
                let finalSize = supportBase;
                if (linear) {
                    // Alan 8/4/26 - Sit the label just left of the node and 2px above its own
                    // horizontal branch. The old perpendicular boost near the tips pushed labels
                    // a whole row down into someone else's tip text; nudging further toward the
                    // root is what actually clears the crowded tip zone.
                    offRoot = 5; offPerp = 2;
                    if (isTipZone) { offRoot += 8; finalSize = Math.max(6, supportBase - 1); }
                } else if (isTipZone) { offRoot += 20; offPerp += 14; finalSize = Math.max(6, supportBase - 1); }

                const sz = Math.max(1, finalSize / visualK);
                const xOff = (ux * offRoot + px * offPerp) / visualK;
                // Vertical adjust
                const yOff = (uy * offRoot + py * offPerp) / visualK;

                window.d3v7.select(this)
                    .attr("x", xOff).attr("y", yOff)
                    .style("font-size", `${sz}px`)
                    .style("stroke-width", `${haloSvgPx}px`)
                    .style("stroke-linejoin", "round")
                    .style("paint-order", "stroke");
            });

            svg.selectAll("text.phylotree-node-text")
                .style("font-size", `${tipFontSvgPx}px`)
                // Alan 8/17/26 - Preserve phylotree's outward sign for end-aligned radial labels.
                .attr("dx", d => this._tipLabelDx(d, visualK))
                .attr("dy", 1.9 / visualK);

            // Alan 8/28/26 - Desktop labels keep a constant screen size; coarse-pointer labels
            // zoom uniformly. Either way annotations follow the final label geometry on the
            // same trailing debounce; trees without annotations no-op here.
            this._scheduleAnnotationRedraw();
        }

        _attachEventHandlers() {
            // Panic Drag Stop
            const self = this;
            const kDrag = (e) => {
                if (e.target.closest('.node, .internal-node')) e.stopPropagation();
            };
            if (!this.container.__killDragAttached) {
                this.container.__killDragAttached = true;
                this.container.addEventListener('mousedown', kDrag, true);
                this.container.addEventListener('pointerdown', kDrag, true);
            }

            // Click Handlers
            window.d3v7.select(this.container).selectAll(".node").on("click", function (event, d) {
                event.stopPropagation();
                // Toggle Selection using ID
                const id = self._getNodeId(d);
                if (!id) return;

                // Alan 5/11/26 - Toggle visible selection state without treating hidden Deselect state as set deletion.
                const selected = self._toggleVisibleSelection(id);

                self._updateNodeStylesOnly();
                // Fire callback with full node details, but state is now in Viewer
                if (self.callbacks.onTipClick) {
                    self.callbacks.onTipClick({
                        name: id,
                        display_name: d.data.name || id,
                        is_leaf: !d.children || !d.children.length,
                        selected
                    });
                }
                self._updateStats();
            });
        }

        _updateStats() {
            // Alan 5/11/26 - Report only visible active selections so Deselect disables edit actions.
            const selCount = this._getVisibleSelectionIds().length;
            if (this.callbacks.onSelectionChange) this.callbacks.onSelectionChange(selCount);
        }

        /**
         * Hook into phylotree's native selection system.
         * Syncs phylotree selections with our selection sets.
         */
        _hookPhylotreeSelection() {
            if (!this.tree || !this.tree.display) return;

            const self = this;
            const display = this.tree.display;

            // Set selection callback to sync with our selection sets
            if (typeof display.selectionCallback === 'function') {
                display.selectionCallback((selectedNodes) => {
                    // Sync phylotree's selection to our active set
                    // selectedNodes is an array of nodes that phylotree considers selected
                    selectedNodes.forEach(node => {
                        const id = self._getNodeId(node);
                        if (id) {
                            // Alan 5/11/26 - Native phylotree selections should make locally hidden nodes visible again.
                            self.hiddenSelectionIds.delete(id);
                            self.selectedIds.add(id);
                        }
                        // Alan 8/24/26 - "All internal branches" and friends hand over real node
                        // objects, so register their stable clade IDs here too.
                        self._syncCladeSelection(node, true);
                    });

                    // Update our styling
                    self._updateNodeStylesOnly();
                    self._updateStats();
                });
            }

            // Also try the event-based approach if available
            if (typeof display.on === 'function') {
                display.on('selectionChange', (selectedNodes) => {
                    selectedNodes.forEach(node => {
                        const id = self._getNodeId(node);
                        if (id) {
                            // Alan 5/11/26 - Native selection-change events should reveal nodes hidden by Deselect.
                            self.hiddenSelectionIds.delete(id);
                            self.selectedIds.add(id);
                        }
                        // Alan 8/24/26 - Keep the stable clade registry in step with native selection events.
                        self._syncCladeSelection(node, true);
                    });
                    self._updateNodeStylesOnly();
                    self._updateStats();
                });
            }
        }

        /**
         * Robust click handling using Native DOM Capture Phase.
         * Intercepts clicks BEFORE phylotree sees them.
         */
        // Alan 8/17/26 - Resolve a node-menu target from either a node element or a rendered
        // branch edge. phylotree binds path.branch to {source,target}; the distal target owns
        // the branch's canonical descendant set regardless of where along the path was clicked.
        _getContextMenuNode(target) {
            if (!target || typeof target.closest !== 'function') return null;
            const nodeElement = target.closest('.node, .internal-node');
            const branchElement = target.closest('path.branch');
            let node = window.d3v7.select(target).datum()
                || (nodeElement ? window.d3v7.select(nodeElement).datum() : null);
            if (branchElement) {
                const edge = window.d3v7.select(branchElement).datum();
                if (edge?.target) node = edge.target;
            }
            return node || null;
        }

        _overrideClickBehavior() {
            const self = this;

            // Remove any existing listeners to prevent duplicates
            if (this._clickListener) {
                this.container.removeEventListener('click', this._clickListener, true);
            }
            if (this._contextMenuListener) {
                this.container.removeEventListener('contextmenu', this._contextMenuListener);
            }

            // Define click listener (for selection)
            this._clickListener = function (event) {
                // Alan 8/30/26 - Native node-menu items are controls, not tree selection
                // gestures. In Navigate mode the touch click guard below must not swallow
                // their D3 callbacks (Prune, Rename, Collapse, and so on) before they bubble
                // to the menu item. The pointer bookkeeping has the same exemption.
                const target = event.target;
                if (target?.closest?.('.phylotree-context-menu')) return;

                // Only intercept Left Click without modifiers (simple select)
                const isSimpleClick = event.button === 0 && !event.shiftKey && !event.ctrlKey && !event.metaKey && !event.altKey;

                if (!isSimpleClick) return; // Let phylotree handle shift+click, etc.

                // Alan 8/28/26 - Touch selection is handled from pointerup after movement is measured.
                // This blocks both navigate-mode taps and the compatibility click following a
                // select-mode tap without changing genuine mouse clicks.
                const touchState = self.mobileTouchState;
                const touchGenerated = event.pointerType === 'touch'
                    || Boolean(event.sourceCapabilities?.firesTouchEvents);
                const suppressedTarget = touchState?.suppressClickTarget;
                const sameSuppressedTarget = suppressedTarget && (
                    event.target === suppressedTarget
                    || suppressedTarget.contains?.(event.target)
                    || event.target.contains?.(suppressedTarget)
                );
                const recentTouchOnSameTarget = Date.now() < (touchState?.suppressClickUntil || 0)
                    && sameSuppressedTarget;
                if ((touchGenerated && self.mobileInteractionMode !== 'select')
                    || recentTouchOnSameTarget) {
                    event.preventDefault();
                    event.stopPropagation();
                    return;
                }

                // Check if target is a node text or part of a node
                if (target.classList.contains('phylotree-node-text') || target.tagName === 'circle') {

                    // Stop phylotree directly!
                    event.stopPropagation();
                    event.preventDefault();

                    // Get D3 data
                    const d = window.d3v7.select(target).datum();

                    const id = self._getNodeId(d);
                    if (id) {
                        // Alan 5/11/26 - Toggle visible selection state while preserving Deselect-hidden set membership.
                        const nowSelected = self._toggleVisibleSelection(id);
                        // Alan 8/24/26 - Mirror internal-node clicks into the stable clade registry
                        // so bulk collapse targets exactly the clades that were clicked, not every
                        // clade that happens to share this one's support label.
                        self._syncCladeSelection(d, nowSelected);
                        self._updateNodeStylesOnly();
                        self._updateStats();
                    }
                }
            };

            // Define context menu listener (for Right Click)
            this._contextMenuListener = function (event) {
                const target = event.target;
                // Alan 8/17/26 - Resolve both node and incoming-branch right clicks to a distal node.
                const d = self._getContextMenuNode(target);
                if (d && (target.classList.contains('phylotree-node-text')
                    || target.tagName === 'circle'
                    || target.closest('.node, .internal-node, path.branch'))) {
                    event.preventDefault(); // Stop Chrome menu
                    event.stopPropagation();

                    // Alan 8/17/26 - Dispatch the already-resolved distal node to phylotree's menu.
                    if (self.tree && self.tree.display) {
                        self.tree.display.handle_node_click(d, event);
                    }
                }
            };

            // Attach listeners
            this.container.addEventListener('click', this._clickListener, true);
            this.container.addEventListener('contextmenu', this._contextMenuListener);
        }

        // Alan 8/16/26 - Attach background pointer handlers for left-drag box-select (right-drag pans instead).
        _attachBoxSelectHandlers() {
            // Alan 5/11/26 - Keep box-select inert until an SVG has rendered.
            if (!this.container || !window.d3v7) return;
            // Alan 5/11/26 - Remove stale handlers before redraws attach fresh ones.
            if (this.boxSelectState.pointerDownListener) this.container.removeEventListener('pointerdown', this.boxSelectState.pointerDownListener, true);
            // Alan 5/11/26 - Remove stale context-menu suppression before redraws attach fresh ones.
            if (this.boxSelectState.contextMenuListener) this.container.removeEventListener('contextmenu', this.boxSelectState.contextMenuListener, true);
            // Alan 5/11/26 - Remove stale keydown tracking before redraws attach fresh modifier listeners.
            if (this.boxSelectState.modifierKeyDownListener) window.removeEventListener('keydown', this.boxSelectState.modifierKeyDownListener, true);
            // Alan 5/11/26 - Remove stale keyup tracking before redraws attach fresh modifier listeners.
            if (this.boxSelectState.modifierKeyUpListener) window.removeEventListener('keyup', this.boxSelectState.modifierKeyUpListener, true);
            // Alan 5/11/26 - Keep the overlay positioned relative to the tree container.
            if (getComputedStyle(this.container).position === 'static') this.container.style.position = 'relative';
            // Alan 5/11/26 - Capture pointerdown early so background box-select does not trigger phylotree pan.
            this.boxSelectState.pointerDownListener = (event) => this._handleBoxSelectPointerDown(event);
            // Alan 5/11/26 - Suppress the browser context menu only after a right-drag selection gesture.
            this.boxSelectState.contextMenuListener = (event) => this._handleBoxSelectContextMenu(event);
            // Alan 5/11/26 - Track Alt/Ctrl/Cmd directly because some right-button pointer events omit modifier state.
            this.boxSelectState.modifierKeyDownListener = (event) => this._syncBoxSelectModifiers(event);
            // Alan 5/11/26 - Clear modifier state when keys are released after or during a drag.
            this.boxSelectState.modifierKeyUpListener = (event) => this._syncBoxSelectModifiers(event);
            // Alan 5/11/26 - Use capture so background box-select wins before D3 zoom handlers.
            this.container.addEventListener('pointerdown', this.boxSelectState.pointerDownListener, true);
            // Alan 5/11/26 - Use capture so right-drag cleanup can block the native context menu.
            this.container.addEventListener('contextmenu', this.boxSelectState.contextMenuListener, true);
            // Alan 5/11/26 - Listen on window so modifier state is available before the pointer gesture starts.
            window.addEventListener('keydown', this.boxSelectState.modifierKeyDownListener, true);
            // Alan 5/11/26 - Listen on window so Alt/Ctrl/Cmd release state stays current.
            window.addEventListener('keyup', this.boxSelectState.modifierKeyUpListener, true);
        }

        // Alan 8/28/26 - Track touch pointers independently of D3. Navigate gestures pass through untouched;
        // Select-mode node gestures are measured here so a drag can never become a tap.
        _attachTouchInteractionHandlers() {
            const state = this.mobileTouchState;
            if (!this.container || !state) return;
            if (state.pointerDownListener) this.container.removeEventListener('pointerdown', state.pointerDownListener, true);
            if (state.pointerMoveListener) window.removeEventListener('pointermove', state.pointerMoveListener, true);
            if (state.pointerUpListener) window.removeEventListener('pointerup', state.pointerUpListener, true);
            if (state.pointerCancelListener) window.removeEventListener('pointercancel', state.pointerCancelListener, true);

            state.pointers.clear();
            state.pointerDownListener = (event) => this._handleTouchPointerDown(event);
            state.pointerMoveListener = (event) => this._handleTouchPointerMove(event);
            state.pointerUpListener = (event) => this._handleTouchPointerEnd(event, false);
            state.pointerCancelListener = (event) => this._handleTouchPointerEnd(event, true);
            this.container.addEventListener('pointerdown', state.pointerDownListener, true);
            window.addEventListener('pointermove', state.pointerMoveListener, true);
            window.addEventListener('pointerup', state.pointerUpListener, true);
            window.addEventListener('pointercancel', state.pointerCancelListener, true);
        }

        _handleTouchPointerDown(event) {
            if (event.pointerType !== 'touch' || !this.mobileTouchState) return;
            // Alan 8/28/26 - The native action menu is inside the tree container, but its taps
            // are controls, not tree gestures. Tracking them used to suppress the compatibility
            // click before phylotree's shared Prune/Copy/Collapse callback could execute.
            if (event.target?.closest?.('.phylotree-context-menu')) return;
            const node = this._getContextMenuNode(event.target);
            const pointer = {
                pointerId: event.pointerId,
                startX: event.clientX,
                startY: event.clientY,
                moved: false,
                target: event.target,
                node,
                boxSelecting: Boolean(this.boxSelectState.drag?.pointerId === event.pointerId)
            };
            this.mobileTouchState.pointers.set(event.pointerId, pointer);
            if (this.mobileTouchState.pointers.size > 1) {
                this.mobileTouchState.pointers.forEach(active => { active.moved = true; });
            }
            // Alan 8/28/26 - Empty-background Select gestures belong to the box selector. Node
            // gestures are owned here; phylotree's zoom filter also rejects Select-mode touch.
            if (this.mobileInteractionMode === 'select' && node) {
                event.preventDefault();
                event.stopPropagation();
                try { this.container.setPointerCapture(event.pointerId); } catch (_) { }
            }
        }

        _handleTouchPointerMove(event) {
            if (event.pointerType !== 'touch' || !this.mobileTouchState) return;
            const pointer = this.mobileTouchState.pointers.get(event.pointerId);
            if (!pointer) return;
            if (Math.hypot(event.clientX - pointer.startX, event.clientY - pointer.startY) >= 9) {
                pointer.moved = true;
            }
            if (this.mobileTouchState.pointers.size > 1) {
                this.mobileTouchState.pointers.forEach(active => { active.moved = true; });
            }
            if (this.mobileInteractionMode === 'select' && pointer.node) {
                event.preventDefault();
                event.stopPropagation();
            }
        }

        _handleTouchPointerEnd(event, cancelled) {
            if (event.pointerType !== 'touch' || !this.mobileTouchState) return;
            const state = this.mobileTouchState;
            const pointer = state.pointers.get(event.pointerId);
            if (!pointer) return;
            if (Math.hypot(event.clientX - pointer.startX, event.clientY - pointer.startY) >= 9) {
                pointer.moved = true;
            }
            const wasMultiTouch = state.pointers.size > 1;
            state.pointers.delete(event.pointerId);
            state.suppressClickUntil = Date.now() + 700;
            state.suppressClickTarget = pointer.target;

            if (!cancelled && !wasMultiTouch && !pointer.moved
                && this.mobileInteractionMode === 'select' && pointer.node) {
                const id = this._getNodeId(pointer.node);
                if (id) {
                    const nowSelected = this._toggleVisibleSelection(id);
                    this._syncCladeSelection(pointer.node, nowSelected);
                    state.lastTargetNode = pointer.node;
                    this._updateNodeStylesOnly();
                    this._updateStats();
                    this.container.dispatchEvent(new CustomEvent('dikarya:mobile-target-change', {
                        detail: { available: true }
                    }));
                }
            }
            if (this.mobileInteractionMode === 'select' && pointer.node) {
                event.preventDefault();
                event.stopPropagation();
            }
            try { this.container.releasePointerCapture(event.pointerId); } catch (_) { }
        }

        // Alan 5/11/26 - Start box-select only from empty tree background or toolbar mode.
        _handleBoxSelectPointerDown(event) {
            // Alan 5/11/26 - Leave node right-click and normal node selection behavior untouched.
            if (!this._isBoxSelectBackgroundTarget(event.target)) return;
            // Alan 8/28/26 - Touch navigation belongs to D3. Only explicit Select mode may claim
            // a touch background drag for a selection rectangle.
            if (event.pointerType === 'touch' && this.mobileInteractionMode !== 'select') return;
            // Alan 8/16/26 - Mouse buttons swapped: left-drag on empty background now draws the
            // selection box, and panning moved to the right button (see the zoom filter in phylotree.js).
            const leftDrag = event.button === 0;
            // Alan 8/16/26 - Ignore middle-click, right-button pan, and other pointer starts.
            if (!leftDrag) return;
            // Alan 5/11/26 - Stop D3 pan/zoom from seeing the selection gesture.
            event.preventDefault();
            // Alan 5/11/26 - Stop downstream SVG handlers while the rectangle gesture starts.
            event.stopPropagation();
            // Alan 8/16/26 - Box select is a left-button gesture now, so no right-button bookkeeping is needed.
            this._startBoxSelectDrag(event, false);
        }

        // Alan 5/11/26 - Prevent browser menus for background right-drag box-select gestures.
        _handleBoxSelectContextMenu(event) {
            // Alan 5/11/26 - Suppress context menus during or just after any right-drag box gesture, even over nodes.
            if (this.boxSelectState.drag?.rightButton || Date.now() < (this.boxSelectState.suppressContextMenuUntil || 0)) {
                // Alan 5/11/26 - Keep the browser or phylotree menu from covering a completed box gesture.
                event.preventDefault();
                // Alan 5/11/26 - Stop later context-menu listeners on the same container from opening node menus.
                event.stopImmediatePropagation();
                // Alan 5/11/26 - Context menu handling is complete for suppressed box gestures.
                return;
            }
            // Alan 8/17/26 - Preserve node and incoming-branch context menus. Branch paths are bound to
            // {source,target}; the bubble-phase listener resolves their distal target.
            // Alan 8/25/26 - A drawn annotation's own right-click-to-edit target has to be let
            // through here too. This listener is on the CONTAINER in the capture phase, so its
            // stopPropagation() ran before the event ever reached the g.clade-annotation the
            // edit handler is attached to -- right-clicking a bracket, label or branch bubble
            // did nothing at all, not even open the browser menu.
            if (event.target?.closest?.('.node, .internal-node, path.branch, .clade-annotation')) return;
            // Alan 8/16/26 - Background right-drag now pans the tree, so the browser menu must stay closed.
            event.preventDefault();
            // Alan 5/11/26 - Keep background context clicks from bubbling into unrelated handlers.
            event.stopPropagation();
        }

        // Alan 5/11/26 - Keep a direct modifier snapshot for browsers that omit Alt on right-button pointer events.
        _syncBoxSelectModifiers(event) {
            // Alan 5/12/26 - Normalize key identity because Option/Alt can vary across browsers and platforms.
            const isAltKey = event.key === 'Alt' || event.key === 'Option' || event.code === 'AltLeft' || event.code === 'AltRight';
            // Alan 5/12/26 - Normalize Ctrl identity for the same reason as Alt.
            const isCtrlKey = event.key === 'Control' || event.code === 'ControlLeft' || event.code === 'ControlRight';
            // Alan 5/12/26 - Normalize Cmd/Meta identity for macOS keyboard events.
            const isMetaKey = event.key === 'Meta' || event.code === 'MetaLeft' || event.code === 'MetaRight';
            // Alan 5/12/26 - Store whether this key event is a press rather than a release.
            const isKeyDown = event.type === 'keydown';
            // Alan 5/12/26 - Store Alt/Option state from both explicit key events and aggregate modifier flags.
            this.boxSelectState.modifiers.alt = Boolean(event.altKey || (isAltKey && isKeyDown));
            // Alan 5/12/26 - Store Ctrl state from both explicit key events and aggregate modifier flags.
            this.boxSelectState.modifiers.ctrl = Boolean(event.ctrlKey || (isCtrlKey && isKeyDown));
            // Alan 5/12/26 - Store Cmd/Meta state from both explicit key events and aggregate modifier flags.
            this.boxSelectState.modifiers.meta = Boolean(event.metaKey || (isMetaKey && isKeyDown));
            // Alan 5/12/26 - Remember recent Alt presses so right-button events that omit altKey still remove.
            if (isAltKey && isKeyDown) this.boxSelectState.modifiers.lastAltDownAt = Date.now();
            // Alan 5/12/26 - Clear recent Alt memory immediately when the browser does deliver keyup.
            if (isAltKey && !isKeyDown) this.boxSelectState.modifiers.lastAltDownAt = 0;
            // Alan 5/12/26 - Remember recent Ctrl presses for toggle mode on browsers that omit ctrlKey.
            if (isCtrlKey && isKeyDown) this.boxSelectState.modifiers.lastCtrlDownAt = Date.now();
            // Alan 5/12/26 - Clear recent Ctrl memory immediately when keyup is delivered.
            if (isCtrlKey && !isKeyDown) this.boxSelectState.modifiers.lastCtrlDownAt = 0;
            // Alan 5/12/26 - Remember recent Meta presses for toggle mode on browsers that omit metaKey.
            if (isMetaKey && isKeyDown) this.boxSelectState.modifiers.lastMetaDownAt = Date.now();
            // Alan 5/12/26 - Clear recent Meta memory immediately when keyup is delivered.
            if (isMetaKey && !isKeyDown) this.boxSelectState.modifiers.lastMetaDownAt = 0;
        }

        // Alan 5/11/26 - Treat non-node SVG/container areas as valid box-select start targets.
        _isBoxSelectBackgroundTarget(target) {
            // Alan 5/11/26 - Defensive guard for non-element event targets.
            if (!target || typeof target.closest !== 'function') return false;
            // Alan 8/17/26 - Keep tip, internal-node, and incoming-branch context menus out of box select.
            if (target.closest('.node, .internal-node, path.branch')) return false;
            // Alan 5/11/26 - Avoid hijacking toolbar or form controls if they bubble through the container.
            if (target.closest('button, a, input, select, textarea, label')) return false;
            // Alan 5/11/26 - Allow empty SVG background and the surrounding tree container.
            return Boolean(target.closest('svg') || target === this.container || this.container.contains(target));
        }

        // Alan 5/11/26 - Create the drag state and overlay for a box-select gesture.
        _startBoxSelectDrag(event, rightButton) {
            // Alan 5/11/26 - Cancel any previous incomplete drag before starting a new one.
            this._cancelBoxSelectDrag();
            // Alan 5/11/26 - Create a lightweight DOM overlay rather than changing the SVG scene.
            const overlay = this._createBoxSelectOverlay();
            // Alan 5/11/26 - Store viewport coordinates so DOM hit testing stays correct under zoom/pan.
            const drag = {
                pointerId: event.pointerId,
                startX: event.clientX,
                startY: event.clientY,
                currentX: event.clientX,
                currentY: event.clientY,
                moved: false,
                rightButton,
                mode: this._boxSelectModeFromEvent(event),
                overlay,
                moveListener: null,
                upListener: null,
                // Alan 8/28/26 - Pointer cancellation must share the drag's idempotent cleanup path.
                cancelListener: null,
                keyListener: null
            };
            // Alan 5/11/26 - Store the active drag so movement, release, and Esc can coordinate.
            this.boxSelectState.drag = drag;
            // Alan 5/12/26 - Color the rectangle according to add/remove/toggle mode before first paint.
            this._styleBoxSelectOverlay(drag);
            // Alan 5/11/26 - Update the overlay immediately so slow drags feel responsive.
            this._positionBoxSelectOverlay(drag);
            // Alan 5/11/26 - Track movement on window so the pointer can leave the SVG during drag.
            drag.moveListener = (moveEvent) => this._updateBoxSelectDrag(moveEvent);
            // Alan 5/11/26 - Finish on pointerup anywhere in the viewport.
            drag.upListener = (upEvent) => this._finishBoxSelectDrag(upEvent);
            // Alan 8/28/26 - Abort a box selection when the browser cancels its touch pointer.
            drag.cancelListener = (cancelEvent) => {
                if (cancelEvent.pointerId === drag.pointerId) this._cancelBoxSelectDrag();
            };
            // Alan 5/11/26 - Let Escape cancel a rectangle before it changes selection.
            drag.keyListener = (keyEvent) => {
                // Alan 5/11/26 - Cancel only the active box-select drag on Escape.
                if (keyEvent.key === 'Escape') this._cancelBoxSelectDrag();
            };
            // Alan 5/11/26 - Register temporary drag listeners for the gesture lifetime only.
            window.addEventListener('pointermove', drag.moveListener, true);
            // Alan 5/11/26 - Register temporary pointerup listener for cleanup and selection.
            window.addEventListener('pointerup', drag.upListener, true);
            // Alan 8/28/26 - Listen for browser gesture cancellation for the lifetime of this drag only.
            window.addEventListener('pointercancel', drag.cancelListener, true);
            // Alan 5/11/26 - Register temporary Escape listener for keyboard cancellation.
            window.addEventListener('keydown', drag.keyListener, true);
            // Alan 5/11/26 - Capture the pointer when possible to keep drag updates steady.
            try { this.container.setPointerCapture(event.pointerId); } catch (_) { }
        }

        // Alan 5/11/26 - Update drag bounds and reveal the rectangle once the user moves far enough.
        _updateBoxSelectDrag(event) {
            // Alan 5/11/26 - Ignore movement if no box-select gesture is active.
            const drag = this.boxSelectState.drag;
            // Alan 5/11/26 - Ignore unrelated pointer streams.
            if (!drag || event.pointerId !== drag.pointerId) return;
            // Alan 5/11/26 - Stop D3 pan/zoom while resizing the box.
            event.preventDefault();
            // Alan 5/11/26 - Stop downstream handlers while resizing the box.
            event.stopPropagation();
            // Alan 5/11/26 - Track the latest viewport pointer position.
            drag.currentX = event.clientX;
            // Alan 5/11/26 - Track the latest viewport pointer position.
            drag.currentY = event.clientY;
            // Alan 5/12/26 - Preserve remove/toggle once seen so pointerup cannot downgrade the gesture to add.
            drag.mode = this._mergeBoxSelectMode(drag.mode, this._boxSelectModeFromEvent(event));
            // Alan 5/12/26 - Recolor the rectangle if the user presses Alt/Ctrl during the drag.
            this._styleBoxSelectOverlay(drag);
            // Alan 5/11/26 - Treat tiny pointer drift as a click, not a selection rectangle.
            drag.moved = drag.moved || Math.hypot(drag.currentX - drag.startX, drag.currentY - drag.startY) >= 5;
            // Alan 5/11/26 - Keep the visual rectangle synced with the pointer.
            this._positionBoxSelectOverlay(drag);
        }

        // Alan 5/11/26 - Finalize a box-select drag and apply membership changes.
        _finishBoxSelectDrag(event) {
            // Alan 5/11/26 - Ignore pointerup if no box-select gesture is active.
            const drag = this.boxSelectState.drag;
            // Alan 5/11/26 - Ignore unrelated pointer streams.
            if (!drag || event.pointerId !== drag.pointerId) return;
            // Alan 5/11/26 - Stop the release from triggering phylotree pan or browser context behavior.
            event.preventDefault();
            // Alan 5/11/26 - Stop downstream handlers on selection release.
            event.stopPropagation();
            // Alan 5/11/26 - Use the release coordinates for the final rectangle.
            drag.currentX = event.clientX;
            // Alan 5/11/26 - Use the release coordinates for the final rectangle.
            drag.currentY = event.clientY;
            // Alan 5/12/26 - Merge final modifiers without losing an earlier Alt/Ctrl/Cmd drag mode.
            drag.mode = this._mergeBoxSelectMode(drag.mode, this._boxSelectModeFromEvent(event));
            // Alan 5/11/26 - Compute before cleanup because cleanup removes the overlay state.
            const rect = this._boxSelectViewportRect(drag);
            // Alan 5/11/26 - Record whether the gesture was large enough to select nodes.
            const shouldSelect = drag.moved && rect.width >= 3 && rect.height >= 3;
            // Alan 5/11/26 - Suppress the upcoming browser context menu after any background right-drag gesture.
            if (drag.rightButton) this.boxSelectState.suppressContextMenuUntil = Date.now() + 500;
            // Alan 5/11/26 - Remove overlay and temporary listeners before applying selection.
            this._cancelBoxSelectDrag();
            // Alan 5/11/26 - Avoid changing selection on accidental taps or tiny drags.
            if (!shouldSelect) {
                // Alan 5/12/26 - Clear modifier memory after tiny right-drags so Alt cannot stick.
                this._resetBoxSelectModifierMemory();
                // Alan 5/12/26 - Finish without changing selection for accidental tiny drags.
                return;
            }
            // Alan 5/11/26 - Apply the rectangle to visible leaf sequences only.
            const result = this._applyBoxSelection(rect, drag.mode);
            // Alan 5/12/26 - Clear modifier memory after every completed gesture so plain right-drag keeps adding.
            this._resetBoxSelectModifierMemory();
            // Alan 5/11/26 - Notify the controller for optional status feedback.
            if (this.callbacks.onBoxSelect) this.callbacks.onBoxSelect(result);
        }

        // Alan 5/11/26 - Cancel active drag visuals and temporary listeners without changing selection.
        _cancelBoxSelectDrag() {
            // Alan 5/11/26 - Read the active drag once so cleanup can be idempotent.
            const drag = this.boxSelectState.drag;
            // Alan 5/11/26 - Clear active state even if no drag existed.
            this.boxSelectState.drag = null;
            // Alan 5/11/26 - Nothing else to remove when there is no active drag.
            if (!drag) return;
            // Alan 5/11/26 - Remove the visual rectangle from the tree container.
            if (drag.overlay?.parentNode) drag.overlay.parentNode.removeChild(drag.overlay);
            // Alan 5/11/26 - Remove the temporary pointermove listener.
            if (drag.moveListener) window.removeEventListener('pointermove', drag.moveListener, true);
            // Alan 5/11/26 - Remove the temporary pointerup listener.
            if (drag.upListener) window.removeEventListener('pointerup', drag.upListener, true);
            // Alan 8/28/26 - Remove the temporary cancel listener with the rest of the drag listeners.
            if (drag.cancelListener) window.removeEventListener('pointercancel', drag.cancelListener, true);
            // Alan 5/11/26 - Remove the temporary Escape listener.
            if (drag.keyListener) window.removeEventListener('keydown', drag.keyListener, true);
            // Alan 5/11/26 - Release pointer capture if the browser accepted it.
            try { this.container.releasePointerCapture(drag.pointerId); } catch (_) { }
        }

        // Alan 5/11/26 - Build the translucent rectangle that follows the box-select drag.
        _createBoxSelectOverlay() {
            // Alan 5/11/26 - Use HTML overlay styling so it stays readable above any SVG theme.
            const overlay = document.createElement('div');
            // Alan 5/11/26 - Keep the overlay non-interactive so pointer events stay with the drag.
            overlay.style.pointerEvents = 'none';
            // Alan 5/11/26 - Position the overlay relative to the tree container.
            overlay.style.position = 'absolute';
            // Alan 5/11/26 - Place the selection box above the SVG.
            overlay.style.zIndex = '20';
            // Alan 5/11/26 - Use the journal gold selection color for consistency.
            overlay.style.border = '1px solid rgba(201,169,98,.95)';
            // Alan 5/11/26 - Use a faint fill so selected area is visible without hiding labels.
            overlay.style.background = 'rgba(201,169,98,.16)';
            // Alan 5/11/26 - Add a subtle inset to make the box visible on light and dark backgrounds.
            overlay.style.boxShadow = 'inset 0 0 0 1px rgba(255,255,255,.18)';
            // Alan 5/11/26 - Hide until movement passes the drag threshold.
            overlay.style.display = 'none';
            // Alan 5/11/26 - Append to the container so the overlay follows container scroll/position.
            this.container.appendChild(overlay);
            // Alan 5/11/26 - Return the overlay so drag state can update and remove it.
            return overlay;
        }

        // Alan 5/12/26 - Color box-select rectangles so delete mode is clearly distinct from add mode.
        _styleBoxSelectOverlay(drag) {
            // Alan 5/12/26 - Skip styling if cleanup already removed the overlay.
            if (!drag?.overlay) return;
            // Alan 5/12/26 - Use red for Alt/remove mode so deletion gestures are visually explicit.
            const palette = drag.mode === 'remove'
                ? { border: 'rgba(220,38,38,.95)', background: 'rgba(220,38,38,.14)', inset: 'rgba(254,202,202,.35)' }
                : drag.mode === 'toggle'
                    ? { border: 'rgba(59,130,246,.95)', background: 'rgba(59,130,246,.14)', inset: 'rgba(191,219,254,.35)' }
                    : { border: 'rgba(201,169,98,.95)', background: 'rgba(201,169,98,.16)', inset: 'rgba(255,255,255,.18)' };
            // Alan 5/12/26 - Apply the mode-specific rectangle border.
            drag.overlay.style.border = `1px solid ${palette.border}`;
            // Alan 5/12/26 - Apply the mode-specific translucent fill.
            drag.overlay.style.background = palette.background;
            // Alan 5/12/26 - Apply a subtle inset that matches the current mode color.
            drag.overlay.style.boxShadow = `inset 0 0 0 1px ${palette.inset}`;
        }

        // Alan 5/11/26 - Position the visual rectangle inside the tree container.
        _positionBoxSelectOverlay(drag) {
            // Alan 5/11/26 - Bail if cleanup already removed the overlay.
            if (!drag?.overlay || !this.container) return;
            // Alan 5/11/26 - Keep tiny click drift invisible until it becomes a real drag.
            drag.overlay.style.display = drag.moved ? 'block' : 'none';
            // Alan 5/11/26 - Convert viewport coordinates to container-relative overlay coordinates.
            const containerRect = this.container.getBoundingClientRect();
            // Alan 5/11/26 - Normalize left/top regardless of drag direction.
            const left = Math.min(drag.startX, drag.currentX) - containerRect.left + this.container.scrollLeft;
            // Alan 5/11/26 - Normalize left/top regardless of drag direction.
            const top = Math.min(drag.startY, drag.currentY) - containerRect.top + this.container.scrollTop;
            // Alan 5/11/26 - Normalize width regardless of drag direction.
            const width = Math.abs(drag.currentX - drag.startX);
            // Alan 5/11/26 - Normalize height regardless of drag direction.
            const height = Math.abs(drag.currentY - drag.startY);
            // Alan 5/11/26 - Apply pixel geometry directly for smooth drag updates.
            drag.overlay.style.left = `${left}px`;
            // Alan 5/11/26 - Apply pixel geometry directly for smooth drag updates.
            drag.overlay.style.top = `${top}px`;
            // Alan 5/11/26 - Apply pixel geometry directly for smooth drag updates.
            drag.overlay.style.width = `${width}px`;
            // Alan 5/11/26 - Apply pixel geometry directly for smooth drag updates.
            drag.overlay.style.height = `${height}px`;
        }

        // Alan 5/11/26 - Convert drag state to a viewport rectangle for DOM hit testing.
        _boxSelectViewportRect(drag) {
            // Alan 5/11/26 - Normalize viewport bounds regardless of drag direction.
            const left = Math.min(drag.startX, drag.currentX);
            // Alan 5/11/26 - Normalize viewport bounds regardless of drag direction.
            const right = Math.max(drag.startX, drag.currentX);
            // Alan 5/11/26 - Normalize viewport bounds regardless of drag direction.
            const top = Math.min(drag.startY, drag.currentY);
            // Alan 5/11/26 - Normalize viewport bounds regardless of drag direction.
            const bottom = Math.max(drag.startY, drag.currentY);
            // Alan 5/11/26 - Return both edges and dimensions for selection thresholds.
            return { left, right, top, bottom, width: right - left, height: bottom - top };
        }

        // Alan 5/11/26 - Decide whether box-select adds, removes, or toggles selected sequences.
        _boxSelectModeFromEvent(event) {
            // Alan 5/12/26 - Read modifier memory once so right-button gestures can use recent keydown fallback.
            const modifiers = this.boxSelectState.modifiers || {};
            // Alan 8/16/26 - Box select is a left-button gesture now, so apply the recent-key fallback to any
            // box drag rather than only right-button ones. This helper is only ever called during a box gesture.
            // Alan 5/12/26 - Treat only a currently remembered recent Alt keydown as remove mode.
            const recentAlt = modifiers.alt && Date.now() - (modifiers.lastAltDownAt || 0) < 1500;
            // Alan 5/12/26 - Treat only currently remembered recent Ctrl/Cmd keydowns as toggle mode.
            const recentToggle = (modifiers.ctrl && Date.now() - (modifiers.lastCtrlDownAt || 0) < 1500) || (modifiers.meta && Date.now() - (modifiers.lastMetaDownAt || 0) < 1500);
            // Alan 5/11/26 - Alt/Option drag removes sequences from the active set.
            if (event.altKey || event.getModifierState?.('Alt') || event.getModifierState?.('AltGraph') || recentAlt) return 'remove';
            // Alan 5/11/26 - Ctrl/Cmd drag toggles sequence membership.
            if (event.ctrlKey || event.metaKey || event.getModifierState?.('Control') || event.getModifierState?.('Meta') || recentToggle) return 'toggle';
            // Alan 5/11/26 - Plain drag adds sequences to the active set.
            return 'add';
        }

        // Alan 8/16/26 - Clear remembered modifier keys after a box gesture so later left-drags select normally.
        _resetBoxSelectModifierMemory() {
            // Alan 5/12/26 - Clear remembered Alt state that browsers may fail to keyup after right-button drags.
            this.boxSelectState.modifiers.alt = false;
            // Alan 5/12/26 - Clear remembered Ctrl state for the same reason.
            this.boxSelectState.modifiers.ctrl = false;
            // Alan 5/12/26 - Clear remembered Meta state for the same reason.
            this.boxSelectState.modifiers.meta = false;
            // Alan 5/12/26 - Clear recent Alt timestamp so it cannot affect a later plain drag.
            this.boxSelectState.modifiers.lastAltDownAt = 0;
            // Alan 5/12/26 - Clear recent Ctrl timestamp so it cannot affect a later plain drag.
            this.boxSelectState.modifiers.lastCtrlDownAt = 0;
            // Alan 5/12/26 - Clear recent Meta timestamp so it cannot affect a later plain drag.
            this.boxSelectState.modifiers.lastMetaDownAt = 0;
        }

        // Alan 5/12/26 - Keep destructive modifier modes sticky for the life of one box-select drag.
        _mergeBoxSelectMode(currentMode, eventMode) {
            // Alan 5/12/26 - Remove has highest priority because Alt-drag should never become add on mouse release.
            if (currentMode === 'remove' || eventMode === 'remove') return 'remove';
            // Alan 5/12/26 - Toggle stays sticky unless Alt/remove appears later in the gesture.
            if (currentMode === 'toggle' || eventMode === 'toggle') return 'toggle';
            // Alan 5/12/26 - Fall back to normal add mode when no modifier was observed.
            return 'add';
        }

        // Alan 5/11/26 - Apply a viewport rectangle to visible terminal labels/sequences.
        _applyBoxSelection(rect, mode) {
            // Alan 5/11/26 - Collect visible leaf IDs whose rendered labels or markers intersect the box.
            const ids = this._leafIdsIntersectingViewportRect(rect);
            // Alan 8/16/26 - Alt/left-drag is a prune request; let the controller run the backend prune action.
            if (mode === 'remove') return { matched: ids.length, changed: ids.length, mode, ids };
            // Alan 5/11/26 - Track actual membership changes for status feedback.
            let changed = 0;
            // Alan 5/12/26 - Update temporary action selection according to the gesture mode.
            ids.forEach(id => { if (this._applyBoxSelectionToId(id, mode)) changed += 1; });
            // Alan 5/11/26 - Clear native phylotree selection flags when box gestures remove or toggle tips.
            if (mode === 'remove' || mode === 'toggle') this._clearNativeSelectionForIds(ids);
            // Alan 5/11/26 - Refresh action buttons and persist via the existing selection-change callback.
            this._updateStats();
            // Alan 5/11/26 - Repaint selected labels after the bulk update.
            this._updateNodeStylesOnly();
            // Alan 5/12/26 - Return useful counts and IDs for controller status messages and future actions.
            return { matched: ids.length, changed, mode, ids };
        }

        // Alan 5/11/26 - Update one sequence ID for a box-select gesture.
        _applyBoxSelectionToId(id, mode) {
            // Alan 5/11/26 - Guard against empty DOM IDs from unexpected nodes.
            if (!id) return false;
            // Alan 5/12/26 - Remove mode clears temporary action selection without editing color groups.
            if (mode === 'remove') {
                // Alan 5/12/26 - Delete from temporary current selection.
                const removedSelected = this.currentSelectionIds.delete(id);
                // Alan 5/11/26 - Also clear any transient Deselect mask for the same sequence.
                const removedHidden = this.hiddenSelectionIds.delete(id);
                // Alan 5/11/26 - Return whether membership changed for status counts.
                return removedSelected || removedHidden;
            }
            // Alan 5/11/26 - Toggle mode uses the same visible-selection semantics as single clicks.
            if (mode === 'toggle') {
                // Alan 5/11/26 - Toggle mode always changes visible membership for a valid ID.
                this._toggleVisibleSelection(id);
                // Alan 5/11/26 - Report the toggle as a change.
                return true;
            }
            // Alan 5/11/26 - Add mode should also reveal IDs hidden by the Deselect button.
            const wasVisible = this._isVisibleSelection(id);
            // Alan 5/11/26 - Remove transient Deselect masking before adding the ID.
            this.hiddenSelectionIds.delete(id);
            // Alan 5/12/26 - Add the ID to temporary action selection, not a color group.
            this.selectedIds.add(id);
            // Alan 5/11/26 - Report a change only when the sequence was not already visible.
            return !wasVisible;
        }

        // Alan 5/11/26 - Return visible leaf node IDs intersecting a viewport rectangle.
        _leafIdsIntersectingViewportRect(rect) {
            // Alan 5/11/26 - Locate the rendered SVG each time because phylotree redraws it.
            const svg = window.d3v7.select(this.container).select("svg");
            // Alan 5/11/26 - No rendered SVG means there is nothing to select.
            if (svg.empty()) return [];
            // Alan 5/11/26 - Preserve DOM order while avoiding duplicate IDs.
            const ids = [];
            // Alan 5/11/26 - Track duplicate IDs so repeated labels do not double-count.
            const seen = new Set();
            // Alan 5/11/26 - Use the viewer instance inside the D3 each callback.
            const self = this;
            // Alan 5/11/26 - Inspect only rendered tip groups, not internal nodes.
            svg.selectAll("g.node").each(function (d) {
                // Alan 5/11/26 - Skip internal nodes so box select targets sequences only.
                if (d?.children && d.children.length) return;
                // Alan 5/11/26 - Skip tips hidden by metric filters or phylotree display rules.
                if (d?.notshown) return;
                // Alan 5/11/26 - Skip DOM nodes with no rendered footprint.
                const bounds = this.getBoundingClientRect();
                // Alan 5/11/26 - Ignore invisible or collapsed groups.
                if (!bounds.width && !bounds.height) return;
                // Alan 5/11/26 - Use rectangle intersection in viewport coordinates.
                const intersects = bounds.right >= rect.left && bounds.left <= rect.right && bounds.bottom >= rect.top && bounds.top <= rect.bottom;
                // Alan 5/11/26 - Ignore labels outside the selection rectangle.
                if (!intersects) return;
                // Alan 5/11/26 - Convert the datum to the stable original sequence ID.
                const id = self._getNodeId(d);
                // Alan 5/11/26 - Add each ID once in DOM order.
                if (id && !seen.has(id)) {
                    // Alan 5/11/26 - Mark the ID seen before storing it.
                    seen.add(id);
                    // Alan 5/11/26 - Store the selectable leaf ID.
                    ids.push(id);
                }
            });
            // Alan 5/11/26 - Return the matching leaf IDs to the bulk selection operation.
            return ids;
        }

        // --- SELECTION STATE MANAGEMENT ---

        _getNodeId(node) {
            // Robust ID extraction that handles multiple data structures:
            // 1. Tree traversal nodes: node.data.name, node.data.__original_name
            // 2. D3 datums: node.name directly, or nested differently
            if (!node) return null;

            // Try nested data first (tree traversal structure)
            if (node.data) {
                if (node.data.__original_name) return node.data.__original_name;
                if (node.data.name) return node.data.name;
            }

            // Try direct properties (D3 datum structure)
            if (node.__original_name) return node.__original_name;
            if (node.name) return node.name;
            if (node.id) return node.id;

            return null;
        }

        // Alan 5/10/26 - Build the rendered node ID set so restored selections can drop pruned nodes.
        _getCurrentNodeIdSet() {
            const ids = new Set();
            if (!this.tree) return ids;
            this.tree.traverse_and_compute(n => {
                const id = this._getNodeId(n);
                if (id) ids.add(id);
            });
            return ids;
        }

        // Alan 5/10/26 - Remove selection IDs that no longer exist after prune or recompute reloads.
        _trimSelectionSetsToCurrentTree() {
            const currentIds = this._getCurrentNodeIdSet();
            if (currentIds.size === 0) return false;
            let changed = false;
            for (const memberSet of Object.values(this.selectionSets)) {
                if (!(memberSet instanceof Set)) continue;
                for (const id of Array.from(memberSet)) {
                    if (!currentIds.has(id)) {
                        memberSet.delete(id);
                        changed = true;
                    }
                }
            }
            // Alan 5/12/26 - Drop temporary action selections for nodes no longer present after pruning/reload.
            for (const id of Array.from(this.currentSelectionIds)) {
                if (!currentIds.has(id)) {
                    // Alan 5/12/26 - Remove stale temporary selection IDs after structural tree changes.
                    this.currentSelectionIds.delete(id);
                    // Alan 5/12/26 - Mark state changed when temporary selection was trimmed.
                    changed = true;
                }
            }
            // Alan 5/11/26 - Keep transient hidden selections limited to active IDs still present in the tree.
            for (const id of Array.from(this.hiddenSelectionIds)) {
                if (!currentIds.has(id) || !this.currentSelectionIds.has(id)) {
                    this.hiddenSelectionIds.delete(id);
                }
            }
            return changed;
        }

        /**
         * Clear temporary action selection while preserving persistent color groups.
         * Use after backend mutations when node references become stale.
         */
        clearSelection() {
            // Alan 5/12/26 - Clear only temporary action selection, not saved color-group membership.
            this.currentSelectionIds.clear();
            // Alan 5/12/26 - Clear transient Deselect state when current selection is removed.
            this.hiddenSelectionIds.clear();
            // Alan 8/24/26 - Selected clades are part of the same transient selection.
            this.selectedCladeIds.clear();
            this._invalidateCladeSelection();
            this._updateStats();
            this._updateNodeStylesOnly();
        }

        // Alan 5/12/26 - Remove only pruned tips from selection sets so unrelated color annotations survive pruning.
        removeIdsFromSelectionSets(ids) {
            // Alan 5/12/26 - Normalize the input once so callers can pass arrays safely.
            const removedIds = new Set(Array.isArray(ids) ? ids.filter(Boolean) : []);
            // Alan 5/12/26 - Nothing to remove when the prune action had no boxed names.
            if (removedIds.size === 0) return 0;
            // Alan 5/12/26 - Track membership removals for callers that want diagnostics.
            let removedCount = 0;
            // Alan 5/12/26 - Remove each pruned tip from every saved selection/color set.
            for (const memberSet of Object.values(this.selectionSets)) {
                // Alan 5/12/26 - Ignore malformed persisted set values defensively.
                if (!(memberSet instanceof Set)) continue;
                // Alan 5/12/26 - Delete every pruned ID from this set without touching other members.
                for (const id of removedIds) {
                    // Alan 5/12/26 - Count actual membership changes.
                    if (memberSet.delete(id)) removedCount += 1;
                }
            }
            // Alan 5/12/26 - Clear transient hidden-selection masks for pruned tips only.
            for (const id of removedIds) this.hiddenSelectionIds.delete(id);
            // Alan 5/12/26 - Clear temporary action selections for pruned tips only.
            for (const id of removedIds) this.currentSelectionIds.delete(id);
            // Alan 5/12/26 - Refresh labels so remaining set colors stay visible.
            // Alan 8/24/26 - Also repaints automatic highlights derived from this group.
            this._refreshPersistentColorVisuals();
            // Alan 5/12/26 - Refresh toolbar counts after pruning selection memberships.
            this._updateStats();
            // Alan 5/12/26 - Return how many saved set memberships were removed.
            return removedCount;
        }

        // Alan 5/12/26 - Apply the active color group to the current temporary selection.
        addCurrentSelectionToActiveColorGroup() {
            // Alan 5/12/26 - Resolve the active persistent color group defensively.
            const group = this.selectionSets[this.activeSelectionSet];
            // Alan 5/12/26 - No active group means no color can be applied.
            if (!(group instanceof Set)) return 0;
            // Alan 5/12/26 - Use visible temporary action selections as the color target.
            const ids = this._getVisibleSelectionIds();
            // Alan 5/12/26 - Remove selected tips from every other group so each tip has one color.
            this._removeIdsFromAllColorGroups(ids);
            // Alan 5/12/26 - Track how many selected tips receive this group color.
            let changed = 0;
            // Alan 5/12/26 - Add each currently selected node to the active color group.
            ids.forEach(id => {
                // Alan 5/12/26 - Add after clearing previous color ownership.
                group.add(id);
                // Alan 5/12/26 - Count selected tips colored by this action.
                changed += 1;
            });
            // Alan 5/12/26 - Repaint labels so applied color is visible immediately.
            // Alan 8/24/26 - Also repaints automatic highlights derived from this group.
            this._refreshPersistentColorVisuals();
            // Alan 5/12/26 - Keep action button counts synchronized.
            this._updateStats();
            // Alan 5/12/26 - Return new membership count for status text.
            return changed;
        }

        // Alan 5/12/26 - Clear current temporary selections from every color group.
        clearCurrentSelectionColorGroups() {
            // Alan 5/12/26 - Use visible temporary action selections as the clear-color target.
            const ids = this._getVisibleSelectionIds();
            // Alan 5/12/26 - Remove selected tips from all groups because colors are mutually exclusive.
            const changed = this._removeIdsFromAllColorGroups(ids);
            // Alan 5/12/26 - Repaint labels so cleared colors disappear immediately.
            // Alan 8/24/26 - Also repaints automatic highlights derived from this group.
            this._refreshPersistentColorVisuals();
            // Alan 5/12/26 - Keep action button counts synchronized.
            this._updateStats();
            // Alan 5/12/26 - Return removed membership count for status text.
            return changed;
        }

        // Alan 5/12/26 - Keep legacy callers working by treating active uncolor as clear-color.
        removeCurrentSelectionFromActiveColorGroup() {
            // Alan 5/12/26 - Delegate to the one-color-per-tip clear operation.
            return this.clearCurrentSelectionColorGroups();
        }

        selectionAction(action, filteredNodesPredicate = null) {
            if (!this.tree) return;

            const DEBUG_MODE = new URLSearchParams(window.location.search).has('debug');
            if (DEBUG_MODE) console.log('selectionAction called:', action, 'active set:', this.activeSelectionSet);

            // Alan 5/11/26 - Bulk selection actions intentionally replace local Deselect state.
            this.hiddenSelectionIds.clear();

            if (action === 'all') {
                let firstNode = true;
                this.tree.traverse_and_compute(n => {
                    const id = this._getNodeId(n);
                    if (DEBUG_MODE && firstNode) {
                        console.log('First traversal node:', n);
                        console.log('n.data:', n?.data);
                        console.log('n.name:', n?.name);
                        console.log('_getNodeId(n) returns:', id);
                        firstNode = false;
                    }
                    if (id) this.selectedIds.add(id);
                });
            } else if (action === 'none') {
                this.selectedIds.clear();
            } else if (action === 'all-internal') {
                this.tree.traverse_and_compute(n => {
                    if (n.children && n.children.length) {
                        const id = this._getNodeId(n);
                        if (id) this.selectedIds.add(id);
                    }
                });
            } else if (action === 'all-leaves') {
                this.tree.traverse_and_compute(n => {
                    if (!n.children || !n.children.length) {
                        const id = this._getNodeId(n);
                        if (id) this.selectedIds.add(id);
                    }
                });
            } else if (action === 'inverse') {
                this.tree.traverse_and_compute(n => {
                    const id = this._getNodeId(n);
                    if (!id) return;
                    if (this.selectedIds.has(id)) this.selectedIds.delete(id);
                    else this.selectedIds.add(id);
                });
            } else if (action === 'select-filtered') {
                const predicate = filteredNodesPredicate || ((n) => n.__search_match);
                this.tree.traverse_and_compute(n => {
                    if (predicate(n)) {
                        const id = this._getNodeId(n);
                        if (id) this.selectedIds.add(id);
                    }
                });
            }

            if (DEBUG_MODE) console.log('Selection count after action:', this.selectedIds.size);

            this._updateStats();
            this._updateNodeStylesOnly();
        }

        /**
         * Select the Most Recent Common Ancestor (MRCA) and all nodes connecting
         * the currently selected nodes via Max Parsimony logic.
         * 
         * Note: this.tree.maxParsimony exists but is for character state optimization,
         * not MRCA selection. We implement manual parent traversal instead.
         */
        selectMaxParsimony() {
            if (!this.tree) return;

            const selectedNodes = this.getSelectedNodes();
            if (selectedNodes.length === 0) return;
            if (selectedNodes.length === 1) {
                // Single node: just keep it selected
                return;
            }

            // Step 1: For each selected node, collect all ancestors (path to root)
            const getAncestorsSet = (node) => {
                const ancestors = new Set();
                let current = node;
                while (current) {
                    const id = this._getNodeId(current);
                    if (id) ancestors.add(id);
                    current = current.parent;
                }
                return ancestors;
            };

            // Step 2: Find common ancestors (intersection of all ancestor sets)
            let commonAncestors = null;
            for (const node of selectedNodes) {
                const ancestors = getAncestorsSet(node);
                if (commonAncestors === null) {
                    commonAncestors = ancestors;
                } else {
                    // Intersection
                    commonAncestors = new Set([...commonAncestors].filter(id => ancestors.has(id)));
                }
            }

            if (!commonAncestors || commonAncestors.size === 0) return;

            // Step 3: Find the MRCA (deepest common ancestor = closest to tips)
            // The MRCA is the common ancestor with the greatest depth
            let mrca = null;
            let maxDepth = -1;

            this.tree.traverse_and_compute(n => {
                const id = this._getNodeId(n);
                if (id && commonAncestors.has(id)) {
                    // Calculate depth (distance from root)
                    let depth = 0;
                    let current = n;
                    while (current.parent) {
                        depth++;
                        current = current.parent;
                    }
                    if (depth > maxDepth) {
                        maxDepth = depth;
                        mrca = n;
                    }
                }
            });

            if (!mrca) return;

            // Step 4: Select MRCA and all nodes on paths from MRCA to selected nodes
            const mrcaId = this._getNodeId(mrca);
            // Alan 5/11/26 - Nodes explicitly added by derived selection should be visible after Deselect.
            this.hiddenSelectionIds.delete(mrcaId);
            this.selectedIds.add(mrcaId);

            // For each selected node, walk up to MRCA and select the path
            for (const node of selectedNodes) {
                let current = node;
                while (current) {
                    const id = this._getNodeId(current);
                    if (id) {
                        // Alan 5/11/26 - Reveal path nodes that this action explicitly selects.
                        this.hiddenSelectionIds.delete(id);
                        this.selectedIds.add(id);
                        if (id === mrcaId) break; // Stop at MRCA
                    }
                    current = current.parent;
                }
            }

            this._updateStats();
            this._updateNodeStylesOnly();
        }

        /**
         * Expand current selection to include all descendants (children/leaves)
         * of the currently selected nodes.
         */
        selectAllDescendants() {
            if (!this.tree) return;

            const selectedNodes = this.getSelectedNodes();
            if (selectedNodes.length === 0) return;

            // Recursive helper to add all descendants
            const addDescendants = (node) => {
                const id = this._getNodeId(node);
                if (id) {
                    // Alan 5/11/26 - Reveal descendants that this action explicitly selects.
                    this.hiddenSelectionIds.delete(id);
                    this.selectedIds.add(id);
                }
                // Alan 8/24/26 - Register internal descendants as selected clades too.
                this._syncCladeSelection(node, true);

                if (node.children && node.children.length) {
                    for (const child of node.children) {
                        addDescendants(child);
                    }
                }
            };

            // For each currently selected node, add all its descendants
            for (const node of selectedNodes) {
                addDescendants(node);
            }

            this._updateStats();
            this._updateNodeStylesOnly();
        }

        getSelectedNodes() {
            if (!this.tree) return [];
            const selected = [];
            this.tree.traverse_and_compute(n => {
                const id = this._getNodeId(n);
                // Alan 5/11/26 - Backend actions should only use visible active selections after Deselect.
                if (id && this._isVisibleSelection(id)) selected.push(n);
            });
            return selected;
        }

        getSelectionCount() {
            // Alan 5/11/26 - Count only visible active selections that still exist in the rendered tree.
            return this._getVisibleSelectionIds().length;
        }

        // Alan 5/13/26 - Expose rendered, unpruned tip order so the Alignment Viewer can default to tree order.
        getVisibleTipOrder() {
            if (!this.tree) return [];
            const names = [];
            const seen = new Set();
            this.tree.traverse_and_compute(n => {
                if (n.children && n.children.length) return;
                if (n.notshown) return;
                const id = this._getNodeId(n);
                if (id && !seen.has(id)) {
                    seen.add(id);
                    names.push(id);
                }
            });
            return names;
        }

        // Alan 5/13/26 - Expose visible selected tip names so the Alignment Viewer can default to selection.
        getSelectedTipNames() {
            const selected = this.getSelectedNodes();
            const names = [];
            const seen = new Set();
            for (const node of selected) {
                if (node.children && node.children.length) continue;
                if (node.notshown) continue;
                const id = this._getNodeId(node);
                if (id && !seen.has(id)) {
                    seen.add(id);
                    names.push(id);
                }
            }
            return names;
        }

        // ==================================================================
        // Alan 8/24/26 - CLADE COLLAPSE (viewer-only, never persisted)
        //
        // Collapsing folds a monophyletic group into a wedge. It is purely a
        // display state (phylotree's transient `node.collapsed`): every tip,
        // branch length and support value stays in the model, nothing is written
        // to tree_state.json, no tree-edit API is called, and the edited FASTA is
        // untouched. It is therefore NOT persisted across a page reload.
        // ==================================================================

        // Is this a collapsible clade? Leaves are not, and neither is a unary
        // pass-through node, whose descendant tip set -- and therefore stable ID --
        // is identical to its only child's.
        _isCollapsibleClade(node) {
            const children = node?.children || node?.data?.children || [];
            return Boolean(children && children.length >= 2);
        }

        // Stable ID for an internal node, or null for anything that is not a
        // collapsible clade.
        _getCladeId(node) {
            if (!this._isCollapsibleClade(node)) return null;
            const tipIds = this.getDescendantLeafIds(node);
            if (!tipIds.length) return null;
            return stableCladeIdFromTipNames(tipIds);
        }

        // Keep the clade registry in step with a selection change made against a
        // real node object. Called from every place that selects/deselects one.
        _syncCladeSelection(node, selected) {
            const cladeId = this._getCladeId(node);
            if (!cladeId) return;
            const before = this.selectedCladeIds.size;
            if (selected) this.selectedCladeIds.add(cladeId);
            else this.selectedCladeIds.delete(cladeId);
            if (this.selectedCladeIds.size !== before) this._invalidateCladeSelection();
        }

        // Called wherever the selected-clade set or the parsed tree changes.
        _invalidateCladeSelection() {
            this._cladeSelectionRevision = (this._cladeSelectionRevision || 0) + 1;
            this._cladeNodeCache = null;
        }

        // Resolve the registry back to live nodes in the CURRENT tree, dropping
        // IDs whose clade no longer exists (after a prune, say). The root is
        // excluded: collapsing it would blank the whole tree, and no bulk action
        // should ever be able to do that by accident.
        getSelectedCladeNodes() {
            if (!this.tree || this.selectedCladeIds.size === 0) return [];
            if (this._cladeNodeCache
                && this._cladeNodeCache.revision === this._cladeSelectionRevision) {
                return this._cladeNodeCache.nodes;
            }
            const wanted = new Set(this.selectedCladeIds);
            const found = new Map();
            this.tree.traverse_and_compute((node) => {
                if (node.parent === undefined || node.parent === null) return;
                const cladeId = this._getCladeId(node);
                if (cladeId && wanted.has(cladeId) && !found.has(cladeId)) {
                    found.set(cladeId, node);
                }
            });
            const nodes = Array.from(found.values());
            // Cached by object identity, not by collapsed state: the callers below
            // re-read node.collapsed on every call, so a collapse never needs to
            // invalidate this.
            this._cladeNodeCache = { revision: this._cladeSelectionRevision, nodes };
            return nodes;
        }

        // Reduce a set of clades to the MAXIMAL non-overlapping ones: a clade with
        // a selected ancestor in the same set is dropped. Selecting a clade and one
        // of its descendants and asking to collapse both means one visible collapse,
        // not two, and the descendant would vanish under the ancestor's wedge anyway.
        _maximalClades(nodes) {
            const members = new Set(nodes);
            const hasSelectedAncestor = (node) => {
                let cursor = node.parent;
                while (cursor) {
                    if (members.has(cursor)) return true;
                    cursor = cursor.parent;
                }
                return false;
            };
            return nodes.filter((node) => !hasSelectedAncestor(node));
        }

        // What a bulk collapse would fold: maximal selected clades not already
        // collapsed. Deliberately deterministic and order-preserving.
        getBulkCollapseTargets() {
            const selected = this.getSelectedCladeNodes();
            if (selected.length === 0) return [];
            return this._maximalClades(selected).filter((node) => !node.collapsed);
        }

        // What a bulk expand would unfold: every selected clade that is collapsed,
        // WITHOUT the maximal-clade reduction. Expanding is idempotent and cannot
        // hide anything, and reducing here would strand a nested collapsed clade
        // permanently behind its expanded ancestor.
        getBulkExpandTargets() {
            return this.getSelectedCladeNodes().filter((node) => Boolean(node.collapsed));
        }

        // Count of clades a bulk action currently has to work with, for menu labels.
        getSelectedCladeCount() {
            return this.getSelectedCladeNodes().length;
        }

        // Repaint after a collapse change. phylotree's own menu item only calls
        // update(), which leaves Dikarya's support labels, event handlers and
        // selection colors describing the pre-collapse SVG.
        _refreshAfterCollapse() {
            const display = this.tree?.display;
            if (!display || typeof display.update !== 'function') {
                this._draw();
                return;
            }
            display.update();
            this._addSupportLabels();
            this._attachEventHandlers();
            this._updateNodeStylesOnly();
            this._applyTextSizingFromZoom();
            this._scheduleAnnotationRedraw();
        }

        // Record the one-slot undo entry and tell the controller a collapse happened.
        // `collapsed` is the direction the action moved in, so the controller can word
        // its message without parsing the undo label.
        _recordCollapseUndo(entries, label, collapsed) {
            if (!entries.length) return;
            this._collapseUndo = { entries, label };
            if (typeof this.callbacks.onCollapseChange === 'function') {
                this.callbacks.onCollapseChange({ label, count: entries.length, collapsed });
            }
        }

        // Apply one collapsed state to a list of clades in a single repaint.
        // Returns the number of clades actually changed.
        _setCladesCollapsed(nodes, collapsed, label) {
            const display = this.tree?.display;
            if (!display || typeof display.toggleCollapse !== 'function') return 0;
            const changed = nodes.filter((node) => Boolean(node.collapsed) !== collapsed);
            if (changed.length === 0) return 0;
            this._bulkCollapseInProgress = true;
            try {
                changed.forEach((node) => display.toggleCollapse(node));
            } finally {
                this._bulkCollapseInProgress = false;
            }
            this._refreshAfterCollapse();
            this._recordCollapseUndo(
                changed.map((node) => ({ node, collapsed: !collapsed })),
                label || `${collapsed ? 'collapse' : 'expand'} of ${changed.length} clade${changed.length === 1 ? '' : 's'}`,
                collapsed
            );
            return changed.length;
        }

        // Collapse the current bulk target set. Returns how many clades folded.
        collapseSelectedClades() {
            const targets = this.getBulkCollapseTargets();
            const count = targets.length;
            return this._setCladesCollapsed(
                targets, true,
                `collapse of ${count} clade${count === 1 ? '' : 's'}`
            );
        }

        // Expand every selected clade that is currently collapsed.
        expandSelectedClades() {
            const targets = this.getBulkExpandTargets();
            const count = targets.length;
            return this._setCladesCollapsed(
                targets, false,
                `expand of ${count} clade${count === 1 ? '' : 's'}`
            );
        }

        // Is a collapse/expand available to undo?
        hasCollapseUndo() {
            return Boolean(this._collapseUndo && this._collapseUndo.entries.length);
        }

        // Label for the pending collapse undo, e.g. "collapse of 4 clades".
        getCollapseUndoLabel() {
            return this._collapseUndo ? this._collapseUndo.label : null;
        }

        // Restore the collapse state captured before the last collapse/expand and
        // consume the entry. Returns the label of what was undone, or null.
        undoLastCollapseChange() {
            if (!this.hasCollapseUndo()) return null;
            const { entries, label } = this._collapseUndo;
            this._collapseUndo = null;
            const display = this.tree?.display;
            if (!display || typeof display.toggleCollapse !== 'function') return null;
            this._bulkCollapseInProgress = true;
            try {
                entries.forEach(({ node, collapsed }) => {
                    if (Boolean(node.collapsed) !== Boolean(collapsed)) display.toggleCollapse(node);
                });
            } finally {
                this._bulkCollapseInProgress = false;
            }
            this._refreshAfterCollapse();
            return label;
        }

        // Forget the pending collapse undo (the tree it referred to is gone).
        clearCollapseUndo() {
            this._collapseUndo = null;
        }

        // Watch phylotree's own collapse item so a single "Collapse Clade" from the
        // context menu is undoable too. The event carries the node and its NEW
        // state, so the previous state is simply its negation.
        _hookNativeCollapseEvents() {
            const display = this.tree?.display;
            if (!display || typeof display.on !== 'function') return;
            if (this._nativeCollapseHandler) display.off?.('collapsed', this._nativeCollapseHandler);
            this._nativeCollapseHandler = (node, collapsed) => {
                if (this._bulkCollapseInProgress) return;
                this._recordCollapseUndo(
                    [{ node, collapsed: !collapsed }],
                    collapsed ? 'collapse of 1 clade' : 'expand of 1 clade',
                    collapsed
                );
                // toggleCollapse() emits BEFORE phylotree's own menu handler calls
                // update(), so refreshing inline would rebuild support labels and
                // event handlers against the pre-collapse SVG and then have them
                // wiped by that update. Deferring one turn puts this last.
                if (this._nativeCollapseRefreshTimer) clearTimeout(this._nativeCollapseRefreshTimer);
                this._nativeCollapseRefreshTimer = setTimeout(() => {
                    this._nativeCollapseRefreshTimer = null;
                    this._refreshAfterCollapse();
                }, 0);
            };
            display.on('collapsed', this._nativeCollapseHandler);
        }

        // --- SELECTION SET MANAGEMENT (CRUD) ---

        /**
         * Create a new named selection set.
         * @param {string} name - Name for the new set
         * @returns {boolean} - True if created, false if name already exists or invalid
         */
        createSelectionSet(name, color = null) {
            if (!name || typeof name !== 'string') return false;
            const trimmed = name.trim();
            if (!trimmed || this.selectionSets[trimmed]) return false;

            // Alan 5/12/26 - Create the persistent color group membership bucket.
            this.selectionSets[trimmed] = new Set();
            // Alan 5/12/26 - Store the user-chosen group color or a suggested fallback.
            this.selectionSetColors[trimmed] = this._normalizeColor(color, this.suggestSelectionSetColor());
            return true;
        }

        /**
         * Delete a named selection set.
         * @param {string} name - Name of set to delete
         * @returns {boolean} - True if deleted, false if protected ('Default') or doesn't exist
         */
        deleteSelectionSet(name) {
            if (name === 'Default') return false; // Protected
            if (!this.selectionSets[name]) return false;

            delete this.selectionSets[name];
            // Alan 5/12/26 - Remove deleted group color metadata with the group.
            delete this.selectionSetColors[name];

            // If we deleted the active set, switch back to Default
            if (this.activeSelectionSet === name) {
                this.activeSelectionSet = 'Default';
            }

            // Alan 5/12/26 - Deleting a color group should preserve temporary action selection.
            // Alan 8/24/26 - Also repaints automatic highlights derived from this group.
            this._refreshPersistentColorVisuals();
            this._updateStats();
            return true;
        }

        /**
         * Set the active selection set (for editing).
         * @param {string} name - Name of set to activate
         * @returns {boolean} - True if activated, false if set doesn't exist
         */
        setActiveSelectionSet(name) {
            if (!this.selectionSets[name]) return false;
            this.activeSelectionSet = name;
            // Alan 5/12/26 - Switching color groups should not clear temporary action selection.
            // Alan 5/12/26 - Repaint immediately because active set membership now controls color priority.
            // Alan 8/24/26 - Also repaints automatic highlights derived from this group.
            this._refreshPersistentColorVisuals();
            this._updateStats();
            return true;
        }

        /**
         * Get array of all selection set names.
         * @returns {string[]}
         */
        getSelectionSetNames() {
            return Object.keys(this.selectionSets);
        }

        /**
         * Get the color assigned to a selection set.
         * @param {string} name - Set name
         * @returns {string|null} - CSS color or null if set doesn't exist
         */
        getSelectionSetColor(name) {
            // Alan 5/12/26 - Ensure old saved groups have color metadata before returning.
            this._ensureSelectionSetColors();
            // Alan 5/12/26 - Return user-selected color metadata when available.
            if (this.selectionSetColors[name]) return this.selectionSetColors[name];
            const names = Object.keys(this.selectionSets);
            const index = names.indexOf(name);
            if (index < 0) return null;
            return this._selectionColors[index % this._selectionColors.length];
        }

        // Alan 5/12/26 - Update a persistent color group's user-selected color.
        setSelectionSetColor(name, color) {
            // Alan 5/12/26 - Reject color changes for missing groups.
            if (!this.selectionSets[name]) return false;
            // Alan 5/12/26 - Normalize before storing so inline styles stay safe.
            this.selectionSetColors[name] = this._normalizeColor(color, this.getSelectionSetColor(name) || '#1f77b4');
            // Alan 5/12/26 - Repaint labels immediately after a color edit.
            // Alan 8/24/26 - Also repaints automatic highlights derived from this group.
            this._refreshPersistentColorVisuals();
            // Alan 5/12/26 - Report a successful color edit.
            return true;
        }

        // Alan 5/12/26 - Suggest the next palette color for newly created groups.
        suggestSelectionSetColor() {
            // Alan 5/12/26 - Use group count to rotate through the existing palette.
            const index = Object.keys(this.selectionSets).length % this._selectionColors.length;
            // Alan 5/12/26 - Return a normalized palette color for native color inputs.
            return this._normalizeColor(this._selectionColors[index], '#1f77b4');
        }

        /**
         * Get the currently active selection set name.
         * @returns {string}
         */
        getActiveSelectionSet() {
            return this.activeSelectionSet;
        }

        // Alan 5/8/26 - Serialize/restore selection sets for persistence across page reloads.
        getSelectionSetsData() {
            const sets = {};
            for (const [name, memberSet] of Object.entries(this.selectionSets)) {
                sets[name] = Array.from(memberSet);
            }
            // Alan 5/12/26 - Ensure color metadata exists before serializing color groups.
            this._ensureSelectionSetColors();
            // Alan 5/12/26 - Persist colors additively while keeping the old sets/active payload shape.
            return { sets, active: this.activeSelectionSet, colors: { ...this.selectionSetColors } };
        }

        restoreSelectionSets(data) {
            if (!data || !data.sets) return;
            for (const name of Object.keys(this.selectionSets)) {
                if (name !== 'Default') delete this.selectionSets[name];
            }
            this.selectionSets['Default'].clear();
            // Alan 5/12/26 - Reset color metadata before restoring saved colors.
            this.selectionSetColors = { 'Default': '#1f77b4' };
            for (const [name, ids] of Object.entries(data.sets)) {
                if (name === 'Default') {
                    this.selectionSets['Default'] = new Set(ids);
                } else {
                    this.selectionSets[name] = new Set(ids);
                }
            }
            if (data.active && this.selectionSets[data.active]) {
                this.activeSelectionSet = data.active;
            }
            // Alan 5/12/26 - Restore user-selected colors when newer tree states include them.
            if (data.colors && typeof data.colors === 'object') {
                // Alan 5/12/26 - Copy only colors for groups that still exist.
                Object.entries(data.colors).forEach(([name, color]) => {
                    // Alan 5/12/26 - Ignore color metadata for missing groups.
                    if (!this.selectionSets[name]) return;
                    // Alan 5/12/26 - Normalize restored colors before using them in SVG styles.
                    this.selectionSetColors[name] = this._normalizeColor(color, this.selectionSetColors[name]);
                });
            }
            // Alan 5/12/26 - Backfill colors for old jobs and remove stale color metadata.
            this._ensureSelectionSetColors();
            // Alan 5/12/26 - Normalize old multi-group data to one persistent color per tip.
            this._enforceSingleColorMembership();
            // Alan 5/11/26 - Restored selection sets should not inherit a prior local Deselect state.
            this.hiddenSelectionIds.clear();
            // Alan 5/10/26 - Do not restore saved selections for nodes removed by pruning.
            this._trimSelectionSetsToCurrentTree();
            // Alan 8/24/26 - Restored groups change automatic highlight colours too.
            this._refreshPersistentColorVisuals();
            this._updateStats();
        }

        /**
         * Clear only the temporary action selection, not persistent color groups.
         */
        clearActiveSelection() {
            // Alan 5/12/26 - selectedIds getter now references temporary action selection.
            this.selectedIds.clear();
            // Alan 5/12/26 - Clear transient hidden IDs when temporary selection is emptied.
            this.hiddenSelectionIds.clear();
            this._updateStats();
            this._updateNodeStylesOnly();
        }

        // ==================================================================
        // Alan 8/15/26 - LAYERED CLADE ANNOTATIONS
        //
        // Persisted state carries only canonical leaf IDs and style; every
        // coordinate below is derived from the live SVG on each redraw, so
        // rotation, spacing, pruning and rerooting need no coordinate fixups.
        // ==================================================================

        // Alan 8/15/26 - Replace the whole annotation configuration and repaint.
        setCladeAnnotations(layers, annotations) {
            this.annotationLayers = Array.isArray(layers) ? layers : [];
            this.cladeAnnotations = Array.isArray(annotations) ? annotations : [];
            this._renderCladeAnnotations();
        }

        // Alan 8/15/26 - Give the controller the current configuration for saving.
        getCladeAnnotations() {
            return { layers: this.annotationLayers, annotations: this.cladeAnnotations };
        }

        // Alan 8/15/26 - Report which annotations still describe exactly one clade in the
        // current topology, so the manager can flag the rest without deleting them.
        getCladeAnnotationValidity() {
            return this.annotationValidity;
        }

        // Alan 8/15/26 - Collect the canonical leaf IDs beneath any rendered node. Used by the
        // context menu and by "Annotate selected clade"; never returns rendered positions.
        getDescendantLeafIds(node) {
            const ids = [];
            const seen = new Set();
            const visit = (current) => {
                if (!current) return;
                const children = current.children || current.data?.children || [];
                if (!children || children.length === 0) {
                    const id = this._getNodeId(current);
                    if (id && !seen.has(id)) {
                        seen.add(id);
                        ids.push(id);
                    }
                    return;
                }
                children.forEach(visit);
            };
            visit(node);
            return ids;
        }

        /**
         * Alan 8/26/26 - Resolve canonical leaf IDs to the labels currently ON SCREEN.
         * The annotation editor uses these to suggest a default label, and a tip the user has
         * renamed should contribute the name they gave it, not the original FASTA header.
         * IDs with no matching tip (a pruned member of a stale annotation) are skipped.
         */
        getDisplayLabelsForLeafIds(ids) {
            const wanted = new Set(Array.isArray(ids) ? ids.filter(Boolean) : []);
            if (!wanted.size || !Array.isArray(this.allNodes)) return [];
            const labels = [];
            this.allNodes.forEach((node) => {
                const children = node.children || node.data?.children || [];
                if (children && children.length) return;
                const id = this._getNodeId(node);
                if (!id || !wanted.has(id)) return;
                const label = node.data?.name || node.name || id;
                if (label) labels.push(String(label));
            });
            return labels;
        }

        // Alan 8/17/26 - Compare annotations by canonical descendant membership rather than
        // display order, so rotation and renaming cannot turn Edit into an accidental Add.
        _annotationMembershipKey(ids) {
            return Array.from(new Set(Array.isArray(ids) ? ids.filter(Boolean) : []))
                .sort().join('\u0000');
        }

        // Alan 8/17/26 - Reuse exact membership lookup for both branch context menus and the
        // selected-clade workflow.
        getAnnotationsForMemberIds(memberIds) {
            const key = this._annotationMembershipKey(memberIds);
            if (!key) return [];
            return this.cladeAnnotations.filter((annotation) =>
                annotation && this._annotationMembershipKey(annotation.member_tip_ids) === key
            );
        }

        // Alan 8/17/26 - Distinguish a clade from an annotatable incoming branch. The root
        // is a valid whole-tree clade but deliberately returns false here.
        hasIncomingBranchForMemberIds(memberIds) {
            const key = this._annotationMembershipKey(memberIds);
            if (!key) return false;
            // Alan 8/17/26 - Reject different-sized candidates via the cached leaf count before
            // walking descendants; whole-tree membership now performs no subtree traversals.
            const memberCount = new Set(memberIds.filter(Boolean)).size;
            return this.allNodes.some((node) => node?.parent
                && node.__leafCount === memberCount
                && this._annotationMembershipKey(this.getDescendantLeafIds(node)) === key);
        }

        // Alan 8/17/26 - Resolve a clicked node to the annotations already attached to its
        // exact descendant set. The root has no context-menu annotation action.
        getAnnotationsForNode(node) {
            if (!node?.parent) return [];
            return this.getAnnotationsForMemberIds(this.getDescendantLeafIds(node));
        }

        // Alan 8/21/26 - Count the selected TIPS only, so internal-node selections do not
        // change what the annotation menu offers.
        getSelectedLeafCount() {
            return this.getSelectedNodes().filter((node) => {
                const children = node?.children || node?.data?.children || [];
                return !children || children.length === 0;
            }).length;
        }

        /**
         * Alan 8/21/26 - Resolve which tips a context-menu annotation should cover.
         *
         * Normally that is the clicked branch's descendants. The exception is right-clicking
         * a TIP that belongs to a multi-tip selection which is itself exactly one clade: the
         * user has already said what group they mean, so annotate that group rather than the
         * single tip under the cursor. Requiring the clicked tip to be inside the selection
         * keeps a click on an unrelated branch acting on that branch, and restricting this to
         * tips keeps a click on an inner branch meaning that inner branch.
         */
        getAnnotationTargetLeafIds(node) {
            return this._selectionAnnotationLeafIds(node) || this.getDescendantLeafIds(node);
        }

        // Alan 8/21/26 - The selected clade when it should stand in for the clicked tip, else null.
        _selectionAnnotationLeafIds(node) {
            const children = node?.children || [];
            if (children.length || this.getSelectedLeafCount() < 2) return null;
            const id = this._getNodeId(node);
            if (!id) return null;
            const selectedClade = this.getSelectedCladeLeafIds();
            if (!selectedClade || selectedClade.length < 2) return null;
            return selectedClade.includes(id) ? selectedClade : null;
        }

        // Alan 8/21/26 - True when the context menu will annotate the selection, not the click.
        _isSelectionAnnotationTarget(node) {
            return Boolean(this._selectionAnnotationLeafIds(node));
        }

        /**
         * Alan 8/21/26 - Pick the annotation type the editor should open with.
         * A group of tips gets the right-hand bracket; a lone tip gets branch text. Reading
         * this off the RESOLVED membership is what makes a multi-tip selection default to
         * Clade line instead of stacking a single-tip label over the branch labels.
         */
        getDefaultAnnotationType(node, memberIds = null) {
            const children = node?.children || [];
            if (children.length) return 'clade_line';
            const ids = memberIds || this.getAnnotationTargetLeafIds(node);
            return ids.length > 1 ? 'clade_line' : 'branch_text';
        }

        /**
         * Alan 8/15/26 - Resolve the current selection into a clade.
         * Returns the descendant leaf IDs when the selected leaves are exactly the
         * descendants of one node, otherwise null. Refusing the inexact case is what
         * stops a bracket from being drawn across unrelated intervening tips.
         */
        getSelectedCladeLeafIds() {
            const selectedLeafIds = new Set(
                this.getSelectedNodes()
                    .filter((node) => {
                        const children = node?.children || node?.data?.children || [];
                        return !children || children.length === 0;
                    })
                    .map((node) => this._getNodeId(node))
                    .filter(Boolean)
            );
            if (selectedLeafIds.size === 0) return null;

            for (const node of this.allNodes) {
                const children = node.children || node.data?.children || [];
                if (!children || children.length === 0) continue;
                // __leafCount is precomputed by _cacheNodes, so the expensive descendant walk
                // only runs for the handful of nodes that could possibly match.
                if (node.__leafCount !== selectedLeafIds.size) continue;
                const ids = this.getDescendantLeafIds(node);
                if (ids.length === selectedLeafIds.size && ids.every((id) => selectedLeafIds.has(id))) {
                    return ids;
                }
            }
            // A FastTree soft polytomy is still one unresolved clade to the user even though
            // its Newick representation contains arbitrary near-zero binary edges. Honor the
            // selection only when it is the COMPLETE zero-length component and its tips have
            // been made adjacent, so unrelated partial/non-contiguous selections stay invalid.
            const polytomy = this._zeroLengthPolytomyForMemberIds(Array.from(selectedLeafIds));
            if (polytomy) {
                const visibleOrder = this.getVisibleTipOrder();
                const indices = visibleOrder
                    .map((id, index) => selectedLeafIds.has(id) ? index : -1)
                    .filter((index) => index >= 0);
                if (indices.length === selectedLeafIds.size
                    && indices[indices.length - 1] - indices[0] + 1 === indices.length) {
                    return visibleOrder.slice(indices[0], indices[indices.length - 1] + 1);
                }
            }
            // A single selected tip is its own (one-member) clade.
            if (selectedLeafIds.size === 1) return Array.from(selectedLeafIds);
            return null;
        }

        // Alan 8/15/26 - Replace the current selection with the given leaf IDs, so the
        // Annotation Manager can highlight an annotation's members using the existing
        // selection mechanism instead of introducing a second highlight system.
        selectLeafIds(ids) {
            const wanted = new Set(Array.isArray(ids) ? ids.filter(Boolean) : []);
            this.currentSelectionIds.clear();
            this.hiddenSelectionIds.clear();
            // Alan 8/24/26 - This REPLACES the selection with a leaf set, so no clade stays selected.
            this.selectedCladeIds.clear();
            this._invalidateCladeSelection();
            let matched = 0;
            for (const node of this.allNodes) {
                const id = this._getNodeId(node);
                if (id && wanted.has(id)) {
                    this.currentSelectionIds.add(id);
                    matched += 1;
                }
            }
            this._updateNodeStylesOnly();
            this._updateStats();
            return matched;
        }

        // Alan 8/15/26 - Coalesce layout/zoom bursts into one annotation repaint. Trees with no
        // annotations never touch the DOM here, so they carry no measurable cost.
        _scheduleAnnotationRedraw() {
            if (!this.cladeAnnotations.length && !this._annotationsDrawn) return;
            if (this.annotationRedrawTimer) clearTimeout(this.annotationRedrawTimer);
            this.annotationRedrawTimer = setTimeout(() => {
                this.annotationRedrawTimer = null;
                this._renderCladeAnnotations();
            }, 60);
        }

        // Alan 8/15/26 - Resolve one style property through the annotation, then its layer,
        // then the shared default. Layer defaults are never copied into annotations, so a layer
        // edit restyles every annotation that has no override for that property.
        _resolveAnnotationStyle(annotation, layer, field) {
            return this._resolveAnnotationStyleEntry(annotation, layer, field).value;
        }

        // Alan 8/17/26 - Resolve a style property AND report whether the value was inherited
        // rather than chosen. Dark mode lightens untouched default ink, and that decision used
        // to be made by comparing the resolved colour with #1f2937 -- which also caught a user
        // who deliberately picked that exact colour and then saw dark mode change it. The
        // decision now follows inheritance: an annotation-level override is always honoured
        // verbatim, whatever its value.
        _resolveAnnotationStyleEntry(annotation, layer, field) {
            const defaults = window.DikaryaCladeAnnotations.DEFAULTS;
            const own = annotation ? annotation[field] : null;
            if (own !== null && own !== undefined && own !== '') {
                return { value: own, isDefault: false };
            }
            const inherited = layer ? layer['default_' + field] : null;
            if (inherited !== null && inherited !== undefined && inherited !== '') {
                // Alan 8/24/26 - A layer that stores this field at all has had it chosen: the
                // server leaves it absent otherwise. No value-as-sentinel, so a user who types
                // the shared default gets exactly that and not the automatic value.
                if (ANNOTATION_OPTIONAL_LAYER_STYLE_FIELDS.has(field)) {
                    return { value: inherited, isDefault: false };
                }
                // Every other layer field always stores a concrete value (the server fills the
                // shared default in when none was sent), so "the layer still carries the shared
                // default" is the closest thing to "untouched" that exists at layer level.
                return { value: inherited, isDefault: inherited === defaults[field] };
            }
            return { value: defaults[field], isDefault: true };
        }

        // Alan 8/17/26 - Split a stored label into the lines the SVG will actually draw. The
        // editor is a textarea and the validator preserves newlines, but a plain <text> node
        // collapses them, so multiline labels used to render as one run-on line.
        _annotationLabelLines(label) {
            const text = String(label === null || label === undefined ? '' : label);
            // Alan 8/17/26 - Normalize tabs and cap lines exactly as the server validator does.
            const lines = text.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
                .replace(/\t/g, '    ').split('\n');
            // Keep interior blank lines (they are deliberate spacing) but never let a
            // trailing newline add an empty row that shifts the block off-centre.
            while (lines.length > 1 && lines[lines.length - 1].trim() === '') lines.pop();
            return (lines.length ? lines : ['']).slice(0, 10);
        }

        // Alan 8/17/26 - Read old missing/short-lived type values lazily without rewriting state.
        _annotationType(annotation) {
            const type = annotation?.annotation_type;
            if (type === 'branch_text') return 'branch_text';
            if (type === 'branch_bubble' || type === 'bubble') return 'branch_bubble';
            // Alan 8/24/26 - Highlights are a clade annotation, so they keep the clade lane,
            // the bracket and the label, and add a band behind the clade.
            if (type === 'clade_highlight') return 'clade_highlight';
            return 'clade_line';
        }

        // ==================================================================
        // Alan 8/24/26 - AUTOMATIC HIGHLIGHT COLOURS
        //
        // A clade highlight that the user has not styled does not fall back to one shared
        // colour. It resolves, in order, to:
        //   1. the persistent colour group its tips already belong to, softened for use as a
        //      large translucent area;
        //   2. a palette colour chosen so successive highlights differ from one another.
        //
        // Everything here is a pure function of the saved annotations plus the existing
        // selection-set state, so the same tree always produces the same colours, and the
        // editor preview can show exactly what the figure will draw before anything is saved.
        // ==================================================================

        _hexToRgb(hex) {
            const value = typeof hex === 'string' ? hex.trim() : '';
            if (!/^#[0-9a-fA-F]{6}$/.test(value)) return null;
            return {
                r: parseInt(value.slice(1, 3), 16),
                g: parseInt(value.slice(3, 5), 16),
                b: parseInt(value.slice(5, 7), 16)
            };
        }

        _rgbToHex(r, g, b) {
            const channel = (v) => Math.max(0, Math.min(255, Math.round(v)))
                .toString(16).padStart(2, '0');
            return `#${channel(r)}${channel(g)}${channel(b)}`;
        }

        // Alan 8/24/26 - Standard sRGB <-> HSL, kept local so softening a group colour needs
        // no colour library and stays byte-for-byte reproducible across browsers.
        _hexToHsl(hex) {
            const rgb = this._hexToRgb(hex);
            if (!rgb) return null;
            const r = rgb.r / 255, g = rgb.g / 255, b = rgb.b / 255;
            const max = Math.max(r, g, b), min = Math.min(r, g, b);
            const l = (max + min) / 2;
            const d = max - min;
            if (d === 0) return { h: 0, s: 0, l };
            const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
            let h;
            if (max === r) h = ((g - b) / d + (g < b ? 6 : 0)) / 6;
            else if (max === g) h = ((b - r) / d + 2) / 6;
            else h = ((r - g) / d + 4) / 6;
            return { h, s, l };
        }

        _hslToHex(h, s, l) {
            if (s === 0) return this._rgbToHex(l * 255, l * 255, l * 255);
            const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
            const p = 2 * l - q;
            const toChannel = (t) => {
                let value = t;
                if (value < 0) value += 1;
                if (value > 1) value -= 1;
                if (value < 1 / 6) return p + (q - p) * 6 * value;
                if (value < 1 / 2) return q;
                if (value < 2 / 3) return p + (q - p) * (2 / 3 - value) * 6;
                return p;
            };
            return this._rgbToHex(
                toChannel(h + 1 / 3) * 255, toChannel(h) * 255, toChannel(h - 1 / 3) * 255
            );
        }

        /**
         * Alan 8/24/26 - Turn a group's line colour into a background of the same hue.
         *
         * The categorical colours the colour groups use are picked to be legible as thin
         * marks; spread over a whole clade at any useful opacity they read as neon. Keeping
         * the hue and capping saturation and lightness gives an area fill that is recognisably
         * "the blue group" without shouting. Deterministic, so the same group always softens
         * to the same fill.
         */
        _softenHighlightColor(hex) {
            const hsl = this._hexToHsl(hex);
            if (!hsl) return null;
            return this._hslToHex(hsl.h, Math.min(hsl.s, 0.5), 0.45);
        }

        /**
         * Alan 8/26/26 - The colour a group mark or tip label is actually PAINTED with.
         *
         * Hue and saturation are kept, so a tip still reads as "the blue group"; only lightness
         * is floored, and only in dark mode. Light mode is untouched.
         *
         * Deliberately NOT folded into getSelectionSetColor(): that value is the user's stored
         * choice and has to keep flowing unchanged to the swatches, the group editor and
         * _persistentGroupColorForTips(), which derives clade-highlight fills from it. Lifting
         * it there would make saved state and highlight fills drift with the theme.
         *
         * Resolved here and written inline by the caller, never as a CSS override, for the same
         * reason the highlight styles are: the export path clones the live SVG without the
         * page stylesheet, so a themed CSS rule would make the exported figure differ from the
         * one on screen.
         */
        _displayColorForTheme(hex) {
            if (!hex || !this._isDarkTheme()) return hex;
            const hsl = this._hexToHsl(hex);
            if (!hsl) return hex;
            if (hsl.l >= DARK_LABEL_MIN_LIGHTNESS) return hex;
            return this._hslToHex(hsl.h, hsl.s, DARK_LABEL_MIN_LIGHTNESS);
        }

        /**
         * Alan 8/24/26 - Which persistent colour group owns this tip, or null.
         *
         * Split out of _getNodeDisplayColor so the highlight and the tip label agree about
         * group membership by construction. Deliberately stops before that method's search-match
         * and sequence-of-interest colours: those are transient view state, not a statement that
         * these taxa are a group, and a highlight derived from them would change colour when the
         * user typed in the search box.
         */
        _persistentGroupNameForTip(id) {
            if (!id || !this.selectionSets) return null;
            const setNames = Object.keys(this.selectionSets);
            // The active group wins, so adding a tip to a group visibly recolours it.
            if (setNames.includes(this.activeSelectionSet)
                && this.selectionSets[this.activeSelectionSet]?.has(id)) {
                return this.activeSelectionSet;
            }
            for (const name of setNames) {
                if (name === this.activeSelectionSet) continue;
                if (this.selectionSets[name]?.has(id)) return name;
            }
            return null;
        }

        /**
         * Alan 8/24/26 - The shared group colour for a whole clade, or null.
         *
         * Requires EVERY member to be in the same group. A clade of 18 blue tips and 5 orange
         * ones has no single group colour, and averaging them would invent a muddy brown that
         * corresponds to nothing in the figure, so mixed membership falls through to the
         * palette instead.
         */
        _persistentGroupColorForTips(memberIds) {
            const ids = Array.isArray(memberIds) ? memberIds.filter(Boolean) : [];
            if (!ids.length) return null;
            let groupName = null;
            for (const id of ids) {
                const name = this._persistentGroupNameForTip(id);
                if (!name) return null;
                if (groupName === null) groupName = name;
                else if (groupName !== name) return null;
            }
            const color = groupName ? this.getSelectionSetColor(groupName) : null;
            return color ? this._softenHighlightColor(color) : null;
        }

        /**
         * Alan 8/24/26 - Is this highlight's colour chosen, or worked out from the tree?
         *
         * An annotation-level colour is always a choice. Otherwise the stored mode decides,
         * annotation first and then layer. When neither carries a mode -- state saved before
         * the field existed -- fall back to the old rule that a layer still holding the shared
         * default gold was never styled, so those keep behaving exactly as they did.
         */
        _resolveHighlightColorIntent(annotation, layer) {
            const defaults = window.DikaryaCladeAnnotations.DEFAULTS;
            const own = annotation ? annotation.highlight_color : null;
            if (own !== null && own !== undefined && own !== '') {
                return { mode: 'fixed', color: own };
            }
            const layerColor = layer ? layer.default_highlight_color : null;
            const fixedColor = (layerColor !== null && layerColor !== undefined && layerColor !== '')
                ? layerColor : defaults.highlight_color;

            const ownMode = annotation ? annotation.highlight_color_mode : null;
            if (ownMode === 'fixed') return { mode: 'fixed', color: fixedColor };
            if (ownMode === 'auto') return { mode: 'auto', color: null };

            const layerMode = layer ? layer.default_highlight_color_mode : null;
            if (layerMode === 'fixed') return { mode: 'fixed', color: fixedColor };
            if (layerMode === 'auto') return { mode: 'auto', color: null };

            // No mode stored anywhere: the legacy reading of the colour value.
            return (layerColor === null || layerColor === undefined || layerColor === ''
                || layerColor === defaults.highlight_color)
                ? { mode: 'auto', color: null }
                : { mode: 'fixed', color: layerColor };
        }

        _highlightColorIsAutomatic(annotation, layer) {
            return this._resolveHighlightColorIntent(annotation, layer).mode === 'auto';
        }

        /**
         * Alan 8/24/26 - Are two candidate band colours close enough to read as the same wash?
         *
         * Spread over a clade at 0.2 opacity, two colours 10 degrees of hue apart are simply
         * one colour with a wobble -- which is what a softened group blue (#397dac) and the
         * first palette blue (#3b6fb6) were. Compares hue first, but only calls it a clash when
         * lightness and saturation are also close, so a deliberately pale and a deliberately
         * deep version of one hue still read as two regions. Near-greys have no usable hue and
         * are compared on lightness alone.
         */
        _highlightColorsAreTooClose(a, b) {
            const x = this._hexToHsl(a);
            const y = this._hexToHsl(b);
            if (!x || !y) return false;
            const dl = Math.abs(x.l - y.l);
            const ds = Math.abs(x.s - y.s);
            if (x.s < 0.12 || y.s < 0.12) return dl < 0.15 && ds < 0.2;
            let dh = Math.abs(x.h - y.h);
            if (dh > 0.5) dh = 1 - dh;
            // 0.055 of the hue circle is about 20 degrees.
            return dh < 0.055 && dl < 0.25 && ds < 0.4;
        }

        /**
         * Alan 8/24/26 - Work out the fill for every clade highlight in one pass.
         *
         * Two passes, because a colour that is already decided must be visible to a colour
         * that is still being chosen:
         *
         *   Pass 1 places everything whose colour is already settled -- a colour the user
         *   fixed, a colour derived from the clade's persistent colour group, and a palette
         *   slot that was stored when the annotation was first saved. These are never changed.
         *
         *   Pass 2 chooses palette colours for the rest, avoiding any colour that would read
         *   as the same wash as a neighbouring band, whichever pass placed it. Fixed and
         *   derived colours therefore take part in clash avoidance without consuming a palette
         *   slot -- the palette exists for clades with no colour of their own.
         *
         * Every clade_highlight is walked, including ones the current rooting cannot resolve
         * and ones on hidden layers, so neither a reroot nor a hidden layer can renumber the
         * colours of anything else.
         *
         * @param {Map} positions   canonical leaf id -> {index}, for the adjacency rule. Optional.
         * @param {Object} pending  an unsaved annotation being previewed, appended at the end.
         * @returns {{colors: Map, slots: Map}} id -> hex, and id -> palette index for the
         *          highlights that were given one in pass 2.
         */
        _automaticHighlightAssignments(positions = null, pending = null) {
            const layerById = new Map();
            for (const layer of this.annotationLayers || []) {
                if (layer && layer.id) layerById.set(layer.id, layer);
            }

            const entries = [];
            for (const annotation of this.cladeAnnotations || []) {
                if (!annotation || !annotation.id) continue;
                if (this._annotationType(annotation) !== 'clade_highlight') continue;
                const layer = layerById.get(annotation.layer_id) || null;
                const intent = this._resolveHighlightColorIntent(annotation, layer);
                entries.push({
                    id: annotation.id,
                    members: annotation.member_tip_ids || [],
                    intent,
                    slot: Number.isInteger(annotation.automatic_highlight_slot)
                        ? annotation.automatic_highlight_slot : null
                });
            }
            if (pending && (pending.id || pending.annotation?.id)) {
                // Alan 8/24/26 - A draft may arrive either as the bare (id, members, slot)
                // triple the swatch asks with, or as the whole annotation object the editor is
                // about to save. When the object is given, its OWN colour intent, membership
                // and slot are used, because those -- not the pre-edit ones still sitting in
                // this.cladeAnnotations -- are what the save will write.
                const draftAnnotation = pending.annotation || null;
                const draftLayer = draftAnnotation
                    ? (pending.layer || layerById.get(draftAnnotation.layer_id) || null)
                    : null;
                const draftId = pending.id || draftAnnotation.id;
                const draftMembers = pending.memberIds
                    || (draftAnnotation && draftAnnotation.member_tip_ids) || [];
                const draftSlot = Number.isInteger(pending.slot)
                    ? pending.slot
                    : (draftAnnotation
                        && Number.isInteger(draftAnnotation.automatic_highlight_slot)
                        ? draftAnnotation.automatic_highlight_slot : null);
                const draft = {
                    id: draftId,
                    members: draftMembers,
                    intent: draftAnnotation
                        ? this._resolveHighlightColorIntent(draftAnnotation, draftLayer)
                        : { mode: 'auto', color: null },
                    // A slot the editor is reserving is one the annotation does not hold yet,
                    // so forcing the palette path also means ignoring any stored slot; that is
                    // what lets the editor ask for the slot a clade would get even while it
                    // derives a group colour.
                    slot: pending.forcePalette ? null : draftSlot,
                    forcePalette: !!pending.forcePalette
                };
                // Alan 8/24/26 - In edit mode the pending id names an annotation that is
                // already saved. The edited membership and colour intent are what the user is
                // looking at, so they REPLACE the saved entry instead of being discarded --
                // dropping them made the preview resolve against the pre-edit annotation and
                // show a colour the save would not produce.
                const index = entries.findIndex((entry) => entry.id === draftId);
                if (index >= 0) entries[index] = draft;
                else entries.push(draft);
            }

            const span = (members) => {
                if (!positions) return null;
                let min = Infinity;
                let max = -Infinity;
                for (const member of members) {
                    const entry = positions.get(member);
                    if (!entry) continue;
                    if (entry.index < min) min = entry.index;
                    if (entry.index > max) max = entry.index;
                }
                return min === Infinity ? null : { min, max };
            };
            // Touching counts as adjacent, so two clades stacked one directly above the other
            // do not come out the same colour.
            const touches = (a, b) => !!a && !!b && a.min <= b.max + 1 && b.min <= a.max + 1;

            const colors = new Map();
            const slots = new Map();
            const placed = [];
            const useCount = HIGHLIGHT_PALETTE.map(() => 0);
            const deferred = [];

            for (const entry of entries) {
                entry.span = span(entry.members);
                if (entry.intent.mode === 'fixed') {
                    // Never altered, but still blocks a neighbour from picking its twin.
                    placed.push({ color: entry.intent.color, span: entry.span });
                    continue;
                }
                const group = entry.forcePalette
                    ? null : this._persistentGroupColorForTips(entry.members);
                if (group) {
                    colors.set(entry.id, group);
                    placed.push({ color: group, span: entry.span });
                    continue;
                }
                if (entry.slot !== null) {
                    const index = ((entry.slot % HIGHLIGHT_PALETTE.length)
                        + HIGHLIGHT_PALETTE.length) % HIGHLIGHT_PALETTE.length;
                    const color = HIGHLIGHT_PALETTE[index];
                    colors.set(entry.id, color);
                    slots.set(entry.id, entry.slot);
                    useCount[index] += 1;
                    placed.push({ color, span: entry.span });
                    continue;
                }
                deferred.push(entry);
            }

            for (const entry of deferred) {
                const neighbourColors = placed
                    .filter((other) => touches(entry.span, other.span))
                    .map((other) => other.color);
                const order = HIGHLIGHT_PALETTE
                    .map((_, index) => index)
                    .sort((a, b) => (useCount[a] - useCount[b]) || (a - b));
                let chosen = order.find((index) => !neighbourColors.some(
                    (color) => this._highlightColorsAreTooClose(HIGHLIGHT_PALETTE[index], color)
                ));
                // Every colour clashes with something next door: take the least-used one, so
                // the outcome is still deterministic rather than arbitrary.
                if (chosen === undefined) chosen = order[0];
                useCount[chosen] += 1;
                colors.set(entry.id, HIGHLIGHT_PALETTE[chosen]);
                slots.set(entry.id, chosen);
                placed.push({ color: HIGHLIGHT_PALETTE[chosen], span: entry.span });
            }

            return { colors, slots };
        }

        /**
         * Alan 8/24/26 - The colour one highlight would receive with nothing chosen for it.
         *
         * Used by the renderer and by the editor's live preview, so what the user sees before
         * saving is what gets drawn. `annotationId` may name an annotation that does not exist
         * yet; it is then assigned last, which is where saving will put it.
         */
        resolveAutomaticHighlightColor(annotationId, memberIds, slot = null, draft = null) {
            const { colors } = this._automaticHighlightAssignments(
                this._lastAnnotationPositions || null,
                { id: annotationId, memberIds, slot,
                  annotation: draft?.annotation || null, layer: draft?.layer || null }
            );
            return colors.get(annotationId) || HIGHLIGHT_PALETTE[0];
        }

        /**
         * Alan 8/24/26 - The colour a DRAFT highlight would be painted with, resolved from the
         * draft itself rather than from whatever is currently saved under the same id.
         *
         * The editor mutates a copy: while "Fixed red" is being switched to "Automatic", the
         * saved annotation still says fixed. Feeding the saved object into the assignment made
         * the preview and the swatch show a colour the save would not produce, because a fixed
         * entry takes no palette slot and a draft that is now automatic needs one. The draft
         * carries its own id, membership, layer, colour, mode and stored slot, so it can stand
         * in for the future saved state exactly -- and it REPLACES the saved entry rather than
         * being appended, so the annotation is never counted twice.
         */
        resolveDraftHighlightColor(annotation, layer = null) {
            if (!annotation || !annotation.id) return HIGHLIGHT_PALETTE[0];
            const { colors } = this._automaticHighlightAssignments(
                this._lastAnnotationPositions || null, { annotation, layer }
            );
            return colors.get(annotation.id) || HIGHLIGHT_PALETTE[0];
        }

        /**
         * Alan 8/24/26 - The palette slot a new automatic highlight should be saved with.
         *
         * Stored on the annotation so its colour survives other annotations being deleted or
         * added. Deliberately computed as if the clade had no colour group: a clade that is in
         * one today takes its fill from the group, but if it later leaves the group the stored
         * slot is what stops it jumping to whatever colour is free at that moment.
         */
        reserveAutomaticHighlightSlot(annotationId, memberIds) {
            const { slots } = this._automaticHighlightAssignments(
                this._lastAnnotationPositions || null,
                { id: annotationId, memberIds, forcePalette: true }
            );
            const slot = slots.get(annotationId);
            // Alan 8/24/26 - No slot means "nothing was reserved", which must NOT collapse to
            // 0: 0 is a real palette slot, and a stored slot beats the clash-avoidance pass, so
            // returning it here used to pin the highlight to the first palette blue forever --
            // even directly beside another blue band. Null lets the renderer choose each time.
            return Number.isInteger(slot) ? slot : null;
        }

        // Alan 8/24/26 - True when the page is in dark mode. Guarded: the annotation code also
        // runs under Node in the regression harness, which has no documentElement.
        _isDarkTheme() {
            try {
                return !!document.documentElement?.classList?.contains('dark');
            } catch (_) {
                return false;
            }
        }

        /**
         * Alan 8/24/26 - The colour and opacity a highlight is actually drawn with.
         *
         * Both are resolved here and written inline by the caller. Nothing about a highlight's
         * appearance may live in a stylesheet: the export path clones the live SVG without the
         * page's CSS, so a themed CSS override would silently make the exported figure differ
         * from the one the user was looking at.
         */
        _effectiveHighlightStyle(item, assignments = null) {
            const style = item.style || {};
            const intent = this._resolveHighlightColorIntent(item.annotation, item.layer);
            const color = intent.mode === 'fixed'
                ? intent.color
                : ((assignments && assignments.get(item.annotation?.id))
                    // Alan 8/24/26 - No precomputed assignment means this is a one-off
                    // resolution -- the editor preview. Resolve it from the item's OWN
                    // annotation and layer, which in the editor is the unsaved draft.
                    || this.resolveDraftHighlightColor(item.annotation, item.layer));
            // An opacity the user picked is theirs in both themes; only the automatic one
            // adapts, because a wash tuned for white is invisible on black.
            const opacity = style.highlight_opacity_is_default
                ? (this._isDarkTheme() ? AUTO_HIGHLIGHT_OPACITY.dark : AUTO_HIGHLIGHT_OPACITY.light)
                : style.highlight_opacity;
            return { color, opacity };
        }

        // Alan 8/24/26 - Automatic highlight opacity depends on the theme, and the theme can be
        // toggled while a tree is on screen, so repaint when the class changes. One observer per
        // viewer; harmless where there is no DOM to observe.
        // Alan 8/26/26 - Group mark/label colours are theme-dependent too now
        // (_displayColorForTheme), and they are written inline, so a toggle has to restyle the
        // nodes as well or the labels keep the previous theme's colours until an unrelated
        // redraw. Guarded on this.tree the same way the other out-of-band callers are.
        _watchThemeChanges() {
            if (this._themeObserver || typeof MutationObserver !== 'function') return;
            try {
                const root = document.documentElement;
                if (!root) return;
                this._themeObserver = new MutationObserver(() => {
                    if (this.tree && typeof this._updateNodeStylesOnly === 'function') {
                        this._updateNodeStylesOnly();
                    }
                    this._scheduleAnnotationRedraw();
                });
                this._themeObserver.observe(root, {
                    attributes: true, attributeFilter: ['class']
                });
            } catch (_) { /* no DOM to watch; inline styles are still correct as drawn */ }
        }

        // Alan 8/24/26 - True for the types that annotate a clade as a whole rather than one
        // incoming branch. These are the ones that occupy a right-hand lane and that remain
        // valid on the root, which has no incoming branch to hang a label on.
        _isCladeAnnotationType(type) {
            return type === 'clade_line' || type === 'clade_highlight';
        }

        // Alan 8/15/26 - Measure rendered label width with a throwaway <text> in the live SVG,
        // cached by text+font so long lists of annotations do not re-measure every redraw.
        _measureAnnotationText(svgNode, text, style) {
            const key = `${style.font_family}|${style.font_size}|${style.font_style}|${style.font_weight}|${text}`;
            const cached = this.annotationTextWidthCache.get(key);
            if (cached !== undefined) return cached;

            const probe = document.createElementNS('http://www.w3.org/2000/svg', 'text');
            probe.setAttribute('visibility', 'hidden');
            probe.style.fontFamily = annotationFontStack(style.font_family);
            probe.style.fontSize = `${style.font_size}px`;
            probe.style.fontStyle = style.font_style;
            probe.style.fontWeight = style.font_weight;
            probe.textContent = text;
            svgNode.appendChild(probe);
            let width = 0;
            try { width = probe.getComputedTextLength(); } catch (_) { width = text.length * style.font_size * 0.55; }
            svgNode.removeChild(probe);

            // Bound the cache so a user typing in the editor cannot grow it without limit.
            if (this.annotationTextWidthCache.size > 2000) this.annotationTextWidthCache.clear();
            this.annotationTextWidthCache.set(key, width);
            return width;
        }

        // Alan 8/17/26 - A multiline label is as wide as its WIDEST line, not as wide as the
        // whole string with newlines in it, so the lane reserves the right amount of space.
        _measureAnnotationLabel(svgNode, lines, style) {
            let width = 0;
            for (const line of lines) {
                width = Math.max(width, this._measureAnnotationText(svgNode, line, style));
            }
            return width;
        }

        /**
         * Alan 8/15/26 - One pass over the rendered leaves producing everything the
         * annotation layout needs: vertical position per canonical leaf ID, the tip order
         * used for contiguity/clade tests, the right edge of the drawn tip labels, and the
         * row pitch used to size single-tip ticks.
         */
        _buildAnnotationGeometry(svg) {
            const positions = new Map();
            const leaves = [];
            let labelRight = -Infinity;
            const self = this;

            svg.selectAll('g.node, g.internal-node').each(function (d) {
                if (!d) return;
                const children = d.children || d.data?.children || [];
                if (children && children.length) return;
                const x = (typeof d.screen_x === 'number') ? d.screen_x : d.y;
                const y = (typeof d.screen_y === 'number') ? d.screen_y : d.x;
                if (typeof x !== 'number' || typeof y !== 'number') return;
                const right = self._tipLabelRightEdge(this, x);
                if (Number.isFinite(right) && right > labelRight) labelRight = right;
                leaves.push({ node: d, y, right });
            });

            if (!leaves.length) return null;

            leaves.sort((a, b) => a.y - b.y);
            const tipOrder = [];
            // Alan 8/24/26 - The right edge of EACH row's own label, in tip order. A clade
            // annotation is placed from the labels of its own descendants, so one very long
            // name elsewhere in the tree cannot push a small local annotation across the page.
            const tipRights = [];
            leaves.forEach((leaf, index) => {
                const id = self._getNodeId(leaf.node);
                if (!id) return;
                // A duplicated canonical name cannot address one leaf; the server refuses
                // membership for it, so the first occurrence here is never load-bearing.
                if (!positions.has(id)) positions.set(id, { y: leaf.y, index: tipOrder.length });
                tipOrder.push(id);
                tipRights.push(Number.isFinite(leaf.right) ? leaf.right : null);
            });

            // Row pitch from the median gap, so uneven spacing does not produce silly ticks.
            const gaps = [];
            for (let i = 1; i < leaves.length; i += 1) gaps.push(leaves[i].y - leaves[i - 1].y);
            gaps.sort((a, b) => a - b);
            const rowPitch = gaps.length ? (gaps[Math.floor(gaps.length / 2)] || 10) : 10;

            // Alan 8/24/26 - Kept so the editor's live preview can work out which automatic
            // colour a not-yet-saved highlight will get, including the adjacency rule, without
            // re-measuring the DOM on every keystroke.
            this._lastAnnotationPositions = positions;

            return {
                positions,
                tipOrder,
                tipRights,
                labelRight: Number.isFinite(labelRight) ? labelRight : 0,
                rowPitch: Math.max(4, rowPitch)
            };
        }

        /**
         * Alan 8/24/26 - The right-hand edge of one leaf's VISIBLE tip label.
         *
         * Was the bounding box of the whole leaf <g>. That box is the union of everything the
         * group happens to contain, which is not only the label: phylotree puts the node bubble
         * in there, and it appends a branch-tracer <line> running all the way out to
         * right_most_leaf when tip alignment is on -- and never removes that line for a leaf
         * when alignment is switched back off. Any of those makes the measured edge far wider
         * than the text, and since this value is where the annotation lane starts, the result
         * was a large empty band between the tip labels and the bracket.
         *
         * Measuring the label element itself is exact whichever of those is responsible. The
         * text carries its own transform when tips are aligned, and getBBox() reports a child's
         * geometry before its own transform, so that translation is added back here. Falls back
         * to the old group measurement for anything without a rendered label.
         */
        _tipLabelRightEdge(groupElement, nodeX) {
            const measure = (element, offsetX) => {
                try {
                    const box = element.getBBox();
                    if (!box || !(box.width > 0)) return null;
                    return nodeX + offsetX + box.x + box.width;
                } catch (_) {
                    return null;
                }
            };

            let label = null;
            try {
                label = groupElement.querySelector('text.phylotree-node-text');
            } catch (_) { /* fall through to the group measurement */ }

            if (label) {
                let offsetX = 0;
                try {
                    // Rectangular layout only translates; radial rotation is out of scope for
                    // annotations, and consolidate() returns null when there is no transform.
                    const matrix = label.transform?.baseVal?.consolidate()?.matrix;
                    if (matrix && Number.isFinite(matrix.e)) offsetX = matrix.e;
                } catch (_) { /* untransformed label */ }
                const right = measure(label, offsetX);
                if (right !== null) return right;
            }

            const fallback = measure(groupElement, 0);
            // A leaf with no drawn label at all still occupies its own branch tip.
            return fallback !== null ? fallback : nodeX;
        }

        /**
         * Alan 8/15/26 - Index every node's descendant leaf block once per redraw.
         * In a rectangular layout a clade's leaves are exactly one contiguous run of the
         * tip order, so "does this member set form one clade?" becomes a set lookup on
         * "firstIndex:lastIndex" instead of a fresh traversal per annotation.
         */
        // Alan 8/17/26 - Build clade and incoming-branch topology indexes together.
        _buildAnnotationTopologyIndexes(positions) {
            const cladeBlocks = new Set();
            const branchNodes = new Map();
            // Alan 8/24/26 - Also keep the node ITSELF for every clade block, root included.
            // A clade highlight starts at its internal node's x coordinate, and the root has
            // no incoming branch, so branchNodes alone cannot supply it.
            const cladeNodes = new Map();
            const visit = (node) => {
                const children = node.children || [];
                if (!children.length) {
                    const id = this._getNodeId(node);
                    const entry = id ? positions.get(id) : null;
                    if (!entry) return null;
                    // Alan 8/17/26 - Index terminal clades while excluding only a root branch target.
                    const key = `${entry.index}:${entry.index}`;
                    cladeBlocks.add(key);
                    if (!cladeNodes.has(key)) cladeNodes.set(key, node);
                    if (node.parent) branchNodes.set(key, node);
                    return { min: entry.index, max: entry.index };
                }
                let min = Infinity;
                let max = -Infinity;
                for (const child of children) {
                    const span = visit(child);
                    if (!span) continue;
                    if (span.min < min) min = span.min;
                    if (span.max > max) max = span.max;
                }
                if (min === Infinity) return null;
                const key = `${min}:${max}`;
                // Alan 8/17/26 - Every node, including the root, is a valid clade block for
                // a publication bracket; only non-root nodes own an incoming branch.
                cladeBlocks.add(key);
                // Alan 8/24/26 - A knuckle (one child) repeats its child's descendant block.
                // Children are visited before this line, so the first writer for a key is
                // always the deepest node carrying it -- the one drawn closest to the clade it
                // labels. An unconditional set() here put the SHALLOWEST node back, which
                // started the band out at the top of the unary chain instead.
                if (!cladeNodes.has(key)) cladeNodes.set(key, node);
                if (node.parent) branchNodes.set(key, node);
                return { min, max };
            };
            for (const root of this.allNodes) {
                if (!root.parent) visit(root);
            }
            return { cladeBlocks, branchNodes, cladeNodes };
        }

        // Alan 8/17/26 - Retain the incoming-branch index helper for renderer compatibility.
        _buildCladeNodeIndex(positions) {
            return this._buildAnnotationTopologyIndexes(positions).branchNodes;
        }

        // Alan 8/17/26 - Retain the clade-block helper for DOM-free geometry callers.
        _buildCladeBlockIndex(positions) {
            return this._buildAnnotationTopologyIndexes(positions).cladeBlocks;
        }

        // Alan 8/15/26 - True when the sorted tip indices occupy one unbroken block of the
        // current tip order. Necessary for a clade but NOT sufficient: a rerooting can leave
        // a member set sitting together on screen while no node has exactly that descendant
        // set, so _resolveAnnotationsForRender also demands a matching clade block.
        _annotationIsContiguous(indices) {
            if (!indices.length) return false;
            return indices[indices.length - 1] - indices[0] + 1 === indices.length;
        }

        /**
         * Alan 8/15/26 - Decide, for every stored annotation, whether it still describes
         * exactly one clade and therefore may be drawn.
         *
         * Split out of _renderCladeAnnotations so the decision is testable without a DOM:
         * it takes the geometry it needs and returns plain data.
         *
         * An annotation that is no longer exactly one clade is reported as invalid and is
         * NOT returned for drawing. Previously a contiguous-but-not-a-clade set was still
         * drawn as one continuous (dashed) vertical bracket, which reads as a taxonomic
         * claim about taxa that no longer form that group. The Annotation Manager keeps
         * showing the warning, the state stays persisted, and rerooting back to a topology
         * where the set is a clade again makes it render again on the next redraw.
         */
        // Alan 8/17/26 - Accept a precomputed incoming-branch index for type-aware resolution.
        // Alan 8/24/26 - Accept the clade-node index too, so a highlight knows where its
        // internal node is drawn. Optional, so existing four-argument callers still work.
        _resolveAnnotationsForRender(positions, cladeBlocks, layerById, branchNodes = null,
                                     cladeNodes = null) {
            const validity = new Map();
            const resolved = [];
            // Alan 8/17/26 - Accept the precomputed branch index, with compatibility fallbacks.
            const incomingBranchNodes = branchNodes
                || (cladeBlocks instanceof Map
                    ? cladeBlocks
                    : this._buildCladeNodeIndex(positions));
            for (const annotation of this.cladeAnnotations) {
                if (!annotation || !annotation.id) continue;
                const layer = layerById.get(annotation.layer_id) || null;
                const members = Array.isArray(annotation.member_tip_ids) ? annotation.member_tip_ids : [];
                const seen = new Set();
                const indices = [];
                for (const member of members) {
                    const entry = positions.get(member);
                    if (!entry || seen.has(entry.index)) continue;
                    seen.add(entry.index);
                    indices.push(entry.index);
                }
                if (!indices.length) {
                    validity.set(annotation.id, { present: 0, valid: false });
                    continue;
                }
                indices.sort((a, b) => a - b);
                // Alan 8/17/26 - Branch text and bubbles additionally require a non-root target.
                const blockKey = `${indices[0]}:${indices[indices.length - 1]}`;
                const annotationType = this._annotationType(annotation);
                const isClade = this._annotationIsContiguous(indices)
                    && cladeBlocks.has(blockKey);
                const softPolytomy = this._annotationIsContiguous(indices)
                    ? this._zeroLengthPolytomyForMemberIds(members) : null;
                const hasIncomingBranch = incomingBranchNodes.has(blockKey);
                // Alan 8/24/26 - Clade lines and clade highlights need only be one clade;
                // the branch types additionally need a branch to sit on.
                const valid = (isClade || Boolean(softPolytomy))
                    && (this._isCladeAnnotationType(annotationType) || hasIncomingBranch);
                validity.set(annotation.id, { present: indices.length, valid });
                // Invalid annotations are kept in state and flagged in the manager, but they
                // are never drawn: any line beside the tips would imply a clade that the
                // current topology does not contain.
                if (!valid) continue;
                // Hidden layers keep their annotations but neither draw nor reserve width.
                if (!layer || layer.visible === false) continue;
                // Alan 8/17/26 - Carry the target node and save order into branch stacking.
                resolved.push({
                    annotation,
                    layer,
                    indices,
                    targetNode: incomingBranchNodes.get(blockKey) || null,
                    // Alan 8/24/26 - The node the highlight band starts at. Falls back to the
                    // incoming-branch target, which is the same node everywhere but the root.
                    cladeNode: (cladeNodes && cladeNodes.get(blockKey))
                        || incomingBranchNodes.get(blockKey)
                        || softPolytomy?.anchor || null,
                    savedIndex: this.cladeAnnotations.indexOf(annotation)
                });
            }
            return { validity, resolved };
        }

        /**
         * Alan 8/15/26 - Draw all visible annotations into a dedicated <g> inside phylotree's
         * own container group, so they share the tree's coordinate space and zoom/pan transform.
         */
        _renderCladeAnnotations() {
            const svg = window.d3v7.select(this.container).select('svg');
            if (svg.empty()) return;
            const svgNode = svg.node();

            // Annotations belong to phylotree's own container group: it holds the node
            // groups, so the coordinate space matches, and it carries the zoom/pan transform.
            const enclosure = svg.select('g.phylotree-container');
            if (enclosure.empty()) return;

            enclosure.selectAll('g.clade-annotations').remove();
            // Alan 8/24/26 - Highlight bands live in their own group so they can sit BEHIND
            // the branches, node/support labels and tip labels rather than on top of them.
            enclosure.selectAll('g.clade-annotation-highlights').remove();
            this.annotationValidity = new Map();

            // Remember the width phylotree itself wants, so hiding a layer gives the space
            // back. Whenever phylotree writes a fresh width (spacing, sort, layout change) it
            // differs from the value we last wrote, and that fresh value becomes the new base.
            const currentWidth = svgNode.getAttribute('width') || '';
            if (currentWidth !== svgNode.getAttribute('data-annotation-set-width')) {
                svgNode.setAttribute('data-annotation-base-width', currentWidth);
            }
            const baseWidth = parseFloat(svgNode.getAttribute('data-annotation-base-width'));
            // Alan 8/17/26 - Preserve phylotree's own viewBox separately from annotation expansion.
            const currentViewBox = svgNode.getAttribute('viewBox') || '';
            if (currentViewBox !== svgNode.getAttribute('data-annotation-set-viewbox')) {
                svgNode.setAttribute('data-annotation-base-viewbox', currentViewBox);
            }
            const baseViewBox = svgNode.getAttribute('data-annotation-base-viewbox') || '';
            const setWidth = (value) => {
                svgNode.setAttribute('width', String(value));
                svgNode.setAttribute('data-annotation-set-width', String(value));
            };
            const restoreWidth = () => {
                if (Number.isFinite(baseWidth)) setWidth(baseWidth);
                // Alan 8/17/26 - Restore both dimensions of the original viewport with its width.
                if (baseViewBox) svgNode.setAttribute('viewBox', baseViewBox);
                else svgNode.removeAttribute('viewBox');
                svgNode.setAttribute('data-annotation-set-viewbox', baseViewBox);
            };

            const layerById = new Map();
            for (const layer of this.annotationLayers) {
                if (layer && layer.id) layerById.set(layer.id, layer);
            }

            if (!this.cladeAnnotations.length) {
                this._annotationsDrawn = false;
                restoreWidth();
                return;
            }

            // Radial layout is deliberately out of scope: a vertical bracket has no meaning
            // there. State stays persisted and reappears when the tree returns to rectangular.
            if (this.options.layout === 'radial') {
                this._annotationsDrawn = false;
                restoreWidth();
                return;
            }

            // Alan 8/24/26 - Automatic opacity follows the theme, which can be toggled with a
            // tree on screen; repaint when it changes.
            this._watchThemeChanges();
            const geometry = this._buildAnnotationGeometry(svg);
            if (!geometry) { this._annotationsDrawn = false; restoreWidth(); return; }
            const { positions, labelRight, rowPitch } = geometry;
            // Alan 8/17/26 - Build clade validity and incoming-branch targets in the same pass.
            // Alan 8/24/26 - cladeNodes additionally covers the root, which highlights allow.
            const { cladeBlocks, branchNodes, cladeNodes } =
                this._buildAnnotationTopologyIndexes(positions);

            // Scale annotation text with the matching desktop/mobile tip-label behavior so the
            // bracket keeps its relationship to the labels.
            const { k } = this._getSvgAndZoomGroup();
            // Alan 8/28/26 - Match coarse-pointer annotations to the uniform mobile tree zoom;
            // desktop annotations keep following desktop's constant-screen-size labels.
            const uniformMobileZoom = window.matchMedia?.('(pointer: coarse)')?.matches;
            const scale = uniformMobileZoom ? 1 : 1 / (k || 1);

            const {
                GAP_FROM_TREE, GAP_BETWEEN_LANES, LINE_TO_TEXT_GAP,
                LINE_WIDTH, BRANCH_GAP, HIGHLIGHT_PAD_X
            } = this._annotationLaneMetrics(scale);

            // Alan 8/15/26 - Resolve each annotation against the current tree once. Only the
            // ones that are still exactly one clade come back for drawing; the rest are
            // recorded as invalid for the manager's warning and left off the figure.
            // Alan 8/17/26 - Pass the shared incoming-branch index into type-aware validation.
            const { validity, resolved } = this._resolveAnnotationsForRender(
                // Alan 8/17/26 - Reuse branch targets built with the clade-block index.
                positions, cladeBlocks, layerById, branchNodes, cladeNodes
            );
            this.annotationValidity = validity;

            if (!resolved.length) {
                this._annotationsDrawn = false;
                restoreWidth();
                return;
            }

            const group = enclosure.append('g').attr('class', 'clade-annotations');
            // Alan 8/24/26 - Behind EVERYTHING phylotree drew: first child of the same
            // container group, so it shares the tree's coordinate space and zoom transform
            // while painting under the branches. pointer-events is off here as well as in
            // CSS, because an exported/cloned SVG carries no stylesheet.
            const highlightGroup = enclosure.insert('g', ':first-child')
                .attr('class', 'clade-annotation-highlights')
                .style('pointer-events', 'none');

            const orderedLayers = this.annotationLayers
                .filter((layer) => layer && layer.visible !== false && layerById.has(layer.id))
                .slice()
                .sort((a, b) => (a.order || 0) - (b.order || 0));

            const yOf = (index) => {
                const id = geometry.tipOrder[index];
                const entry = id ? positions.get(id) : null;
                return entry ? entry.y : 0;
            };

            // Alan 8/17/26 - Resolve text/style and measure once; both clade lanes and incoming-branch
            // annotations consume this exact item shape and the same SVG text primitive.
            for (const item of resolved) {
                const style = {};
                for (const field of ANNOTATION_STYLE_FIELDS) {
                    const entry = this._resolveAnnotationStyleEntry(item.annotation, item.layer, field);
                    style[field] = ANNOTATION_NUMERIC_STYLE_FIELDS.has(field)
                        ? Number(entry.value) : entry.value;
                    style[field + '_is_default'] = entry.isDefault;
                }
                item.style = style;
                item.lines = this._annotationLabelLines(item.annotation.label);
                item.label = item.lines.join('\n');
                item.type = this._annotationType(item.annotation);
                item.top = yOf(item.indices[0]);
                item.bottom = yOf(item.indices[item.indices.length - 1]);
                item.textWidth = this._measureAnnotationLabel(svgNode, item.lines, style) * scale;
                item.scaledFontSize = style.font_size * scale;
                item.metrics = this._annotationLayoutMetrics(item, LINE_TO_TEXT_GAP);
            }

            // Alan 8/24/26 - Each clade annotation is placed from ITS OWN descendants' tip
            // labels, not from the longest label anywhere in the tree. One long name in an
            // unrelated part of the figure used to drag every bracket out with it, which is
            // what left a small nested annotation such as "sensu stricto" stranded hundreds of
            // pixels from the sequences it names.
            for (const item of resolved) {
                item.localLabelRight = this._localTipLabelRight(item.indices, geometry);
                item.preferredLaneX = item.localLabelRight + GAP_FROM_TREE;
            }

            // Not seeded from labelRight: the first lane is allowed to sit beside its own
            // clade. Later lanes are pushed out only as far as clearing the earlier ones needs.
            let cursorX = -Infinity;

            for (const layer of orderedLayers) {
                // Alan 8/17/26 - Reserve right-side lanes only for publication-style clade lines.
                // Alan 8/24/26 - Highlights keep the bracket and label, so they share the
                // clade lane with clade lines; only the band behind them is extra.
                const items = resolved.filter((item) => item.layer.id === layer.id
                    && this._isCladeAnnotationType(item.type));
                if (!items.length) continue;

                const placement = this._layoutAnnotationLanes(
                    items, rowPitch, GAP_BETWEEN_LANES, cursorX
                );
                cursorX = placement.cursorX;

                placement.lanes.forEach((lane, laneIndex) => {
                    const laneX = lane.x;
                    // Alan 8/24/26 - Bands are flushed per LANE, not per layer: two lanes are
                    // separate columns, and stretching one out to the other's longest label
                    // smears it across the gap between them.
                    const laneKey = `${layer.id}#${laneIndex}`;
                    for (const item of lane.items) {
                        item.laneX = laneX;
                        // Alan 8/24/26 - The band has to reach past the label it belongs to,
                        // which is only known once the lane this item landed in is placed.
                        item.highlightRight = laneX + item.metrics.laneWidth + HIGHLIGHT_PAD_X;
                        item.highlightLayerId = layer.id;
                        item.highlightLaneId = laneKey;
                        const entry = this._drawOneAnnotation(
                            group, item, laneX, LINE_TO_TEXT_GAP, LINE_WIDTH, rowPitch
                        );
                        // Alan 8/21/26 - Right-click the bracket or its label to edit it.
                        this._attachAnnotationInteraction(entry, item, {
                            x: laneX - LINE_WIDTH,
                            y: item.metrics.renderTop,
                            width: item.metrics.laneWidth + LINE_WIDTH,
                            height: Math.max(
                                item.scaledFontSize,
                                item.metrics.renderBottom - item.metrics.renderTop
                            )
                        });
                    }
                });
            }

            // Alan 8/24/26 - Paint the highlight bands now that every lane is placed, so each
            // band knows how far right its own label reaches. Larger clades are painted first
            // and nested ones on top of them, so a section inside a section stays legible; the
            // order is taken from the descendant span rather than from creation order, which
            // would otherwise make the hierarchy depend on the order the user happened to add
            // the annotations in.
            const highlightItems = this._orderCladeHighlights(resolved);
            this._alignHighlightRightEdges(highlightItems);
            // Alan 8/24/26 - Work the automatic colours out once for the whole tree rather than
            // per band, so the "second highlight gets a different colour" rule can see them all.
            const highlightColors = highlightItems.length
                ? this._automaticHighlightAssignments(positions).colors
                : null;
            for (const item of highlightItems) {
                const fallbackRight = Number.isFinite(item.preferredLaneX)
                    ? item.preferredLaneX : labelRight + GAP_FROM_TREE;
                const { band, label } = this._cladeHighlightRects(
                    item, rowPitch, HIGHLIGHT_PAD_X, fallbackRight
                );
                if (!band) continue;
                // Alan 8/24/26 - Resolve the colour and opacity that are actually visible and
                // write both inline. Nothing themed may be left to CSS: the export clones this
                // SVG without the page stylesheet, so a CSS-only dark-mode opacity used to make
                // the exported figure paler than the one the user was looking at.
                const effective = this._effectiveHighlightStyle(item, highlightColors);
                this._appendHighlightRect(
                    highlightGroup, band, effective, item.annotation.id
                );
                // Alan 8/24/26 - A label taller than its clade gets its own backing piece in
                // the annotation lane, at the same colour and opacity. It never reaches back
                // across the tip labels, so a three-line note on one sequence cannot make the
                // rows above and below it look like members of the clade.
                if (label) {
                    this._appendHighlightRect(
                        highlightGroup, label, effective, item.annotation.id
                    );
                }
            }

            // Alan 8/17/26 - Branch annotations are attached to the exact node resolved from the saved
            // descendant set. Layer order affects only stacking on that SAME branch.
            const layerOrder = new Map(orderedLayers.map((layer, index) => [layer.id, index]));
            const branchItems = resolved
                // Alan 8/24/26 - Only the branch types go here; both clade types were already
                // drawn in their lane, and a highlight must not also stack over the branch.
                .filter((item) => !this._isCladeAnnotationType(item.type) && item.targetNode?.parent)
                .sort((a, b) => (layerOrder.get(a.layer.id) - layerOrder.get(b.layer.id))
                    || (a.savedIndex - b.savedIndex));
            const branchCursors = new Map();
            for (const item of branchItems) {
                const node = item.targetNode;
                const point = this._annotationNodePoint(node);
                const parentPoint = this._annotationNodePoint(node.parent);
                if (!point || !parentPoint) continue;
                const branchKey = item.indices.join(':');
                let bottom = branchCursors.get(branchKey);
                if (!Number.isFinite(bottom)) {
                    bottom = point.y - BRANCH_GAP;
                    const supportBox = this._annotationSupportBox(svg, node, point);
                    if (supportBox) bottom = Math.min(bottom, supportBox.top - BRANCH_GAP);
                }
                const branchMetrics = this._branchAnnotationMetrics(item);
                branchMetrics.midY = bottom - branchMetrics.boxHeight / 2;
                branchMetrics.renderTop = bottom - branchMetrics.boxHeight;
                branchMetrics.renderBottom = bottom;
                item.metrics = branchMetrics;
                const centerX = (parentPoint.x + point.x) / 2;
                const branchEntry = this._drawBranchAnnotation(group, item, centerX, LINE_WIDTH);
                // Alan 8/21/26 - Right-click the branch label or bubble to edit it.
                this._attachAnnotationInteraction(branchEntry, item, {
                    x: centerX - branchMetrics.boxWidth / 2,
                    y: branchMetrics.renderTop,
                    width: branchMetrics.boxWidth,
                    height: branchMetrics.boxHeight
                });
                branchCursors.set(branchKey, branchMetrics.renderTop - BRANCH_GAP);
            }

            this._annotationsDrawn = true;

            // Grow the canvas so long labels and large fonts are never clipped. The tree
            // itself is untouched -- shrinking it to fit would make the biology unreadable.
            let requiredRight = Number.isFinite(cursorX)
                ? cursorX : labelRight + GAP_FROM_TREE;
            // Alan 8/17/26 - Capture the full annotation bounds for width and viewBox expansion.
            let annotationBox = null;
            try {
                // Alan 8/17/26 - Measure once and reuse the same bounds for all viewport axes.
                annotationBox = group.node().getBBox();
                // Alan 8/24/26 - Union in the highlight bands, which extend half a row above
                // the first tip and below the last one and would otherwise be clipped.
                if (highlightItems.length) {
                    const bandBox = highlightGroup.node().getBBox();
                    if (bandBox && bandBox.width && bandBox.height) {
                        const minX = Math.min(annotationBox.x, bandBox.x);
                        const minY = Math.min(annotationBox.y, bandBox.y);
                        const maxX = Math.max(annotationBox.x + annotationBox.width,
                            bandBox.x + bandBox.width);
                        const maxY = Math.max(annotationBox.y + annotationBox.height,
                            bandBox.y + bandBox.height);
                        annotationBox = { x: minX, y: minY, width: maxX - minX, height: maxY - minY };
                    }
                }
                requiredRight = Math.max(requiredRight, annotationBox.x + annotationBox.width);
            } catch (_) { /* fall back to the layout cursor */ }
            // The container group carries translate(...) alone before any zoom, and
            // translate(...) scale(k) afterwards, so convert group units to SVG units.
            let offsetX = 0;
            const transform = enclosure.attr('transform') || '';
            const match = /translate\(\s*(-?[\d.]+)/.exec(transform);
            if (match) offsetX = parseFloat(match[1]) || 0;
            const needed = Math.ceil(offsetX + requiredRight * (k || 1) + 24);
            if (Number.isFinite(baseWidth)) {
                setWidth(Math.max(baseWidth, needed));
            } else if (needed > 0) {
                setWidth(needed);
            }
            // Alan 8/17/26 - Branch labels may extend above or left of their short incoming segment. Expand
            // the viewBox instead of shrinking text or clipping the saved/exported figure.
            if (annotationBox) {
                const raw = (baseViewBox || `0 0 ${Number.isFinite(baseWidth) ? baseWidth : needed} ${parseFloat(svgNode.getAttribute('height')) || 800}`)
                    .trim().split(/[\s,]+/).map(Number);
                if (raw.length === 4 && raw.every(Number.isFinite)) {
                    const translate = /translate\(\s*(-?[\d.]+)(?:[ ,]+(-?[\d.]+))?/.exec(transform);
                    const tx = translate ? (parseFloat(translate[1]) || 0) : 0;
                    const ty = translate ? (parseFloat(translate[2]) || 0) : 0;
                    const left = tx + annotationBox.x * (k || 1) - 12;
                    const top = ty + annotationBox.y * (k || 1) - 12;
                    const right = tx + (annotationBox.x + annotationBox.width) * (k || 1) + 12;
                    const bottom = ty + (annotationBox.y + annotationBox.height) * (k || 1) + 12;
                    const minX = Math.min(raw[0], left);
                    const minY = Math.min(raw[1], top);
                    const maxX = Math.max(raw[0] + raw[2], right, needed);
                    const maxY = Math.max(raw[1] + raw[3], bottom);
                    const nextViewBox = `${minX} ${minY} ${maxX - minX} ${maxY - minY}`;
                    svgNode.setAttribute('viewBox', nextViewBox);
                    svgNode.setAttribute('data-annotation-set-viewbox', nextViewBox);
                }
            }
        }

        /**
         * Alan 8/24/26 - The right edge of the tip labels belonging to ONE clade.
         *
         * This, not the tree-wide `labelRight`, is where that clade's annotation lane wants to
         * begin. A single very long sequence name somewhere else in the figure used to define
         * the lane for every annotation, so a small nested clade with short labels got its
         * bracket pushed across a wide empty band.
         *
         * A label that could not be measured contributes nothing; if none of the clade's rows
         * could be measured at all, the tree-wide edge is used, which is never narrower than
         * this clade's own labels and therefore cannot draw over the tips.
         */
        _localTipLabelRight(indices, geometry) {
            const rights = geometry && geometry.tipRights;
            let right = -Infinity;
            if (Array.isArray(rights)) {
                for (const index of indices || []) {
                    const value = rights[index];
                    if (Number.isFinite(value) && value > right) right = value;
                }
            }
            return Number.isFinite(right)
                ? right
                : ((geometry && Number.isFinite(geometry.labelRight)) ? geometry.labelRight : 0);
        }

        /**
         * Alan 8/24/26 - Pack one layer's clade annotations into non-overlapping lanes.
         *
         * Smallest clade first, so a nested annotation takes the lane NEAREST the tree and its
         * containing clade is the one pushed outward. The other way round (which is what
         * sorting by vertical extent produced) put the outer clade's long label in the inner
         * lane and shoved the short inner label past it -- the "sensu stricto stranded far to
         * the right" case.
         *
         * Overlap is tested against every annotation already in a lane rather than against a
         * running low-water mark, because the order the items arrive in is no longer sorted by
         * vertical position.
         */
        _packAnnotationLanes(items, rowPitch) {
            const ordered = (items || []).slice().sort((a, b) =>
                (a.indices.length - b.indices.length)
                || (a.metrics.renderTop - b.metrics.renderTop)
                || ((a.savedIndex || 0) - (b.savedIndex || 0)));
            const lanes = [];
            for (const item of ordered) {
                const padding = Math.max(item.scaledFontSize || 0, rowPitch) * 0.6;
                const top = item.metrics.renderTop;
                const bottom = item.metrics.renderBottom;
                let placed = null;
                for (const lane of lanes) {
                    const clashes = lane.items.some((other) =>
                        other.metrics.renderTop - padding < bottom
                        && top - padding < other.metrics.renderBottom);
                    if (!clashes) { placed = lane; break; }
                }
                if (placed) placed.items.push(item);
                else lanes.push({ items: [item] });
            }
            return lanes;
        }

        /**
         * Alan 8/24/26 - Pack one layer's clade annotations into lanes and give each lane its
         * horizontal position. Pure, so the placement rule is pinned by a test rather than only
         * by reading the draw loop.
         *
         * A lane begins where the annotations IN IT want it -- just past their own clades' tip
         * labels -- and is moved right only as far as clearing the lanes already placed
         * requires. `cursorX` is that running floor and may be -Infinity for the first lane,
         * which is what lets the innermost lane sit beside its own labels no matter how wide
         * the rest of the figure is.
         */
        _layoutAnnotationLanes(items, rowPitch, gapBetweenLanes, cursorX = -Infinity) {
            const lanes = this._packAnnotationLanes(items, rowPitch);
            let cursor = cursorX;
            lanes.forEach((lane, index) => {
                let laneX = -Infinity;
                let laneWidth = 0;
                for (const item of lane.items) {
                    if (Number.isFinite(item.preferredLaneX)) {
                        laneX = Math.max(laneX, item.preferredLaneX);
                    }
                    laneWidth = Math.max(laneWidth, item.metrics.laneWidth || 0);
                }
                if (Number.isFinite(cursor)) laneX = Math.max(laneX, cursor);
                if (!Number.isFinite(laneX)) laneX = 0;
                lane.index = index;
                lane.x = laneX;
                lane.width = laneWidth;
                cursor = laneX + laneWidth + gapBetweenLanes;
            });
            return { lanes, cursorX: cursor };
        }

        // Alan 8/17/26 - Keep pure multiline clade-line geometry shared by preview and rendering.
        _annotationLayoutMetrics(item, textGap) {
            const fontSize = item.scaledFontSize || 0;
            const lineCount = Math.max(1, (item.lines || ['']).length);
            const lineHeight = fontSize * 1.25;
            const blockHeight = lineCount * lineHeight;
            const midY = (item.top + item.bottom) / 2;
            // Alan 8/17/26 - Branch bubble dimensions now live in _branchAnnotationMetrics.
            // The text block can be taller than the clade it labels (a two-line name on a
            // two-tip clade), so the reserved extent is the union of the two.
            return {
                isBubble: false, lineHeight, blockHeight, midY, padX: 0, padY: 0,
                boxWidth: item.textWidth, boxHeight: blockHeight,
                textX: textGap,
                laneWidth: textGap + item.textWidth,
                renderTop: Math.min(item.top, midY - blockHeight / 2),
                renderBottom: Math.max(item.bottom, midY + blockHeight / 2)
            };
        }

        /**
         * Alan 8/24/26 - The layout constants, converted from screen pixels into the tree's
         * own units for the current zoom.
         *
         * Annotation text is held at a constant SCREEN size, so every gap beside it has to be
         * divided by the zoom factor to keep the same apparent distance. Pulled out of the
         * renderer so the relationship between the lane and the tip labels -- a modest gap
         * after the longest label, not a band of empty figure -- is pinned by a test rather
         * than only by reading the draw code.
         */
        _annotationLaneMetrics(scale) {
            return {
                // Distance from the right edge of the longest DRAWN tip label to the bracket.
                GAP_FROM_TREE: 18 * scale,
                GAP_BETWEEN_LANES: 14 * scale,
                LINE_TO_TEXT_GAP: 6 * scale,
                LINE_WIDTH: Math.max(0.5, 1.5 * scale),
                // Alan 8/17/26 - Keep stacked branch labels clear of their branch and each other.
                BRANCH_GAP: 5 * scale,
                // Alan 8/24/26 - Breathing room around a highlight band, so it reads as a
                // deliberate region rather than a rectangle clipped onto the branch strokes.
                HIGHLIGHT_PAD_X: 8 * scale
            };
        }

        /**
         * Alan 8/24/26 - Append one highlight band. Shared by the tree and the editor preview
         * so they cannot drift apart, and so the drawn attributes can be asserted without a DOM.
         *
         * Square corners on purpose: a rounded rectangle behind a clade reads as a UI card
         * rather than as a region of a figure. No rx attribute is set at all.
         *
         * Colour and opacity arrive already resolved and are written inline, because the export
         * path clones this SVG without the page stylesheet.
         */
        _appendHighlightRect(parent, rect, effective, annotationId) {
            const band = parent.append('rect')
                .attr('class', 'clade-annotation-highlight')
                .attr('x', rect.x)
                .attr('y', rect.y)
                .attr('width', rect.width)
                .attr('height', rect.height)
                .style('fill', effective.color)
                .style('fill-opacity', effective.opacity)
                // No border by default: an outline around a translucent band competes with
                // the branches it is supposed to sit quietly behind.
                .style('stroke', 'none')
                .style('pointer-events', 'none');
            if (annotationId) band.attr('data-annotation-id', annotationId);
            return band;
        }

        /**
         * Alan 8/24/26 - Give every band in one LANE the same right edge.
         *
         * Each band is first sized to clear its OWN label, which left bands sharing a lane
         * ragged: a clade whose name happens to be longer than its neighbour's stuck out past
         * it for no reason the reader can see. Flushing them to the widest edge in the lane
         * restores the publication look and still clears the longest label, so nothing is
         * clipped.
         *
         * Per lane and not per layer. A layer can hold several lanes -- that is how nested
         * annotations avoid each other -- and flushing a whole layer stretched an inner band
         * out to an outer lane's long label, dragging it across the lane in between and
         * undoing exactly the compactness the local lane placement is there to give.
         *
         * The lane identity is ephemeral, assigned while drawing. Nothing about horizontal
         * position is ever persisted: every redraw recomputes it from the live layout.
         */
        _alignHighlightRightEdges(items) {
            const laneKey = (item) => item.highlightLaneId
                || item.highlightLayerId || item.layer?.id || '';
            const widest = new Map();
            for (const item of items || []) {
                if (!Number.isFinite(item.highlightRight)) continue;
                const key = laneKey(item);
                const current = widest.get(key);
                if (current === undefined || item.highlightRight > current) {
                    widest.set(key, item.highlightRight);
                }
            }
            for (const item of items || []) {
                const shared = widest.get(laneKey(item));
                if (Number.isFinite(shared)) item.highlightRight = shared;
            }
            return items;
        }

        /**
         * Alan 8/24/26 - Paint order for the highlight bands: the largest clade first and
         * nested ones over it, so a section inside a section still reads as nested.
         *
         * Deliberately NOT creation order: which annotation the user happened to add first
         * says nothing about the hierarchy, and clade size does, so two trees with the same
         * annotations always paint the same way.
         */
        _orderCladeHighlights(resolved) {
            return (resolved || [])
                .filter((item) => item && item.type === 'clade_highlight')
                .sort((a, b) => (b.indices.length - a.indices.length)
                    || (a.indices[0] - b.indices[0])
                    || (a.savedIndex - b.savedIndex));
        }

        /**
         * Alan 8/24/26 - The band behind one highlighted clade, in the tree's own coordinates.
         *
         * Pure geometry, so it is testable without a DOM. Every input is derived from the
         * live layout on each redraw, exactly like the brackets, which is why rotation,
         * spacing changes, pruning and rerooting need no stored coordinates.
         *
         *   left   the clade's internal node, nudged slightly back so the band does not
         *          clip the node itself. The nudge is capped at a quarter of the incoming
         *          branch: covering that branch would say the ancestral lineage is part of
         *          the highlighted clade, which is a different phylogenetic claim.
         *   top    half a row above the FIRST DESCENDANT TIP.
         *   bottom half a row below the LAST DESCENDANT TIP.
         *   right  past the annotation's own label, so the band covers the clade AND the
         *          thing naming it. Falls back to just outside the tip labels when the item
         *          never got a lane.
         *
         * The vertical extent comes from the descendant rows and from nothing else. It used to
         * be the union of those rows with the annotation TEXT block, which meant a three-line
         * label on a single sequence painted the rows above and below it too -- the band across
         * the tree is a statement about which taxa are in the clade, so a longer caption must
         * never add taxa to it. The text is covered by a separate piece instead; see
         * _cladeHighlightRects.
         */
        _cladeHighlightRect(item, rowPitch, padX, fallbackRight) {
            return this._cladeHighlightRects(item, rowPitch, padX, fallbackRight).band;
        }

        /**
         * Alan 8/24/26 - The one or two rectangles a highlight is drawn from.
         *
         *   band   the biological clade: its height is the descendant tip rows plus about half
         *          a row at each end, whatever the label says.
         *   label  present only when the annotation's text block is taller than that band. It
         *          backs the text in the ANNOTATION LANE only, starting just left of the
         *          bracket, so the two pieces meet edge to edge and read as one stepped region
         *          without ever reaching back across the tip labels.
         *
         * The two never overlap: where a label piece exists the band stops at the lane, so a
         * translucent wash is never painted twice over the same pixels and the drawn colour is
         * exactly the resolved one. With a one-line label there is no second rectangle at all.
         */
        _cladeHighlightRects(item, rowPitch, padX, fallbackRight) {
            const empty = { band: null, label: null };
            const point = this._annotationNodePoint(item?.cladeNode);
            if (!point) return empty;
            const parentPoint = this._annotationNodePoint(item.cladeNode.parent);
            const backward = parentPoint
                ? Math.max(0, Math.min(padX, (point.x - parentPoint.x) * 0.25))
                : padX;
            const left = point.x - backward;
            const right = Number.isFinite(item.highlightRight) ? item.highlightRight : fallbackRight;
            const metrics = item.metrics || {};
            const padY = rowPitch * 0.5;
            // Biological extent only. renderTop/renderBottom describe the text block and are
            // deliberately not consulted here.
            const top = item.top - padY;
            const bottom = item.bottom + padY;

            const labelTop = Number.isFinite(metrics.renderTop) ? metrics.renderTop : top;
            const labelBottom = Number.isFinite(metrics.renderBottom) ? metrics.renderBottom : bottom;
            // A little air around the text, but a small fraction of a row: the backing sits in
            // the lane, where being generous costs nothing, yet it should still look attached.
            // It is DECORATION on a piece that is already needed -- it must never be what
            // decides the piece exists. Adding it before this comparison made an ordinary
            // one-line label on a single tip (a ~15px text block inside a 20px row) test as
            // overflowing by exactly the padding, so every such highlight was split into two
            // rectangles for no visible reason.
            const labelPadY = padY * 0.5;
            // The actual text bounds against the actual band, with no tolerance: an exact fit
            // (labelTop === top) is a fit, and float noise at that boundary would only decide
            // between two renderings that are within a fraction of a pixel of each other.
            const overflows = labelTop < top || labelBottom > bottom;

            // Where the lane begins. The bracket is drawn at laneX, so backing it from a little
            // left of that is what makes the step touch the band.
            let laneLeft = Number.isFinite(item.laneX) ? item.laneX - padX : NaN;
            if (!Number.isFinite(laneLeft) && Number.isFinite(metrics.laneWidth)) {
                laneLeft = right - metrics.laneWidth - padX;
            }
            const canSplit = overflows && Number.isFinite(laneLeft)
                && laneLeft > left && laneLeft < right;

            const band = {
                x: left,
                y: top,
                // With a label piece the band stops where that piece starts, so the two abut
                // instead of overlapping; otherwise it runs the whole way as before.
                width: Math.max(1, (canSplit ? laneLeft : right) - left),
                height: Math.max(1, bottom - top)
            };
            if (!canSplit) return { band, label: null };

            const labelY = Math.min(top, labelTop - labelPadY);
            const labelEnd = Math.max(bottom, labelBottom + labelPadY);
            return {
                band,
                label: {
                    x: laneLeft,
                    y: labelY,
                    width: Math.max(1, right - laneLeft),
                    height: Math.max(1, labelEnd - labelY)
                }
            };
        }

        // Alan 8/17/26 - Normalize phylotree's rectangular screen coordinates for branch layout.
        _annotationNodePoint(node) {
            if (!node) return null;
            const x = (typeof node.screen_x === 'number') ? node.screen_x : node.y;
            const y = (typeof node.screen_y === 'number') ? node.screen_y : node.x;
            return (Number.isFinite(x) && Number.isFinite(y)) ? { x, y } : null;
        }

        // Alan 8/17/26 - Reserve the rendered support label nearest the incoming branch.
        _annotationSupportBox(svg, node, point) {
            let found = null;
            svg.selectAll('g.node, g.internal-node').each(function (datum) {
                if (found || datum !== node) return;
                const support = this.querySelector('text.node-support-value');
                if (!support) return;
                try {
                    const box = support.getBBox();
                    found = {
                        left: point.x + box.x,
                        right: point.x + box.x + box.width,
                        top: point.y + box.y,
                        bottom: point.y + box.y + box.height
                    };
                } catch (_) { /* detached support label */ }
            });
            return found;
        }

        // Alan 8/17/26 - Share multiline branch text/bubble dimensions with the SVG preview.
        _branchAnnotationMetrics(item) {
            const fontSize = item.scaledFontSize || 0;
            const lineCount = Math.max(1, (item.lines || ['']).length);
            const lineHeight = fontSize * 1.25;
            const blockHeight = lineCount * lineHeight;
            const isBubble = item.type === 'branch_bubble';
            const padX = isBubble ? Math.max(3, fontSize * 0.55) : 0;
            const padY = isBubble ? Math.max(2, fontSize * 0.35) : 0;
            return {
                isBubble,
                lineHeight,
                blockHeight,
                padX,
                padY,
                boxWidth: item.textWidth + padX * 2,
                boxHeight: blockHeight + padY * 2,
                cornerRadius: Math.max(2, fontSize * 0.6)
            };
        }

        // Alan 8/17/26 - Render the label as one <tspan> per logical line, centred as a block
        // on the clade midpoint. A bare <text> node collapses "\n", so the multiline labels the
        // editor accepts used to come out as a single run-on line.
        // Alan 8/17/26 - Accept optional vertical centering and text anchoring from branch labels.
        _appendAnnotationLabel(parent, item, x, cssClass, options = {}) {
            const metrics = item.metrics;
            const lines = item.lines && item.lines.length ? item.lines : [''];
            // Alan 8/17/26 - Allow branch primitives to center labels and choose their anchor.
            const centerY = Number.isFinite(options.midY) ? options.midY : metrics.midY;
            const anchor = options.anchor || 'start';
            const firstY = centerY - ((lines.length - 1) * metrics.lineHeight) / 2;
            const text = parent.append('text')
                .attr('class', cssClass)
                .attr('x', x)
                .attr('y', firstY)
                // Alan 8/17/26 - Apply the caller's start or centered text anchor.
                .attr('text-anchor', anchor)
                .attr('dominant-baseline', 'central')
                .style('font-family', annotationFontStack(item.style.font_family))
                .style('font-size', `${item.scaledFontSize}px`)
                .style('font-style', item.style.font_style)
                .style('font-weight', item.style.font_weight)
                .style('fill', item.style.text_color)
                .style('pointer-events', 'none');
            lines.forEach((line, index) => {
                // .text() and not .html(): a label such as "<svg onload=alert(1)>" must appear
                // literally as characters, here and in the exported SVG.
                text.append('tspan')
                    .attr('x', x)
                    .attr('dy', index === 0 ? 0 : metrics.lineHeight)
                    .text(line);
            });
            return text;
        }

        // Alan 8/17/26 - Draw branch text and bubbles through one live/preview SVG primitive.
        _drawBranchAnnotation(group, item, centerX, lineWidth) {
            const metrics = item.metrics;
            const inkClass = (base, isDefault) =>
                isDefault ? `${base} clade-annotation-default-ink` : base;
            const entry = group.append('g')
                .attr('class', 'clade-annotation branch-annotation')
                .attr('data-annotation-type', item.type)
                .attr('data-annotation-id', item.annotation.id || 'preview');

            if (metrics.isBubble) {
                entry.append('rect')
                    .attr('class', inkClass(
                        'clade-annotation-bubble', item.style.line_color_is_default
                    ))
                    .attr('x', centerX - metrics.boxWidth / 2)
                    .attr('y', metrics.midY - metrics.boxHeight / 2)
                    .attr('width', Math.max(1, metrics.boxWidth))
                    .attr('height', Math.max(1, metrics.boxHeight))
                    .attr('rx', Math.min(metrics.boxHeight / 2, metrics.cornerRadius))
                    .style('fill', item.style.fill_color)
                    .style('fill-opacity', item.style.fill_opacity)
                    .style('stroke', item.style.line_color)
                    .style('stroke-width', `${lineWidth}px`);
            }

            this._appendAnnotationLabel(
                entry,
                item,
                centerX,
                metrics.isBubble
                    ? 'clade-annotation-label clade-annotation-bubble-label'
                    : inkClass(
                        'clade-annotation-label', item.style.text_color_is_default
                    ),
                { anchor: 'middle', midY: metrics.midY }
            );
            return entry;
        }

        /**
         * Alan 8/24/26 - The stand-in clade a highlight preview is drawn over.
         *
         * The preview has no topology, so it invents one -- but it has to invent something the
         * tree could plausibly draw. It used to span the whole preview box (24 .. height-24),
         * i.e. a clade far taller than any label, so a three-line annotation never reached
         * outside the band and the stepped backing the figure would draw was never previewed.
         * A small clade of two rows, centred, shows the difference: a one-line label sits
         * inside the band, a three-line one does not.
         *
         * This is a STYLE preview, not a miniature of the selected clade -- it deliberately
         * knows nothing about the live tree and draws no neighbouring taxa.
         */
        _previewHighlightGeometry(width, height) {
            const rowPitch = 20;
            const midY = height / 2;
            return {
                rowPitch,
                padX: 8,
                // Two tip rows, so the band is rowPitch * 2 tall once padded.
                top: midY - rowPitch / 2,
                bottom: midY + rowPitch / 2,
                nodeX: 12,
                laneX: 24,
                fallbackRight: width - 12
            };
        }

        // Alan 8/17/26 - Render the editor preview with the same primitives used by the tree.
        // Alan 8/24/26 - `options` carries the editor session's membership and annotation id so
        // an unsaved highlight previews the automatic colour it will actually be given.
        renderAnnotationPreview(svgElement, annotation, layer, options = {}) {
            if (!svgElement || !window.d3v7) return;
            const svg = window.d3v7.select(svgElement);
            svg.selectAll('*').remove();
            const width = Math.max(260, svgElement.clientWidth || 420);
            const height = 120;
            svg.attr('viewBox', `0 0 ${width} ${height}`)
                .attr('width', '100%').attr('height', height);
            const style = {};
            for (const field of ANNOTATION_STYLE_FIELDS) {
                const entry = this._resolveAnnotationStyleEntry(annotation, layer, field);
                style[field] = ANNOTATION_NUMERIC_STYLE_FIELDS.has(field)
                    ? Number(entry.value) : entry.value;
                style[field + '_is_default'] = entry.isDefault;
            }
            const lines = this._annotationLabelLines(annotation.label || 'Annotation preview');
            const previewId = options.annotationId || 'preview';
            const previewType = this._annotationType(annotation);
            const geom = previewType === 'clade_highlight'
                ? this._previewHighlightGeometry(width, height) : null;
            const item = {
                annotation: Object.assign({ id: previewId }, annotation, {
                    member_tip_ids: options.memberIds || annotation.member_tip_ids || []
                }),
                // Alan 8/24/26 - The layer is part of the item, not just of `style`:
                // _effectiveHighlightStyle() resolves the colour intent from (annotation,
                // layer), so omitting it here made a layer-level Fixed colour invisible in the
                // preview while the saved figure drew it.
                layer: layer || null,
                style,
                lines,
                type: previewType,
                scaledFontSize: style.font_size,
                textWidth: this._measureAnnotationLabel(svgElement, lines, style),
                top: geom ? geom.top : 24,
                bottom: geom ? geom.bottom : height - 24
            };
            const group = svg.append('g').attr('class', 'clade-annotations annotation-preview-svg');
            const lineWidth = 1.5;
            // Alan 8/24/26 - Highlights preview through the clade-line primitives plus their
            // band, so what the editor shows is what the figure will draw.
            if (this._isCladeAnnotationType(item.type)) {
                item.metrics = this._annotationLayoutMetrics(item, 7);
                if (item.type === 'clade_highlight') {
                    // A stand-in for the clade's internal node; the preview has no topology.
                    item.cladeNode = { screen_x: geom.nodeX, screen_y: (item.top + item.bottom) / 2 };
                    item.laneX = geom.laneX;
                    item.highlightRight = geom.laneX + item.metrics.laneWidth + geom.padX;
                    // Alan 8/24/26 - The same stepped primitive the figure uses, so a multiline
                    // label previews with its own backing piece rather than with one tall band
                    // the tree would never draw.
                    const { band, label } = this._cladeHighlightRects(
                        item, geom.rowPitch, geom.padX, geom.fallbackRight
                    );
                    if (band) {
                        // Alan 8/24/26 - Exactly the resolution the tree uses, so the swatch the
                        // user approves is the fill the figure gets -- automatic group colour,
                        // automatic palette colour, or their own override.
                        const effective = this._effectiveHighlightStyle(item);
                        // Appended before the bracket, so the band is behind it here too.
                        this._appendHighlightRect(group, band, effective, null);
                        if (label) this._appendHighlightRect(group, label, effective, null);
                    }
                }
                // Alan 8/24/26 - Same lane and row pitch the band above was built from, so a
                // later change to _previewHighlightGeometry() cannot slide the bracket off it.
                this._drawOneAnnotation(
                    group, item, geom ? geom.laneX : 24, 7, lineWidth,
                    geom ? geom.rowPitch : 20
                );
            } else {
                const branchY = height - 20;
                group.append('line')
                    .attr('x1', 24).attr('x2', width - 24)
                    .attr('y1', branchY).attr('y2', branchY)
                    .style('stroke', '#9ca3af').style('stroke-width', '1px');
                item.metrics = this._branchAnnotationMetrics(item);
                item.metrics.midY = branchY - 8 - item.metrics.boxHeight / 2;
                item.metrics.renderTop = item.metrics.midY - item.metrics.boxHeight / 2;
                item.metrics.renderBottom = item.metrics.midY + item.metrics.boxHeight / 2;
                this._drawBranchAnnotation(group, item, width / 2, lineWidth);
            }
        }

        // Alan 8/17/26 - Draw the publication-style clade bracket beside its complete descendant-tip span.
        _drawOneAnnotation(group, item, laneX, textGap, lineWidth, rowPitch) {
            const metrics = item.metrics || this._annotationLayoutMetrics(item, textGap);
            const tickHalf = Math.max(1, rowPitch * 0.4);
            const entry = group.append('g')
                .attr('class', 'clade-annotation')
                .attr('data-annotation-type', item.type || 'line')
                .attr('data-annotation-id', item.annotation.id);

            // Alan 8/17/26 - Tag ink that was INHERITED rather than chosen, so dark mode can
            // lighten it. Comparing the resolved colour with the default used to relabel an
            // explicit #1f2937 as "untouched"; a colour the user actually picked is now left
            // exactly as chosen, because that is what the exported figure has to reproduce.
            const inkClass = (base, isDefault) =>
                isDefault ? `${base} clade-annotation-default-ink` : base;

            // Alan 8/17/26 - Clade lines always draw as a bracket, including a short one-tip tick.
            const singleTip = item.top === item.bottom;
            entry.append('line')
                .attr('class', inkClass('clade-annotation-line', item.style.line_color_is_default))
                .attr('x1', laneX).attr('x2', laneX)
                .attr('y1', singleTip ? item.top - tickHalf : item.top)
                .attr('y2', singleTip ? item.bottom + tickHalf : item.bottom)
                .style('stroke', item.style.line_color)
                .style('stroke-width', `${lineWidth}px`)
                .style('stroke-linecap', 'round');

            // Alan 8/17/26 - Clade-line labels always use inherited ink without bubble handling.
            this._appendAnnotationLabel(
                entry, item, laneX + metrics.textX,
                // Alan 8/17/26 - Use the clade-line text class after bubble drawing moved out.
                inkClass('clade-annotation-label', item.style.text_color_is_default)
            );
            return entry;
        }

        /**
         * Alan 8/21/26 - Make one drawn annotation right-clickable so it can be edited where
         * the user is looking at it, instead of only through its branch menu or the manager.
         *
         * The whole g.clade-annotations group is pointer-events: none so annotations never
         * intercept clicks meant for the tree, and the label text sets it explicitly too. The
         * hit target is therefore an invisible rect covering just this annotation's own drawn
         * extent, added behind the ink. It is stripped from export clones.
         */
        _attachAnnotationInteraction(entry, item, bounds) {
            if (!entry || entry.empty?.() || window.VIEW_ONLY) return;
            const annotationId = item?.annotation?.id;
            if (!annotationId || annotationId === 'preview') return;
            if (!bounds || !Number.isFinite(bounds.x) || !Number.isFinite(bounds.y)) return;

            entry.insert('rect', ':first-child')
                .attr('class', 'clade-annotation-hit')
                .attr('x', bounds.x)
                .attr('y', bounds.y)
                .attr('width', Math.max(1, bounds.width))
                .attr('height', Math.max(1, bounds.height))
                .style('fill', 'transparent')
                .style('stroke', 'none')
                .style('pointer-events', 'all')
                .style('cursor', 'context-menu');

            const node = entry.node();
            if (!node) return;
            node.addEventListener('contextmenu', (event) => {
                if (typeof this._onEditCladeAnnotation !== 'function') return;
                event.preventDefault();
                event.stopPropagation();
                this._onEditCladeAnnotation(annotationId, item.annotation);
            });
        }

        _getSvgAndZoomGroup() {
            const svg = window.d3v7.select(this.container).select("svg");
            if (svg.empty()) return { svg, k: 1 };
            const z = this.cachedZoomNode || svg.node();
            // If z is detached, zoomTransform might fail or return identity.
            let t = { k: 1 };
            try { t = window.d3v7.zoomTransform(z); } catch (_) { }
            return { svg, k: t.k || 1 };
        }

        _findZoomGroup(svg) {
            // Robust logic: prefer group containing nodes. 
            // If multiple transform groups, find the one wrapping .node
            const nodes = svg.selectAll(".node").nodes();
            if (nodes.length > 0) {
                // Return the parent of the first node. Usually nodes are in a dedicated group.
                return window.d3v7.select(nodes[0].parentNode);
            }
            // Fallback
            const c = svg.selectAll("g[transform]").nodes();
            if (c.length) return window.d3v7.select(c[0]);
            return svg;
        }

        _cleanupZoomObserver() {
            if (this.zoomDebounceTimer) { clearTimeout(this.zoomDebounceTimer); this.zoomDebounceTimer = null; }
            if (this.zoomObserver) {
                try { this.zoomObserver.disconnect(); } catch (_) { }
            }
            this.zoomObservedNodes = [];
            // Do NOT clear cachedZoomNode here, as it breaks re-attachment logic
        }

        _attachZoomObserverTo(node) {
            this._cleanupZoomObserver();
            if (!node) return;

            if (!this.zoomObserver) {
                this.zoomObserver = new MutationObserver(() => {
                    if (this.rafPending) return;

                    // Throttle: If timer is running, we wait.
                    if (this.zoomDebounceTimer) return;

                    this.rafPending = true;
                    this.zoomDebounceTimer = setTimeout(() => {
                        requestAnimationFrame(() => {
                            this.rafPending = false;
                            this.zoomDebounceTimer = null;
                            this._applyTextSizingFromZoom();
                        });
                    }, 16); // ~1 frame delay
                });
            }

            this.zoomObserver.observe(node, { attributes: true, attributeFilter: ["transform"] });
            this.zoomObservedNodes.push(node);

            // Also observe parent SVG for global zoom
            const svg = node.closest('svg');
            if (svg && svg !== node) {
                this.zoomObserver.observe(svg, { attributes: true, attributeFilter: ["transform"] });
                this.zoomObservedNodes.push(svg);
            }
        }
    }

    // Expose Global Class
    window.DikaryaTreeViewer = DikaryaTreeViewer;

})();
