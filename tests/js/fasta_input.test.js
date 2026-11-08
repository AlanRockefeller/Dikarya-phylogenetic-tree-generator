/** Run shipped FASTA input handlers: node --test tests/js/fasta_input.test.js */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

const template = fs.readFileSync('app/templates/sequence_entry.html', 'utf8');
const parser = template.slice(template.indexOf('    function parseFasta('), template.indexOf('    function isGenBankAccession('));
const handlers = template.slice(template.indexOf("    document.getElementById('add_sequences_btn').addEventListener('click'"),
    template.indexOf('    // Clear Queue Button'));
const valid = { valid: true, record_count: 1, total_bases: 4, issues: [], issue_count: 0, repair: null };
const invalid = { ...valid, valid: false, issue_count: 1, diagnostic_id: 'reference123',
    issues: [{ message: 'Invalid symbol at line 2, column 5: <img onerror=alert(1)>' }] };

function harness(answer = () => valid) {
    const nodes = {}, queue = [], calls = [], statuses = [];
    function element(id = '') {
        const classes = new Set(), listeners = {};
        return {
            id, value: '', disabled: false, innerHTML: '', textContent: '', open: false, children: [], files: [],
            classList: { add(x) { classes.add(x); }, remove(x) { classes.delete(x); }, contains(x) { return classes.has(x); },
                toggle(x, on) { if (on) classes.add(x); else classes.delete(x); } },
            addEventListener(type, callback) { listeners[type] = callback; },
            dispatchEvent(event) { return listeners[event.type]?.(event); },
            click() { return this.disabled ? undefined : listeners.click?.(); },
            change() { return listeners.change?.({ target: this }); },
            appendChild(child) { this.children.push(child); },
            replaceChildren() { this.children = []; },
            showModal() { this.open = true; }, close() { this.open = false; },
        };
    }
    for (const id of ['sequence_text', 'add_sequences_btn', 'sequence_file', 'add_file_btn',
        'check_fasta_btn', 'check_file_fasta_btn', 'fasta-repair-dialog', 'fasta-repair-summary',
        'fasta-repair-original', 'fasta-repair-preview', 'fasta-repair-issues', 'fasta-repair-changes',
        'fasta-repair-close', 'fasta-repair-apply', 'fasta-use-accessions']) nodes[id] = element(id);
    const context = vm.createContext({
        Event, AbortController, setTimeout, clearTimeout, window: {openImportSection() {}},
        sequenceTextInput: nodes.sequence_text, addSequencesBtn: nodes.add_sequences_btn,
        document: { getElementById(id) { return nodes[id]; }, createElement() { return element(); },
            querySelector() { return { getAttribute() { return 'csrf-token'; } }; } },
        parseGenBankAccessions() { return []; }, shouldAddGenBankLocations() { return false; },
        showStatus(message, level) { statuses.push({ message, level }); },
        updateAddSequencesButtonState() { nodes.add_sequences_btn.disabled = false; },
        addSequences(items) { queue.push(...items); return items.length; },
        reportWorkflowFailure() {}, httpError(response, message) { return new Error(message); },
        async fetch(url, options) {
            calls.push({ url, options, body: JSON.parse(options.body) });
            const data = await answer(calls.length);
            return { ok: true, async json() { return data; } };
        },
    });
    vm.runInContext('let fastaAddRunning = false;\n' + parser + handlers, context);
    return { nodes, queue, calls, statuses, run(code) { return vm.runInContext(code, context); } };
}

test('valid paste is checked before queueing and whitespace never becomes a base', async () => {
    const h = harness();
    h.nodes.sequence_text.value = '>one description\r\nA C\tGT\r\n';
    await h.nodes.add_sequences_btn.click();
    assert.equal(h.calls[0].url, '/api/fasta/check');
    assert.equal(h.calls[0].options.headers['X-CSRFToken'], 'csrf-token');
    assert.equal(h.calls[0].body.sequence, '>one description\r\nA C\tGT\r\n');
    assert.equal(h.queue.length, 1);
    assert.equal(h.queue[0].sequence, 'ACGT');
});

test('rejected input stays intact, empty records are not queued, and errors use text nodes', async () => {
    const h = harness(() => invalid);
    const raw = '>one\nACGT\n>empty\n';
    h.nodes.sequence_text.value = raw;
    await h.nodes.add_sequences_btn.click();
    assert.equal(h.queue.length, 0);
    assert.equal(h.nodes.sequence_text.value, raw);
    assert.equal(h.nodes['fasta-repair-dialog'].open, true);
    assert.equal(h.nodes['fasta-repair-apply'].disabled, true);
    const issue = h.nodes['fasta-repair-issues'].children[0];
    assert.equal(issue.innerHTML, '');
    assert.match(issue.textContent, /<img onerror/);
    assert.match(h.nodes['fasta-repair-summary'].textContent, /reference123/);
});

test('formatting preview changes input only after Apply, and Add remains a separate action', async () => {
    const raw = '>one\n1 ACGT 4\n', fixed = '>one\nACGT\n';
    const h = harness(n => n === 1 ? { ...invalid, repair: {
        valid: true, issues: [], sequence: fixed, changes: ['Remove line-number tokens.'],
    } } : valid);
    h.nodes.sequence_text.value = raw;
    await h.nodes.check_fasta_btn.click();
    assert.equal(h.nodes.sequence_text.value, raw);
    assert.equal(h.nodes['fasta-repair-original'].value, raw);
    assert.equal(h.nodes['fasta-repair-preview'].value, fixed);
    assert.equal(h.queue.length, 0);
    h.nodes['fasta-repair-apply'].click();
    assert.equal(h.nodes.sequence_text.value, fixed);
    assert.equal(h.queue.length, 0);
    await h.nodes.add_sequences_btn.click();
    assert.equal(h.calls[1].body.sequence, fixed);
    assert.equal(h.queue[0].sequence, 'ACGT');
});

test('an accession list gets the fetch prompt without FASTA issues or repair guidance', async () => {
    const h = harness(() => ({ ...invalid, diagnostic_id: null, input_kind: 'accessions', accession_count: 1,
        issues: [{ message: 'No FASTA records were found.' }],
        repair: { valid: false, issues: [{ message: 'No FASTA records were found.' }], sequence: 'AB123456\n', changes: ['Normalize.'] } }));
    h.nodes.sequence_text.value = 'AB123456';
    await h.nodes.check_fasta_btn.click();
    assert.match(h.nodes['fasta-repair-summary'].textContent, /list of 1 GenBank accession/);
    assert.equal(h.nodes['fasta-repair-issues'].children.length, 0);
    assert.equal(h.nodes['fasta-repair-changes'].children.length, 0);
    assert.equal(h.nodes['fasta-use-accessions'].classList.contains('hidden'), false);
    assert.equal(h.nodes['fasta-repair-apply'].classList.contains('hidden'), true);
    assert.equal(h.nodes['fasta-repair-apply'].disabled, true);
});

test('replacement text and a duplicate Add cannot be consumed by an in-flight check', async () => {
    let resolve;
    const h = harness(() => new Promise(done => { resolve = done; }));
    h.nodes.sequence_text.value = '>old\nACGT\n';
    const pending = h.nodes.add_sequences_btn.click();
    h.nodes.sequence_text.value = '>new\nTTAA\n';
    h.nodes.add_sequences_btn.disabled = false;
    await h.nodes.add_sequences_btn.click();
    assert.equal(h.calls.length, 1);
    resolve(valid); await pending;
    assert.equal(h.queue.length, 0);
    assert.equal(h.nodes.sequence_text.value, '>new\nTTAA\n');
});

test('a stale repair cannot overwrite text edited after its preview', async () => {
    const h = harness(() => ({ ...invalid, repair: {
        valid: true, issues: [], sequence: '>one\nACGT\n', changes: ['Remove numbering.'],
    } }));
    h.nodes.sequence_text.value = '>one\n1 ACGT\n';
    await h.nodes.check_fasta_btn.click();
    h.nodes.sequence_text.value = '>edited\nTTAA\n';
    h.nodes['fasta-repair-apply'].click();
    assert.equal(h.nodes.sequence_text.value, '>edited\nTTAA\n');
    assert.equal(h.queue.length, 0);
});

test('rejected upload sends the filename and cannot add its records', async () => {
    const h = harness(() => invalid);
    h.nodes.sequence_file.files = [{ name: 'problem.fa', async text() { return '>one\nACGT!\n'; } }];
    await h.nodes.sequence_file.change();
    assert.equal(h.calls[0].body.filename, 'problem.fa');
    assert.equal(h.calls[0].body.source, 'upload');
    assert.equal(h.nodes.add_file_btn.classList.contains('hidden'), true);
    h.nodes.add_file_btn.click();
    assert.equal(h.queue.length, 0);
});

test('an old file check cannot replace the newer file or show an obsolete repair', async () => {
    let finish;
    const h = harness(n => n === 1 ? new Promise(done => { finish = done; }) : valid);
    h.nodes.sequence_file.files = [{ name: 'old.fa', async text() { return '>old\nACGT!\n'; } }];
    const pending = h.nodes.sequence_file.change();
    await new Promise(resolve => setImmediate(resolve));
    h.nodes.sequence_file.files = [{ name: 'new.fa', async text() { return '>new\nTTAA\n'; } }];
    await h.nodes.sequence_file.change();
    finish(invalid); await pending;
    assert.equal(h.nodes['fasta-repair-dialog'].open, false);
    h.nodes.add_file_btn.click();
    assert.equal(h.queue.length, 1);
    assert.equal(h.queue[0].name, 'new');
    assert.equal(h.queue[0].sequence, 'TTAA');
});
