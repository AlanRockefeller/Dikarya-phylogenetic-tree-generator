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
                supportBasePx: 9,
                tipBasePx: 12,
                layout: 'linear',
                alignTips: false
            }, initialOptions);

            this.tree = null;
            this.newick = null;
            this.allNodes = []; // Node Cache

            // State - Multiple Selection Sets
            // selectionSets is the primary data structure: { 'Default': Set(), 'Edible': Set(), ... }
            this.selectionSets = { 'Default': new Set() };
            this.activeSelectionSet = 'Default';
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
                if (id && this.selectedIds.has(id)) {
                    // Appends {Selected} to the node, recognized as a comment/tag by most tree viewers
                    return "{Selected}";
                }
                return "";
            });
        }

        exportSVG() {
            const svg = this.container.querySelector('svg');
            if (!svg) return;

            // Clone
            const clone = svg.cloneNode(true);

            // 1. Clean Dark Reader artifacts
            const removeDarkReader = (el) => {
                if (!el || !el.getAttribute) return;
                [...el.attributes].forEach(attr => {
                    if (attr.name.startsWith("data-darkreader")) el.removeAttribute(attr.name);
                });
                const style = el.getAttribute("style");
                if (style) {
                    const cleanStyle = style.replace(/--darkreader-[^;]+;?/g, "").trim();
                    if (cleanStyle) el.setAttribute("style", cleanStyle);
                    else el.removeAttribute("style");
                }
                for (const child of el.children) removeDarkReader(child);
            };
            removeDarkReader(clone);
            clone.querySelectorAll("style").forEach(s => {
                if ((s.textContent || "").toLowerCase().includes("darkreader")) s.remove();
            });

            // 2. Extract relevant CSS rules from document.styleSheets
            const extractRelevantStyles = () => {
                // Selectors that are relevant for the tree SVG
                const relevantPatterns = [
                    /\.node\b/, /\.branch\b/, /\.phylotree/, /\.internal-node/,
                    /\.tree-/, /circle/, /path/, /text/, /line/,
                    /\.node-support-value/, /\.selected/
                ];

                let cssText = '';
                try {
                    for (const sheet of document.styleSheets) {
                        // Skip cross-origin stylesheets (cssRules will throw)
                        let rules;
                        try {
                            rules = sheet.cssRules || sheet.rules;
                        } catch (e) {
                            continue; // CORS-blocked stylesheet
                        }
                        if (!rules) continue;

                        for (const rule of rules) {
                            if (rule.type !== CSSRule.STYLE_RULE) continue;
                            const selector = rule.selectorText || '';
                            // Check if selector is relevant to tree elements
                            const isRelevant = relevantPatterns.some(pattern => pattern.test(selector));
                            if (isRelevant) {
                                // Clean dark reader variables from the rule
                                let ruleText = rule.cssText.replace(/--darkreader-[^;:]+:[^;]+;?/g, '');
                                cssText += ruleText + '\n';
                            }
                        }
                    }
                } catch (e) {
                    console.warn('Could not extract all stylesheets:', e);
                }
                return cssText;
            };

            const extractedCSS = extractRelevantStyles();

            // 3. Embed extracted styles in <defs><style>
            if (extractedCSS) {
                let defs = clone.querySelector('defs');
                if (!defs) {
                    defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
                    clone.insertBefore(defs, clone.firstChild);
                }
                const styleEl = document.createElementNS('http://www.w3.org/2000/svg', 'style');
                styleEl.setAttribute('type', 'text/css');
                styleEl.textContent = extractedCSS;
                defs.appendChild(styleEl);
            }

            // 4. Apply minimal inline overrides for selection/search highlighting only
            // This replaces the expensive per-node getComputedStyle loop
            const origNodes = svg.querySelectorAll('.node circle, .node path, .node rect');
            const cloneNodes = clone.querySelectorAll('.node circle, .node path, .node rect');
            const n = Math.min(origNodes.length, cloneNodes.length);

            for (let i = 0; i < n; i++) {
                const origEl = origNodes[i];
                const cloneEl = cloneNodes[i];

                // Check if this node has inline selection/highlight styles applied by _styleNode
                const origStyle = origEl.getAttribute('style') || '';
                // Only copy fill/stroke if they indicate selection (orange) or search match (blue)
                if (origStyle.includes('fill') || origStyle.includes('stroke')) {
                    // Extract just fill and stroke from inline style
                    const fillMatch = origStyle.match(/fill\s*:\s*([^;]+)/);
                    const strokeMatch = origStyle.match(/stroke\s*:\s*([^;]+)/);
                    let overrideStyle = '';
                    if (fillMatch) overrideStyle += `fill:${fillMatch[1].trim()};`;
                    if (strokeMatch) overrideStyle += `stroke:${strokeMatch[1].trim()};`;
                    if (overrideStyle) {
                        cloneEl.setAttribute('style', overrideStyle);
                    }
                }
            }

            // Also handle text elements that might have special styling
            const origTexts = svg.querySelectorAll('text.node-support-value');
            const cloneTexts = clone.querySelectorAll('text.node-support-value');
            const tn = Math.min(origTexts.length, cloneTexts.length);

            for (let i = 0; i < tn; i++) {
                const origEl = origTexts[i];
                const cloneEl = cloneTexts[i];
                // Copy positioning and sizing attributes that are dynamically applied
                ['x', 'y', 'text-anchor', 'dominant-baseline'].forEach(attr => {
                    const val = origEl.getAttribute(attr);
                    if (val) cloneEl.setAttribute(attr, val);
                });
                // Copy essential inline styles for support labels
                const origStyle = origEl.getAttribute('style') || '';
                const fontSize = origStyle.match(/font-size\s*:\s*([^;]+)/);
                const strokeWidth = origStyle.match(/stroke-width\s*:\s*([^;]+)/);
                let essentialStyle = '';
                if (fontSize) essentialStyle += `font-size:${fontSize[1].trim()};`;
                if (strokeWidth) essentialStyle += `stroke-width:${strokeWidth[1].trim()};`;
                if (essentialStyle) {
                    const existing = cloneEl.getAttribute('style') || '';
                    cloneEl.setAttribute('style', essentialStyle + existing);
                }
            }

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

        // --- INTERNAL HELPERS ---

        _updateNodeStylesOnly() {
            if (!this.tree) return;
            const svg = window.d3v7.select(this.container).select("svg");
            if (svg.empty()) return;
            const self = this;

            svg.selectAll(".node").each(function (d) {
                const el = window.d3v7.select(this);

                // Try to find a styleable element (phylotree uses text for tip labels)
                let shape = el.select("circle");
                if (shape.empty()) shape = el.select("path");
                if (shape.empty()) shape = el.select("rect");
                if (shape.empty()) shape = el.select("text"); // Add text element support

                const id = self._getNodeId(d);

                if (!shape.empty()) {
                    self._styleNode(shape, d);
                }
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

        _computeSupportStats() {
            if (!this.allNodes || !this.allNodes.length) return { maxSupport: 0, supportType: 'none' };

            let maxSupport = 0;
            let supportValues = [];

            // Fast Iteration over Cached Nodes
            // Leaf counts are already pre-computed in _cacheNodes()

            // Support value extraction
            for (const node of this.allNodes) {
                if (!node.children || node.children.length === 0) continue; // Skip tips for support
                const support = node.data?.confidence || node.data?.bootstrap_values ||
                    node.data?.bootstrap || node.data?.support; // check standard fields

                if (support !== undefined && support !== null && support !== "") {
                    const val = parseFloat(support);
                    if (!isNaN(val)) {
                        supportValues.push(val);
                        maxSupport = Math.max(maxSupport, val);
                    }
                }
            }

            let supportType = 'none';
            if (supportValues.length > 0) {
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

                    // Label Extract - Prioritize explicit fields
                    // Only check name if it definitely looks like support (not just a generated node name)
                    let rawLabel = d.data?.confidence || d.data?.bootstrap_values || d.data?.bootstrap || d.data?.support;

                    // Fallback to name only if specifically formatted as support-like (numeric) and NOT the default Node_X
                    if ((rawLabel === undefined || rawLabel === null || rawLabel === "") && d.data?.name) {
                        const n = d.data.name;
                        // Logic: if purely numeric, treat as support. 
                        // Or if matches internal node pattern with support suffix specifically: Node_X_Support
                        if (/^\d+(\.\d+)?$/.test(n)) {
                            rawLabel = n;
                        } else {
                            // STRICTER CHECK: Only extract if it looks like the default internal node naming scheme
                            // e.g. Node_5_100 or Node_5_0.95
                            // Reject "Sample_123"
                            const match = n.match(/^Node_\d+_(\d+(?:\.\d+)?)$/);
                            if (match) {
                                rawLabel = match[1];
                            }
                        }
                    }

                    if (!rawLabel || isNaN(parseFloat(rawLabel))) {
                        group.select("text.node-support-value").remove();
                        return;
                    }
                    const numVal = parseFloat(rawLabel);

                    // Threshold Filter - decide PP vs bootstrap by value magnitude
                    const EPS = 1e-9;
                    if (numVal <= 1.0) {
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

                if (self.selectedIds.has(id)) {
                    self.selectedIds.delete(id);
                } else {
                    self.selectedIds.add(id);
                }

                self._updateNodeStylesOnly();
                // Fire callback with full node details, but state is now in Viewer
                if (self.callbacks.onTipClick) {
                    self.callbacks.onTipClick({
                        name: id,
                        display_name: d.data.name || id,
                        is_leaf: !d.children || !d.children.length,
                        selected: self.selectedIds.has(id)
                    });
                }
                self._updateStats();
            });
        }

        _updateStats() {
            const selCount = this.selectedIds.size;
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
                        if (self.selectedIds.has(id)) {
                            self.selectedIds.delete(id);
                        } else {
                            self.selectedIds.add(id);
                        }
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

        /**
         * Clear ALL selections across ALL selection sets.
         * Use after backend mutations when node references become stale.
         */
        clearSelection() {
            // Clear every selection set, not just the active one
            for (const setName of Object.keys(this.selectionSets)) {
                this.selectionSets[setName].clear();
            }
            this._updateStats();
            this._updateNodeStylesOnly();
        }

        selectionAction(action, filteredNodesPredicate = null) {
            if (!this.tree) return;

            const DEBUG_MODE = new URLSearchParams(window.location.search).has('debug');
            if (DEBUG_MODE) console.log('selectionAction called:', action, 'active set:', this.activeSelectionSet);

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
            this.selectedIds.add(mrcaId);

            // For each selected node, walk up to MRCA and select the path
            for (const node of selectedNodes) {
                let current = node;
                while (current) {
                    const id = this._getNodeId(current);
                    if (id) {
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
                if (id) this.selectedIds.add(id);

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
                if (id && this.selectedIds.has(id)) selected.push(n);
            });
            return selected;
        }

        getSelectionCount() {
            return this.selectedIds.size;
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

        /**
         * Clear only the ACTIVE selection set (not all sets).
         * Uses the selectedIds getter which references this.selectionSets[this.activeSelectionSet].
         */
        clearActiveSelection() {
            this.selectedIds.clear(); // selectedIds getter returns the active set
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
