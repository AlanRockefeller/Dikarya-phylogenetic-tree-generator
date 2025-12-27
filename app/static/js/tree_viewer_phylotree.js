(function () {
  function _numPx(n, fallback) {
    const x = Number(n);
    return Number.isFinite(x) && x > 0 ? Math.floor(x) : fallback;
  }

  function _getCtor() {
    // phylotree@1 builds vary:
    // - sometimes window.phylotree is the constructor function
    // - sometimes window.phylotree.phylotree is the constructor
    console.log("_getCtor: checking window.phylotree...", window.phylotree);
    
    if (window.phylotree && typeof window.phylotree.phylotree === "function") {
      console.log("Found phylotree constructor at window.phylotree.phylotree");
      return window.phylotree.phylotree;
    }
    if (typeof window.phylotree === "function") {
      console.log("Found phylotree constructor at window.phylotree");
      return window.phylotree;
    }
    console.error("Phylotree constructor not found!");
    return null;
  }

  function renderPhylotree(newick, elementId, callbacks) {
    const container = document.getElementById(elementId);
    if (!container) return;

    container.innerHTML = "";

    // We REQUIRE d3v3 to exist for phylotree@1
    if (!window.d3v3) {
      container.textContent =
        "Phylotree viewer error: D3 v3 is not loaded (window.d3v3 missing).";
      console.error("window.d3v3 is not defined!");
      return;
    }

    console.log("D3v3 available:", window.d3v3.version);
    console.log("D3v3.svg:", window.d3v3.svg);
    console.log("D3v3.svg.line:", window.d3v3.svg && window.d3v3.svg.line);

    const Ctor = _getCtor();
    if (!Ctor) {
      container.textContent =
        "Phylotree viewer error: phylotree constructor missing (library likely failed to load).";
      console.log("window.phylotree =", window.phylotree);
      console.log("typeof window.phylotree =", typeof window.phylotree);
      return;
    }

    // Basic Newick sanity
    if (typeof newick !== "string" || !newick.trim().endsWith(";")) {
      container.textContent = "Invalid Newick.";
      return;
    }

    // Size
    container.style.width = "100%";
    if (!container.style.height) container.style.height = "600px";
    const rect = container.getBoundingClientRect();
    const w = _numPx(rect.width, 900);
    const h = _numPx(rect.height, 600);

    // Make sure d3v3 is the active d3 for phylotree runtime
    const d3_saved = window.d3;
    window.d3 = window.d3v3;

    try {
      console.log("Creating phylotree with newick length:", newick.length);
      const tree = new Ctor(newick);
      console.log("Phylotree instance created:", tree);

      // Create an SVG for d3v3 to draw into
      const svg = window.d3v3.select(container).append("svg")
        .attr("width", w)
        .attr("height", h);

      console.log("SVG created");

      // Some builds support tree.svg(svgSelection)
      if (typeof tree.svg === "function") {
        tree.svg(svg);
        console.log("Called tree.svg()");
      }

      // Typical render path
      if (typeof tree.layout === "function") {
        tree.layout();
        console.log("Called tree.layout()");
      }
      
      if (typeof tree.update === "function") {
        tree.update();
        console.log("Called tree.update()");
      }

      // Tip click hookup (only if the API exists)
      if (callbacks?.onTipClick) {
        try {
          if (typeof tree.selectionCallback === "function") {
            tree.selectionCallback((n) => callbacks.onTipClick(n));
          }
        } catch (_) {}
      }

      // Debug counts
      const counts = {
        svg: container.querySelectorAll("svg").length,
        g: container.querySelectorAll("g").length,
        path: container.querySelectorAll("path").length,
        text: container.querySelectorAll("text").length,
      };
      console.log("phylotree rendered counts:", counts);
      window.__phylotree_debug = { tree, container, w, h, counts };
    } catch (e) {
      console.error("Phylotree render failed:", e);
      container.textContent = "Phylotree render failed: " + e.message;
    } finally {
      window.d3 = d3_saved; // restore v5 for the rest of the app
    }
  }

  window.renderPhylotree = renderPhylotree;
})();
