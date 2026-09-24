// Alan 9/23/26 - Polytomy connector pegs: which tips get one, their screen-fixed geometry,
// that the support fade never reaches them, and that an SVG export carries them as drawn.
/**
 * Loads the shipped tree_viewer_phylotree_v2.js in a vm sandbox with a small fake SVG DOM
 * (no jsdom in this repo). The fake supports exactly what the code under test touches:
 * attributes, classList, inline style, child lists, deep clone, and simple selectors
 * ("tag.class", ".a b", ":not(.x)", comma lists). The stylesheets handed to the export are
 * parsed from the real tree_viewer.css and phylotree.css, so the export assertions check the
 * stroke a figure will actually get.
 *
 * Usage: node polytomy_connectors.test.js <repo-root>
 */
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = process.argv[2] || path.resolve(__dirname, '..', '..');

// ------------------------------------------------------------------ fake DOM

function makeStyle() {
    const props = new Map();
    const style = {
        setProperty(name, value) { props.set(name, String(value)); },
        removeProperty(name) { props.delete(name); },
        getPropertyValue(name) { return props.get(name) || ''; },
        get cssText() { return Array.from(props, ([k, v]) => `${k}:${v};`).join(''); },
        set cssText(text) {
            props.clear();
            String(text || '').split(';').forEach((part) => {
                const i = part.indexOf(':');
                if (i > 0) props.set(part.slice(0, i).trim(), part.slice(i + 1).trim());
            });
        },
        get stroke() { return props.get('stroke') || ''; },
        get strokeOpacity() { return props.get('stroke-opacity') || ''; }
    };
    return style;
}

class FakeElement {
    constructor(tagName) {
        this.tagName = tagName;
        this.attrs = new Map();
        this.children = [];
        this.parentNode = null;
        this.style = makeStyle();
        this._text = '';
    }
    get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
    set textContent(value) { this._text = String(value); this.children = []; }
    get firstChild() { return this.children[0] || null; }
    get isConnected() { return true; }
    get attributes() { return Array.from(this.attrs, ([name, value]) => ({ name, value })); }
    get classList() {
        const el = this;
        const read = () => new Set((el.attrs.get('class') || '').split(/\s+/).filter(Boolean));
        const write = (set) => el.attrs.set('class', Array.from(set).join(' '));
        return {
            contains: (name) => read().has(name),
            add: (...names) => { const s = read(); names.forEach((n) => s.add(n)); write(s); },
            remove: (...names) => { const s = read(); names.forEach((n) => s.delete(n)); write(s); },
            toggle: (name, force) => {
                const s = read();
                const on = force === undefined ? !s.has(name) : Boolean(force);
                if (on) s.add(name); else s.delete(name);
                write(s);
                return on;
            }
        };
    }
    setAttribute(name, value) {
        if (name === 'style') this.style.cssText = value;
        else this.attrs.set(name, String(value));
    }
    getAttribute(name) {
        if (name === 'style') return this.style.cssText || null;
        return this.attrs.has(name) ? this.attrs.get(name) : null;
    }
    removeAttribute(name) {
        if (name === 'style') this.style.cssText = '';
        else this.attrs.delete(name);
    }
    appendChild(child) {
        if (child.parentNode) child.remove();
        child.parentNode = this;
        this.children.push(child);
        return child;
    }
    insertBefore(child, ref) {
        if (child.parentNode) child.remove();
        child.parentNode = this;
        const index = ref ? this.children.indexOf(ref) : -1;
        if (index < 0) this.children.push(child);
        else this.children.splice(index, 0, child);
        return child;
    }
    remove() {
        if (!this.parentNode) return;
        const siblings = this.parentNode.children;
        siblings.splice(siblings.indexOf(this), 1);
        this.parentNode = null;
    }
    cloneNode(deep) {
        const copy = new FakeElement(this.tagName);
        this.attrs.forEach((v, k) => copy.attrs.set(k, v));
        copy.style.cssText = this.style.cssText;
        copy._text = this._text;
        if (deep) this.children.forEach((child) => copy.appendChild(child.cloneNode(true)));
        return copy;
    }
    getBoundingClientRect() { return { width: 0, height: 0, top: 0, left: 0 }; }
    _descendants() {
        const out = [];
        const walk = (el) => el.children.forEach((c) => { out.push(c); walk(c); });
        walk(this);
        return out;
    }
    querySelectorAll(selector) {
        const groups = selector.split(',').map((s) => s.trim().split(/\s+/));
        return this._descendants().filter((el) => groups.some((parts) => matchesChain(el, parts, this)));
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

function matchesCompound(el, compound) {
    const nots = [];
    const base = compound.replace(/:not\(([^)]*)\)/g, (_, inner) => { nots.push(inner); return ''; });
    const m = base.match(/^([a-z]*)((?:\.[\w-]+)*)$/i);
    if (!m) throw new Error(`fake DOM cannot parse selector "${compound}"`);
    if (m[1] && el.tagName !== m[1]) return false;
    const classes = m[2].split('.').filter(Boolean);
    if (!classes.every((c) => el.classList.contains(c))) return false;
    return !nots.some((inner) => matchesCompound(el, inner));
}

function matchesChain(el, parts, scope) {
    if (!matchesCompound(el, parts[parts.length - 1])) return false;
    let rest = parts.slice(0, -1);
    let node = el.parentNode;
    while (rest.length && node && node !== scope) {
        if (matchesCompound(node, rest[rest.length - 1])) rest = rest.slice(0, -1);
        node = node.parentNode;
    }
    return rest.length === 0;
}

// Top-level style rules only; @media blocks are skipped (nothing tested lives in one).
function parseStyleRules(cssText) {
    const css = cssText.replace(/\/\*[\s\S]*?\*\//g, '');
    const rules = [];
    let i = 0;
    while (i < css.length) {
        const open = css.indexOf('{', i);
        if (open < 0) break;
        const selector = css.slice(i, open).trim();
        let depth = 1, j = open + 1;
        while (j < css.length && depth) { if (css[j] === '{') depth += 1; else if (css[j] === '}') depth -= 1; j += 1; }
        const body = css.slice(open + 1, j - 1).trim();
        if (!selector.startsWith('@')) {
            rules.push({ type: 1, selectorText: selector, cssText: `${selector} { ${body} }` });
        }
        i = j;
    }
    return rules;
}

// ------------------------------------------------------------------ viewer

function loadViewer() {
    const styleSheets = ['app/static/css/tree_viewer.css', 'app/static/css/phylotree.css']
        .map((file) => ({ cssRules: parseStyleRules(fs.readFileSync(path.join(REPO, file), 'utf8')) }));
    const document = {
        styleSheets,
        body: { classList: { contains: () => false } },
        getElementById: () => null,
        createElementNS: (_ns, tag) => new FakeElement(tag)
    };
    const sandbox = {
        console, document, URLSearchParams, setTimeout, clearTimeout,
        location: { search: '' },
        CSSRule: { STYLE_RULE: 1 },
        addEventListener() {}, removeEventListener() {},
        getComputedStyle: (el) => ({ strokeOpacity: el.style.strokeOpacity || '1' }),
        matchMedia: () => ({ matches: false })
    };
    const ctx = vm.createContext(sandbox);
    ctx.window = ctx;
    vm.runInContext(
        fs.readFileSync(path.join(REPO, 'app/static/js/tree_viewer_phylotree_v2.js'), 'utf8'),
        ctx, { filename: 'tree_viewer_phylotree_v2.js' }
    );
    return { Viewer: ctx.DikaryaTreeViewer, document };
}

const { Viewer, document } = loadViewer();

// root
//  +- P (0.05, bootstrap 40: weak) = polytomy A(0) B(0) C(0.02) D(1e-7) G(1e-6) H(2e-6)
//  +- Q (0.03, bootstrap 90)       = bifurcation E(0) F(0.01)
function buildTree() {
    const node = (id, length, children, support) => {
        const n = { id, data: { name: support === undefined ? id : String(support), attribute: length }, children: children || [] };
        n.children.forEach((c) => { c.parent = n; });
        return n;
    };
    const tips = {
        A: node('A', '0'), B: node('B', '0'), C: node('C', '0.02'),
        D: node('D', '1e-7'), G: node('G', '1e-6'), H: node('H', '2e-6'),
        E: node('E', '0'), F: node('F', '0.01')
    };
    const P = node('P', '0.05', [tips.A, tips.B, tips.C, tips.D, tips.G, tips.H], 40);
    const Q = node('Q', '0.03', [tips.E, tips.F], 90);
    const root = node('root', null, [P, Q]);
    root.parent = null;
    return { root, P, Q, tips, all: [root, P, Q, ...Object.values(tips)] };
}

// Draw it the way phylotree's update() does: every edge (edge-styler), then every node
// (node-styler). Returns the SVG and a lookup from node id to its path/group.
function render(viewer, tree) {
    const svg = new FakeElement('svg');
    svg.setAttribute('width', '400');
    svg.setAttribute('height', '300');
    const container = svg.appendChild(new FakeElement('g'));
    container.setAttribute('class', 'phylotree-container');
    const paths = {}, groups = {};
    tree.all.filter((n) => n.parent).forEach((n) => {
        const p = container.appendChild(new FakeElement('path'));
        p.setAttribute('class', 'branch');
        p.setAttribute('d', 'M0,0V10H5');
        p.appendChild(new FakeElement('title')).textContent = `Length = ${n.data.attribute}`;
        paths[n.id] = p;
        viewer._styleBranchSupport({ node: () => p }, { source: n.parent, target: n });
    });
    tree.all.filter((n) => !n.children.length).forEach((n) => {
        const g = container.appendChild(new FakeElement('g'));
        g.setAttribute('class', 'node');
        const text = g.appendChild(new FakeElement('text'));
        text.setAttribute('class', 'phylotree-node-text');
        text.setAttribute('dx', '3.96');
        text.textContent = n.id;
        groups[n.id] = g;
        viewer._drawPolytomyConnector({ node: () => g }, n);
    });
    return { svg, paths, groups };
}

function makeViewer(overrides = {}) {
    const viewer = Object.create(Viewer.prototype);
    viewer.options = Object.assign({ layout: 'linear', alignTips: false, supportFade: true }, overrides);
    viewer.tipLabelGap = 2;
    viewer.lastStats = { supportType: 'BS' };
    viewer.getSelectedCladeNodes = () => [];
    viewer._annotatedBranchNodes = new Set();
    return viewer;
}

const peg = (group) => group.querySelector('path.polytomy-connector');
const dx = (group) => Number(group.querySelector('text.phylotree-node-text').getAttribute('dx'));

const results = [];
function test(name, fn) {
    try { fn(); results.push({ name, ok: true }); } catch (e) { results.push({ name, ok: false, error: e }); }
}

// ------------------------------------------------------------------ cases

test('a zero-length tip directly on a multifurcation gets a peg', () => {
    const tree = buildTree();
    const { groups } = render(makeViewer(), tree);
    for (const id of ['A', 'B', 'D']) {
        const p = peg(groups[id]);
        assert.ok(p, `${id} should have a connector`);
        assert.strictEqual(p.getAttribute('d'), 'M0,0H8');
        assert.strictEqual(p.getAttribute('fill'), 'none');
        assert.strictEqual(p.getAttribute('pointer-events'), 'none');
        // The label starts past the connector plus the ordinary 2px tip gap.
        assert.strictEqual(dx(groups[id]), 10);
        // Drawn under the label.
        assert.strictEqual(groups[id].firstChild, p);
    }
});

test('the zero-length floor is ZERO_LENGTH_POLYTOMY_EPSILON, inclusive', () => {
    const { groups } = render(makeViewer(), buildTree());
    assert.ok(peg(groups.G), '1e-6 is at the floor and counts as zero');
    assert.strictEqual(peg(groups.H), null, '2e-6 is above the floor');
});

test('a positive-length child of the same polytomy gets no peg', () => {
    const { groups } = render(makeViewer(), buildTree());
    assert.strictEqual(peg(groups.C), null);
    assert.strictEqual(dx(groups.C), 3.96, 'label left exactly as phylotree placed it');
});

test('a zero-length child of an ordinary bifurcation gets no peg', () => {
    const { groups } = render(makeViewer(), buildTree());
    assert.strictEqual(peg(groups.E), null);
    assert.strictEqual(peg(groups.F), null);
});

test('hidden siblings do not count toward a multifurcation', () => {
    const tree = buildTree();
    ['C', 'D', 'G', 'H'].forEach((id) => { tree.tips[id].notshown = true; });
    const { groups } = render(makeViewer(), tree);
    assert.strictEqual(peg(groups.A), null, 'only A and B are displayed under P');
});

test('the peg is display only: branch lengths and topology are untouched', () => {
    const tree = buildTree();
    const snapshot = () => tree.all.map((n) => [n.id, n.data.attribute, n.children.map((c) => c.id).join(',')]);
    const before = JSON.stringify(snapshot());
    render(makeViewer(), tree);
    assert.strictEqual(JSON.stringify(snapshot()), before);
});

test('the peg holds a fixed screen length at any zoom, like the labels', () => {
    const tree = buildTree();
    const viewer = makeViewer();
    viewer._labelVisualK = 2;
    const { groups } = render(viewer, tree);
    assert.strictEqual(peg(groups.A).getAttribute('d'), 'M0,0H4');
    assert.strictEqual(dx(groups.A), 5);
    // Zooming back out updates the same element in place.
    const first = peg(groups.A);
    viewer._labelVisualK = 0.5;
    viewer._drawPolytomyConnector({ node: () => groups.A }, tree.tips.A);
    assert.strictEqual(peg(groups.A), first);
    assert.strictEqual(groups.A.querySelectorAll('path.polytomy-connector').length, 1);
    assert.strictEqual(first.getAttribute('d'), 'M0,0H16');
});

test('radial layout points the peg outward along the label angle', () => {
    const tree = buildTree();
    tree.tips.A.text_align = 'end';
    tree.tips.A.text_angle = 180;
    tree.tips.B.text_align = 'start';
    tree.tips.B.text_angle = 12;
    const { groups } = render(makeViewer({ layout: 'radial' }), tree);
    assert.strictEqual(peg(groups.A).getAttribute('d'), 'M0,0H-8');
    assert.strictEqual(peg(groups.A).getAttribute('transform'), 'rotate(180)');
    assert.strictEqual(dx(groups.A), -10);
    assert.strictEqual(peg(groups.B).getAttribute('transform'), 'rotate(12)');
});

test('aligned tips get no peg (their tracer already connects them), and it is removed', () => {
    const tree = buildTree();
    const viewer = makeViewer();
    const { groups } = render(viewer, tree);
    assert.ok(peg(groups.A));
    viewer.options.alignTips = true;
    viewer._drawPolytomyConnector({ node: () => groups.A }, tree.tips.A);
    assert.strictEqual(peg(groups.A), null);
    assert.strictEqual(tree.tips.A.__polytomyConnector, null);
});

test('the peg is not a phylotree branch, so edge binding and the fade pass skip it', () => {
    const { svg } = render(makeViewer(), buildTree());
    // phylotree's edgeCssSelectors() and _applySupportFading() both select these.
    const branches = svg.querySelectorAll('path.branch, path.branch-selected, path.branch-tagged');
    assert.ok(branches.every((el) => !el.classList.contains('polytomy-connector')));
    assert.strictEqual(branches.length, 10);
    // _updateNodeStylesOnly paints node shapes with the colour-group colour; not the peg.
    const shapes = svg.querySelectorAll('g.node circle, g.node path:not(.polytomy-connector), g.node rect');
    assert.strictEqual(shapes.length, 0);
});

test('support fading does not fade the peg', () => {
    const tree = buildTree();
    const { paths, groups } = render(makeViewer(), tree);
    // P's own branch (bootstrap 40) is faded...
    assert.strictEqual(paths.P.getAttribute('data-support-fade'), 'weak');
    assert.strictEqual(paths.P.style.getPropertyValue('stroke-opacity'), '0.3');
    // ...and the connectors of its unresolved tips are not.
    for (const id of ['A', 'B', 'D', 'G']) {
        const p = peg(groups[id]);
        assert.strictEqual(p.getAttribute('data-support-fade'), null);
        assert.strictEqual(p.style.getPropertyValue('stroke-opacity'), '');
    }
    // The stylesheet pins them at full opacity even if something upstream were to set one.
    const css = fs.readFileSync(path.join(REPO, 'app/static/css/tree_viewer.css'), 'utf8');
    const rule = parseStyleRules(css).find((r) => r.selectorText === '#tree-container .polytomy-connector');
    assert.ok(rule && /stroke-opacity:\s*1 !important/.test(rule.cssText), 'full-opacity guard missing');
});

test('the peg mirrors its tip branch selection and tag state', () => {
    const tree = buildTree();
    const viewer = makeViewer();
    const { paths, groups } = render(viewer, tree);
    const restyle = () => viewer._styleBranchSupport({ node: () => paths.A }, { source: tree.P, target: tree.tips.A });
    paths.A.classList.add('branch-selected');
    restyle();
    assert.ok(peg(groups.A).classList.contains('polytomy-connector-selected'));
    paths.A.classList.remove('branch-selected');
    paths.A.classList.add('branch-tagged');
    restyle();
    assert.ok(!peg(groups.A).classList.contains('polytomy-connector-selected'));
    assert.ok(peg(groups.A).classList.contains('polytomy-connector-tagged'));
});

test('the peg shares every branch stroke rule', () => {
    const sheets = ['app/static/css/tree_viewer.css', 'app/static/css/phylotree.css']
        .flatMap((file) => parseStyleRules(fs.readFileSync(path.join(REPO, file), 'utf8')));
    const selectorsOf = (rule) => rule.selectorText.split(',').map((s) => s.trim());
    for (const [branch, connector] of [
        ['.branch', '.polytomy-connector'],
        ['#tree-container .branch', '#tree-container .polytomy-connector'],
        ['.dark #tree-container .branch', '.dark #tree-container .polytomy-connector'],
        ['.branch-selected', '.polytomy-connector.polytomy-connector-selected'],
        ['.branch-tagged', '.polytomy-connector.polytomy-connector-tagged']
    ]) {
        const rule = sheets.find((r) => selectorsOf(r).includes(branch));
        assert.ok(rule, `no rule for ${branch}`);
        assert.ok(selectorsOf(rule).includes(connector), `${connector} is not styled with ${branch}`);
    }
});

test('SVG export preserves the pegs exactly as displayed', () => {
    const tree = buildTree();
    const viewer = makeViewer({ supportFade: false });
    viewer._labelVisualK = 2;
    tree.tips.A.text_align = 'start';
    const { svg } = render(viewer, tree);
    const shown = svg.querySelectorAll('path.polytomy-connector');
    assert.strictEqual(shown.length, 4);

    const { clone } = viewer._buildExportClone(svg);
    const exported = clone.querySelectorAll('path.polytomy-connector');
    assert.strictEqual(exported.length, shown.length);
    exported.forEach((el, i) => {
        for (const attr of ['d', 'transform', 'class', 'fill']) {
            assert.strictEqual(el.getAttribute(attr), shown[i].getAttribute(attr), attr);
        }
        assert.strictEqual(el.style.getPropertyValue('stroke-opacity'), '');
    });
    // Each exported label keeps the offset that clears its peg.
    const labels = clone.querySelectorAll('g.node text.phylotree-node-text');
    assert.ok(labels.some((t) => t.getAttribute('dx') === '5'));
    // The standalone figure carries an unscoped stroke for them (the #tree-container rules
    // cannot match outside the page), the same one exported branches get.
    const embedded = clone.querySelector('style').textContent;
    const rule = parseStyleRules(embedded).find((r) =>
        r.selectorText.split(',').map((s) => s.trim()).includes('.polytomy-connector'));
    assert.ok(rule, 'no unscoped .polytomy-connector rule in the exported stylesheet');
    assert.ok(rule.selectorText.includes('.branch'), 'connector stroke is not the branch stroke');
    assert.ok(/stroke:\s*#999/.test(rule.cssText) && /stroke-width:\s*2px/.test(rule.cssText), rule.cssText);
});

test('a faded export still leaves the pegs solid', () => {
    const tree = buildTree();
    const viewer = makeViewer({ supportFade: true });
    viewer.allNodes = tree.all;
    const { svg } = render(viewer, tree);
    const { clone } = viewer._buildExportClone(svg);
    const faded = clone.querySelectorAll('g.phylotree-container path.branch').filter((el) => el.style.getPropertyValue('stroke-opacity'));
    assert.strictEqual(faded.length, 1, 'only P is faded');
    assert.ok(clone.querySelector('g.support-fade-legend'), 'a faded export carries its key');
    clone.querySelectorAll('path.polytomy-connector')
        .forEach((el) => assert.strictEqual(el.style.getPropertyValue('stroke-opacity'), ''));
});

// ------------------------------------------------------------------ report

const failed = results.filter((r) => !r.ok);
results.forEach((r) => console.log(`${r.ok ? 'ok  ' : 'FAIL'} ${r.name}${r.ok ? '' : `\n     ${r.error && r.error.stack}`}`));
if (failed.length) {
    console.log(`FAIL polytomy connectors: ${failed.length} of ${results.length} failed`);
    process.exit(1);
}
console.log(`PASS polytomy connectors (${results.length} cases)`);
