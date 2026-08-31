// Alan 8/28/26 - Exercise mobile wrapper/input invariants without claiming browser-rendered D3 motion.
/** Focused mobile/touch interaction regressions for the tree viewer wrapper. */
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = process.argv[2] || path.resolve(__dirname, '..', '..');

// Alan 8/28/26 - Provide only the DOM primitives needed to observe wrapper-owned state and listeners.
function classList() {
    const values = new Set();
    return {
        add(...names) { names.forEach(name => values.add(name)); },
        remove(...names) { names.forEach(name => values.delete(name)); },
        toggle(name, force) {
            if (force === true) values.add(name);
            else if (force === false) values.delete(name);
            else if (values.has(name)) values.delete(name); else values.add(name);
            return values.has(name);
        },
        contains(name) { return values.has(name); }
    };
}

// Alan 8/28/26 - Load the shipped wrapper verbatim in a small observable browser sandbox.
function loadViewer() {
    const containerListeners = new Map();
    const windowListeners = new Map();
    const menuTargets = new Set();
    const contextMenu = {
        style: { display: 'none' },
        contains(target) { return menuTargets.has(target); }
    };
    const container = {
        innerHTML: '', children: [], classList: classList(), style: {},
        addEventListener(type, fn) { containerListeners.set(type + ':' + fn, { type, fn }); },
        removeEventListener(type, fn) { containerListeners.delete(type + ':' + fn); },
        contains() { return true; },
        querySelector(selector) { return selector === '.phylotree-context-menu' ? contextMenu : null; },
        querySelectorAll() { return []; },
        getBoundingClientRect() { return { left: 0, top: 0, width: 390, height: 700 }; },
        setPointerCapture() {}, releasePointerCapture() {}, dispatchEvent() {}
    };
    const body = { classList: classList() };
    const document = {
        body,
        getElementById() { return container; },
        addEventListener(type, fn) { windowListeners.set('document:' + type + ':' + fn, { type, fn }); },
        removeEventListener(type, fn) { windowListeners.delete('document:' + type + ':' + fn); },
        querySelector() { return null; }, querySelectorAll() { return []; },
        createElement() { return { style: {}, parentNode: null }; }
    };
    const sandbox = {
        console, document, location: { search: '' }, URLSearchParams,
        Map, Set, Math, Date, JSON, Object, Array, String, Number, Boolean,
        setTimeout(fn) { fn(); return 1; }, clearTimeout() {}, requestAnimationFrame(fn) { fn(); },
        getComputedStyle() { return { position: 'relative' }; },
        MutationObserver: function () { return { observe() {}, disconnect() {} }; },
        CustomEvent: function (type, init) { this.type = type; this.detail = init?.detail; },
        addEventListener(type, fn) { windowListeners.set('window:' + type + ':' + fn, { type, fn }); },
        removeEventListener(type, fn) { windowListeners.delete('window:' + type + ':' + fn); },
        d3v7: { select() { return { datum() { return null; } }; } }
    };
    const ctx = vm.createContext(sandbox);
    ctx.window = ctx; ctx.self = ctx; ctx.globalThis = ctx;
    vm.runInContext(
        fs.readFileSync(path.join(REPO, 'app/static/js/tree_viewer_phylotree_v2.js'), 'utf8'),
        ctx, { filename: 'tree_viewer_phylotree_v2.js' }
    );
    return { Viewer: ctx.DikaryaTreeViewer, body, container, contextMenu, menuTargets, containerListeners, windowListeners };
}

// Alan 8/28/26 - Build pointer events with observable cancellation flags for input-ownership assertions.
function pointer(overrides = {}) {
    return Object.assign({
        pointerType: 'touch', pointerId: 1, button: 0, clientX: 20, clientY: 30,
        target: {}, preventDefault() { this.prevented = true; },
        stopPropagation() { this.stopped = true; }
    }, overrides);
}

// Alan 8/28/26 - Pin mode ownership and established desktop mouse behavior at the wrapper boundary.
const { Viewer, body, container, contextMenu, menuTargets, containerListeners, windowListeners } = loadViewer();
const viewer = new Viewer('tree-container', {}, {});
viewer._isBoxSelectBackgroundTarget = () => true;
let boxStarts = 0;
viewer._startBoxSelectDrag = () => { boxStarts += 1; };

const navigateTouch = pointer();
viewer._handleBoxSelectPointerDown(navigateTouch);
assert.strictEqual(boxStarts, 0, 'Navigate touch must pass through instead of box-selecting');
assert.ok(!navigateTouch.prevented, 'Navigate touch must not be prevented before D3');

viewer.setMobileInteractionMode('select');
const selectTouch = pointer({ pointerId: 2 });
viewer._handleBoxSelectPointerDown(selectTouch);
assert.strictEqual(boxStarts, 1, 'Select touch may start background box selection');
assert.ok(selectTouch.prevented && selectTouch.stopped, 'Select box gesture must own the pointer');

viewer.setMobileInteractionMode('navigate');
assert.strictEqual(viewer.mobileInteractionMode, 'navigate', 'Done returns behavior to Navigate mode');
assert.ok(!container.classList.contains('mobile-select-mode'), 'Done removes the visible Select-mode class');
const mouseLeft = pointer({ pointerType: 'mouse', pointerId: 3 });
viewer._handleBoxSelectPointerDown(mouseLeft);
assert.strictEqual(boxStarts, 2, 'desktop mouse left drag must retain box selection');
const mouseRight = pointer({ pointerType: 'mouse', pointerId: 4, button: 2 });
viewer._handleBoxSelectPointerDown(mouseRight);
assert.strictEqual(boxStarts, 2, 'desktop mouse right drag must remain available to D3 pan');
assert.ok(!mouseRight.prevented, 'right-button pan must not be prevented');

let contextMenus = 0;
viewer.tree = { display: { handle_node_click() { contextMenus += 1; } } };
viewer._getContextMenuNode = () => ({ data: { name: 'desktop-node' } });
viewer._overrideClickBehavior();
viewer._contextMenuListener({
    target: {
        classList: { contains(name) { return name === 'phylotree-node-text'; } },
        closest() { return null; }, tagName: 'text'
    },
    preventDefault() {}, stopPropagation() {}
});
assert.strictEqual(contextMenus, 1, 'desktop node context menu still invokes phylotree');

const node = { data: { name: 'tip-a' } };
viewer._getContextMenuNode = target => target.node || null;
viewer._getNodeId = value => value?.data?.name || null;
viewer._updateNodeStylesOnly = () => {};
viewer._updateStats = () => {};
viewer._syncCladeSelection = () => {};

viewer.setMobileInteractionMode('select');
viewer._handleTouchPointerDown(pointer({ pointerId: 10, target: { node } }));
viewer._handleTouchPointerEnd(pointer({ pointerId: 10, target: { node } }), false);
assert.ok(viewer.currentSelectionIds.has('tip-a'), 'stationary Select-mode tap toggles a tip');

viewer.currentSelectionIds.clear();
viewer._handleTouchPointerDown(pointer({ pointerId: 11, target: { node } }));
viewer._handleTouchPointerMove(pointer({ pointerId: 11, target: { node }, clientX: 29 }));
viewer._handleTouchPointerEnd(pointer({ pointerId: 11, target: { node }, clientX: 29 }), false);
assert.strictEqual(viewer.currentSelectionIds.size, 0, 'movement at the 9px threshold suppresses tip selection');

// Alan 8/28/26 - Multi-touch and pointer cancellation invalidate taps and always release wrapper state.
viewer._handleTouchPointerDown(pointer({ pointerId: 12, target: { node } }));
viewer._handleTouchPointerDown(pointer({ pointerId: 13, target: { node }, clientX: 80 }));
viewer._handleTouchPointerEnd(pointer({ pointerId: 13, target: { node }, clientX: 80 }), false);
viewer._handleTouchPointerEnd(pointer({ pointerId: 12, target: { node } }), false);
assert.strictEqual(viewer.currentSelectionIds.size, 0, 'multi-touch invalidates a Select-mode tap');
viewer._handleTouchPointerDown(pointer({ pointerId: 14, target: { node } }));
viewer._handleTouchPointerEnd(pointer({ pointerId: 14, target: { node } }), true);
assert.strictEqual(viewer.currentSelectionIds.size, 0, 'pointercancel cannot toggle selection');
assert.strictEqual(viewer.mobileTouchState.pointers.size, 0, 'pointercancel removes its tracked pointer');

// Alan 8/28/26 - Navigation wrapper bookkeeping must pass through and leave figure spacing untouched.
const spacingBefore = JSON.stringify(viewer.spacingState);
viewer.setMobileInteractionMode('navigate');
viewer._handleTouchPointerDown(pointer({ pointerId: 20, target: { node } }));
viewer._handleTouchPointerMove(pointer({ pointerId: 20, target: { node }, clientX: 90 }));
viewer._handleTouchPointerEnd(pointer({ pointerId: 20, target: { node }, clientX: 90 }), false);
assert.strictEqual(JSON.stringify(viewer.spacingState), spacingBefore,
    'one-finger navigation wrapper handling does not change spacingState');
assert.strictEqual(viewer.currentSelectionIds.size, 0, 'navigation movement beginning on a tip cannot select it');

viewer._handleTouchPointerDown(pointer({ pointerId: 21, target: { node } }));
viewer._handleTouchPointerDown(pointer({ pointerId: 22, target: { node }, clientX: 100 }));
viewer._handleTouchPointerMove(pointer({ pointerId: 22, target: { node }, clientX: 140 }));
viewer._handleTouchPointerEnd(pointer({ pointerId: 22, target: { node }, clientX: 140 }), false);
viewer._handleTouchPointerEnd(pointer({ pointerId: 21, target: { node } }), false);
assert.strictEqual(JSON.stringify(viewer.spacingState), spacingBefore,
    'multi-touch navigation wrapper handling does not change spacingState');

viewer.tree = null;
viewer.updateSpacing(5, 0);
assert.strictEqual(viewer.spacingState.x, 5, 'explicit spacing API still changes spacing');

// Alan 8/28/26 - Programmatic buttons must use the same camera callback as pinch without
// changing figure spacing; reciprocal button factors restore the original scale.
const cameraFactors = [];
viewer.tree = { display: { zoom_by(factor) { cameraFactors.push(factor); return true; } } };
const spacingBeforeCamera = JSON.stringify(viewer.spacingState);
assert.ok(viewer.zoomCamera(1.25), 'zoom + reaches the shared D3 camera API');
assert.ok(viewer.zoomCamera(1 / 1.25), 'zoom - reaches the shared D3 camera API');
assert.deepStrictEqual(cameraFactors, [1.25, 0.8], 'camera receives reciprocal scale factors in order');
assert.ok(Math.abs(cameraFactors.reduce((scale, factor) => scale * factor, 1) - 1) < 1e-12,
    'zoom in followed by zoom out returns to the original camera scale');
assert.strictEqual(JSON.stringify(viewer.spacingState), spacingBeforeCamera,
    'repeated camera zoom leaves spacingState byte-for-byte unchanged');

// Alan 8/28/26 - Reattachment must replace, not accumulate, every permanent touch listener.
viewer._attachTouchInteractionHandlers();
viewer._attachTouchInteractionHandlers();
for (const type of ['pointerdown', 'pointermove', 'pointerup', 'pointercancel']) {
    const expected = type === 'pointerdown' ? viewer.mobileTouchState.pointerDownListener
        : type === 'pointermove' ? viewer.mobileTouchState.pointerMoveListener
        : type === 'pointerup' ? viewer.mobileTouchState.pointerUpListener
            : viewer.mobileTouchState.pointerCancelListener;
    const registry = type === 'pointerdown' ? containerListeners : windowListeners;
    const active = Array.from(registry.values()).filter(entry => entry.type === type && entry.fn === expected);
    assert.strictEqual(active.length, 1, 'reattachment leaves one active ' + type + ' listener');
}

// Alan 8/28/26 - Mobile node-menu cleanup must hide the native menu and release its document listener.
viewer.tree = { display: { handle_node_click() { contextMenu.style.display = 'block'; } } };
viewer.mobileTouchState.lastTargetNode = node;
assert.ok(viewer.openMobileNodeActions(), 'a current touch target can open the existing node menu');
assert.ok(body.classList.contains('tree-mobile-node-menu-open'), 'mobile node-menu body state opens');
viewer.closeMobileNodeActions();
assert.strictEqual(contextMenu.style.display, 'none', 'closing mobile node actions hides the native menu');
assert.ok(!body.classList.contains('tree-mobile-node-menu-open'), 'mobile node-menu body state closes');
assert.strictEqual(viewer.mobileTouchState.menuDismissListener, null, 'mobile node-menu dismiss listener is released');
assert.strictEqual(viewer.mobileTouchState.menuActionListener, null, 'mobile node-menu action listener is released');

// Alan 8/28/26 - A touch on a native action item must bypass tree-gesture bookkeeping so
// phylotree's existing click callback executes for the most recently selected live node.
const actionTarget = {
    closest(selector) { return selector === '.phylotree-context-menu' ? contextMenu : null; }
};
menuTargets.add(actionTarget);
const actionCalls = [];
let sharedActions = null;
viewer.tree = {
    display: {
        handle_node_click(targetNode) {
            contextMenu.style.display = 'block';
            sharedActions = {
                Prune() { actionCalls.push(['Prune', targetNode, contextMenu.style.display]); },
                Copy() { actionCalls.push(['Copy', targetNode, contextMenu.style.display]); }
            };
        }
    }
};
const newerNode = { data: { name: 'tip-b' } };
viewer.mobileTouchState.lastTargetNode = newerNode;
assert.ok(viewer.openMobileNodeActions(), 'the most-recent node opens the shared action definitions');
const menuPointer = pointer({ pointerId: 30, target: actionTarget });
viewer._handleTouchPointerDown(menuPointer);
viewer._handleTouchPointerEnd(menuPointer, false);
assert.ok(!viewer.mobileTouchState.pointers.has(30), 'menu controls never enter tree touch tracking');
// Alan 8/30/26 - Users commonly tap Done before opening Node actions. The container's
// capture-phase selection guard must let that Navigate-mode compatibility click reach
// the native D3 menu callback; otherwise Prune appears to run but sends no request.
viewer.setMobileInteractionMode('navigate');
const menuClick = pointer({
    pointerId: 30,
    target: actionTarget,
    button: 0,
    sourceCapabilities: { firesTouchEvents: true }
});
viewer._clickListener(menuClick);
assert.ok(!menuClick.prevented && !menuClick.stopped,
    'Navigate-mode touch clicks on node actions must reach the native menu callback');
const dismiss = viewer.mobileTouchState.menuDismissListener;
dismiss({ target: actionTarget });
assert.strictEqual(contextMenu.style.display, 'block', 'inside pointerdown does not dismiss before action click');
sharedActions.Prune();
assert.deepStrictEqual(actionCalls[0], ['Prune', newerNode, 'block'],
    'mobile Prune invokes the shared callback for the current node before dismissal');
sharedActions.Copy();
assert.deepStrictEqual(actionCalls[1], ['Copy', newerNode, 'block'],
    'a non-mutating mobile action uses the same current-node callback boundary');

viewer.mobileTouchState.menuActionListener({ target: actionTarget });
assert.strictEqual(contextMenu.style.display, 'none', 'action click dismisses only after callback handling');
assert.strictEqual(viewer.mobileTouchState.menuDismissListener, null, 'action dismissal releases pointer listener');
assert.strictEqual(viewer.mobileTouchState.menuActionListener, null, 'action dismissal releases click listener');

viewer.mobileTouchState.lastTargetNode = null;
assert.ok(!viewer.openMobileNodeActions(), 'a stale target cleared by redraw cannot open or invoke actions');

// Alan 8/28/26 - Fit intentionally remains redraw-only; this harness does not claim visual D3 fitting.
const source = fs.readFileSync(path.join(REPO, 'app/static/js/tree_viewer_phylotree_v2.js'), 'utf8');
assert.match(source, /fitToView\(\)\s*\{[\s\S]*?this\._draw\(\);\s*\}/,
    'Fit remains the existing redraw behavior; no bounding-box fit was introduced');

console.log('PASS mobile touch interactions');
