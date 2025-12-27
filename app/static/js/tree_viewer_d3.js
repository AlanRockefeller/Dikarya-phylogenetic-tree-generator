/**
 * Simple D3 Tree Viewer.
 * Assumes d3 v7 is loaded (or we use a CDN in the template).
 * For this mock, we'll write a basic implementation.
 */

function renderD3Tree(treeJson, elementId, callbacks) {
    const d3 = window.d3v5 || window.d3;
    const container = document.getElementById(elementId);
    const width = container.offsetWidth || 800;
    const height = 600;

    // Check if D3 is loaded
    if (typeof d3 === 'undefined') {
        container.innerHTML = "Error: D3 library not found.";
        return;
    }

    // Convert our JSON structure to D3 hierarchy
    // Our JSON: { name, children: [] }
    // D3 needs hierarchy(data)

    // Helper to clean/prepare data if needed
    const root = d3.hierarchy(treeJson.tree_structure || treeJson);

    const treeLayout = d3.tree().size([height - 40, width - 160]);
    treeLayout(root);

    const svg = d3.select(`#${elementId}`).append("svg")
        .attr("width", width)
        .attr("height", height)
        .append("g")
        .attr("transform", "translate(80,20)");

    // Links
    svg.selectAll(".link")
        .data(root.links())
        .enter().append("path")
        .attr("class", "link")
        .attr("d", d3.linkHorizontal()
            .x(d => d.y)
            .y(d => d.x));

    // Nodes
    const node = svg.selectAll(".node")
        .data(root.descendants())
        .enter().append("g")
        .attr("class", d => "node" + (d.children ? " node--internal" : " node--leaf"))
        .attr("transform", d => `translate(${d.y},${d.x})`);

    node.append("circle")
        .attr("r", 4)
        .on("click", (event, d) => {
            // Highlight
            d3.selectAll(".node").classed("selected", false);
            d3.select(event.currentTarget.parentNode).classed("selected", true);

            if (callbacks.onTipClick) {
                callbacks.onTipClick(d.data);
            }
        });

    node.append("text")
        .attr("dy", 3)
        .attr("x", d => d.children ? -8 : 8)
        .style("text-anchor", d => d.children ? "end" : "start")
        .text(d => d.data.display_name || d.data.name)
        .on("click", (event, d) => {
            d3.selectAll(".node").classed("selected", false);
            d3.select(event.currentTarget.parentNode).classed("selected", true);
            if (callbacks.onTipClick) {
                callbacks.onTipClick(d.data);
            }
        });
}
