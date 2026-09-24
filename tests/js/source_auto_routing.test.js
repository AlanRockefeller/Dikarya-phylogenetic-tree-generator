/**
 * Runs the Tree Builder's source auto-routing and segment selector as the
 * browser runs them.
 *
 * Both IIFEs are extracted verbatim from sequence_entry.html and executed here
 * against a stub DOM, so the tests cannot drift away from what is served.
 *
 * Usage: node source_auto_routing.test.js <path-to-extracted-script> <case-json>
 * Prints the resulting field values / selected panel as JSON on stdout.
 */
// Deliberately NOT 'use strict': a direct eval() in strict mode gets its own
// scope, so the declarations in the extracted script would not be visible here.

const fs = require('fs');

const scenario = JSON.parse(process.argv[3]);

// --- A DOM stub with exactly the surface these two IIFEs touch. -------------

class StubClassList {
    constructor(initial) { this._set = new Set(initial || []); }
    add(...names) { names.forEach(n => this._set.add(n)); }
    remove(...names) { names.forEach(n => this._set.delete(n)); }
    contains(name) { return this._set.has(name); }
}

class StubElement {
    constructor(tagName, attrs) {
        this.tagName = (tagName || 'DIV').toUpperCase();
        this.dataset = {};
        this.classList = new StubClassList((attrs && attrs.classes) || []);
        this.children = [];
        this.value = '';
        this.type = (attrs && attrs.type) || '';
        this.hidden = false;
        this.tabIndex = 0;
        this.textContent = '';
        this.title = '';
        this._attrs = {};
        this._listeners = {};
        this._focused = false;
    }
    // The real DOM keeps className and classList in step; the badge is built
    // with className, and it is looked up again by class.
    set className(value) {
        this.classList = new StubClassList(String(value).split(/\s+/).filter(Boolean));
    }
    get className() { return [...this.classList._set].join(' '); }
    setAttribute(name, value) { this._attrs[name] = String(value); }
    getAttribute(name) { return this._attrs[name] ?? null; }
    appendChild(child) { child.parent = this; this.children.push(child); return child; }
    removeChild(child) {
        this.children = this.children.filter(c => c !== child);
    }
    remove() { if (this.parent) this.parent.removeChild(this); }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    querySelectorAll(selector) {
        const wanted = selector.replace(/^\./, '');
        const out = [];
        const walk = node => {
            node.children.forEach(child => {
                if (child.classList.contains(wanted)) out.push(child);
                walk(child);
            });
        };
        walk(this);
        return out;
    }
    addEventListener(name, handler) {
        (this._listeners[name] = this._listeners[name] || []).push(handler);
    }
    dispatchEvent(event) {
        (this._listeners[event.type] || []).forEach(h => h.call(this, event));
        return true;
    }
    focus() { this._focused = true; }
    setSelectionRange() { /* not every control supports it */ }
}

function makeEvent(type) { return { type, preventDefault() {} }; }

const nodesById = {};
const panels = [];
const tabs = [];

const tablist = new StubElement('div');
tablist.children = tabs;

const SOURCES = ['inaturalist', 'mycomap', 'paste', 'upload', 'blast', 'mushroom_observer'];
const PANEL_FIELDS = {
    inaturalist: [['inaturalist_url', 'text']],
    mycomap: [['mycomap_url', 'text']],
    paste: [['sequence_text', 'textarea']],
    upload: [],
    blast: [['blast_query', 'text']],
    mushroom_observer: [['mushroom_observer_input', 'text']],
};

SOURCES.forEach(name => {
    const tab = new StubElement('button', { classes: ['source-segment'] });
    tab.dataset.sourceSegment = name;
    tab.parent = tablist;
    tabs.push(tab);

    const panel = new StubElement('div', { classes: ['source-panel'] });
    panel.dataset.sourcePanel = name;
    PANEL_FIELDS[name].forEach(([id, kind]) => {
        const field = new StubElement(kind === 'textarea' ? 'TEXTAREA' : 'INPUT',
            { type: kind === 'textarea' ? '' : 'text' });
        nodesById[id] = field;
        field.parent = panel;
        panel.children.push(field);
    });
    // Every panel gets a results block so the MutationObserver wiring is real.
    const results = new StubElement('div', { classes: ['hidden'] });
    results.id = `${name}_results`;
    nodesById[`${name}_results`] = results;
    results.parent = panel;
    panel.children.push(results);

    panels.push(panel);
});

nodesById.source_segmented = tablist;

const document = {
    getElementById(id) { return nodesById[id] || null; },
    querySelectorAll(selector) {
        const wanted = selector.replace(/^\./, '');
        if (wanted === 'source-panel') return panels;
        if (wanted === 'source-segment') return tabs;
        return [];
    },
    createElement(tagName) { return new StubElement(tagName); },
};

const storage = {};
const window = {
    localStorage: {
        getItem(key) { return key in storage ? storage[key] : null; },
        setItem(key, value) { storage[key] = String(value); },
    },
    setTimeout(fn) { fn(); return 0; },
};

const statuses = [];
function showStatus(message, level) { statuses.push([message, level]); }

// `querySelectorAll` on a results node has to see the id-suffix selectors the
// real code uses ('[id$="_results"]'), so intercept those two forms.
const realPanelQuery = StubElement.prototype.querySelectorAll;
StubElement.prototype.querySelectorAll = function (selector) {
    if (selector.includes('[id$=')) {
        const suffixes = selector.split(',').map(part => {
            const m = /\[id\$="([^"]+)"\]/.exec(part);
            return m ? m[1] : null;
        }).filter(Boolean);
        const out = [];
        const walk = node => {
            node.children.forEach(child => {
                if (child.id && suffixes.some(s => child.id.endsWith(s))) out.push(child);
                walk(child);
            });
        };
        walk(this);
        return out;
    }
    if (selector.includes('input, textarea')) {
        const out = [];
        const walk = node => {
            node.children.forEach(child => {
                if (child.tagName === 'INPUT' || child.tagName === 'TEXTAREA') out.push(child);
                walk(child);
            });
        };
        walk(this);
        return out;
    }
    return realPanelQuery.call(this, selector);
};

// --- MutationObserver stub: fires the callback when a watched node's class
//     set is mutated through `observedTargets`.
const observers = [];
class MutationObserver {
    constructor(callback) { this.callback = callback; this.targets = []; }
    observe(target, _options) { this.targets.push(target); observers.push(this); }
}

function revealResults(source) {
    const node = nodesById[`${source}_results`];
    node.classList.remove('hidden');
    observers.forEach(observer => {
        if (observer.targets.includes(node)) {
            observer.callback([{ target: node }]);
        }
    });
}

eval(fs.readFileSync(process.argv[2], 'utf8'));

// --- Drive the scenario. ----------------------------------------------------

function selectedPanel() {
    const shown = panels.find(panel => !panel.hidden);
    return shown ? shown.dataset.sourcePanel : null;
}

function badges() {
    return tabs
        .filter(tab => tab.querySelector('.source-segment-badge'))
        .map(tab => tab.dataset.sourceSegment);
}

(scenario.steps || []).forEach(step => {
    if (step.set) {
        const field = nodesById[step.set];
        field.value = step.value;
        if (step.event) field.dispatchEvent(makeEvent(step.event));
    }
    if (step.clickTab) {
        const tab = tabs.find(t => t.dataset.sourceSegment === step.clickTab);
        tab.dispatchEvent(makeEvent('click'));
    }
    if (step.typeIn) {
        const field = nodesById[step.typeIn];
        field.value = step.value;
        field.parent.dispatchEvent(makeEvent('input'));
    }
    if (step.reveal) revealResults(step.reveal);
});

const fields = {};
Object.keys(nodesById).forEach(id => {
    if (nodesById[id].tagName === 'INPUT' || nodesById[id].tagName === 'TEXTAREA') {
        fields[id] = nodesById[id].value;
    }
});

process.stdout.write(JSON.stringify({
    fields,
    selected: selectedPanel(),
    badges: badges(),
    status: statuses,
}));
