/**
 * Phylotree v2 renderer - CLEAN VERSION
 * Fixed: Click interactions, rectangular layout, and render reliability.
 */
(function () {
  'use strict';

  function renderPhylotree(newick, elementId, callbacks) {
    const container = document.getElementById(elementId);
    if (!container) {
      console.error('Container not found:', elementId);
      return;
    }

    container.innerHTML = '';

    // Check for D3 v7
    if (!window.d3v7) {
      container.innerHTML = '<div class="alert alert-danger">D3 v7 is required for phylotree v2</div>';
      console.error('D3 v7 not found (window.d3v7 is undefined)');
      return;
    }

    // Check for phylotree
    const phylotreeLib = window.phylotree;
    if (!phylotreeLib) {
      container.innerHTML = '<div class="alert alert-danger">Phylotree v2 library not loaded</div>';
      return;
    }

    // Validate newick
    if (typeof newick !== 'string' || !newick.trim().endsWith(';')) {
      container.innerHTML = '<div class="alert alert-danger">Invalid Newick format</div>';
      return;
    }

    // Get dimensions
    container.style.width = '100%';
    if (!container.style.height) container.style.height = '600px';

    const rect = container.getBoundingClientRect();
    const width = rect.width || 900;
    const height = rect.height || 600;

    console.log('Rendering phylotree v2 with dimensions:', width, 'x', height);

    try {
      const PhylotreeClass = phylotreeLib.phylotree;

      // Parse the tree
      const tree = new PhylotreeClass(newick);
      
      // ---------------------------------------------------------
      // RENDER CONFIGURATION
      // ---------------------------------------------------------
      const renderOptions = {
        container: "#" + elementId,   // Must be string ID for clicks to work
        width: width,
        height: height,
        'draw-size-bubbles': false,
        'zoom': true,
        'is-radial': false,           // False = Rectangular layout
        'align-tips': true,           // True = Aligns names to the right (cleaner look)
        'left-right-spacing': 'fit-to-size',
        'top-bottom-spacing': 'fit-to-size',
        'node-styler': function (element, data) {
             // Optional: Add custom styling here if needed
        }
      };

      console.log('Calling tree.render with options:', renderOptions);

      // CRITICAL: Switch global d3 to v7 for the library
      const d3_saved = window.d3;
      window.d3 = window.d3v7;

      let renderer = null;

      try {
        // Attempt Render
        renderer = tree.render(renderOptions);
        
        // MANUALLY FORCE SVG INTO DOM
        // (Fixes the blank screen issue)
        if (container.children.length === 0) {
            console.log('Container empty. Attempting manual renderer update...');
            
            if (renderer) {
                if (typeof renderer.update === 'function') renderer.update();
                
                if (renderer.svg) {
                    const svgNode = renderer.svg.node ? renderer.svg.node() : renderer.svg;
                    if (svgNode) {
                        container.appendChild(svgNode);
                        console.log('Manually appended SVG from renderer');
                    }
                }
            }
        }

        // Re-attach callbacks if provided (e.g. clicking a node)
        if (renderer && callbacks && callbacks.onTipClick) {
            // Note: v2 handles clicks internally via 'selection', 
            // but we can hook into d3 events here if needed.
             window.d3v7.select(container).selectAll(".node").on("click", function(event, d) {
                 const nodeName = d.data.name;
                 if (nodeName) {
                     callbacks.onTipClick({ name: nodeName, display_name: nodeName });
                 }
             });
        }

      } catch (innerErr) {
        console.error('Inner render error:', innerErr);
        container.innerHTML = `<div class="alert alert-danger">Render Error: ${innerErr.message}</div>`;
      } finally {
        // Restore d3
        window.d3 = d3_saved;
      }

    } catch (error) {
      console.error('Phylotree v2 fatal error:', error);
      container.innerHTML = `<div class="alert alert-danger">Fatal Error: ${error.message}</div>`;
    }
  }

  // Export globally
  window.renderPhylotree = renderPhylotree;
})();
