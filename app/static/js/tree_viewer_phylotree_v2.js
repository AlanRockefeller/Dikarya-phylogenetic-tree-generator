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
        renderOptions = renderOptions || {};
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

        // 2b. PRE-PROCESS NAMES (No-op)
        // We do NOT replace underscores with spaces anymore, because we want to 
        // "Preserve original for display" as per the canonical data policy.
        // The backend restores the original name (which might have spaces) 
        // so we just display what we get.
        /*
        tree.traverse_and_compute((node) => {
            if (node.data && node.data.name) {
                // If it's a string, we process it
                if (typeof node.data.name === 'string') {
                    node.data.__original_name = node.data.name;
                    node.data.name = node.data.name.replace(/_/g, " ");
                }
            } else if (node.name) {
                // Fallback: sometimes phylotree puts name directly on node?
                node.__original_name = node.name;
                node.name = node.name.replace(/_/g, " ");
            }
        });
        */

        // 3. STATE
        let state = {
            layout: 'linear',
            alignTips: false,
            width: 800,
            height: 600,
            showSupport: (renderOptions.showSupport !== undefined) ? renderOptions.showSupport : true
        };

        const rect = container.getBoundingClientRect();
        state.width = rect.width || 800;
        state.height = rect.height || 600;

        // --- Zoom sizing plumbing (screen-constant text) ---
        let cachedZoomNode = null;
        let zoomObserver = null;
        let rafPending = false;
        let supportLabelsTimer = null;

        function cleanupZoomObserver() {
            try {
                if (zoomObserver) zoomObserver.disconnect();
            } catch (_) { }
            zoomObserver = null;
            rafPending = false;
            cachedZoomNode = null;
        }

        function findZoomGroup(svgSel) {
            // Prefer a group that both has a transform AND contains nodes.
            const svgNode = svgSel.node();
            if (!svgNode) return svgSel;

            // If cached node is still alive, use it.
            if (cachedZoomNode && cachedZoomNode.isConnected) {
                return window.d3v7.select(cachedZoomNode);
            }

            // Gather candidates with a transform attribute
            const candidates = svgSel.selectAll("g[transform]").nodes();
            if (candidates && candidates.length) {
                // Pick the first candidate that contains a node group
                for (const g of candidates) {
                    if (g.querySelector && g.querySelector("g.node, g.internal-node")) {
                        cachedZoomNode = g;
                        return window.d3v7.select(g);
                    }
                }
                // Fallback: first transformed group
                cachedZoomNode = candidates[0];
                return window.d3v7.select(candidates[0]);
            }

            // Final fallback: parent of a node group, else svg itself
            const anyNode = svgSel.select("g.node").node();
            if (anyNode && anyNode.parentNode) {
                cachedZoomNode = anyNode.parentNode;
                return window.d3v7.select(anyNode.parentNode);
            }

            cachedZoomNode = svgNode;
            return svgSel;
        }

        function getSvgAndZoomGroup() {
            const svg = window.d3v7.select(container).select("svg");
            if (svg.empty()) return { svg, zoomGroup: null, k: 1 };

            const zoomGroup = findZoomGroup(svg);
            const zoomTransform = window.d3v7.zoomTransform(zoomGroup.node() || svg.node());
            const k = zoomTransform.k || 1;
            return { svg, zoomGroup, k };
        }

        function computeAdaptiveHaloColor() {
            // Dark mode extensions often change container background; adapt halo accordingly
            const bg = window.getComputedStyle(container).backgroundColor || "";
            let haloColor = "rgba(255,255,255,0.85)";

            const rgb = bg.match(/\d+/g);
            if (rgb && rgb.length >= 3) {
                const r = Number(rgb[0]), g = Number(rgb[1]), b = Number(rgb[2]);
                const lum = 0.299 * r + 0.587 * g + 0.114 * b;
                if (lum < 128) haloColor = "rgba(0,0,0,0.65)";
            }
            return haloColor;
        }

        function applyTextSizingFromZoom() {
            const { svg, zoomGroup, k } = getSvgAndZoomGroup();
            if (!zoomGroup || svg.empty()) return;

            // Read base sizes from options, but allow live DOM overrides (no re-render needed)
            let supportBase = (renderOptions.supportBasePx) ? Number(renderOptions.supportBasePx) : 9;
            let tipBase = (renderOptions.tipBasePx) ? Number(renderOptions.tipBasePx) : 12;

            const sInput = document.getElementById("input-support-font");
            const tInput = document.getElementById("input-tip-font");

            if (sInput) {
                const v = Number(sInput.value);
                if (Number.isFinite(v)) supportBase = v;
            }
            if (tInput) {
                const v = Number(tInput.value);
                if (Number.isFinite(v)) tipBase = v;
            }

            // Safety clamps (these are SCREEN px)
            supportBase = Math.max(6, Math.min(60, supportBase));
            tipBase = Math.max(8, Math.min(60, tipBase));

            // Screen-constant halo width
            const haloColor = computeAdaptiveHaloColor();
            const haloScreenPx = 3; // publication-friendly
            const haloSvgPx = Math.max(0.75, Math.min(4, haloScreenPx / k));

            // Update support labels
            svg.selectAll("text.node-support-value").each(function (d) {
                const el = window.d3v7.select(this);
                if (!d || !d.__supportVec) return;

                const { ux, uy, px, py, isTipZone } = d.__supportVec;

                // Offsets in SCREEN px, then divide by k for SVG coords
                let offRoot = 10;
                let offPerp = 6;
                let supportScreenPx = supportBase;

                if (isTipZone) {
                    // Pull away from tip labels more aggressively near the tips
                    offRoot += 20;
                    offPerp += 14;
                    supportScreenPx = Math.max(6, supportBase - 1);
                }

                const fontSvgPx = Math.max(1, supportScreenPx / k);
                const xOff = (ux * offRoot + px * offPerp) / k;
                const yOff = (uy * offRoot + py * offPerp) / k;

                // Root fallback: keep labels tucked left/down
                if (!d.parent && Math.abs(xOff) < 1e-9 && Math.abs(yOff) < 1e-9) {
                    el.attr("x", -10 / k).attr("y", 10 / k);
                } else {
                    el.attr("x", xOff).attr("y", yOff);
                }

                el.style("font-size", `${fontSvgPx}px`)
                    .style("paint-order", "stroke")
                    // .style("stroke", haloColor) // let CSS handle color for light/dark correctness
                    .style("stroke-width", `${haloSvgPx}px`)
                    .style("stroke-linejoin", "round");
            });

            // Update tip labels (screen-constant). Also scale dx/dy to keep spacing consistent.
            const tipFontSvgPx = Math.max(1, tipBase / k);
            const tipDxScreen = 2.0;
            const tipDyScreen = 1.9;
            const tipDxSvg = tipDxScreen / k;
            const tipDySvg = tipDyScreen / k;

            svg.selectAll("text.phylotree-node-text")
                .style("font-size", `${tipFontSvgPx}px`)
                .attr("dx", tipDxSvg)
                .attr("dy", tipDySvg);
        }

        function attachZoomObserverTo(node) {
            if (!node) return;

            if (zoomObserver) {
                try { zoomObserver.disconnect(); } catch (_) { }
                zoomObserver = null;
            }

            zoomObserver = new MutationObserver(() => {
                if (rafPending) return;
                rafPending = true;
                requestAnimationFrame(() => {
                    rafPending = false;
                    applyTextSizingFromZoom();
                });
            });

            // 1. Observe the primary node (usually the zoom group)
            zoomObserver.observe(node, { attributes: true, attributeFilter: ["transform"] });
            node.__dikaryaZoomObserverAttached = true;

            // 2. Fallback: If primary node is NOT the SVG, also observe the SVG element.
            // This ensures we catch zoom events even if they are applied to the root SVG.
            if (node.tagName && node.tagName.toLowerCase() !== 'svg') {
                const svg = (node.closest && node.closest('svg')) || container.querySelector('svg');
                if (svg) {
                    zoomObserver.observe(svg, { attributes: true, attributeFilter: ["transform"] });
                }
            }
        }

        // Expose hook so controller can resize without re-render (nice UX)
        container.__applyTextSizingFromZoom = () => {
            // keep renderOptions in sync if controller calls it
            const sInput = document.getElementById("input-support-font");
            const tInput = document.getElementById("input-tip-font");

            if (sInput) {
                const val = parseInt(sInput.value, 10);
                if (!isNaN(val)) renderOptions.supportBasePx = val;
            }
            if (tInput) {
                const val = parseInt(tInput.value, 10);
                if (!isNaN(val)) renderOptions.tipBasePx = val;
            }
            applyTextSizingFromZoom();
        };

        // 4. DRAW FUNCTION
        function draw() {
            // Tear down any observer bound to old SVG/zoomGroup to avoid leaks
            cleanupZoomObserver();
            if (supportLabelsTimer) {
                clearTimeout(supportLabelsTimer);
                supportLabelsTimer = null;
            }

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

                // add support labels shortly after render
                supportLabelsTimer = setTimeout(() => addSupportLabels(), 150);

                // --- THE NUCLEAR FIX FOR STICKY PAN ---

                // 1. Force CSS to kill all text selection
                container.style.userSelect = 'none';
                container.style.webkitUserSelect = 'none';
                container.style.outline = 'none';

                // 2. Use "Capture Phase" Listeners
                const killDrag = (e) => {
                    const target = e.target;
                    const isNode = target.closest('.node, .internal-node');
                    if (isNode) e.stopPropagation();
                };

                if (!container.__killDragAttached) {
                    container.__killDragAttached = true;
                    container.addEventListener('mousedown', killDrag, true);
                    container.addEventListener('pointerdown', killDrag, true);
                }

                // 3. HANDLE CLICKS (Using D3 for logic)
                if (callbacks && callbacks.onTipClick) {
                    window.d3v7.select(container).selectAll(".node").on("click", function (event, d) {
                        event.stopPropagation();

                        d.selected = !d.selected;

                        const circle = window.d3v7.select(this).select("circle");
                        updateNodeStyle(circle, d);

                        const nodeName = d.data.__original_name || d.data.name;
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
            const svg = window.d3v7.select(container).select("svg");
            if (svg.empty()) return;

            // Find zoom group and cache it; attach observer to it.
            const zoomGroup = findZoomGroup(svg);
            cachedZoomNode = zoomGroup.node() || svg.node();

            if (!state.showSupport) {
                window.d3v7.select(container)
                    .selectAll("text.node-support-value")
                    .remove();

                // FIXED: Ensure tip labels still resize and zoom observer attaches
                applyTextSizingFromZoom();
                attachZoomObserverTo(cachedZoomNode);
                return;
            }

            const ppThreshold = (renderOptions.ppThreshold !== undefined) ? renderOptions.ppThreshold : 0.9;
            const bootThreshold = (renderOptions.bootstrapThreshold !== undefined) ? renderOptions.bootstrapThreshold : 70;
            const minDescendants = (renderOptions.minTips !== undefined) ? renderOptions.minTips : 0;

            // Internal nodes only
            const nodeGroups = svg.selectAll("g.node, g.internal-node").filter(function (d) {
                return d && d.children && d.children.length > 0;
            });

            // tip-zone heuristic: based on max horizontal extent
            let maxY = 0;
            svg.selectAll("g.node, g.internal-node").each(d => {
                if (d && typeof d.y === "number" && d.y > maxY) maxY = d.y;
            });
            const tipZoneStart = 0.85 * maxY;

            nodeGroups.each(function (d) {
                const group = window.d3v7.select(this);

                // --- 0. Min Tips Filter ---
                if (minDescendants > 0) {
                    let count = 0;
                    if (d.leaves) {
                        count = d.leaves().length;
                    } else if (d.value !== undefined) {
                        count = d.value;
                    } else {
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

                // --- 1. Label Extraction ---
                let rawLabel = d.data?.confidence;
                if (rawLabel === undefined || rawLabel === null || rawLabel === "") {
                    rawLabel = d.data?.name || d.data?.bootstrap_values || d.data?.bootstrap || d.data?.support;
                }

                let label = rawLabel;

                // Cleanup for "Node_12_100" or "Node_12"
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
                if (isNaN(numValue)) {
                    group.select("text.node-support-value").remove();
                    return;
                }

                // --- 2. Filtering (Semantic + Epsilon) ---
                let displayValue = "";
                const EPS = 1e-9;

                if (numValue <= 1.0) {
                    if (numValue + EPS < ppThreshold) {
                        group.select("text.node-support-value").remove();
                        return;
                    }
                    displayValue = numValue.toFixed(2);
                } else if (numValue <= 100) {
                    if (numValue + EPS < bootThreshold) {
                        group.select("text.node-support-value").remove();
                        return;
                    }
                    displayValue = Math.round(numValue).toString();
                } else {
                    group.select("text.node-support-value").remove();
                    return;
                }

                // --- 3. Vector setup (store for zoom sizing) ---
                // d.x = vertical, d.y = horizontal in standard cluster layout
                // Vector points FROM node TO parent
                let textAnchor = "middle";
                let ux = 0, uy = 0, px = 0, py = 0;

                if (d.parent) {
                    const vx = d.parent.y - d.y;
                    const vy = d.parent.x - d.x;
                    const len = Math.hypot(vx, vy);

                    if (len && len > 1e-6) {
                        ux = vx / len;
                        uy = vy / len;

                        // Perpendicular: (-uy, ux)
                        px = -uy;
                        py = ux;

                        // Flip to make py positive (down-ish) for "below branch" behavior
                        if (py < 0) {
                            px = -px;
                            py = -py;
                        }

                        if (ux < -0.2) textAnchor = "end";
                        else if (ux > 0.2) textAnchor = "start";
                        else textAnchor = "middle";
                    } else {
                        // degenerate vector
                        ux = 0; uy = 0; px = 0; py = 1;
                        textAnchor = "end";
                    }
                } else {
                    // root
                    ux = 0; uy = 0; px = 0; py = 1;
                    textAnchor = "end";
                }

                d.__supportVec = {
                    ux, uy, px, py,
                    textAnchor,
                    isTipZone: (typeof d.y === "number" && d.y > tipZoneStart)
                };

                // --- 4. Draw / Update ---
                let text = group.select("text.node-support-value");
                if (text.empty()) {
                    text = group.append("text").attr("class", "node-support-value");
                }

                // Stable styling here; size/halo/position handled by applyTextSizingFromZoom()
                text
                    .attr("text-anchor", textAnchor)
                    .attr("dominant-baseline", "hanging")
                    .attr("font-family", "sans-serif")
                    .attr("font-weight", "500")
                    .style("paint-order", "stroke")
                    .style("pointer-events", "none")
                    .style("display", "block")
                    .text(displayValue);
            });

            // Now apply screen-constant sizing and attach zoom observer
            applyTextSizingFromZoom();
            attachZoomObserverTo(cachedZoomNode);
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
            state.ladderize = !state.ladderize;
            draw();
        });

        // Zoom/Fit Controls - ROBUST IMPLEMENTATION via Synthetic Events
        // Because accessing the internal D3 zoom behavior of the library is brittle.
        bindBtn('btn-zoom-in', () => {
            const svg = document.querySelector("#" + elementId + " svg");
            if (svg) {
                // Simulate wheel up (zoom in)
                // Need to target the center of the svg
                const rect = svg.getBoundingClientRect();
                const cx = rect.left + rect.width / 2;
                const cy = rect.top + rect.height / 2;

                svg.dispatchEvent(new WheelEvent('wheel', {
                    clientX: cx,
                    clientY: cy,
                    deltaY: -300, // Negative for zoom in
                    bubbles: true, cancelable: true,
                    view: window
                }));
            }
        });

        bindBtn('btn-zoom-out', () => {
            const svg = document.querySelector("#" + elementId + " svg");
            if (svg) {
                // Simulate wheel down (zoom out)
                const rect = svg.getBoundingClientRect();
                const cx = rect.left + rect.width / 2;
                const cy = rect.top + rect.height / 2;

                svg.dispatchEvent(new WheelEvent('wheel', {
                    clientX: cx,
                    clientY: cy,
                    deltaY: 300, // Positive for zoom out
                    bubbles: true, cancelable: true,
                    view: window
                }));
            }
        });

        bindBtn('btn-fit', () => {
            // Re-drawing is the cleanest "Fit" for phylotree as it resets transform
            draw();
        });

        bindBtn('btn-toggle-support', () => {
            options.showSupport = !options.showSupport;
            const btn = document.getElementById('btn-toggle-support');
            if (btn) btn.textContent = options.showSupport ? "Hide Node Support" : "Show Node Support";
            draw();
        });
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

            // Clone for export (strip Dark Reader artifacts + keep our inline styles)
            const clone = svg.cloneNode(true);

            (function removeDarkReader(el) {
                if (!el || !el.getAttribute) return;

                // Remove data-darkreader-* attributes
                const attrs = [...el.attributes];
                for (const attr of attrs) {
                    if (attr.name && attr.name.startsWith("data-darkreader")) {
                        el.removeAttribute(attr.name);
                    }
                }

                // Clean style attribute: remove --darkreader-* vars
                const style = el.getAttribute("style");
                if (style) {
                    const cleanStyle = style.replace(/--darkreader-[^;]+;?/g, "").trim();
                    if (cleanStyle) el.setAttribute("style", cleanStyle);
                    else el.removeAttribute("style");
                }

                // Recurse
                for (const child of el.children) removeDarkReader(child);
            })(clone);

            // Remove <style> blocks that mention darkreader
            clone.querySelectorAll("style").forEach(s => {
                const t = (s.textContent || "").toLowerCase();
                if (t.includes("darkreader")) s.remove();
            });
            clone.querySelectorAll("style.darkreader").forEach(s => s.remove());

            const data = (new XMLSerializer()).serializeToString(clone);
            const blob = new Blob([data], { type: "image/svg+xml;charset=utf-8" });
            const url = URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.href = url;
            link.download = `tree_${Date.now()}.svg`;
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
        });
        // --- 6. Stats & Return ---
        function computeSupportStats(tree) {
            let maxSupport = 0;
            let minSupport = Infinity;
            let supportValues = [];

            tree.traverse_and_compute((node) => {
                if (!node.children || node.children.length === 0) return;

                const support = node.data?.confidence || node.data?.bootstrap_values ||
                    node.data?.bootstrap || node.data?.support;

                if (support !== undefined && support !== null && support !== "") {
                    const val = parseFloat(support);
                    if (!isNaN(val)) {
                        supportValues.push(val);
                        maxSupport = Math.max(maxSupport, val);
                        minSupport = Math.min(minSupport, val);
                    }
                }
            });

            let supportType = 'none';
            if (supportValues.length === 0) {
                supportType = 'none';
            } else {
                // Heuristic:
                // If ANY value > 1, it's likely Bootstrap/Percent (0-100)
                // If ALL values <= 1, it's likely Posterior Probability (0-1)
                // If both ranges exist cleanly (e.g. some 0.9 and some 90), mixed?
                // But usually 0.95 is valid in both. 
                // We'll trust the checked max value.

                // Check if we have values that are clearly > 1
                const hasLargeValues = maxSupport > 1.0;

                // Check if we have values that are clearly <= 1 (but could be part of 0-100)
                // This is hard to disambiguate. 1.0 could be 100% scaled or 1.0 probability.

                // Simple robust rule:
                if (maxSupport > 1.0) {
                    supportType = 'BS';
                } else {
                    supportType = 'PP';
                }
            }

            return { maxSupport, supportType };
        }

        const stats = computeSupportStats(tree);
        container.__treeStats = stats;

    }

    window.renderPhylotree = renderPhylotree;
})();
