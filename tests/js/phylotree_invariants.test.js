/**
 * Structural-invariant regression tests for the Dikarya copy of phylotree.js.
 *
 * These cover the six defects Codex reported on PR #5 (a synthetic PR that
 * existed only to expose the vendored-but-locally-modified bundle to review),
 * plus the neighbouring bugs that audit turned up.
 *
 * The bundle is executed VERBATIM in a vm context, loaded in the same order
 * job_viewer.html loads it (lodash, then `window._$1 = window._`, then
 * underscore, then phylotree), so these tests cannot pass against a file that
 * would fail in the browser. The single deviation is one appended line that
 * re-exports the module-private TreeRender constructor, because the selection
 * code under test is a method on its prototype while the constructor itself
 * needs a real DOM. Nothing else about the source is altered.
 *
 * Usage: node phylotree_invariants.test.js <repo-root> [--json]
 * Prints "PASS <n>" on success and exits non-zero with a diagnostic on
 * failure; with --json it always exits 0 and prints one record per test so a
 * pytest driver can report each finding separately.
 */
'use strict';

const fs = require('fs');
const vm = require('vm');
const path = require('path');
const assert = require('assert');

const REPO = process.argv[2] || path.resolve(__dirname, '..', '..');
const TEST_EXPORT = '  exports.__TreeRender = TreeRender;\n';

function loadBundle() {
    // Just enough of a browser for the module-level code and the event
    // plumbing; anything that needs real layout is stubbed per-test instead.
    const listeners = [];
    const sandbox = {
        console, setTimeout, clearTimeout, Math, Date, JSON,
        CustomEvent: function CustomEvent(type, init) {
            this.type = type;
            this.detail = init && init.detail;
        },
        document: {
            addEventListener(type, fn) { listeners.push([type, fn]); },
            removeEventListener() {},
            dispatchEvent() { return true; },
            createElement() { return null; },
            querySelector() { return null; },
            querySelectorAll() { return []; }
        }
    };
    const ctx = vm.createContext(sandbox);
    ctx.globalThis = ctx;
    ctx.window = ctx;
    ctx.self = ctx;

    const read = f => fs.readFileSync(path.join(REPO, f), 'utf8');
    const run = (src, name) => vm.runInContext(src, ctx, { filename: name });

    run(read('app/static/vendor/lodash-4.min.js'), 'lodash');
    ctx._$1 = ctx._;                       // job_viewer.html does exactly this
    run(read('app/static/vendor/underscore-1.13.6-min.js'), 'underscore');

    const src = read('app/static/js/phylotree.js');
    const anchor = '  exports.centerOfTree = centerOfTree;';
    assert.ok(src.includes(anchor), 'export block not found; harness needs updating');
    run(src.replace(anchor, TEST_EXPORT + anchor), 'phylotree.js');

    return ctx.phylotree;
}

const P = loadBundle();
const Phylotree = P.phylotree;
const TreeRender = P.__TreeRender;

/**
 * Loads tree_viewer_phylotree_v2.js into its own vm context with just enough of
 * a browser for the module body, and hands it the real phylotree bundle. Nothing
 * in the file under test is altered.
 */
function loadViewer() {
    const container = {
        innerHTML: '',
        textContent: '',
        appendChild() {},
        addEventListener() {},
        querySelector() { return null; },
        querySelectorAll() { return []; },
        getBoundingClientRect() { return { width: 800, height: 600, top: 0, left: 0 }; }
    };
    const sandbox = {
        console, setTimeout, clearTimeout, Math, Date, JSON, URLSearchParams,
        addEventListener() {}, removeEventListener() {},
        location: { search: '' },
        navigator: { userAgent: 'node' },
        document: {
            addEventListener() {}, removeEventListener() {},
            getElementById() { return container; },
            createElement() { return null; },
            querySelector() { return null; },
            querySelectorAll() { return []; }
        }
    };
    const ctx = vm.createContext(sandbox);
    ctx.globalThis = ctx;
    ctx.window = ctx;
    ctx.self = ctx;
    // The viewer reaches for these two globals before it builds anything.
    ctx.phylotree = P;
    ctx.d3v7 = {};

    vm.runInContext(
        fs.readFileSync(path.join(REPO, 'app/static/js/tree_viewer_phylotree_v2.js'), 'utf8'),
        ctx,
        { filename: 'tree_viewer_phylotree_v2.js' }
    );

    return { ctx, container };
}

const AS_JSON = process.argv.includes('--json');
const results = [];
let group = 'general';

function section(name) {
    group = name;
}

// Tests are registered here and executed by run() below, so a case may return a
// promise (the viewer's render() is async) without the sync ones changing shape.
const pending = [];

function test(name, fn) {
    pending.push({ group, name, fn });
}

async function run() {
    for (const t of pending) {
        try {
            await t.fn();
            results.push({ group: t.group, name: t.name, ok: true, error: null });
        } catch (e) {
            const detail = e && e.stack ? e.stack.split('\n').slice(0, 4).join('\n    ') : String(e);
            results.push({ group: t.group, name: t.name, ok: false, error: detail });
        }
    }
    report();
}

/** assert.rejects, but returning the error so a case can inspect the message. */
async function expectRejection(promise, message) {
    let err = null;
    try {
        await promise;
    } catch (e) {
        err = e;
    }
    assert.ok(err, message || 'expected a rejection, got none');
    return err;
}

// ---------------------------------------------------------------- invariants

/** deepStrictEqual, but tolerant of arrays that came out of the vm realm. */
function assertSameList(actual, expected, message) {
    assert.deepStrictEqual(Array.from(actual), Array.from(expected), message);
}

function nodeName(n) {
    return (n && n.data && n.data.name) || (n && n.name) || '<unnamed>';
}

/** Every child points back at its parent, and only the root has no parent. */
function assertParentInvariant(tree, label) {
    const root = tree.getNodes();
    assert.ok(root, label + ': tree has no root');
    assert.ok(!root.parent, label + ': root still carries a parent pointer');

    const seen = new Set([root]);
    (function walk(n) {
        (n.children || []).forEach(c => {
            assert.strictEqual(c.parent, n,
                label + ': ' + nodeName(c) + ' does not point back at ' + nodeName(n));
            assert.ok(!seen.has(c), label + ': cycle or shared node at ' + nodeName(c));
            seen.add(c);
            walk(c);
        });
    })(root);

    // Following parent pointers from anywhere reaches the one root.
    seen.forEach(n => {
        let hops = 0;
        let cur = n;
        while (cur.parent) {
            cur = cur.parent;
            assert.ok(++hops <= seen.size,
                label + ': parent chain from ' + nodeName(n) + ' does not terminate');
        }
        assert.strictEqual(cur, root, label + ': ' + nodeName(n) + ' reaches a different root');
    });

    return seen;
}

/** Every cached link joins two nodes of the CURRENT tree. */
function assertLinkInvariant(tree, label) {
    const live = assertParentInvariant(tree, label);
    assert.ok(Array.isArray(tree.links), label + ': links is not an array');
    assert.strictEqual(tree.links.length, live.size - 1,
        label + ': expected ' + (live.size - 1) + ' links, found ' + tree.links.length);
    tree.links.forEach(l => {
        assert.ok(live.has(l.source), label + ': link from a node outside the current tree');
        assert.ok(live.has(l.target), label + ': link to a node outside the current tree');
        assert.strictEqual(l.target.parent, l.source, label + ': link disagrees with parent pointers');
    });
}

/**
 * Sorted tip names, copied out of the vm realm: arrays built inside the
 * sandbox have a different Array.prototype, which deepStrictEqual rejects.
 */
function tipNames(tree) {
    return Array.from(tree.getTips()).map(n => n.data.name).sort();
}

/** Sum of branch lengths on the path between two tips. */
function tipDistance(tree, a, b) {
    const up = n => {
        const chain = [];
        while (n) { chain.push(n); n = n.parent; }
        return chain;
    };
    const pa = up(tree.getNodeByName(a));
    const pb = up(tree.getNodeByName(b));
    const setb = new Set(pb);
    const lca = pa.find(n => setb.has(n));
    const bl = n => {
        const v = parseFloat(tree.branch_length_accessor(n));
        return isNaN(v) ? 0 : v;
    };
    let d = 0;
    for (const n of pa) { if (n === lca) break; d += bl(n); }
    for (const n of pb) { if (n === lca) break; d += bl(n); }
    return d;
}

/**
 * A deliberately strict Newick reader: it accepts only well-formed strings, so
 * an empty group, a doubled comma or a dangling separator is a hard failure
 * rather than something a lenient parser quietly repairs.
 */
function parseNewickStrict(s) {
    assert.ok(/;\s*$/.test(s), 'newick does not end in a semicolon: ' + s);
    let i = 0;
    const body = s.slice(0, s.lastIndexOf(';'));

    function label() {
        if (body[i] === "'") {
            i++;
            let out = '';
            while (i < body.length) {
                if (body[i] === "'" && body[i + 1] === "'") { out += "'"; i += 2; continue; }
                if (body[i] === "'") { i++; break; }
                out += body[i++];
            }
            return out;
        }
        // '{' ends an unquoted label: a {Tag} comment is not part of the name.
        let out = '';
        while (i < body.length && !'(),:;{'.includes(body[i])) out += body[i++];
        return out;
    }

    function node() {
        const n = { children: [], name: '', length: null };
        if (body[i] === '(') {
            i++;
            for (;;) {
                assert.notStrictEqual(body[i], ',',
                    'empty child before a comma at offset ' + i + ' in ' + s);
                assert.notStrictEqual(body[i], ')',
                    'empty group or trailing comma at offset ' + i + ' in ' + s);
                n.children.push(node());
                if (body[i] === ',') { i++; continue; }
                break;
            }
            assert.strictEqual(body[i], ')', 'unclosed group at offset ' + i + ' in ' + s);
            i++;
        }
        n.name = label();
        if (body[i] === '{') { while (i < body.length && body[i] !== '}') i++; i++; }
        if (body[i] === ':') { i++; n.length = parseFloat(label()); }
        return n;
    }

    const root = node();
    assert.strictEqual(i, body.length, 'trailing junk at offset ' + i + ' in ' + s);
    return root;
}

function newickTips(s) {
    const out = [];
    (function walk(n) {
        if (!n.children.length) { out.push(n.name); return; }
        n.children.forEach(walk);
    })(parseNewickStrict(s));
    return out.sort();
}

// ===========================================================================
// Finding 1 - deleting a direct child of a bifurcating root
// ===========================================================================

section('delete-below-root');

test('deleting a leaf below a bifurcating root promotes the surviving child', () => {
    const tree = new Phylotree('((A:0.1,B:0.2)I:0.05,C:0.3);');
    const oldRoot = tree.getNodes();

    tree.deleteANode(tree.getNodeByName('C'));

    assertSameList(tipNames(tree), ['A', 'B']);
    assert.notStrictEqual(tree.getNodes(), oldRoot, 'the emptied root was not replaced');
    assert.strictEqual(tree.getNodes().data.name, 'root');
    assert.strictEqual(tree.getNodes().depth, 0, 'promoted root kept a stale depth');
    assert.strictEqual(tree.getNodes().data.attribute, undefined,
        'the promoted root kept the branch length it no longer has');
    assertLinkInvariant(tree, 'after deleting below a bifurcating root');
    newickTips(tree.getNewick());
});

test('the deleted node and old root are gone from the model entirely', () => {
    const tree = new Phylotree('((A:0.1,B:0.2)I:0.05,C:0.3);');
    const oldRoot = tree.getNodes();
    const deleted = tree.getNodeByName('C');

    tree.deleteANode(deleted);

    const live = new Set(tree.getNodes().descendants());
    assert.ok(!live.has(deleted), 'deleted node is still reachable');
    assert.ok(!live.has(oldRoot), 'the former root is still reachable');
    tree.links.forEach(l => {
        assert.ok(l.source !== oldRoot && l.target !== oldRoot,
            'a link still references the old root');
        assert.ok(l.target !== deleted, 'a link still references the deleted node');
    });
});

test('deleting a leaf under a non-root parent still collapses correctly', () => {
    const tree = new Phylotree('(((A:0.1,B:0.2)I:0.05,D:0.4)J:0.6,C:0.3);');
    tree.deleteANode(tree.getNodeByName('D'));
    assertSameList(tipNames(tree), ['A', 'B', 'C']);
    assertLinkInvariant(tree, 'after deleting under a non-root parent');
});

test('deleting an internal node lifts its children to the grandparent', () => {
    const tree = new Phylotree('((A:0.1,B:0.2)I:0.05,(C:0.3,D:0.4)J:0.6,E:0.7);');
    tree.deleteANode(tree.getNodeByName('I'));
    assertSameList(tipNames(tree), ['A', 'B', 'C', 'D', 'E']);
    assert.strictEqual(tree.getNodeByName('A').parent, tree.getNodes(),
        'lifted child was not reparented onto the root');
    assertLinkInvariant(tree, 'after deleting an internal node');
});

test('deleting the root itself is refused', () => {
    const tree = new Phylotree('((A:0.1,B:0.2)I:0.05,C:0.3);');
    tree.deleteANode(tree.getNodes());
    assertSameList(tipNames(tree), ['A', 'B', 'C'], 'the root was deletable');
    assertLinkInvariant(tree, 'after refusing to delete the root');
});

// ===========================================================================
// Finding 2 - addChild() must maintain the parent/child invariant
// ===========================================================================

section('add-child-parent');

test('addChild() assigns the child parent', () => {
    const tree = new Phylotree('(A:1,B:1);');
    const root = tree.getNodes();
    const child = tree.createNode('C', [null, [0.5]]);

    tree.addChild(root, child);

    assert.strictEqual(child.parent, root, 'addChild left the child looking like a root');
    assert.ok(root.children.includes(child), 'addChild did not attach the child');
});

test('addChild() works when the parent has no children yet', () => {
    const tree = new Phylotree('(A:1,B:1);');
    const parent = tree.createNode('P', [null, [1]]);
    const child = tree.createNode('C', [null, [1]]);

    tree.addChild(parent, child);

    assertSameList(parent.children, [child]);
    assert.strictEqual(child.parent, parent);
});

test('neighborJoining() builds a tree that can be traversed upward', () => {
    const tree = P.neighborJoining([[0, 0.4], [0.4, 0]], 2, ['a', 'b']);
    const root = tree.getNodes();

    assertSameList(root.children.map(c => c.data.name), ['a', 'b']);
    root.children.forEach(c => {
        assert.strictEqual(c.parent, root, c.data.name + ' was left parentless');
    });

    // pathToRoot walks `parent` until it is falsy; createNode's empty-string
    // default used to stop it dead at the starting node.
    const path2 = Phylotree.prototype.pathToRoot(root.children[0]);
    assert.strictEqual(path2.length, 2, 'pathToRoot stopped before reaching the root');
    assert.strictEqual(path2[path2.length - 1], root);
});

// ===========================================================================
// Finding 3 - Newick must never emit a separator for a child it skips
// ===========================================================================

section('newick-hidden-children');

function hiddenCase(newick, hide, expectTips) {
    const tree = new Phylotree(newick);
    hide.forEach(n => { tree.getNodeByName(n).notshown = true; });
    const out = tree.getNewick();
    assertSameList(newickTips(out), expectTips.slice().sort(),
        'wrong visible tips in ' + out);
    return out;
}

test('hidden first child does not leave a leading comma', () => {
    hiddenCase('(A:0.1,B:0.2,C:0.3);', ['A'], ['B', 'C']);
});

test('hidden middle child does not leave a doubled comma', () => {
    hiddenCase('(A:0.1,B:0.2,C:0.3);', ['B'], ['A', 'C']);
});

test('hidden last child does not leave a trailing comma', () => {
    hiddenCase('(A:0.1,B:0.2,C:0.3);', ['C'], ['A', 'B']);
});

test('several hidden children at once stay valid', () => {
    hiddenCase('(A:0.1,B:0.2,C:0.3,D:0.4,E:0.5);', ['A', 'C', 'E'], ['B', 'D']);
});

test('all but one child hidden does not produce a stray separator', () => {
    hiddenCase('(A:0.1,B:0.2,C:0.3);', ['A', 'B'], ['C']);
});

test('an entirely hidden clade disappears instead of becoming a fake tip', () => {
    // ((A,B)I,C) with A and B filtered out must export C only. Writing `I` as
    // a terminal would invent a taxon, and `I` is frequently a support value.
    const out = hiddenCase('((A:0.1,B:0.2)I:0.05,C:0.3);', ['A', 'B'], ['C']);
    assert.ok(!/\(\s*\)/.test(out), 'produced an empty group: ' + out);
    assert.ok(!/\bI\b/.test(out), 'the internal label leaked into the export: ' + out);
});

test('a numeric support label on a fully hidden clade is not exported', () => {
    const out = hiddenCase('((A:0.1,B:0.2)98:0.05,C:0.3,D:0.4);', ['A', 'B'], ['C', 'D']);
    assert.ok(!/98/.test(out), 'a support value became a terminal taxon: ' + out);
});

test('nested internal clades with no visible descendant vanish entirely', () => {
    const out = hiddenCase(
        '((((A:0.1,B:0.2)I:0.05,(E:0.15,F:0.25)K:0.07)J:0.3,C:0.4)L:0.5,D:0.6);',
        ['A', 'B', 'E', 'F'], ['C', 'D']);
    ['I', 'J', 'K'].forEach(n => {
        assert.ok(!new RegExp('\\b' + n + '\\b').test(out),
            n + ' survived as a terminal: ' + out);
    });
    assert.ok(!/\(\s*\)/.test(out), 'produced an empty group: ' + out);
});

test('one surviving descendant several levels down is still exported', () => {
    const out = hiddenCase(
        '((((A:0.1,B:0.2)I:0.05,C:0.3)J:0.15,D:0.4)K:0.5,E:0.6);',
        ['A', 'C', 'D'], ['B', 'E']);
    assert.ok(!/\(\s*\)/.test(out), 'produced an empty group: ' + out);
    // The surviving tip is a terminal; every ancestor stays internal.
    (function walk(n) {
        if (!n.children.length) {
            assert.ok(['B', 'E'].includes(n.name), 'unexpected terminal ' + n.name);
        }
        n.children.forEach(walk);
    })(parseNewickStrict(out));
});

test('hiding all but one tip in the whole tree leaves exactly that tip', () => {
    const out = hiddenCase(
        '(((A:0.1,B:0.2)I:0.05,C:0.3)J:0.15,(D:0.4,E:0.5)K:0.6);',
        ['A', 'B', 'C', 'D'], ['E']);
    assert.ok(!/\(\s*\)/.test(out), 'produced an empty group: ' + out);
    assert.ok(!/,/.test(out), 'a separator survived with a single tip: ' + out);
});

test('hiding every tip makes low-level serialization an explicit failure', () => {
    const tree = new Phylotree('((A:0.1,B:0.2)I:0.05,(C:0.3,D:0.4)J:0.6);');
    ['A', 'B', 'C', 'D'].forEach(n => { tree.getNodeByName(n).notshown = true; });
    assert.throws(
        () => tree.getNewick(),
        /No visible sequences remain to export\./,
        'a fully filtered tree was serialized as a fake unnamed terminal'
    );
});

test('one visible tip remains a valid one-taxon export', () => {
    const out = hiddenCase('(A:0.1,B:0.2,C:0.3);', ['A', 'B'], ['C']);
    assert.ok(!/\(\s*\)/.test(out), 'produced an empty group: ' + out);
    assert.ok(!/,/.test(out), 'a separator survived with one visible tip: ' + out);
});

test('two visible tips remain a valid two-taxon export', () => {
    const out = hiddenCase('(A:0.1,B:0.2,C:0.3);', ['A'], ['B', 'C']);
    assert.ok(!/,{2}|\(,|,\)/.test(out), 'produced an invalid separator: ' + out);
});

test('a node left with an empty children array contributes no terminal', () => {
    // An internal node is what carries a `children` array; emptying it leaves
    // a clade with nothing under it, which is not the same thing as a tip.
    const tree = new Phylotree('((A:0.1,B:0.2)I:0.05,C:0.3);');
    tree.getNodeByName('I').children = [];
    const out = tree.getNewick();
    assert.ok(!/\(\s*\)/.test(out), 'produced an empty group: ' + out);
    assertSameList(newickTips(out), ['C']);
});

test('a hidden clade cannot smuggle a tip in through a visible sibling subtree', () => {
    // J keeps a visible tip, I does not; only J may be written.
    const out = hiddenCase(
        '(((A:0.1,B:0.2)I:0.05,(C:0.3,D:0.4)J:0.6)L:0.7,E:0.8);',
        ['A', 'B', 'C'], ['D', 'E']);
    assert.ok(!/\bI\b/.test(out), 'the emptied clade was written: ' + out);
});

test('a hidden internal node removes its whole clade', () => {
    hiddenCase('((A:0.1,B:0.2)I:0.05,C:0.3,D:0.4);', ['I'], ['C', 'D']);
});

test('nested hidden nodes stay valid', () => {
    hiddenCase('(((A:0.1,B:0.2)I:0.05,C:0.3)J:0.6,D:0.4,E:0.5);', ['B', 'C'], ['A', 'D', 'E']);
});

test('nothing hidden still round-trips with branch lengths and labels intact', () => {
    const tree = new Phylotree("((A:0.1,'B c,d':0.2)I:0.05,C:0.3);");
    const parsed = parseNewickStrict(tree.getNewick());
    assertSameList(newickTips(tree.getNewick()), ['A', 'B c,d', 'C']);
    const inner = parsed.children.find(c => c.children.length);
    assert.strictEqual(inner.name, 'I');
    assert.strictEqual(inner.length, 0.05);
    assert.strictEqual(inner.children.find(c => c.name === 'A').length, 0.1);
});

test('a label needing quotes survives being the survivor of hidden siblings', () => {
    const out = hiddenCase("(A:0.1,'B (x) y':0.2,C:0.3);", ['A'], ['B (x) y', 'C']);
    assert.ok(out.includes("'B (x) y'"), 'quoting was lost: ' + out);
});

test('the annotator still runs for every written node', () => {
    const tree = new Phylotree('(A:0.1,B:0.2,C:0.3);');
    tree.getNodeByName('B').notshown = true;
    const seen = [];
    const out = tree.getNewick(n => { seen.push(n.data.name); return ''; });
    assert.ok(!seen.includes('B'), 'the annotator was called for a hidden node');
    assert.ok(seen.includes('A') && seen.includes('C'));
    newickTips(out);
});

test('the viewer\'s own {Selected} export stays valid with filtered tips', () => {
    // This is exactly getNewickString() in tree_viewer_phylotree_v2.js after
    // _applySequenceFilters() has hidden tips the metric sliders exclude.
    const tree = new Phylotree('((A:0.1,B:0.2)I:0.05,(C:0.3,D:0.4)J:0.6,E:0.7);');
    ['B', 'E'].forEach(n => { tree.getNodeByName(n).notshown = true; });
    const selected = new Set(['A', 'C']);

    const out = tree.getNewick(n => (selected.has(n.data.name) ? '{Selected}' : ''));

    assertSameList(newickTips(out), ['A', 'C', 'D']);
    assert.ok(out.includes('{Selected}'), 'the selection tag was dropped: ' + out);
});

test('the viewer refuses export when its metric filters hide every tip', () => {
    const { ctx } = loadViewer();
    const metrics = ['A', 'B', 'C'].map(name => ({
        name,
        query_cover: 50,
        blast_metrics_available: true
    }));
    const viewer = new ctx.DikaryaTreeViewer('tree-container', {}, {
        sequenceMetrics: metrics,
        queryCoverThreshold: 90
    });
    viewer.tree = new Phylotree('((A:0.1,B:0.2)I:0.05,C:0.3);');
    viewer._cacheNodes();

    const stats = viewer._applySequenceFilters({ updateDisplay: false });
    assert.strictEqual(stats.visibleTips, 0, 'the real viewer filter did not hide every tip');
    assert.throws(
        () => viewer.getNewickString(),
        /No visible sequences remain to export\./,
        'the Dikarya-facing export path treated a zero-tip tree as downloadable'
    );
});

// ===========================================================================
// Finding 4 - cached links must follow a topology change
// ===========================================================================

section('links-after-reroot');

test('links are rebuilt after a reroot', () => {
    const tree = new Phylotree('((A:0.1,B:0.2)I:0.05,C:0.3);');
    const before = tree.links.slice();

    tree.reroot(tree.getNodeByName('A'));

    assertLinkInvariant(tree, 'after reroot');
    const live = new Set(tree.getNodes().descendants());
    before.forEach(l => {
        assert.ok(!tree.links.includes(l) || (live.has(l.source) && live.has(l.target)),
            'a link object from the previous topology survived');
    });
});

test('no stale node from the old topology remains linked after a reroot', () => {
    const tree = new Phylotree('(((A:0.1,B:0.2)I:0.05,C:0.3)J:0.15,D:0.4);');
    const oldRoot = tree.getNodes();

    tree.reroot(tree.getNodeByName('B'));

    const live = new Set(tree.getNodes().descendants());
    assert.ok(!live.has(oldRoot) || tree.getNodes() === oldRoot,
        'the previous root is still hanging off the tree');
    tree.links.forEach(l => {
        assert.ok(live.has(l.source) && live.has(l.target), 'link references a detached node');
    });
    assertSameList(tipNames(tree), ['A', 'B', 'C', 'D']);
});

test('depths are recomputed when the hierarchy is replaced', () => {
    const tree = new Phylotree('(((A:0.1,B:0.2)I:0.05,C:0.3)J:0.15,D:0.4);');
    tree.reroot(tree.getNodeByName('A'));
    assert.strictEqual(tree.getNodes().depth, 0);
    tree.getNodes().each(n => {
        assert.strictEqual(n.depth, n.parent ? n.parent.depth + 1 : 0,
            nodeName(n) + ' carries a stale depth');
    });
});

test('a selection made after rerooting lands on current nodes', () => {
    const tree = new Phylotree('((A:0.1,B:0.2)I:0.05,C:0.3);');
    tree.reroot(tree.getNodeByName('A'));

    const live = new Set(tree.getNodes().descendants());
    const targets = tree.links.map(l => l.target);
    targets.forEach(t => assert.ok(live.has(t), 'link target is not part of the current tree'));

    // This is what the brush handler does: clear through phylotree.links, then
    // read the result back off the live hierarchy.
    targets.forEach(t => { t.selected = true; });
    const selected = tree.getNodes().descendants().filter(n => n.selected);
    assert.strictEqual(selected.length, live.size - 1,
        'clearing through the cached links missed live nodes');
});

test('rerooting on the current root leaves the tree intact', () => {
    const tree = new Phylotree('((A:0.1,B:0.2)I:0.05,C:0.3);');
    const root = tree.getNodes();

    tree.reroot(root);

    assert.strictEqual(tree.getNodes(), root, 'rerooting on the root discarded the hierarchy');
    assertSameList(tipNames(tree), ['A', 'B', 'C']);
    assertLinkInvariant(tree, 'after rerooting on the root');
});

test('replacing the hierarchy with nothing empties the link cache', () => {
    const tree = new Phylotree('((A:0.1,B:0.2)I:0.05,C:0.3);');
    tree.update(null);
    assertSameList(tree.links, []);
});

test('an unparseable tree yields an empty model instead of throwing', () => {
    // A truncated Newick leaves `nodes` as a plain array; the constructor used
    // to call .links() and then .descendants() on it and take the viewer down.
    ['(((', '((A,B)', '(A:0.1,'].forEach(bad => {
        const tree = new Phylotree(bad);
        assert.ok(Array.isArray(tree.nodes), bad + ' unexpectedly parsed');
        assertSameList(tree.links, [], bad + ' left links behind');
    });
});

test('clearing the hierarchy leaves every derived view empty', () => {
    const tree = new Phylotree('((A:0.1,B:0.2)I:0.05,C:0.3);');
    tree.update(null);
    assertSameList(tree.links, []);
    assertSameList(tree.getTips(), [], 'getTips() tripped over a null hierarchy');
    let visited = 0;
    tree.traverse_and_compute(() => { visited++; });
    assert.strictEqual(visited, 0);
});

test('an empty model traverses nothing rather than visiting the array', () => {
    const tree = new Phylotree('(((');
    const seen = [];
    tree.traverse_and_compute(n => seen.push(n));
    assertSameList(seen, [], 'the traversal handed the callback a non-node');
    assertSameList(tree.getTips(), [], 'getTips() did not cope with an empty model');
});

test('a normal tree still traverses every node, subtrees included', () => {
    const tree = new Phylotree('((A:0.1,B:0.2)I:0.05,C:0.3);');
    let all = 0;
    tree.traverse_and_compute(() => { all++; });
    assert.strictEqual(all, 5, 'visited ' + all + ' of 5 nodes');

    let clade = 0;
    tree.traverse_and_compute(() => { clade++; }, 'pre-order', tree.getNodeByName('I'));
    assert.strictEqual(clade, 3, 'the explicit root argument stopped working');
});

test('a well-formed tree is still parsed normally', () => {
    const tree = new Phylotree('((A:0.1,B:0.2)I:0.05,C:0.3);');
    assert.ok(!Array.isArray(tree.nodes));
    assertLinkInvariant(tree, 'freshly parsed');
});

// ===========================================================================
// Invalid Newick must be an explicit failure, never a blank tree
// ===========================================================================

section('invalid-newick-surfacing');

const BROKEN = ['(((', '((A,B)', '(A:0.1,', '((A,B),C', 'not a tree at all ((('];
const GOOD = ['((A:0.1,B:0.2)I:0.05,C:0.3);', '(A:1,B:1);', 'A;'];

test('the viewer classifies every unparseable Newick as a failure', () => {
    const { ctx } = loadViewer();
    BROKEN.forEach(bad => {
        const tree = new Phylotree(bad);
        const why = ctx.describeTreeParseFailure(tree);
        assert.ok(why, 'no failure reported for ' + JSON.stringify(bad));
        assert.ok(/pars|truncat|malformed|no sequences/i.test(why),
            'unhelpful message for ' + JSON.stringify(bad) + ': ' + why);
    });
});

test('the viewer classifies well-formed Newick as usable', () => {
    const { ctx } = loadViewer();
    GOOD.forEach(ok => {
        assert.strictEqual(ctx.describeTreeParseFailure(new Phylotree(ok)), null,
            'a valid tree was rejected: ' + ok);
    });
});

test('render() rejects on malformed Newick instead of drawing nothing', async () => {
    const { ctx } = loadViewer();
    for (const bad of BROKEN) {
        const viewer = new ctx.DikaryaTreeViewer('tree-container', {}, {});
        const err = await expectRejection(viewer.render(bad),
            'render() accepted ' + JSON.stringify(bad) + ' and would have drawn a blank tree');
        assert.ok(/pars|truncat|malformed|no sequences/i.test(err.message),
            'unhelpful render error: ' + err.message);
        assert.strictEqual(viewer.tree, null,
            'a viewer left an unusable tree in place after a failed parse');
    }
});

test('a failed render leaves no half-built model to mistake for a loaded tree', async () => {
    const { ctx } = loadViewer();
    const viewer = new ctx.DikaryaTreeViewer('tree-container', {}, {});
    await expectRejection(viewer.render('((A,B)'));
    assert.strictEqual(viewer.tree, null);
    assertSameList(viewer.allNodes, [], 'stale nodes survived a failed parse');
});

test('render() gets past the parse guard for a valid tree', async () => {
    // It cannot finish without a real DOM, but it must fail somewhere OTHER
    // than the guard - otherwise the guard is rejecting good trees.
    const { ctx } = loadViewer();
    const viewer = new ctx.DikaryaTreeViewer('tree-container', {}, {});
    let err = null;
    try {
        await viewer.render('((A:0.1,B:0.2)I:0.05,C:0.3);');
    } catch (e) {
        err = e;
    }
    if (err) {
        assert.ok(!/pars|truncat|malformed|no sequences/i.test(err.message),
            'the parse guard rejected a valid tree: ' + err.message);
    }
    assert.ok(viewer.tree, 'a valid tree was discarded');
    assertSameList(tipNames(viewer.tree), ['A', 'B', 'C']);
});

// ===========================================================================
// Phase 4 - branch lengths across a reroot
// ===========================================================================

section('branch-lengths');

test('rerooting preserves the distance between tips', () => {
    const newick = '(((A:0.1,B:0.2)I:0.05,C:0.3)J:0.15,D:0.4);';
    const tree = new Phylotree(newick);
    const pairs = [['A', 'B'], ['A', 'C'], ['A', 'D'], ['B', 'D'], ['C', 'D']];
    const before = pairs.map(([a, b]) => tipDistance(tree, a, b));

    tree.reroot(tree.getNodeByName('C'));

    const after = pairs.map(([a, b]) => tipDistance(tree, a, b));
    pairs.forEach(([a, b], i) => {
        assert.ok(Math.abs(before[i] - after[i]) < 1e-9,
            'reroot changed the ' + a + '-' + b + ' distance from '
            + before[i] + ' to ' + after[i]);
    });
});

test('rerooting a tree with no branch lengths keeps the invariants', () => {
    const tree = new Phylotree('((A,B)I,C);');
    tree.reroot(tree.getNodeByName('A'));
    assertSameList(tipNames(tree), ['A', 'B', 'C']);
    assertLinkInvariant(tree, 'after rerooting a tree with no branch lengths');
});

// ===========================================================================
// Finding 5 - the renderer swap during a reroot must keep listeners
// ===========================================================================

section('reroot-listeners');

/** A stand-in renderer: real prototype, so on/off/emit are the shipped ones. */
function fakeRenderer(options) {
    const container = {
        appendChild() {},
        dispatchEvent() { return true; },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        ownerDocument: { defaultView: { CustomEvent: function () {} } }
    };
    const d = Object.create(TreeRender.prototype);
    d._eventListeners = {};
    d._selectionCallback = null;
    d.container = container;
    d.options = options || {};
    d.selection_attribute_name = 'selected';
    d.selectionLabel = function (n) { if (n) { this.selection_attribute_name = n; } return this; };
    d.update = function () { return this; };
    d.show = function () { return container; };
    return d;
}

/** A tree whose render() hands back a stand-in renderer instead of touching DOM. */
function treeWithFakeDisplay(newick) {
    const tree = new Phylotree(newick);
    tree.render = function (options) {
        this.display = fakeRenderer(options);
        return this.display;
    };
    tree.render({});
    return tree;
}

test('a rerooted listener survives the reroot that fires it', () => {
    const tree = treeWithFakeDisplay('((A:0.1,B:0.2)I:0.05,C:0.3);');
    const calls = [];
    tree.display.on('rerooted', root => calls.push(root));

    tree.reroot(tree.getNodeByName('A'));

    assert.strictEqual(calls.length, 1, 'the rerooted listener fired ' + calls.length + ' times');
    assert.strictEqual(calls[0], tree.getNodes(), 'the listener was handed a stale root');
});

test('unrelated listeners survive a reroot too', () => {
    const tree = treeWithFakeDisplay('((A:0.1,B:0.2)I:0.05,C:0.3);');
    let clicks = 0;
    tree.display.on('nodeClick', () => { clicks++; });

    tree.reroot(tree.getNodeByName('A'));

    tree.display.emit('nodeClick', null);
    assert.strictEqual(clicks, 1, 'the nodeClick listener did not survive the reroot');
});

test('repeated reroots do not duplicate listeners', () => {
    const tree = treeWithFakeDisplay('(((A:0.1,B:0.2)I:0.05,C:0.3)J:0.15,D:0.4);');
    let fired = 0;
    tree.display.on('rerooted', () => { fired++; });

    tree.reroot(tree.getNodeByName('A'));
    tree.reroot(tree.getNodeByName('D'));
    tree.reroot(tree.getNodeByName('C'));

    assert.strictEqual(fired, 3, 'expected one call per reroot, got ' + fired);
});

test('a listener can still be removed after a reroot', () => {
    const tree = treeWithFakeDisplay('((A:0.1,B:0.2)I:0.05,C:0.3);');
    let fired = 0;
    const handler = () => { fired++; };
    tree.display.on('rerooted', handler);

    tree.reroot(tree.getNodeByName('A'));
    assert.strictEqual(fired, 1);

    tree.display.off('rerooted', handler);
    tree.reroot(tree.getNodeByName('B'));
    assert.strictEqual(fired, 1, 'off() did not reach the transferred listener');
});

test('the selection callback survives a reroot', () => {
    const tree = treeWithFakeDisplay('((A:0.1,B:0.2)I:0.05,C:0.3);');
    const cb = () => {};
    tree.display._selectionCallback = cb;

    tree.reroot(tree.getNodeByName('A'));

    assert.strictEqual(tree.display._selectionCallback, cb,
        'the legacy selection callback was dropped');
});

test('the selection label survives a reroot', () => {
    const tree = treeWithFakeDisplay('((A:0.1,B:0.2)I:0.05,C:0.3);');
    tree.display.selection_attribute_name = 'my-tag';

    tree.reroot(tree.getNodeByName('A'));

    assert.strictEqual(tree.display.selection_attribute_name, 'my-tag');
});

// ===========================================================================
// Finding 6 - `this` inside the binary-selection callbacks
// ===========================================================================

section('binary-selection');

/** The smallest object modifySelection actually needs, on the real prototype. */
function selectionHarness(newick, options) {
    const tree = new Phylotree(newick);
    const d = Object.create(TreeRender.prototype);
    d.phylotree = tree;
    d.links = tree.links;
    d.selection_attribute_name = 'selected';
    d._eventListeners = {};
    d._selectionCallback = null;
    d.countHandler = () => null;
    d.refresh = function () { return this; };
    d.update = function () { return this; };
    d.placenodes = function () { return this; };
    d.options = Object.assign({
        selectable: true,
        'restricted-selectable': [],
        'binary-selectable': false,
        'attribute-list': ['selected', 'other']
    }, options || {});
    return { tree, display: d };
}

test('binary-selectable with a functional selector does not throw on `this`', () => {
    const { display } = selectionHarness('((A:0.1,B:0.2)I:0.05,C:0.3);', {
        'binary-selectable': true
    });

    display.modifySelection(l => l.target.data.name === 'A', 'selected', false, true);

    const picked = display.links.filter(l => l.selected).map(l => l.target.data.name);
    assertSameList(picked, ['A'], 'functional selector picked ' + picked);
    assert.strictEqual(display.phylotree.getNodeByName('A').selected, true);
});

test('binary-selectable clears the other attributes in the list', () => {
    const { display } = selectionHarness('((A:0.1,B:0.2)I:0.05,C:0.3);', {
        'binary-selectable': true
    });
    const a = display.phylotree.getNodeByName('A');
    a.other = true;
    display.links.filter(l => l.target === a).forEach(l => { l.other = true; });

    display.modifySelection(l => l.target.data.name === 'A', 'selected', false, true);

    assert.strictEqual(a.other, false, 'the competing attribute was not cleared');
});

test('binary-selectable with an array selector does not throw on `this`', () => {
    const { tree, display } = selectionHarness('((A:0.1,B:0.2)I:0.05,C:0.3);', {
        'binary-selectable': true
    });

    display.modifySelection([tree.getNodeByName('B')], 'selected', false, true);

    assert.strictEqual(tree.getNodeByName('B').selected, true);
    const picked = display.links.filter(l => l.selected).map(l => l.target.data.name);
    assertSameList(picked, ['B'], 'array selector picked ' + picked);
});

test('binary-selectable deselects on a second pass', () => {
    const { tree, display } = selectionHarness('((A:0.1,B:0.2)I:0.05,C:0.3);', {
        'binary-selectable': true
    });

    display.modifySelection([tree.getNodeByName('B')], 'selected', false, true);
    display.modifySelection([tree.getNodeByName('B')], 'selected', false, true);

    assert.strictEqual(tree.getNodeByName('B').selected, false, 'toggle did not deselect');
    assert.strictEqual(display.links.filter(l => l.selected).length, 0);
});

test('binary-selectable disabled keeps the plain selection path working', () => {
    const { tree, display } = selectionHarness('((A:0.1,B:0.2)I:0.05,C:0.3);');

    display.modifySelection(l => l.target.data.name === 'C', 'selected', false, true);

    assert.strictEqual(tree.getNodeByName('C').selected, true);
    // A node that was never selected is left untouched rather than written to,
    // so assert it is not selected rather than that it holds a literal false.
    assert.ok(!tree.getNodeByName('A').selected);
    const picked = display.links.filter(l => l.selected).map(l => l.target.data.name);
    assertSameList(picked, ['C'], 'plain functional selector picked ' + picked);
});

test('the plain array selector still selects and deselects', () => {
    const { tree, display } = selectionHarness('((A:0.1,B:0.2)I:0.05,C:0.3);');
    const c = tree.getNodeByName('C');

    display.modifySelection([c], 'selected', false, true);
    assert.strictEqual(c.selected, true);

    display.modifySelection([c], 'selected', false, true);
    assert.strictEqual(c.selected, false);
});

// ---------------------------------------------------------------------------

function report() {
    if (AS_JSON) {
        console.log(JSON.stringify(results));
        return;
    }
    const failures = results.filter(r => !r.ok);
    if (failures.length) {
        console.error(failures.length + ' FAILED:\n\n'
            + failures.map(r => r.name + '\n    ' + r.error).join('\n\n'));
        process.exit(1);
    }
    console.log('PASS ' + results.length);
}

run().catch(e => {
    console.error('harness crashed: ' + (e && e.stack ? e.stack : e));
    process.exit(1);
});
