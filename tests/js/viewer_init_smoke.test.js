/**
 * Init smoke test for the tree viewer's browser bundle.
 *
 * WHY THIS EXISTS
 * ---------------
 * On 2026-08-24 a `const` in tree_viewer_controller.js was read ~16 lines above
 * its declaration. The entire controller body lives inside one
 * `DOMContentLoaded` callback, so that temporal-dead-zone ReferenceError killed
 * the whole viewer bootstrap and /job/<id>/view hung on "loading" forever for
 * every user. `node --check` cannot see it (TDZ is a runtime error) and no
 * Python test executes this file at all.
 *
 * This harness closes that gap the only way that generalises: it actually RUNS
 * the bootstrap. It loads the viewer scripts in the same order job_viewer.html
 * loads them, fires DOMContentLoaded against a permissive stub DOM, and fails
 * if the bootstrap throws. That catches the whole family of init-time faults -
 * TDZ, typos, bad destructures, missing globals - all of which present to the
 * user as the identical "loads forever" symptom.
 *
 * WHAT IT DOES NOT DO
 * -------------------
 * The DOM is a stub, so this proves the code RUNS, not that a tree is drawn
 * correctly. A wrong SVG transform would sail straight through. Rendering
 * fidelity is what scripts/dikarya_viewer_smoke.py checks against the live
 * site, and ultimately what a human looking at the screen checks.
 *
 * Usage: node viewer_init_smoke.test.js <repo-root> [--json]
 */
'use strict';

const fs = require('fs');
const vm = require('vm');
const path = require('path');

const REPO = process.argv[2] || path.resolve(__dirname, '..', '..');
const AS_JSON = process.argv.includes('--json');

/**
 * A stand-in for any DOM node. Every property access returns another stub that
 * is callable, so `getEl('x').classList.add('y')` and `getEl('x')?.value` both
 * work without the harness having to model the real DOM. The special cases
 * below are the ones where returning a stub would change control flow or throw:
 * iteration, length, and primitive coercion.
 */
function makeStub(name) {
    const target = function stub() {};
    target.__stubName = name;
    return new Proxy(target, {
        get(t, prop) {
            if (prop === Symbol.iterator) return [][Symbol.iterator].bind([]);
            if (prop === Symbol.toPrimitive) return () => '';
            if (prop === 'length') return 0;
            if (prop === 'nodeType') return 1;
            // Array-ish reads on a NodeList stub must not return a callable.
            if (prop === 'map' || prop === 'forEach' || prop === 'filter') {
                return () => [];
            }
            if (prop === 'then') return undefined;   // never look thenable
            if (prop === 'toString') return () => '';
            if (prop === 'value' || prop === 'textContent' ||
                prop === 'innerHTML' || prop === 'id') return '';
            if (prop === 'dataset' || prop === 'style') return makeStub(prop);
            if (typeof prop === 'symbol') return undefined;
            return makeStub(String(prop));
        },
        set() { return true; },
        has() { return true; },
        apply() { return makeStub(name + '()'); },
        construct() { return makeStub('new ' + name); },
    });
}

function buildSandbox(collected) {
    const listeners = [];
    const doc = {
        readyState: 'loading',
        addEventListener(type, fn) { listeners.push([type, fn]); },
        removeEventListener() {},
        dispatchEvent() { return true; },
        getElementById() { return makeStub('#el'); },
        querySelector() { return makeStub('qs'); },
        querySelectorAll() { return []; },
        createElement() { return makeStub('created'); },
        createElementNS() { return makeStub('createdNS'); },
        createTextNode() { return makeStub('text'); },
        getElementsByClassName() { return []; },
        getElementsByTagName() { return []; },
        body: makeStub('body'),
        head: makeStub('head'),
        documentElement: makeStub('html'),
        cookie: '',
        title: '',
        hidden: false,
        activeElement: null,
    };

    const sandbox = {
        console: {
            log() {}, warn() {}, info() {}, debug() {},
            // A console.error during init is not itself a failure, but it is
            // worth surfacing when something else has already gone wrong.
            error(...a) { collected.consoleErrors.push(a.join(' ')); },
        },
        document: doc,
        setTimeout() { return 0; },
        clearTimeout() {}, setInterval() { return 0; }, clearInterval() {},
        requestAnimationFrame() { return 0; }, cancelAnimationFrame() {},
        queueMicrotask() {},
        // Never let the bootstrap reach the network; a pending promise is the
        // honest simulation of a request that has not come back yet.
        fetch() { return new Promise(() => {}); },
        XMLHttpRequest: function () { return makeStub('xhr'); },
        EventSource: function () { return makeStub('sse'); },
        AbortController: function () { this.signal = {}; this.abort = () => {}; },
        localStorage: {
            getItem() { return null; }, setItem() {},
            removeItem() {}, clear() {},
        },
        navigator: { userAgent: 'node-harness', clipboard: { writeText() { return Promise.resolve(); } } },
        location: { href: 'https://dikarya.us/job/test/view', search: '', pathname: '/job/test/view', hash: '' },
        history: { pushState() {}, replaceState() {} },
        Math, Date, JSON, Promise, Object, Array, String, Number, Boolean,
        Map, Set, WeakMap, WeakSet, RegExp, Error, TypeError, RangeError,
        Symbol, Proxy, Reflect, isNaN, parseFloat, parseInt, encodeURIComponent,
        decodeURIComponent, URLSearchParams, URL, TextEncoder, TextDecoder,
        structuredClone, btoa: (s) => Buffer.from(s).toString('base64'),
        atob: (s) => Buffer.from(s, 'base64').toString(),
        performance: { now: () => 0 },
        alert() {}, confirm() { return false; }, prompt() { return null; },
        getComputedStyle() { return makeStub('computedStyle'); },
        matchMedia() { return { matches: false, addEventListener() {}, addListener() {} }; },
        MutationObserver: function () { return { observe() {}, disconnect() {} }; },
        ResizeObserver: function () { return { observe() {}, disconnect() {} }; },
        IntersectionObserver: function () { return { observe() {}, disconnect() {} }; },
        Blob: function () {}, File: function () {}, FileReader: function () { return makeStub('fr'); },
        FormData: function () { return makeStub('fd'); },
        Image: function () { return makeStub('img'); },
        DOMParser: function () { return { parseFromString: () => makeStub('parsed') }; },
        XMLSerializer: function () { return { serializeToString: () => '' }; },
        CustomEvent: function CustomEvent(type, init) { this.type = type; this.detail = init && init.detail; },
        Event: function Event(type) { this.type = type; },
        WheelEvent: function WheelEvent(type, init) { Object.assign(this, init || {}); this.type = type; },
        MouseEvent: function MouseEvent(type, init) { Object.assign(this, init || {}); this.type = type; },
        KeyboardEvent: function KeyboardEvent(type, init) { Object.assign(this, init || {}); this.type = type; },
        // job_viewer.html injects these before the viewer scripts run.
        JOB_ID: '00000000-0000-0000-0000-000000000000',
        TREE_METHOD: 'fasttree',
        addEventListener(type, fn) { listeners.push([type, fn]); },
        removeEventListener() {},
        dispatchEvent() { return true; },
        showStatus() {},           // provided globally by base_modern.html
        innerWidth: 1280, innerHeight: 900, devicePixelRatio: 1,
        scrollTo() {},
    };

    const ctx = vm.createContext(sandbox);
    ctx.globalThis = ctx;
    ctx.window = ctx;
    ctx.self = ctx;
    return { ctx, listeners };
}

// The scripts job_viewer.html loads, in its order. Vendor bundles first,
// because the viewer code reads d3/_/phylotree at definition time.
const SCRIPTS = [
    'app/static/vendor/d3.v7.min.js',
    'app/static/vendor/lodash-4.min.js',
    '@alias:_$1',                                  // job_viewer.html does this
    'app/static/vendor/underscore-1.13.6-min.js',
    'app/static/js/phylotree.js',
    'app/static/js/tree_viewer_phylotree_v2.js',
    'app/static/js/tree_viewer_api.js',
    'app/static/js/tree_viewer_controller.js',
];

const results = [];
function record(name, ok, detail) {
    results.push({ name, ok, detail: detail || '' });
}

function main() {
    const collected = { consoleErrors: [] };
    const { ctx, listeners } = buildSandbox(collected);

    // --- Phase 1: every script must evaluate at top level.
    for (const rel of SCRIPTS) {
        if (rel === '@alias:_$1') { ctx._$1 = ctx._; continue; }
        const abs = path.join(REPO, rel);
        if (!fs.existsSync(abs)) { record('load:' + rel, false, 'file not found: ' + abs); return; }
        try {
            vm.runInContext(fs.readFileSync(abs, 'utf8'), ctx, { filename: rel });
            record('load:' + rel, true);
        } catch (e) {
            record('load:' + rel, false, `${e.name}: ${e.message}\n${(e.stack || '').split('\n').slice(0, 6).join('\n')}`);
            return;   // later scripts depend on earlier ones; stop at first break
        }
    }

    // --- Phase 2: the controller must actually register a bootstrap.
    const domReady = listeners.filter(([t]) => t === 'DOMContentLoaded');
    record('registers-DOMContentLoaded', domReady.length > 0,
        domReady.length ? '' : 'no DOMContentLoaded listener was registered; ' +
        'the controller never wired itself up');
    if (!domReady.length) return;

    // --- Phase 3: the bootstrap must run without throwing. This is the check
    // that would have caught the TDZ outage.
    const failures = [];
    process.on('unhandledRejection', (err) => {
        failures.push(err instanceof Error ? `${err.name}: ${err.message}` : String(err));
    });

    for (const [, fn] of domReady) {
        try {
            const ret = fn.call(ctx, new ctx.Event('DOMContentLoaded'));
            // The controller's bootstrap is `async`, so a synchronous throw in
            // its prologue surfaces as a rejected promise, not an exception.
            if (ret && typeof ret.catch === 'function') {
                ret.catch((err) => {
                    failures.push(err instanceof Error
                        ? `${err.name}: ${err.message}\n${(err.stack || '').split('\n').slice(0, 8).join('\n')}`
                        : String(err));
                });
            }
        } catch (e) {
            failures.push(`${e.name}: ${e.message}\n${(e.stack || '').split('\n').slice(0, 8).join('\n')}`);
        }
    }

    // Let the microtask queue drain so a rejection from the async bootstrap
    // lands before we report.
    return new Promise((resolve) => {
        setImmediate(() => {
            record('bootstrap-runs-without-throwing', failures.length === 0,
                failures.join('\n---\n'));
            resolve();
        });
    });
}

Promise.resolve(main()).then(() => {
    if (AS_JSON) {
        process.stdout.write(JSON.stringify(results, null, 2));
        process.exit(0);
    }
    const bad = results.filter(r => !r.ok);
    for (const r of bad) console.log(`FAIL ${r.name}\n${r.detail}\n`);
    if (bad.length) process.exit(1);
    console.log(`PASS ${results.length}`);
});
