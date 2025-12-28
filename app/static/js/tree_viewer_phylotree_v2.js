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
                    .style("display", "none");
                return;
            }

            const svg = window.d3v7.select(container).select("svg");

            // Robust selection: Select all 'g' elements, then filter by data to find internal nodes
            // (Internal nodes in D3 hierarchy have 'children' property)
            const nodeGroups = svg.selectAll("g").filter(function (d) {
                return d && d.children && d.children.length > 0;
            });

            nodeGroups.each(function (d) {
                const group = window.d3v7.select(this);
                // Support values are effectively the "name" of the internal node in Newick
                // Check various fields just in case the parser put it elsewhere
                const label = d.data?.name || d.data?.bootstrap_values || d.data?.bootstrap || d.data?.support || d.data?.confidence;

                // Debug logging to help identify where the value is
                if (window.location.search.includes("debug=true")) {
                    console.log("Internal Node Data:", d.data, "Label candidate:", label);
                }

                if (!label) return;
                const numValue = parseFloat(label);
                if (isNaN(numValue) || numValue < 0 || numValue > 100) return;

                let text = group.select("text.node-support-value");
                if (text.empty()) {
                    text = group.append("text").attr("class", "node-support-value");
                }

                text
                    .attr("x", 8).attr("y", -8)
                    .attr("text-anchor", "start")
                    .style("font-size", "12px")
                    .style("font-weight", "bold")
                    .style("fill", "#c00")
                    .style("pointer-events", "none")
                    .text(numValue > 1 ? Math.round(numValue) : numValue.toFixed(2))
                    .style("display", "block");
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
