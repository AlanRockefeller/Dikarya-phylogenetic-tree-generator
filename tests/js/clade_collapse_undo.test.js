/**
 * Behavioural tests for viewer-only clade collapse and for the Undo hotkey.
 *
 * WHY A NODE HARNESS
 * ------------------
 * Collapsing a clade and undoing an edit both live entirely in the browser
 * bundle, so the only way to test them against what is actually served is to
 * run the shipped files. Both harnesses here load the real scripts in the same
 * order job_viewer.html loads them.
 *
 * Two different levels are exercised:
 *
 *   1. The collapse logic, against a REAL phylotree parsed from Newick, so the
 *      node objects (parent/children/data) are the ones the viewer sees. The
 *      renderer is replaced by a stand-in, because everything it does is paint
 *      pixels; `toggleCollapse` is reimplemented there to match phylotree's own
 *      (set `collapsed`, unhide the subtree on expand) since the real method
 *      lives on a constructor the bundle does not export.
 *
 *   2. The Ctrl/Cmd+Z wiring, by booting the whole controller against a stub
 *      DOM and firing real keydown objects at the listener it registers. That
 *      is the only way to prove the hotkey does not steal undo from a text
 *      field, which is a guard no unit test of a helper would cover.
 *
 * Usage: node clade_collapse_undo.test.js <repo-root> [--json]
 */
'use strict';

const fs = require('fs');
const vm = require('vm');
const path = require('path');
const assert = require('assert');

const REPO = process.argv[2] || path.resolve(__dirname, '..', '..');
const AS_JSON = process.argv.includes('--json');
const SELF_TEST_ASYNC_FAILURE = process.argv.includes('--self-test-async-failure');

const SCRIPTS = [
    'app/static/vendor/d3.v7.min.js',
    'app/static/vendor/lodash-4.min.js',
    '@alias:_$1',
    'app/static/vendor/underscore-1.13.6-min.js',
    'app/static/js/phylotree.js',
    'app/static/js/tree_viewer_phylotree_v2.js',
    'app/static/js/tree_viewer_api.js',
    'app/static/js/tree_viewer_controller.js',
];

// ---------------------------------------------------------------------------
// Stub DOM, deliberately permissive: any property read returns another callable
// stub, so wiring code runs without the harness modelling the real page.
// ---------------------------------------------------------------------------
function makeStub(name) {
    const target = function stub() {};
    target.__stubName = name;
    return new Proxy(target, {
        get(t, prop) {
            if (prop === Symbol.iterator) return [][Symbol.iterator].bind([]);
            if (prop === Symbol.toPrimitive) return () => '';
            if (prop === 'length') return 0;
            if (prop === 'nodeType') return 1;
            if (prop === 'map' || prop === 'forEach' || prop === 'filter') return () => [];
            if (prop === 'then') return undefined;
            if (prop === 'toString') return () => '';
            if (prop === 'value' || prop === 'textContent' || prop === 'innerHTML' || prop === 'id') return '';
            if (prop === 'dataset' || prop === 'style' || prop === 'classList') return makeStub(prop);
            if (typeof prop === 'symbol') return undefined;
            return makeStub(String(prop));
        },
        set() { return true; },
        has() { return true; },
        apply() { return makeStub(name + '()'); },
        construct() { return makeStub('new ' + name); },
    });
}

function buildSandbox({ fetchImpl, exposeControllerTestHooks = false } = {}) {
    const docListeners = [];
    const winListeners = [];
    const doc = {
        readyState: 'loading',
        addEventListener(type, fn) { docListeners.push([type, fn]); },
        removeEventListener() {},
        dispatchEvent() { return true; },
        getElementById() { return makeStub('#el'); },
        querySelector() { return null; },        // "no modal is open"
        querySelectorAll() { return []; },
        createElement() { return makeStub('created'); },
        createElementNS() { return makeStub('createdNS'); },
        createTextNode() { return makeStub('text'); },
        getElementsByClassName() { return []; },
        getElementsByTagName() { return []; },
        body: makeStub('body'), head: makeStub('head'),
        documentElement: makeStub('html'),
        cookie: '', title: '', hidden: false, activeElement: null,
    };
    // The Ctrl+Z guard tests `target instanceof HTMLElement`, so the harness
    // needs a real constructor to build both kinds of event target from.
    function HTMLElement() {}
    const sandbox = {
        console: { log() {}, warn() {}, info() {}, debug() {}, error() {} },
        document: doc,
        HTMLElement,
        setTimeout, clearTimeout, setInterval, clearInterval,
        requestAnimationFrame(fn) { return setTimeout(fn, 0); },
        cancelAnimationFrame(id) { clearTimeout(id); },
        queueMicrotask,
        fetch: fetchImpl || (() => new Promise(() => {})),
        XMLHttpRequest: function () { return makeStub('xhr'); },
        EventSource: function () { return makeStub('sse'); },
        AbortController: function () { this.signal = {}; this.abort = () => {}; },
        localStorage: { getItem() { return null; }, setItem() {}, removeItem() {}, clear() {} },
        navigator: { userAgent: 'node-harness', clipboard: { writeText: () => Promise.resolve() } },
        location: { href: 'https://dikarya.us/job/test/view', search: '', pathname: '/job/test/view', hash: '' },
        history: { pushState() {}, replaceState() {} },
        Math, Date, JSON, Promise, Object, Array, String, Number, Boolean,
        Map, Set, WeakMap, WeakSet, RegExp, Error, TypeError, RangeError,
        Symbol, Proxy, Reflect, isNaN, parseFloat, parseInt,
        encodeURIComponent, decodeURIComponent, URLSearchParams, URL,
        TextEncoder, TextDecoder, structuredClone,
        btoa: (s) => Buffer.from(s).toString('base64'),
        atob: (s) => Buffer.from(s, 'base64').toString(),
        performance: { now: () => 0 },
        alert() {}, confirm() { return true; }, prompt() { return null; },
        getComputedStyle() { return makeStub('computedStyle'); },
        matchMedia() { return { matches: false, addEventListener() {}, addListener() {} }; },
        MutationObserver: function () { return { observe() {}, disconnect() {} }; },
        ResizeObserver: function () { return { observe() {}, disconnect() {} }; },
        IntersectionObserver: function () { return { observe() {}, disconnect() {} }; },
        Blob: function () {}, File: function () {},
        FileReader: function () { return makeStub('fr'); },
        FormData: function () { return makeStub('fd'); },
        Image: function () { return makeStub('img'); },
        DOMParser: function () { return { parseFromString: () => makeStub('parsed') }; },
        XMLSerializer: function () { return { serializeToString: () => '' }; },
        CustomEvent: function CustomEvent(type, init) { this.type = type; this.detail = init && init.detail; },
        Event: function Event(type) { this.type = type; },
        KeyboardEvent: function KeyboardEvent(type, init) { Object.assign(this, init || {}); this.type = type; },
        MouseEvent: function MouseEvent(type, init) { Object.assign(this, init || {}); this.type = type; },
        WheelEvent: function WheelEvent(type, init) { Object.assign(this, init || {}); this.type = type; },
        JOB_ID: '00000000-0000-0000-0000-000000000000',
        TREE_METHOD: 'fasttree',
        VIEW_ONLY: false,
        addEventListener(type, fn) { winListeners.push([type, fn]); },
        removeEventListener() {}, dispatchEvent() { return true; },
        showStatus() {},
        innerWidth: 1280, innerHeight: 900, devicePixelRatio: 1, scrollTo() {},
    };
    const ctx = vm.createContext(sandbox);
    ctx.globalThis = ctx;
    ctx.window = ctx;
    ctx.self = ctx;
    for (const rel of SCRIPTS) {
        if (rel === '@alias:_$1') { ctx._$1 = ctx._; continue; }
        let source = fs.readFileSync(path.join(REPO, rel), 'utf8');
        if (exposeControllerTestHooks && rel.endsWith('tree_viewer_controller.js')) {
            const marker = '    // START\n    loadTree();';
            assert.ok(source.includes(marker), 'controller test-hook marker drifted');
            source = source.replace(marker,
                '    window.__testScheduleSelectionSetSave = debouncedSaveSelectionSets;\n\n' + marker);
        }
        vm.runInContext(source, ctx, { filename: rel });
    }
    return { ctx, docListeners, winListeners };
}

// ---------------------------------------------------------------------------
// A viewer bound to a real parsed tree and a stand-in renderer.
// ---------------------------------------------------------------------------
const NEWICK = '(((A,B)ab,(C,D)cd)left,((E,F)ef,(G,H)gh)right)root;';

function makeViewer(ctx, newick = NEWICK) {
    const tree = new ctx.phylotree.phylotree(newick);
    const painted = { updates: 0 };
    tree.display = {
        // Mirrors phylotree's own toggleCollapse: flip the flag, and on expand
        // clear `hidden` down to the next collapsed node. Kept faithful because
        // the collapse semantics under test are exactly these.
        toggleCollapse(node) {
            if (node.collapsed) {
                node.collapsed = false;
                const unhide = (n) => {
                    if (n.children && n.children.length && !n.collapsed) n.children.forEach(unhide);
                    n.hidden = false;
                };
                unhide(node);
            } else {
                node.collapsed = true;
            }
            return this;
        },
        update() { painted.updates += 1; return this; },
        on() { return this; },
        off() { return this; },
    };

    const viewer = Object.create(ctx.DikaryaTreeViewer.prototype);
    viewer.tree = tree;
    viewer.callbacks = {};
    viewer.currentSelectionIds = new ctx.Set();
    viewer.hiddenSelectionIds = new ctx.Set();
    viewer.selectedCladeIds = new ctx.Set();
    viewer._cladeSelectionRevision = 0;
    viewer._cladeNodeCache = null;
    viewer._collapseUndo = null;
    viewer._bulkCollapseInProgress = false;
    // Painters only; replaced so the logic can run with no SVG in sight.
    viewer._addSupportLabels = () => {};
    viewer._attachEventHandlers = () => {};
    viewer._updateNodeStylesOnly = () => {};
    viewer._applyTextSizingFromZoom = () => {};
    viewer._scheduleAnnotationRedraw = () => {};
    viewer._draw = () => { throw new Error('_draw() must not be reached: display.update() exists'); };

    const byName = {};
    tree.traverse_and_compute((n) => {
        const name = n.data && n.data.name;
        if (name) byName[name] = n;
    });
    return { viewer, tree, byName, painted };
}

function tipNames(tree) {
    const names = [];
    tree.traverse_and_compute((n) => {
        if (!n.children || !n.children.length) names.push(n.data.name);
    });
    return names.sort();
}

function select(viewer, nodes) {
    nodes.forEach((n) => viewer._syncCladeSelection(n, true));
}

// ---------------------------------------------------------------------------
const results = [];
const pendingTests = [];
function test(group, name, fn) {
    const result = { group, name, ok: false, error: '' };
    results.push(result);
    try {
        const returned = fn();
        if (returned && typeof returned.then === 'function') {
            const tracked = Promise.resolve(returned).then(() => {
                result.ok = true;
            }, (e) => {
                result.error = `${e.name}: ${e.message}`;
            });
            pendingTests.push(tracked);
            return tracked;
        }
        result.ok = true;
    } catch (e) {
        result.error = `${e.name}: ${e.message}`;
    }
    return Promise.resolve();
}

function main() {
    if (SELF_TEST_ASYNC_FAILURE) {
        test('async-accounting', 'a rejected async assertion keeps its test name', async () => {
            await Promise.resolve();
            assert.fail('intentional async assertion failure');
        });
        return Promise.resolve();
    }
    const { ctx } = buildSandbox();

    // --- stable clade IDs ---------------------------------------------------
    test('stable-clade-id', 'an internal node is identified by its descendant tips', () => {
        const { viewer, byName } = makeViewer(ctx);
        const id = viewer._getCladeId(byName.ab);
        assert.match(id, /^internal:[0-9a-f]{8}$/);
        // Same tip set, different label: identity comes from membership only.
        assert.strictEqual(viewer._getCladeId(byName.ab), id);
        assert.notStrictEqual(viewer._getCladeId(byName.cd), id);
    });

    test('stable-clade-id', 'the hash agrees with the backend, byte for byte', () => {
        // These literals are asserted against Python's
        // _stable_internal_node_id_from_names() in tests/test_clade_collapse.py.
        // If the two ever disagree, a clade the viewer collapses is not the clade
        // the server would prune or reroot on.
        const { viewer, byName } = makeViewer(ctx, '((A,B)ab,C)root;');
        assert.strictEqual(viewer._getCladeId(byName.ab), 'internal:9e2f3271');
        // Order-independent, because the backend sorts before hashing.
        const reversed = makeViewer(ctx, '((B,A)ab,C)root;');
        assert.strictEqual(reversed.viewer._getCladeId(reversed.byName.ab), 'internal:9e2f3271');
    });

    test('stable-clade-id', 'a leaf has no clade ID', () => {
        const { viewer, byName } = makeViewer(ctx);
        assert.strictEqual(viewer._getCladeId(byName.A), null);
    });

    // --- single collapse / expand ------------------------------------------
    test('collapse-single', 'collapsing one clade sets only its collapsed flag', () => {
        const { viewer, tree, byName } = makeViewer(ctx);
        const before = tipNames(tree);
        select(viewer, [byName.ab, byName.cd]);   // two, so the bulk path is live
        assert.strictEqual(viewer._setCladesCollapsed([byName.ab], true, 'collapse of 1 clade'), 1);
        assert.strictEqual(byName.ab.collapsed, true);
        assert.ok(!byName.cd.collapsed);
        // Non-destructive: every tip is still in the model.
        assert.deepStrictEqual(tipNames(tree), before);
    });

    test('collapse-single', 'expanding restores the clade', () => {
        const { viewer, byName } = makeViewer(ctx);
        viewer._setCladesCollapsed([byName.ab], true, 'collapse of 1 clade');
        assert.strictEqual(viewer._setCladesCollapsed([byName.ab], false, 'expand of 1 clade'), 1);
        assert.ok(!byName.ab.collapsed);
    });

    test('collapse-single', 'collapsing an already-collapsed clade changes nothing', () => {
        const { viewer, byName } = makeViewer(ctx);
        viewer._setCladesCollapsed([byName.ab], true, 'x');
        assert.strictEqual(viewer._setCladesCollapsed([byName.ab], true, 'x'), 0);
    });

    // --- bulk ---------------------------------------------------------------
    test('collapse-bulk', 'four selected clades collapse in one action', () => {
        const { viewer, tree, byName } = makeViewer(ctx);
        select(viewer, [byName.ab, byName.cd, byName.ef, byName.gh]);
        assert.strictEqual(viewer.getSelectedCladeCount(), 4);
        assert.strictEqual(viewer.getBulkCollapseTargets().length, 4);
        assert.strictEqual(viewer.collapseSelectedClades(), 4);
        for (const n of ['ab', 'cd', 'ef', 'gh']) {
            assert.strictEqual(byName[n].collapsed, true, n + ' did not collapse');
        }
        assert.strictEqual(tipNames(tree).length, 8);
    });

    test('collapse-bulk', 'bulk expand unfolds every selected collapsed clade', () => {
        const { viewer, byName } = makeViewer(ctx);
        select(viewer, [byName.ab, byName.cd]);
        viewer.collapseSelectedClades();
        assert.strictEqual(viewer.getBulkExpandTargets().length, 2);
        assert.strictEqual(viewer.expandSelectedClades(), 2);
        assert.ok(!byName.ab.collapsed);
        assert.ok(!byName.cd.collapsed);
    });

    test('collapse-bulk', 'a bulk action reports one change, not one per clade', () => {
        const { viewer, byName } = makeViewer(ctx);
        const seen = [];
        viewer.callbacks.onCollapseChange = (c) => seen.push(c);
        select(viewer, [byName.ab, byName.cd, byName.ef]);
        viewer.collapseSelectedClades();
        assert.strictEqual(seen.length, 1);
        assert.strictEqual(seen[0].label, 'collapse of 3 clades');
        assert.strictEqual(seen[0].count, 3);
    });

    // --- overlap ------------------------------------------------------------
    test('collapse-overlap', 'a selected clade inside another collapses once, at the outer one', () => {
        const { viewer, byName } = makeViewer(ctx);
        select(viewer, [byName.left, byName.ab]);
        const targets = viewer.getBulkCollapseTargets();
        assert.strictEqual(targets.length, 1);
        assert.strictEqual(targets[0], byName.left);
        assert.strictEqual(viewer.collapseSelectedClades(), 1);
        assert.strictEqual(byName.left.collapsed, true);
        assert.ok(!byName.ab.collapsed, 'the nested clade must not be collapsed too');
    });

    test('collapse-overlap', 'normalization is deterministic regardless of selection order', () => {
        const forward = makeViewer(ctx);
        select(forward.viewer, [forward.byName.left, forward.byName.ab, forward.byName.cd]);
        const reverse = makeViewer(ctx);
        select(reverse.viewer, [reverse.byName.cd, reverse.byName.ab, reverse.byName.left]);
        assert.deepStrictEqual(
            forward.viewer.getBulkCollapseTargets().map((n) => n.data.name),
            ['left']
        );
        assert.deepStrictEqual(
            reverse.viewer.getBulkCollapseTargets().map((n) => n.data.name),
            ['left']
        );
    });

    test('collapse-overlap', 'expand does NOT normalize, so a nested collapse is still reachable', () => {
        const { viewer, byName } = makeViewer(ctx);
        // Collapse the inner clade first, then the outer one.
        viewer._setCladesCollapsed([byName.ab], true, 'x');
        viewer._setCladesCollapsed([byName.left], true, 'x');
        select(viewer, [byName.left, byName.ab]);
        const targets = viewer.getBulkExpandTargets();
        assert.strictEqual(targets.length, 2,
            'reducing expand to maximal clades would strand the inner collapse forever');
        viewer.expandSelectedClades();
        assert.ok(!byName.left.collapsed);
        assert.ok(!byName.ab.collapsed);
    });

    // --- the root -----------------------------------------------------------
    test('collapse-root', 'the root is never a bulk collapse target', () => {
        const { viewer, byName } = makeViewer(ctx);
        select(viewer, [byName.root, byName.ab, byName.cd]);
        const targets = viewer.getBulkCollapseTargets();
        assert.ok(!targets.includes(byName.root), 'bulk collapse must never blank the tree');
        assert.deepStrictEqual(targets.map((n) => n.data.name).sort(), ['ab', 'cd']);
        viewer.collapseSelectedClades();
        assert.ok(!byName.root.collapsed);
    });

    test('collapse-root', 'selecting every tip does not make the root a target', () => {
        const { viewer, tree, byName } = makeViewer(ctx);
        // Tips carry no clade ID at all, so "all tips selected" cannot be read
        // as "collapse the root".
        tree.traverse_and_compute((n) => {
            if (!n.children || !n.children.length) viewer._syncCladeSelection(n, true);
        });
        assert.strictEqual(viewer.getSelectedCladeCount(), 0);
        assert.strictEqual(viewer.getBulkCollapseTargets().length, 0);
        assert.ok(!byName.root.collapsed);
    });

    // --- deselection --------------------------------------------------------
    test('collapse-bulk', 'the resolved-target memo is invalidated by a selection change', () => {
        const { viewer, byName } = makeViewer(ctx);
        select(viewer, [byName.ab, byName.cd]);
        assert.strictEqual(viewer.getSelectedCladeCount(), 2);   // populates the memo
        select(viewer, [byName.ef]);
        assert.strictEqual(viewer.getSelectedCladeCount(), 3, 'a stale memo hid a new selection');
        viewer._syncCladeSelection(byName.ab, false);
        assert.strictEqual(viewer.getSelectedCladeCount(), 2, 'a stale memo kept a deselected clade');
    });

    test('collapse-bulk', 'deselecting a clade removes it from the target set', () => {
        const { viewer, byName } = makeViewer(ctx);
        select(viewer, [byName.ab, byName.cd]);
        viewer._syncCladeSelection(byName.cd, false);
        assert.deepStrictEqual(
            viewer.getBulkCollapseTargets().map((n) => n.data.name), ['ab']
        );
    });

    // --- undo ---------------------------------------------------------------
    test('collapse-undo', 'nothing is undoable before anything is collapsed', () => {
        const { viewer } = makeViewer(ctx);
        assert.strictEqual(viewer.hasCollapseUndo(), false);
        assert.strictEqual(viewer.undoLastCollapseChange(), null);
    });

    test('collapse-undo', 'undo restores the exact prior collapse state', () => {
        const { viewer, byName } = makeViewer(ctx);
        // cd starts collapsed, so undoing a bulk collapse has to leave it that way.
        viewer._setCladesCollapsed([byName.cd], true, 'x');
        select(viewer, [byName.ab, byName.cd, byName.ef]);
        assert.strictEqual(viewer.collapseSelectedClades(), 2);

        assert.strictEqual(viewer.getCollapseUndoLabel(), 'collapse of 2 clades');
        assert.strictEqual(viewer.undoLastCollapseChange(), 'collapse of 2 clades');
        assert.ok(!byName.ab.collapsed);
        assert.ok(!byName.ef.collapsed);
        assert.strictEqual(byName.cd.collapsed, true, 'cd was collapsed before and must stay so');
    });

    test('collapse-undo', 'undo is consumed by a single use', () => {
        const { viewer, byName } = makeViewer(ctx);
        select(viewer, [byName.ab, byName.cd]);
        viewer.collapseSelectedClades();
        assert.strictEqual(viewer.hasCollapseUndo(), true);
        viewer.undoLastCollapseChange();
        assert.strictEqual(viewer.hasCollapseUndo(), false);
        assert.strictEqual(viewer.undoLastCollapseChange(), null);
    });

    test('collapse-undo', 'a second collapse replaces the single undo slot', () => {
        const { viewer, byName } = makeViewer(ctx);
        select(viewer, [byName.ab, byName.cd]);
        viewer.collapseSelectedClades();
        viewer._setCladesCollapsed([byName.ef], true, 'collapse of 1 clade');
        assert.strictEqual(viewer.getCollapseUndoLabel(), 'collapse of 1 clade');
        viewer.undoLastCollapseChange();
        // Only the most recent action is taken back.
        assert.ok(!byName.ef.collapsed);
        assert.strictEqual(byName.ab.collapsed, true);
        assert.strictEqual(byName.cd.collapsed, true);
    });

    test('collapse-undo', 'an action that changes nothing does not claim the undo slot', () => {
        const { viewer, byName } = makeViewer(ctx);
        viewer._setCladesCollapsed([byName.ab], true, 'collapse of 1 clade');
        viewer._setCladesCollapsed([byName.ab], true, 'collapse of 1 clade');
        // Still the first, real change.
        assert.strictEqual(viewer.hasCollapseUndo(), true);
        viewer.undoLastCollapseChange();
        assert.ok(!byName.ab.collapsed);
    });

    // --- persistence --------------------------------------------------------
    test('collapse-nondestructive', 'collapse touches no persisted tree state', () => {
        const calls = [];
        const { ctx: netCtx } = buildSandbox({
            fetchImpl: (url, opts) => {
                calls.push({ url, method: (opts && opts.method) || 'GET' });
                return Promise.resolve({ ok: true, json: () => Promise.resolve({}), text: () => Promise.resolve('') });
            },
        });
        const { viewer, byName } = makeViewer(netCtx);
        select(viewer, [byName.ab, byName.cd]);
        viewer.collapseSelectedClades();
        viewer.expandSelectedClades();
        viewer.undoLastCollapseChange();
        assert.deepStrictEqual(calls, [], 'collapsing must not call any tree-edit API');
    });

    test('collapse-nondestructive', 'a view-only viewer can still collapse and undo', () => {
        const { ctx: roCtx } = buildSandbox();
        roCtx.VIEW_ONLY = true;
        const { viewer, byName } = makeViewer(roCtx);
        select(viewer, [byName.ab, byName.cd]);
        assert.strictEqual(viewer.collapseSelectedClades(), 2);
        assert.strictEqual(viewer.undoLastCollapseChange(), 'collapse of 2 clades');
    });

    return keyboardTests();
}

// ---------------------------------------------------------------------------
// Ctrl/Cmd+Z, against the real controller bootstrap.
// ---------------------------------------------------------------------------
function keyboardTests() {
    const JOB = '00000000-0000-0000-0000-000000000000';
    const posts = [];
    let persistedState = {
        renames: {}, pruned_taxa: [], root_mode: 'MIDPOINT', is_midpoint_rooted: true,
        selection_sets: { Stale: ['B'] }, active_selection_set: 'Stale',
        selection_set_colors: { Stale: '#ff0000' },
    };

    function jsonResponse(body) {
        return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve(body),
            text: () => Promise.resolve(JSON.stringify(body)),
        });
    }

    const fetchImpl = (url, opts = {}) => {
        const method = opts.method || 'GET';
        if (method !== 'GET') posts.push({ url, method, body: opts.body });
        if (url.endsWith('/tree/undo') && method === 'GET') {
            return jsonResponse({
                status: 'success', available: true, can_undo: true,
                operation: 'prune', label: 'prune of 18 sequences',
            });
        }
        if (url.endsWith('/tree/undo')) {
            persistedState = {
                renames: {}, pruned_taxa: [], root_mode: 'MIDPOINT', is_midpoint_rooted: true,
                selection_sets: { Restored: ['A'] }, active_selection_set: 'Restored',
                selection_set_colors: { Restored: '#00ff00' },
            };
            return jsonResponse({ undone: { operation: 'prune', label: 'prune of 18 sequences' } });
        }
        if (url.endsWith('/tree/selection_sets') && method === 'POST') {
            const stale = JSON.parse(opts.body);
            persistedState = {
                ...persistedState,
                selection_sets: stale.sets,
                active_selection_set: stale.active,
                selection_set_colors: stale.colors,
            };
            return jsonResponse({ status: 'ok' });
        }
        if (url.includes('/download/tree/newick')) {
            return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(NEWICK) });
        }
        if (url.includes('/tree/state')) {
            return jsonResponse(persistedState);
        }
        return jsonResponse({ status: 'ok' });
    };

    const { ctx, docListeners } = buildSandbox({
        fetchImpl,
        // The debounce helper is deliberately closure-private in production.
        // Expose that exact shipped function inside this VM so the race test
        // can arrange the state that existed immediately before Undo.
        exposeControllerTestHooks: true,
    });
    const domReady = docListeners.filter(([t]) => t === 'DOMContentLoaded').map(([, fn]) => fn);
    if (!domReady.length) {
        results.push({ group: 'keyboard', name: 'controller registers a bootstrap', ok: false,
            error: 'no DOMContentLoaded listener' });
        return Promise.resolve();
    }
    domReady.forEach((fn) => { try { fn.call(ctx, new ctx.Event('DOMContentLoaded')); } catch (e) { /* reported below */ } });

    // Let the async bootstrap (tree load + undo-state fetch) settle.
    return new Promise((resolve) => setTimeout(resolve, 60)).then(() => {
        const keydowns = docListeners.filter(([t]) => t === 'keydown').map(([, fn]) => fn);
        test('keyboard', 'the controller registers a keydown handler', () => {
            assert.ok(keydowns.length > 0);
        });
        if (!keydowns.length) return;

        const fire = (init) => {
            let prevented = false;
            const event = Object.assign(Object.create(null), {
                type: 'keydown', preventDefault() { prevented = true; }, stopPropagation() {},
                key: 'z', ctrlKey: false, metaKey: false, shiftKey: false, altKey: false,
                repeat: false, target: ctx.document.body,
            }, init);
            keydowns.forEach((fn) => fn.call(ctx, event));
            return prevented;
        };
        const undoPosts = () => posts.filter((p) => p.url.endsWith('/tree/undo') && p.method === 'POST').length;

        const bodyTarget = Object.assign(new ctx.HTMLElement(), { tagName: 'DIV', isContentEditable: false });
        const inputTarget = Object.assign(new ctx.HTMLElement(), { tagName: 'INPUT', isContentEditable: false });
        const areaTarget = Object.assign(new ctx.HTMLElement(), { tagName: 'TEXTAREA', isContentEditable: false });
        const editableTarget = Object.assign(new ctx.HTMLElement(), { tagName: 'DIV', isContentEditable: true });

        test('keyboard', 'Ctrl+Z in a text input never reaches the tree', () => {
            const before = undoPosts();
            const prevented = fire({ ctrlKey: true, target: inputTarget });
            assert.strictEqual(prevented, false, 'the browser must keep its own text undo');
            assert.strictEqual(undoPosts(), before);
        });

        test('keyboard', 'Ctrl+Z in a textarea never reaches the tree', () => {
            const before = undoPosts();
            fire({ ctrlKey: true, target: areaTarget });
            assert.strictEqual(undoPosts(), before);
        });

        test('keyboard', 'Ctrl+Z in a contenteditable never reaches the tree', () => {
            const before = undoPosts();
            fire({ ctrlKey: true, target: editableTarget });
            assert.strictEqual(undoPosts(), before);
        });

        test('keyboard', 'Shift+Ctrl+Z is left to the browser', () => {
            const before = undoPosts();
            fire({ ctrlKey: true, shiftKey: true, target: bodyTarget });
            assert.strictEqual(undoPosts(), before);
        });

        return test('keyboard', 'Ctrl+Z outside a text field undoes the last tree edit', () => {
            const before = undoPosts();
            ctx.__testScheduleSelectionSetSave();
            const prevented = fire({ ctrlKey: true, target: bodyTarget });
            assert.strictEqual(prevented, true, 'the hotkey must claim the keystroke');
            // Cross the real 800 ms debounce boundary. If performUndo stops
            // cancelling the pending timer, the stale selection-set POST now
            // fires even though clearSelections:false remains in place.
            return new Promise((r) => setTimeout(r, 850)).then(() => {
                assert.strictEqual(undoPosts(), before + 1);
                const selectionPosts = posts.filter(
                    (p) => p.url.endsWith('/tree/selection_sets') && p.method === 'POST'
                );
                assert.deepStrictEqual(selectionPosts, [],
                    'the pre-Undo debounced selection-set save was not cancelled');
            });
        }).then(() => {
            test('keyboard', 'Ctrl+Z outside a text field posted an undo', () => {
                assert.ok(undoPosts() >= 1, 'no undo request was made');
            });
            test('keyboard', 'Undo preserves restored persistent color membership', () => {
                const selectionPosts = posts.filter(
                    (p) => p.url.endsWith('/tree/selection_sets') && p.method === 'POST'
                );
                assert.deepStrictEqual(selectionPosts, [],
                    'Undo posted pre-restore selection-set state');
                assert.deepStrictEqual(persistedState.selection_sets, { Restored: ['A'] });
                assert.strictEqual(persistedState.selection_set_colors.Restored, '#00ff00');
            });
            const before = undoPosts();
            fire({ metaKey: true, target: bodyTarget });
            return new Promise((r2) => setTimeout(r2, 60)).then(() => {
                test('keyboard', 'Cmd+Z works the same as Ctrl+Z', () => {
                    assert.ok(undoPosts() > before, 'Cmd+Z did not reach the undo endpoint');
                });
            });
        });
    });
}

Promise.resolve(main()).then(() => Promise.all(pendingTests)).then(() => {
    if (AS_JSON) {
        process.stdout.write(JSON.stringify(results));
        return;
    }
    const failed = results.filter((r) => !r.ok);
    for (const r of failed) console.error(`FAIL ${r.group}/${r.name}\n    ${r.error}`);
    if (failed.length) process.exit(1);
    console.log(`PASS ${results.length}`);
}).catch((e) => {
    if (AS_JSON) {
        results.push({ group: 'harness', name: 'harness ran', ok: false, error: `${e.name}: ${e.message}` });
        process.stdout.write(JSON.stringify(results));
        return;
    }
    console.error(e);
    process.exit(1);
});
