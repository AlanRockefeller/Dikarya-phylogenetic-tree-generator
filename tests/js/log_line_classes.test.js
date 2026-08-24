/**
 * Behavioural test for JobStatusClient#_buildLogLine, loaded from the shipped
 * app/static/js/job_status.js rather than a copy.
 *
 * Two properties are under test at once:
 *   - command events (stream "cmd") must keep the `cmd` class, which is what
 *     the job-status page styles green and bold. The safe-DOM refactor
 *     whitelisted only stdout/stderr and silently dropped it.
 *   - an untrusted stream value must NOT become a CSS class.
 *
 * Usage: node log_line_classes.test.js <path-to-job_status.js>
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

// Minimal DOM: _buildLogLine only creates elements, sets className/textContent
// and appends children.
function makeElement(tag) {
    const el = {
        tagName: tag,
        _classes: [],
        children: [],
        textContent: '',
        set className(v) { el._classes = String(v).split(/\s+/).filter(Boolean); },
        get className() { return el._classes.join(' '); },
        classList: {
            add(...cs) { cs.forEach(c => { if (!el._classes.includes(c)) el._classes.push(c); }); },
            contains(c) { return el._classes.includes(c); },
        },
        appendChild(child) { el.children.push(child); return child; },
    };
    return el;
}

const sandbox = {
    document: {
        createElement: makeElement,
        createTextNode: text => ({ nodeType: 3, textContent: String(text) }),
        addEventListener() {},
        getElementById: () => null,
        querySelectorAll: () => [],
    },
    window: { addEventListener() {} },
    console,
    setInterval: () => 0,
    clearInterval() {},
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

const source = fs.readFileSync(process.argv[2], 'utf8');
// The file ends with DOMContentLoaded/pagehide wiring that needs no DOM here;
// the stubs above absorb it.
vm.runInContext(source + '\n;globalThis.__JobStatusClient = JobStatusClient;', sandbox, {
    filename: path.basename(process.argv[2]),
});
const JobStatusClient = sandbox.__JobStatusClient;

// _buildLogLine touches no instance state, so a bare prototype call is enough.
const build = (stream) =>
    JobStatusClient.prototype._buildLogLine.call(null, '[ALIGN]', 'some output', stream);

const failures = [];
function check(label, cond, detail) {
    if (!cond) failures.push(`${label}${detail ? ': ' + detail : ''}`);
}

// Known streams each contribute exactly their own class.
for (const stream of ['stdout', 'stderr', 'cmd']) {
    const el = build(stream);
    check(
        `stream="${stream}" gets the ${stream} class`,
        el.classList.contains(stream),
        `classes were "${el.className}"`
    );
    check(
        `stream="${stream}" keeps the base log-line class`,
        el.classList.contains('log-line'),
        `classes were "${el.className}"`
    );
    check(
        `stream="${stream}" adds nothing else`,
        el._classes.length === 2,
        `classes were "${el.className}"`
    );
}

// Untrusted values must never reach the class attribute.
const hostile = [
    'evil arbitrary-class',
    'cmd extra-injected',
    'hidden',
    '',
    undefined,
    null,
    'STDOUT',
    'constructor',
    '__proto__',
    'toString',
];
for (const stream of hostile) {
    const el = build(stream);
    check(
        `stream=${JSON.stringify(stream)} contributes no class`,
        el._classes.length === 1 && el._classes[0] === 'log-line',
        `classes were "${el.className}"`
    );
}

// The line text stays a text node, never markup.
const el = build('cmd');
const textNode = el.children[el.children.length - 1];
check('line content is a text node', textNode.nodeType === 3);
check('line content is preserved', textNode.textContent === 'some output');
check('the tag span carries the tag text', el.children[0].textContent === '[ALIGN]');

if (failures.length) {
    failures.forEach(f => console.error('FAIL ' + f));
    process.exit(1);
}
console.log('ok');
