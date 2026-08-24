/**
 * Adapter for jsPhyloSVG.
 *
 * NOT WIRED UP: job_viewer.html renders through tree_viewer_phylotree_v2.js and
 * never loads this file. Kept as scaffolding for a future renderer option; the
 * body below is a stub, not a working renderer.
 */
function renderJsPhyloSVG(treeJson, elementId, callbacks) {
    // Alan 8/23/26 - Guard the lookup and write text rather than markup; the stub also
    // no longer claims to be rendering something.
    const container = document.getElementById(elementId);
    if (!container) {
        console.error(`renderJsPhyloSVG: element #${elementId} not found.`);
        return;
    }

    // Check for library
    if (typeof Smits === 'undefined') {
        container.textContent = "jsPhyloSVG library (Smits) not found.";
        return;
    }

    // jsPhyloSVG typically takes Newick or XML:
    //   const dataObject = { newick: "..." };
    //   new Smits.PhyloCanvas(dataObject, elementId, 500, 500);
    container.textContent = "The jsPhyloSVG renderer is not available yet.";
}
