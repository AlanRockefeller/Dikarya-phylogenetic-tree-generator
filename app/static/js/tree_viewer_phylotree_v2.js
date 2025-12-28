/**
 * Phylotree v2 Renderer - The "Nuclear Option" for Sticky Pan
 */
(function () {
    'use strict';

    // --- THE "BELT AND SUSPENDERS" PANIC STOP ---
    // If we ever still see sticky pan (e.g. releasing mouse outside the window),
    // this forcibly clears any active zoom drag handlers.
    if (!window.__dikarya_zoom_panic_stop_attached) {
        window.__dikarya_zoom_panic_stop_attached = true;
        window.addEventListener('pointerup', () => {
            if (window.d3v7) {
                window.d3v7.select(window).on('mousemove.zoom', null).on('mouseup.zoom', null);
            }
        }, true);
    }

    function renderPhylotree(newick, elementId, callbacks, renderOptions) {
        const container = document.getElementById(elementId);
        if (!container) return;

        // 1. CLEAR & SETUP
        container.innerHTML = '';

        if (!window.d3v7) {
            container.innerHTML = '<div class="alert alert-danger">D3 v7 is required.</div>';
            return;
        }
        const phylotreeLib = window.phylotree;
        if (!phylotreeLib) return;

        // 2. PARSE
        const tree = new phylotreeLib.phylotree(newick);

        // 3. STATE
        let state = {
            layout: 'linear',
            alignTips: false,
            width: 800,
            height: 600,
            showSupport: (renderOptions && renderOptions.showSupport !== undefined) ? renderOptions.showSupport : true
        };

        const rect = container.getBoundingClientRect();
        state.width = rect.width || 800;
        state.height = rect.height || 600;

        // 4. DRAW FUNCTION
        function draw() {
            container.innerHTML = '';

            const options = {
                container: "#" + elementId,
                width: state.width,
                height: state.height,
                'is-radial': (state.layout === 'radial'),
                'align-tips': state.alignTips,
                'draw-size-bubbles': false,
                'zoom': true,
                'left-right-spacing': 'fit-to-size',
                'top-bottom-spacing': 'fit-to-size',
                'node-styler': function (element, node) {
                    updateNodeStyle(element, node);
                }
            };

            function updateNodeStyle(element, node) {
                if (node.selected) {
                    element.style("fill", "orange").style("stroke", "orange");
                } else {
                    element.style("fill", "").style("stroke", "");
                }
            }

            // IMPORTANT: phylotree's zoom handlers run later (on mousemove/mouseup),
            // so window.d3 must remain the same D3 instance that created them.
            if (!window.__dikarya_d3_locked_to_v7) {
                window.__dikarya_d3_locked_to_v7 = true;
                window.__dikarya_d3_legacy = window.d3; // optional, if you need it elsewhere
                window.d3 = window.d3v7;
            }

            try {
                const renderer = tree.render(options);

                // Manual Append
                if (container.children.length === 0 && renderer) {
                    if (renderer.element) container.appendChild(renderer.element);
                    else if (renderer.svg) container.appendChild(renderer.svg.node());
                }

                // Inside draw(), after tree.render(options) AND append:
                setTimeout(() => addSupportLabels(), 150);

                // --- THE NUCLEAR FIX FOR STICKY PAN ---

                // 1. Force CSS to kill all text selection
                // If the browser tries to select text, it misses the mouseup event -> sticky pan.
                container.style.userSelect = 'none';
                container.style.webkitUserSelect = 'none';
                container.style.outline = 'none';

                // 2. Use "Capture Phase" Listeners
                // We attach a listener to the CONTAINER that runs BEFORE any D3 listeners.
                // If the user clicks something that looks like a node, we STOP it right there.
                const killDrag = (e) => {
                    // Check if the target is part of a node (circle, text, or the group)
                    const target = e.target;
                    const isNode = target.closest('.node'); // Works on SVG elements in modern browsers

                    if (isNode) {
                        // We found a node! Stop the Zoom engine from hearing this event.
                        e.stopPropagation();

                        // BUT allow our own click logic (handled below)
                    }
                };

                // Attach to container with {capture: true}
                // This guarantees we run before D3's zoom listener
                if (!container.__killDragAttached) {
                    container.__killDragAttached = true;
                    container.addEventListener('mousedown', killDrag, true);
                    container.addEventListener('pointerdown', killDrag, true);
                    // pointerdown covers mouse+touch on modern browsers; you can usually drop the others
                }


                // 3. HANDLE CLICKS (Using D3 for logic)
                if (callbacks && callbacks.onTipClick) {
                    window.d3v7.select(container).selectAll(".node").on("click", function (event, d) {
                        // Even though we stopped propagation above for 'mousedown',
                        // 'click' is a separate event that fires later. We catch it here.
                        event.stopPropagation();

                        // Toggle
                        d.selected = !d.selected;

                        // Visual Update
                        const circle = window.d3v7.select(this).select("circle");
                        updateNodeStyle(circle, d);

                        // Callback
                        const nodeName = d.data.name;
                        const isLeaf = !d.children || d.children.length === 0;
                        if (nodeName) callbacks.onTipClick({
                            name: nodeName,
                            display_name: nodeName,
                            is_leaf: isLeaf
                        });
                    });
                }

            } catch (e) {
                console.error("Render error:", e);
            } finally {
                // do not restore window.d3
            }
        }

        function addSupportLabels() {
            if (!state.showSupport) {
                window.d3v7.select(container)
                    .selectAll("text.node-support-value")
                    .remove(); // Idempotent: remove if toggled off
                return;
            }

            const svg = window.d3v7.select(container).select("svg");

            // Robust Zoom Group Detection for Scale
            // 1. Try finding group with specific transform
            let zoomGroup = svg.select("g[transform]");
            // 2. Fallback: looking for group containing nodes
            if (zoomGroup.empty()) {
                zoomGroup = svg.select("g.node").node() ? window.d3v7.select(svg.select("g.node").node().parentNode) : svg;
            }

            const zoomTransform = window.d3v7.zoomTransform(zoomGroup.node() || svg.node());
            const k = zoomTransform.k || 1;

            // Font size: clamp 6px - 9px based on zoom
            // k=1 -> 9px. k=2 -> 4.5px (clamped to 6). k=0.5 -> 18px (clamped to 9).
            let fontPx = Math.max(6, Math.min(9, 9 / k));

            const ppThreshold = (renderOptions && renderOptions.ppThreshold !== undefined) ? renderOptions.ppThreshold : 0.9;
            const bootThreshold = (renderOptions && renderOptions.bootstrapThreshold !== undefined) ? renderOptions.bootstrapThreshold : 70;
            const minDescendants = (renderOptions && renderOptions.minTips !== undefined) ? renderOptions.minTips : 0;

            // Robust selection: Select all 'g' elements, then filter by data to find internal nodes
            const nodeGroups = svg.selectAll("g.node, g.internal-node").filter(function (d) {
                // Must be internal (have children)
                return d && d.children && d.children.length > 0;
            });

            // Calculate maxY for Tip Zone Heuristic (Relative to tree height)
            // d.y corresponds to the horizontal position (distance from root) usually
            let maxY = 0;
            svg.selectAll("g.node").each(d => { if (d.y > maxY) maxY = d.y; });
            const tipZoneStart = 0.85 * maxY;

            nodeGroups.each(function (d) {
                const group = window.d3v7.select(this);

                // --- 0. Min Tips Filter ---
                if (minDescendants > 0) {
                    let count = 0;
                    if (d.leaves) {
                        // Standard D3 hierarchy
                        count = d.leaves().length;
                    } else if (d.value !== undefined) {
                        // often 'value' is leaf count if .count() was called
                        count = d.value;
                    } else {
                        // Manual count fallback
                        const stack = [d];
                        while (stack.length) {
                            const n = stack.pop();
                            if (n.children && n.children.length > 0) {
                                for (const c of n.children) stack.push(c);
                            } else {
                                count++;
                            }
                        }
                    }

                    if (count < minDescendants) {
                        group.select("text.node-support-value").remove();
                        return;
                    }
                }

                // --- 1. Label Extraction (Same as before) ---
                let rawLabel = d.data?.confidence;
                if (rawLabel === undefined || rawLabel === null || rawLabel === "") {
                    rawLabel = d.data?.name || d.data?.bootstrap_values || d.data?.bootstrap || d.data?.support;
                }

                let label = rawLabel;
                // Basic cleanup for "Node_12_100" or "Node_12"
                if (label && typeof label === 'string' && label.startsWith("Node_")) {
                    const match = label.match(/^Node_\d+_(\d+(?:\.\d+)?)$/);
                    if (match) {
                        label = match[1];
                    } else if (label.match(/^Node_\d+$/)) {
                        label = null;
                    } else {
                        const looseMatch = label.match(/_(\d+(?:\.\d+)?)$/);
                        if (looseMatch) label = looseMatch[1];
                        else label = null;
                    }
                }

                if (!label) {
                    group.select("text.node-support-value").remove();
                    return;
                }
                const numValue = parseFloat(label);

                // --- 2. Filtering (Semantic + Epsilon) ---
                if (isNaN(numValue)) {
                    group.select("text.node-support-value").remove();
                    return;
                }

                let displayValue = "";
                // small epsilon for float comparison safety
                const EPS = 1e-9;

                if (numValue <= 1.0) {
                    // Posterior Probability logic
                    if (numValue + EPS < ppThreshold) {
                        group.select("text.node-support-value").remove();
                        return; // Hidden by threshold
                    }
                    displayValue = numValue.toFixed(2);
                } else if (numValue <= 100) {
                    // Bootstrap logic
                    if (numValue + EPS < bootThreshold) {
                        group.select("text.node-support-value").remove();
                        return; // Hidden by threshold
                    }
                    displayValue = Math.round(numValue).toString();
                } else {
                    // Out of range? Remove.
                    group.select("text.node-support-value").remove();
                    return;
                }

                // --- 3. Vector Positioning ---
                // d.x = vertical, d.y = horizontal in standard cluster layout
                // Vector pointing FROM node TO parent
                let xOff = 0, yOff = 0;
                let textAnchor = "middle";

                if (d.parent) {
                    let vx = d.parent.y - d.y; // Horizontal component
                    let vy = d.parent.x - d.x; // Vertical component
                    const len = Math.hypot(vx, vy) || 1;
                    const ux = vx / len;
                    const uy = vy / len;

                    // Perpendicular: (-uy, ux)
                    let px = -uy;
                    let py = ux;

                    // Deterministic Flip: Ensure 'py' is positive leads to "down" on screen?
                    // typically y increases downwards in SVG.
                    // If we want labels BELOW the branch:
                    // If branch is horizontal (uy ~ 0), we want py positive (down).
                    // If branch is vertical (ux ~ 0), perpendicular is horizontal.
                    // Helper Rule: Force py > 0 (downwards)
                    if (py < 0) {
                        px = -px;
                        py = -py;
                    }

                    // Base offset: push along unit vector (away from parent! wait, "vx" is TO parent)
                    // We want label slightly towards root? No, usually towards middle of branch.
                    // d is the node. d.parent is header.
                    // Label is usually placed at the node (d), or distinct from node?
                    // Code logic: d corresponds to the Split.
                    // phylotree draws lines from d.parent to d.
                    // We are at d.

                    // Position:
                    // "Pull toward root": Move slightly along +vx (towards parent)
                    // "Nudge below": Move along perpendicular

                    xOff = (ux * 10) + (px * 6);
                    yOff = (uy * 10) + (py * 6);

                    // Anchoring
                    if (ux < -0.2) textAnchor = "end";
                    else if (ux > 0.2) textAnchor = "start";
                    else textAnchor = "middle";

                    // --- 4. Tip Zone Heuristic ---
                    if (d.y > tipZoneStart) {
                        // Near tips: push further towards root (away from tip)
                        // vx points to parent (root-ward). So add more vx.
                        xOff += (ux * 20);
                        yOff += (uy * 20);

                        // Also shrink font slightly
                        fontPx = Math.max(6, fontPx - 1);
                    }

                    // Safe Fallback for tiny vectors
                    if (len < 1e-6) {
                        xOff = -8; yOff = 8; textAnchor = "end";
                    }

                } else {
                    // Root node or weird state
                    xOff = -10; yOff = 10; textAnchor = "end";
                }


                // --- 5. Draw / Update ---

                // Idempotent: Select specific class
                let text = group.select("text.node-support-value");
                if (text.empty()) {
                    text = group.append("text").attr("class", "node-support-value");
                }

                text
                    .attr("x", xOff)
                    .attr("y", yOff)
                    .attr("text-anchor", textAnchor)
                    .attr("dominant-baseline", "hanging") // Good for "below" text
                    // Inline styles for SVG Export
                    .attr("fill", "#b30000")
                    .attr("font-family", "sans-serif")
                    .attr("font-weight", "500")
                    .style("font-size", `${fontPx}px`) // Dynamic Zoom
                    .style("fill", "#b30000") // Red
                    // Painted Stroke Halo
                    .style("paint-order", "stroke")
                    .style("stroke", "rgba(255,255,255,0.85)")
                    .style("stroke-width", "3px")
                    .style("stroke-linejoin", "round")
                    .style("pointer-events", "none")
                    .style("display", "block")
                    .text(displayValue);
            });
        }

        // INITIAL DRAW
        draw();

        // UI Update for Align Button
        function updateAlignButton() {
            const btn = document.getElementById('btn-align-tips');
            if (!btn) return;
            if (state.alignTips) {
                btn.innerHTML = '<i class="fa fa-outdent"></i> Unalign';
                btn.title = "Unalign Tips";
                btn.classList.add('active');
            } else {
                btn.innerHTML = '<i class="fa fa-indent"></i> Align';
                btn.title = "Align Tips";
                btn.classList.remove('active');
            }
        }
        updateAlignButton();

        // ---------------------------------------------------------
        // 5. TOOLBAR
        // ---------------------------------------------------------
        const bindBtn = (id, handler) => {
            const btn = document.getElementById(id);
            if (btn) {
                const newBtn = btn.cloneNode(true);
                btn.parentNode.replaceChild(newBtn, btn);
                newBtn.addEventListener('click', handler);
            }
        };

        bindBtn('btn-layout-linear', () => { state.layout = 'linear'; draw(); });
        bindBtn('btn-layout-radial', () => { state.layout = 'radial'; draw(); });
        bindBtn('btn-align-tips', () => {
            state.alignTips = !state.alignTips;
            draw();
            updateAlignButton();
        });

        bindBtn('btn-ladderize', () => {
            tree.traverse_and_compute((node) => {
                if (node.children) {
                    node.children.sort((a, b) => {
                        const countA = (a.msg && a.msg.count) ? a.msg.count : 0;
                        const countB = (b.msg && b.msg.count) ? b.msg.count : 0;
                        return countA - countB;
                    });
                }
            });
            draw();
        });

        bindBtn('btn-select-all', () => {
            tree.traverse_and_compute((node) => { node.selected = true; });
            draw();
        });

        bindBtn('btn-select-none', () => {
            tree.traverse_and_compute((node) => { node.selected = false; });
            draw();
        });

        bindBtn('btn-save-svg', (e) => {
            if (e) e.preventDefault();
            const svg = container.querySelector('svg');
            if (!svg) return;
            const data = (new XMLSerializer()).serializeToString(svg);
            const blob = new Blob([data], { type: "image/svg+xml;charset=utf-8" });
            const url = URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.href = url;
            link.download = `tree_${Date.now()}.svg`;
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
        });
    }

    window.renderPhylotree = renderPhylotree;
})();
