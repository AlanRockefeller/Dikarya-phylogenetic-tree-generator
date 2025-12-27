/**
 * Phylotree v2 renderer
 * Uses D3 v7 and the modern phylotree.js v2 API
 * 
 * In v2, the phylotree constructor just parses the tree data.
 * Rendering is done via separate render functions from the library.
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
      console.error('D3 v7 not found');
      return;
    }

    // Check for phylotree
    const phylotreeLib = window.phylotree;
    if (!phylotreeLib) {
      container.innerHTML = '<div class="alert alert-danger">Phylotree v2 library not loaded</div>';
      console.error('Phylotree not found');
      return;
    }

    console.log('Phylotree module:', phylotreeLib);
    console.log('Available exports:', Object.keys(phylotreeLib));

    // Validate newick
    if (typeof newick !== 'string' || !newick.trim().endsWith(';')) {
      container.innerHTML = '<div class="alert alert-danger">Invalid Newick format</div>';
      return;
    }

    console.log('Newick string length:', newick.length);
    console.log('Newick preview (first 200 chars):', newick.substring(0, 200));
    console.log('Newick preview (last 50 chars):', newick.substring(newick.length - 50));

    // Get dimensions
    container.style.width = '100%';
    if (!container.style.height) container.style.height = '600px';
    
    const rect = container.getBoundingClientRect();
    const width = rect.width || 900;
    const height = rect.height || 600;

    console.log('Rendering phylotree v2 with dimensions:', width, 'x', height);

    try {
      // In phylotree v2, we need to use the 'render' export, not the phylotree class
      // The phylotree class is just for parsing
      const PhylotreeClass = phylotreeLib.phylotree;
      const renderFunction = phylotreeLib.render;
      
      if (!PhylotreeClass) {
        console.error('phylotree.phylotree not found');
        container.innerHTML = '<div class="alert alert-danger">Phylotree parser not found</div>';
        return;
      }

      console.log('Creating phylotree instance...');
      console.log('Render function available:', typeof renderFunction);
      
      // Parse the tree
      const tree = new PhylotreeClass(newick);
      
      console.log('Phylotree instance created');

	    // Check if tree has a render method
      if (typeof tree.render === 'function') {
        console.log('Tree has render method - using it');

        // FIX: Phylotree v2 often works best when you pass the DOM node directly
        // or ensure the ID selector is perfect.
        const renderOptions = {
          container: `#${elementId}`,
          width: width,
          height: height,
          'draw-size-bubbles': false,
          'zoom': true,             // Enable zoom to help if tree is off-screen
          'is-radial': false
        };

        console.log('Calling tree.render with options:', renderOptions);

        // CRITICAL: Ensure global d3 is d3v7 for the library's internal calls
        const d3_saved = window.d3;
        window.d3 = window.d3v7;

        try {
          // Attempt 1: Standard Render
          const result = tree.render(renderOptions);

          // Debug the return value
          console.log('Tree.render() returned:', result);

          // FIX: If render() returns an SVG/Element but didn't append it, we do it manually
          if (container.children.length === 0 && result) {
             console.log('Container empty, attempting to append render result...');
             if (result.node) {
                 container.appendChild(result.node()); // If it's a D3 selection
             } else if (result instanceof Element) {
                 container.appendChild(result); // If it's a raw DOM element
             }
          }

          // Attempt 2: If still empty, try "display" property (common in some v2 builds)
          if (container.children.length === 0 && tree.display) {
             console.log('Attempting tree.display.render()...');
             tree.display.render(renderOptions);
          }

          // Attempt 3: Force an update (sometimes required to draw the initial SVG)
          if (container.children.length > 0 && typeof tree.update === 'function') {
             tree.update();
          }

        } catch (err) {
          console.error('tree.render() threw error:', err);
        } finally {
          window.d3 = d3_saved;
        }
      

	      try {
          // FIX 1: Pass the actual DOM element, not the ID string.
          // This prevents D3 selection errors if the ID string isn't perfectly found.
          const renderOptions = {
            container: container, // Pass the DOM node directly
            width: width,
            height: height,
            'draw-size-bubbles': false,
            'zoom': true,
            'is-radial': false,
            'left-right-spacing': 'fit-to-size',
            'top-bottom-spacing': 'fit-to-size'
          };

          console.log('Calling tree.render with options:', renderOptions);

          // CRITICAL: Ensure global d3 is d3v7 for the library's internal calls
          const d3_saved = window.d3;
          window.d3 = window.d3v7;

          let renderer = null;

          try {
            // Attempt Render
            renderer = tree.render(renderOptions);
            console.log('Tree.render() returned renderer instance:', renderer);

            // Check if we need to manually trigger an update on the RENDERER (not the tree)
            if (container.children.length === 0) {
                console.log('Container empty. Attempting manual renderer update...');

                if (renderer && typeof renderer.update === 'function') {
                    // This forces the renderer to draw the SVG
                    renderer.update();
                    console.log('Manual renderer.update() called');
                } else if (renderer && typeof renderer.show === 'function') {
                    renderer.show();
                    console.log('Manual renderer.show() called');
                }
            }

            // Debug: Check if SVG exists inside the renderer object but wasn't attached
            if (container.children.length === 0 && renderer && renderer.svg) {
                console.log('Found detached SVG in renderer, appending manually...');
                if (renderer.svg.node) {
                     container.appendChild(renderer.svg.node());
                } else {
                     container.appendChild(renderer.svg);
                }
            }

          } catch (innerErr) {
            console.error('Inner render error:', innerErr);
          } finally {
            // Restore previous d3
            window.d3 = d3_saved;
          }

          // Final verification
          setTimeout(() => {
            const svgs = container.querySelectorAll('svg');
            console.log(`Final Check: Found ${svgs.length} SVGs in container.`);
            if (svgs.length > 0) {
                 // Force visibility just in case
                 svgs[0].style.width = '100%';
                 svgs[0].style.height = '100%';
            }
          }, 100);

        } catch (err) {
          console.error('tree.render() threw error:', err);
          console.error('Error stack:', err.stack);
          container.innerHTML = `<div class="alert alert-danger">Render error: ${err.message}</div>`;
        }
        
        
        // Wait a moment for render to complete, then check what was created
        setTimeout(() => {
          const svgs = container.querySelectorAll('svg');
          const paths = container.querySelectorAll('path');
          const circles = container.querySelectorAll('circle');
          const texts = container.querySelectorAll('text');
          
          console.log('After render - SVGs:', svgs.length);
          console.log('After render - Paths:', paths.length);
          console.log('After render - Circles:', circles.length);
          console.log('After render - Text elements:', texts.length);
          console.log('Container innerHTML length:', container.innerHTML.length);
          
          if (container.innerHTML.length < 100) {
            console.error('Container appears empty after render!');
            console.log('Container HTML:', container.innerHTML);
          }
          
          // Force visibility with inline styles
          if (svgs.length > 0) {
            svgs[0].style.display = 'block';
            svgs[0].style.visibility = 'visible';
            svgs[0].style.opacity = '1';
            console.log('SVG dimensions:', {
              width: svgs[0].getAttribute('width'),
              height: svgs[0].getAttribute('height'),
              actualWidth: svgs[0].getBoundingClientRect().width,
              actualHeight: svgs[0].getBoundingClientRect().height
            });
          }
        }, 200);
        
      } else {
        console.log('No tree.render method found');
        
        // Create SVG manually
        const svg = window.d3v7.select(container)
          .append('svg')
          .attr('width', width)
          .attr('height', height);

        console.log('SVG created');
        // Fallback: manually render using D3 based on the tree data
        console.log('No render function found, using manual D3 rendering');
        
        // Get nodes and links from tree
        const nodes = tree.nodes || [];
        const links = tree.links || [];
        
        console.log('Nodes:', nodes.length, 'Links:', links.length);
        
        if (nodes.length === 0) {
          container.innerHTML = '<div class="alert alert-warning">Tree has no nodes to display</div>';
          return;
        }
        
        // Simple tree layout with D3
        const treeLayout = window.d3v7.tree()
          .size([height - 40, width - 200]);
        
        // Create hierarchy from tree data
        const root = window.d3v7.hierarchy(tree, d => d.children);
        treeLayout(root);
        
        const g = svg.append('g')
          .attr('transform', 'translate(40, 20)');
        
        // Draw links
        g.selectAll('.link')
          .data(root.links())
          .join('path')
          .attr('class', 'link')
          .attr('d', window.d3v7.linkHorizontal()
            .x(d => d.y)
            .y(d => d.x))
          .style('fill', 'none')
          .style('stroke', '#999')
          .style('stroke-width', '2px');
        
        // Draw nodes
        const node = g.selectAll('.node')
          .data(root.descendants())
          .join('g')
          .attr('class', 'node')
          .attr('transform', d => `translate(${d.y},${d.x})`);
        
        node.append('circle')
          .attr('r', 5)
          .style('fill', '#69b3a2')
          .style('stroke', '#333')
          .style('stroke-width', '2px')
          .style('cursor', 'pointer')
          .on('click', (event, d) => {
            if (callbacks && callbacks.onTipClick) {
              const nodeName = d.data.name || d.data.data?.name;
              if (nodeName) {
                callbacks.onTipClick({
                  name: nodeName,
                  display_name: nodeName
                });
              }
            }
          });
        
        node.append('text')
          .attr('dx', d => d.children ? -8 : 8)
          .attr('dy', 3)
          .style('text-anchor', d => d.children ? 'end' : 'start')
          .style('font-size', '11px')
          .text(d => d.data.name || '');
      }

      console.log('Tree rendering complete');

      // Store reference for debugging
      window.__phylotree_debug = {
        tree: tree,
        container: container
      };

    } catch (error) {
      console.error('Phylotree v2 render error:', error);
      console.error('Error stack:', error.stack);
      container.innerHTML = `<div class="alert alert-danger">
        Error rendering tree: ${error.message}
        <br><small>Check console for details</small>
      </div>`;
    }
  }

  // Export globally
  window.renderPhylotree = renderPhylotree;
})();
