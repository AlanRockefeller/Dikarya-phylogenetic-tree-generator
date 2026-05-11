/**
 * DikaryaTreeViewer - Advanced Phylotree Wrapper
 * Adds selection management, incremental updates, and state persistence.
 */
(function () {
    'use strict';

    const DEBUG_MODE = new URLSearchParams(window.location.search).has('debug');

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
         * Clear only the currently ACTIVE selection set.
         * Use before applying a new selection action to replace (not append)
         * selections in the current working set.
         */
        clearActiveSelection() { }

        // Alan 5/11/26 - Clear visible active selections without mutating saved selection sets.
        deselectCurrentSelection() { return 0; }

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
                // Alan 5/9/26 - Track view-only MycoMap identity filters separately from destructive prune actions.
                identityThreshold: 0,
                // Alan 5/9/26 - Store per-sequence BLAST metrics passed from the job metadata.
                sequenceMetrics: [],
                supportBasePx: 9,
                tipBasePx: 12,
                layout: 'linear',
                alignTips: false
            }, initialOptions);
            // Alan 5/9/26 - Build a lookup once so tree tips can be matched to stored BLAST metrics quickly.
            this.sequenceMetricMap = this._buildSequenceMetricMap(this.options.sequenceMetrics);

            this.tree = null;
            this.newick = null;
            this.allNodes = []; // Node Cache

            // State - Multiple Selection Sets
            // selectionSets is the primary data structure: { 'Default': Set(), 'Edible': Set(), ... }
            this.selectionSets = { 'Default': new Set() };
            this.activeSelectionSet = 'Default';
            // Alan 5/11/26 - Track locally hidden active selections so Deselect leaves selection sets unchanged.
            this.hiddenSelectionIds = new Set();
            // Color palette from d3.schemeCategory10 for selection sets
            this._selectionColors = [
                '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
                '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf'
            ];
            // Getter for backward compatibility - returns the currently active set
            Object.defineProperty(this, 'selectedIds', {
                get: () => this.selectionSets[this.activeSelectionSet] || new Set(),
                set: (val) => { this.selectionSets[this.activeSelectionSet] = val; }
            });

            // Spacing State (relative to default)
            this.spacingState = { x: 0, y: 0 };
            this.spacingTimeout = null;

            // Zoom/UI state
            this.cachedZoomNode = null;
            this.zoomObserver = null;
            this.zoomObservedNodes = [];
            this.rafPending = false;
            this.supportLabelsTimer = null;
            this.lastStats = { supportType: 'none', maxSupport: 0 };
            this.baseSpacing = { x: 20, y: 20 }; // Phylotree fixed_width values (per-node/per-level)
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

            // Tag original order per-parent for correct restoration
            this.tree.traverse_and_compute(n => {
                if (n.children) {
                    n.children.forEach((c, i) => c.__original_index = i);
                }
            });

            // 2b. Cache Nodes & Compute Metadata (Flatten operations)
            this._cacheNodes();

            // 2c. Initial Selection Processing
            // Clear all selection sets on full tree reload
            for (const setName of Object.keys(this.selectionSets)) {
                this.selectionSets[setName].clear();
            }

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

        _draw() {
            if (!this.tree) return;

            // Cleanup observers
            this._cleanupZoomObserver();
            this.cachedZoomNode = null; // Clear cached node on redraw
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

                // Override click behavior: left-click = select, shift/right = menu
                this._overrideClickBehavior();

                // Alan 5/8/26 - Disable wheel-to-zoom so mouse wheel scrolls the page instead of zooming the tree.
                // Intercept wheel events before D3 and let the page scroll instead.
                const svgEl = this.container.querySelector('svg');
                if (svgEl) {
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
                this.spacingState = { x: 0, y: 0 };
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

        applyTextSizing() {
            this._applyTextSizingFromZoom();
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
                    const displayName = renames[originalName];
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
            ['queryCoverThreshold', 'subjectCoverThreshold', 'identityThreshold'].forEach(key => {
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
            this.options.identityThreshold = 0;
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
            //    Prefer getBoundingClientRect — reliable even when SVG attrs use "%" or are unset.
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

                        // White background — JPEG has no alpha channel
                        ctx.fillStyle = '#ffffff';
                        ctx.fillRect(0, 0, canvas.width, canvas.height);
                        ctx.scale(scale, scale);
                        ctx.drawImage(img, 0, 0, width, height);

                        canvas.toBlob(jpgBlob => {
                            if (!jpgBlob) {
                                reject(new Error('canvas.toBlob() returned null — JPEG encoding failed.'));
                                return;
                            }
                            let jpgUrl;
                            try {
                                jpgUrl = URL.createObjectURL(jpgBlob);
                                const link = document.createElement('a');
                                link.href = jpgUrl;
                                link.download = `tree_${Date.now()}.jpg`;
                                document.body.appendChild(link);
                                link.click();
                                document.body.removeChild(link);
                                resolve();
                            } catch (err) {
                                reject(err);
                            } finally {
                                if (jpgUrl) URL.revokeObjectURL(jpgUrl);
                            }
                        }, 'image/jpeg', quality);

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
                const metric = {
                    query_cover: this._metricNumber(record.query_cover),
                    subject_cover: this._metricNumber(record.subject_cover),
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

        // Alan 5/9/26 - Compare one metric against its current threshold while allowing missing metric fields.
        _passesMetricThreshold(metric, field, threshold) {
            if (!metric || metric[field] === null || metric[field] === undefined) return true;
            return Number(metric[field]) + 1e-9 >= threshold;
        }

        // Alan 5/9/26 - Decide whether a leaf should remain visible under the active MycoMap metric sliders.
        _passesSequenceFilters(node) {
            const metric = node.__sequenceMetrics;
            if (!metric || !metric.blast_metrics_available) return true;
            return this._passesMetricThreshold(metric, 'query_cover', this.options.queryCoverThreshold || 0) &&
                this._passesMetricThreshold(metric, 'subject_cover', this.options.subjectCoverThreshold || 0) &&
                this._passesMetricThreshold(metric, 'identity', this.options.identityThreshold || 0);
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
                identityThreshold: this.options.identityThreshold || 0
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

        // Alan 5/11/26 - Treat hidden active-set members as currently deselected while preserving saved sets.
        _isVisibleSelection(id) {
            return Boolean(id && this.selectedIds.has(id) && !this.hiddenSelectionIds.has(id));
        }

        // Alan 5/11/26 - Toggle clicks through the transient hidden-selection layer before changing set membership.
        _toggleVisibleSelection(id) {
            if (!id) return false;
            if (this.hiddenSelectionIds.has(id)) {
                this.hiddenSelectionIds.delete(id);
                return true;
            }
            if (this.selectedIds.has(id)) {
                this.selectedIds.delete(id);
                return false;
            }
            this.selectedIds.add(id);
            return true;
        }

        // Alan 5/11/26 - Return visible active selections for action buttons and local Deselect behavior.
        _getVisibleSelectionIds() {
            this._trimSelectionSetsToCurrentTree();
            return Array.from(this.selectedIds).filter(id => !this.hiddenSelectionIds.has(id));
        }

        // Alan 5/11/26 - Hide current active selections locally without deleting selection-set membership.
        deselectCurrentSelection() {
            const visibleIds = this._getVisibleSelectionIds();
            visibleIds.forEach(id => this.hiddenSelectionIds.add(id));
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
                el.selectAll("text.phylotree-node-text").style("fill", "").style("stroke", "");
            });
        }

        _styleNode(element, node) {
            // Logic: Check all selection sets for membership, apply set-specific color
            // Search Match = Blue highlight (lower priority than selection)
            const id = this._getNodeId(node);
            if (!id) {
                element.style("fill", "").style("stroke", "");
                return;
            }

            // Find which set(s) this node belongs to
            const setNames = Object.keys(this.selectionSets);
            let matchingSetIndex = -1;

            for (let i = 0; i < setNames.length; i++) {
                // Alan 5/11/26 - Skip hidden active-set entries so Deselect clears highlights without editing sets.
                if (setNames[i] === this.activeSelectionSet && this.hiddenSelectionIds.has(id)) continue;
                if (this.selectionSets[setNames[i]].has(id)) {
                    matchingSetIndex = i;
                    break; // Use first matching set's color
                }
            }

            if (matchingSetIndex >= 0) {
                const color = this._selectionColors[matchingSetIndex % this._selectionColors.length];
                element.style("fill", color).style("stroke", color);
            } else if (node.__search_match) {
                element.style("fill", "#0EA5E9").style("stroke", "#0EA5E9");
            } else {
                element.style("fill", "").style("stroke", "");
            }
        }

        _extractSupportValue(node) {
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

            let supportType = 'none';

            // Explicit FastTree Override
            // FastTree support values are SH-like local supports (0-1).
            // They are NOT Bayesian Posteriors (PP), though they look similar.
            if (this.options.treeMethod === 'fasttree' && supportValues.length > 0) {
                supportType = 'SH';
            }
            else if (supportValues.length > 0) {
                const hasLarge = supportValues.some(v => v > 1.0);
                // If we have large values (Bootstrap), we only consider it 'mixed' if we see values <= 1.0
                // that are NOT 1.0 (or 0).
                // RAxML can output 0 or 1 for Bootstrap.
                // We assume 1.0 is low bootstrap (1%) if we have other high values.
                // Posterior Probabilities are usually fractional (0.95, 0.99).

                if (hasLarge) {
                    // Check for clearly fractional small values that imply mixed data
                    const hasFractionalSmall = supportValues.some(v => v < 1.0 && v > 0);
                    supportType = hasFractionalSmall ? 'mixed' : 'BS';
                } else {
                    // Only small values -> PP
                    supportType = 'PP';
                }
            }
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
                return;
            }

            const { maxSupport, supportType } = this.lastStats;
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

                    if (supportType === 'SH') {
                        // SH-like supports are 0-1, so use ppThreshold
                        if (numVal + EPS < ppThreshold) { group.select("text.node-support-value").remove(); return; }
                        rawLabel = numVal.toFixed(2);
                    }
                    else if (numVal <= 1.0) {
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
                    if (d.parent) {
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
                    d.__supportVec = { ux, uy, px, py, textAnchor, isTipZone: (d.y > tipZoneStart) };

                    text.attr("text-anchor", textAnchor)
                        .attr("dominant-baseline", "hanging")
                        .style("pointer-events", "none")
                        .text(rawLabel);
                });

            this._applyTextSizingFromZoom();
        }

        _applyTextSizingFromZoom() {
            if (!this.cachedZoomNode) return;
            const { k } = this._getSvgAndZoomGroup();

            // Base sizes
            let supportBase = this.options.supportBasePx;
            let tipBase = this.options.tipBasePx;
            // Let inputs override if present in DOM (live update)
            const sIn = document.getElementById("input-support-font");
            const tIn = document.getElementById("input-tip-font");
            if (sIn && !isNaN(sIn.value)) supportBase = Number(sIn.value);
            if (tIn && !isNaN(tIn.value)) tipBase = Number(tIn.value);

            // Limit text size to be readable
            const tipFontSvgPx = Math.max(1, tipBase / k);
            const supportFontSvgPx = Math.max(1, supportBase / k);
            const haloSvgPx = Math.max(0.75, 3 / k);

            const svg = window.d3v7.select(this.container).select("svg");

            svg.selectAll("text.node-support-value").each(function (d) {
                if (!d || !d.__supportVec) return;
                const { ux, uy, px, py, isTipZone } = d.__supportVec;

                let offRoot = 10, offPerp = 6;
                let finalSize = supportBase;
                if (isTipZone) { offRoot += 20; offPerp += 14; finalSize = Math.max(6, supportBase - 1); }

                const sz = Math.max(1, finalSize / k);
                const xOff = (ux * offRoot + px * offPerp) / k;
                // Vertical adjust
                const yOff = (uy * offRoot + py * offPerp) / k;

                window.d3v7.select(this)
                    .attr("x", xOff).attr("y", yOff)
                    .style("font-size", `${sz}px`)
                    .style("stroke-width", `${haloSvgPx}px`)
                    .style("stroke-linejoin", "round")
                    .style("paint-order", "stroke");
            });

            svg.selectAll("text.phylotree-node-text")
                .style("font-size", `${tipFontSvgPx}px`)
                .attr("dx", 2.0 / k).attr("dy", 1.9 / k);
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
                // Only intercept Left Click without modifiers (simple select)
                const isSimpleClick = event.button === 0 && !event.shiftKey && !event.ctrlKey && !event.metaKey && !event.altKey;

                if (!isSimpleClick) return; // Let phylotree handle shift+click, etc.

                // Check if target is a node text or part of a node
                const target = event.target;
                if (target.classList.contains('phylotree-node-text') || target.tagName === 'circle') {

                    // Stop phylotree directly!
                    event.stopPropagation();
                    event.preventDefault();

                    // Get D3 data
                    const d = window.d3v7.select(target).datum();

                    const id = self._getNodeId(d);
                    if (id) {
                        // Alan 5/11/26 - Toggle visible selection state while preserving Deselect-hidden set membership.
                        self._toggleVisibleSelection(id);
                        self._updateNodeStylesOnly();
                        self._updateStats();
                    }
                }
            };

            // Define context menu listener (for Right Click)
            this._contextMenuListener = function (event) {
                const target = event.target;
                if (target.classList.contains('phylotree-node-text') || target.tagName === 'circle' || target.closest('.node')) {
                    event.preventDefault(); // Stop Chrome menu
                    event.stopPropagation();

                    const d = window.d3v7.select(target).datum() || window.d3v7.select(target.closest('.node')).datum();

                    if (self.tree && self.tree.display) {
                        self.tree.display.handle_node_click(d, event);
                    }
                }
            };

            // Attach listeners
            this.container.addEventListener('click', this._clickListener, true);
            this.container.addEventListener('contextmenu', this._contextMenuListener);
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
            // Alan 5/11/26 - Keep transient hidden selections limited to active IDs still present in the tree.
            for (const id of Array.from(this.hiddenSelectionIds)) {
                if (!currentIds.has(id) || !this.selectedIds.has(id)) {
                    this.hiddenSelectionIds.delete(id);
                }
            }
            return changed;
        }

        /**
         * Clear ALL selections across ALL selection sets.
         * Use after backend mutations when node references become stale.
         */
        clearSelection() {
            // Clear every selection set, not just the active one
            for (const setName of Object.keys(this.selectionSets)) {
                this.selectionSets[setName].clear();
            }
            // Alan 5/11/26 - Clear transient Deselect state when all selection-set membership is removed.
            this.hiddenSelectionIds.clear();
            this._updateStats();
            this._updateNodeStylesOnly();
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

        // --- SELECTION SET MANAGEMENT (CRUD) ---

        /**
         * Create a new named selection set.
         * @param {string} name - Name for the new set
         * @returns {boolean} - True if created, false if name already exists or invalid
         */
        createSelectionSet(name) {
            if (!name || typeof name !== 'string') return false;
            const trimmed = name.trim();
            if (!trimmed || this.selectionSets[trimmed]) return false;

            this.selectionSets[trimmed] = new Set();
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

            // If we deleted the active set, switch back to Default
            if (this.activeSelectionSet === name) {
                this.activeSelectionSet = 'Default';
            }

            // Alan 5/11/26 - Drop transient Deselect state when deleting selection-set membership.
            this.hiddenSelectionIds.clear();
            this._updateNodeStylesOnly();
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
            // Alan 5/11/26 - Switching sets should show that set's saved selections normally.
            this.hiddenSelectionIds.clear();
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
            const names = Object.keys(this.selectionSets);
            const index = names.indexOf(name);
            if (index < 0) return null;
            return this._selectionColors[index % this._selectionColors.length];
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
            return { sets, active: this.activeSelectionSet };
        }

        restoreSelectionSets(data) {
            if (!data || !data.sets) return;
            for (const name of Object.keys(this.selectionSets)) {
                if (name !== 'Default') delete this.selectionSets[name];
            }
            this.selectionSets['Default'].clear();
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
            // Alan 5/11/26 - Restored selection sets should not inherit a prior local Deselect state.
            this.hiddenSelectionIds.clear();
            // Alan 5/10/26 - Do not restore saved selections for nodes removed by pruning.
            this._trimSelectionSetsToCurrentTree();
            this._updateNodeStylesOnly();
            this._updateStats();
        }

        /**
         * Clear only the ACTIVE selection set (not all sets).
         * Uses the selectedIds getter which references this.selectionSets[this.activeSelectionSet].
         */
        clearActiveSelection() {
            this.selectedIds.clear(); // selectedIds getter returns the active set
            // Alan 5/11/26 - Clear transient hidden IDs when the active selection set is emptied.
            this.hiddenSelectionIds.clear();
            this._updateStats();
            this._updateNodeStylesOnly();
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
