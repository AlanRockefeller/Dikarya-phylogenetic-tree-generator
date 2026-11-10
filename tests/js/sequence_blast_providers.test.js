/** Run the shipped Tree Builder BLAST handlers: node --test tests/js/sequence_blast_providers.test.js */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname, '../../app/templates/sequence_entry.html'), 'utf8');
const parsing = template.slice(template.indexOf('    function parseFasta('),
    template.indexOf('    // GenBank Collection Locations'));
const handlers = template.slice(template.indexOf('    // NCBI and MycoMap BLAST searches'),
    template.indexOf("    document.getElementById('add_sequences_btn').addEventListener('click'"));
const mergeFilters = template.slice(template.indexOf('    function mergeImportFilterDetails('),
    template.indexOf('    // Alan 9/6/26 - Duplicates the queue collapsed'));
const DNA = 'ACGT'.repeat(20);

function harness(fetchResult) {
    const nodes = {};
    const calls = [];
    const statuses = [];
    const queue = [];
    const opened = [];
    const failures = [];
    function element(id) {
        const classes = new Set();
        const listeners = {};
        return {
            id, value: '', disabled: false, innerHTML: '', title: '',
            classList: {
                toggle(name, force) {
                    const add = force === undefined ? !classes.has(name) : force;
                    if (add) classes.add(name); else classes.delete(name);
                    return add;
                },
                contains(name) { return classes.has(name); },
            },
            addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
            dispatchEvent(event) { return Promise.all((listeners[event.type] || []).map(fn => fn(event))); },
            click() { return this.disabled ? Promise.resolve() : this.dispatchEvent({ type: 'click' }); },
        };
    }
    [
        'blast_btn', 'blast_query', 'blast_min_identity', 'blast_max_sequences',
        'mycomap_blast_btn', 'mycomap_blast_query', 'mycomap_blast_min_identity', 'mycomap_blast_max_sequences',
        'add_sequences_btn', 'sequence_text', 'genbank_location_option', 'genbank_add_locations',
        'paste_blast_btn', 'paste_mycomap_blast_btn',
    ].forEach(id => { nodes[id] = element(id); });
    ['blast', 'mycomap_blast'].forEach(source => {
        nodes[`${source}_min_identity`].value = '90';
        nodes[`${source}_max_sequences`].value = '50';
    });
    const context = vm.createContext({
        URL, Event,
        document: {
            getElementById(id) { return nodes[id] || null; },
            querySelector() { return { getAttribute() { return 'csrf-test'; } }; },
        },
        window: { openImportSection(source) { opened.push(source); } },
        updateSequenceInputMeta() {}, updateButtonVisuals() {},
        collectAccessionsForLocationLookup() { return []; },
        showStatus(message, level) { statuses.push({ message, level }); },
        reportWorkflowFailure(action, error) { failures.push({ action, error }); },
        httpError(response, message) { return Object.assign(new Error(message), { httpStatus: response.status }); },
        addSequences(sequences) { queue.push(...sequences); return sequences.length; },
        async fetchInBackground(url, options) {
            calls.push({ url, options, body: JSON.parse(options.body) });
            return fetchResult ? fetchResult(calls.length) : response({ sequences: [] });
        },
    });
    vm.runInContext('let lastBlastQuery = null; let lastMycomapBlastUrl = ""; let importFilterDetails = {};\n' +
        parsing + mergeFilters + handlers, context);
    return {
        nodes, calls, statuses, queue, opened, failures,
        input(id, value) { nodes[id].value = value; return nodes[id].dispatchEvent({ type: 'input' }); },
        run(code) { return vm.runInContext(code, context); },
    };
}

function response(data, status = 200) {
    return { ok: status >= 200 && status < 300, status, async json() { return data; } };
}

test('both providers accept raw DNA, one FASTA record, accessions and NCBI record URLs', () => {
    const h = harness();
    for (const source of ['blast', 'mycomap_blast']) {
        for (const value of [DNA, `>sample ITS\n${DNA}`, 'OR807397.1', 'https://www.ncbi.nlm.nih.gov/nuccore/OR807397.1']) {
            h.input(`${source}_query`, value);
            assert.equal(h.nodes[`${source}_btn`].disabled, false, `${source}: ${value}`);
        }
        for (const value of ['OR807397\nOR807398', `>one\n${DNA}\n>two\n${DNA}`, `>one\n${DNA}!`, '<script>alert(1)</script>']) {
            h.input(`${source}_query`, value);
            assert.equal(h.nodes[`${source}_btn`].disabled, true, `${source}: ${value}`);
        }
    }
});

test('MycoMap enforces 50–20,000 bp while NCBI keeps its 20 bp minimum', () => {
    const h = harness();
    for (const [length, allowed] of [[49, false], [50, true], [20000, true], [20001, false]]) {
        h.input('mycomap_blast_query', 'A'.repeat(length));
        assert.equal(h.nodes.mycomap_blast_btn.disabled, !allowed, `${length} bp`);
    }
    h.input('blast_query', 'A'.repeat(20));
    assert.equal(h.nodes.blast_btn.disabled, false);
    h.input('sequence_text', 'A'.repeat(20));
    assert.equal(h.nodes.paste_blast_btn.classList.contains('hidden'), false);
    assert.equal(h.nodes.paste_mycomap_blast_btn.classList.contains('hidden'), true);
});

test('NCBI sends a normalized accession to its own endpoint with settings and CSRF', async () => {
    const h = harness(() => response({ sequences: [{ name: 'NCBI hit', sequence: DNA }] }));
    h.nodes.blast_min_identity.value = '95';
    h.nodes.blast_max_sequences.value = '10';
    await h.input('blast_query', 'https://www.ncbi.nlm.nih.gov/nuccore/OR807397.1');
    await h.nodes.blast_btn.click();
    assert.equal(h.calls[0].url, '/api/blast');
    assert.deepEqual(h.calls[0].body, { query: 'OR807397.1', min_identity: '95', max_sequences: '10' });
    assert.equal(h.calls[0].options.headers['X-CSRFToken'], 'csrf-test');
    assert.equal(h.queue[0].name, 'NCBI hit');
    assert.equal(h.nodes.blast_query.value, '');
});

test('MycoMap Paste shortcut queues returned metrics and saves its MycoMap.org provenance', async () => {
    const hit = { name: 'MycoMap hit', sequence: DNA, source: 'mycomap', identity: 99, blast_metrics_available: true };
    const filters = { mycomap: { counts: { contaminant: 1 }, filtered_records: [{ name: 'excluded contaminant' }] } };
    const h = harness(() => response({ sequences: [hit], import_filter_details: filters,
        mycomap_blast_url: 'https://mycomap.org/mycoblast/123', warnings: ['One hit was unavailable.'] }));
    await h.input('sequence_text', `>my_query\n${DNA}`);
    assert.equal(h.nodes.paste_mycomap_blast_btn.classList.contains('hidden'), false);
    await h.nodes.paste_mycomap_blast_btn.click();
    assert.deepEqual(h.opened, ['mycomap_blast']);
    assert.equal(h.calls[0].url, '/api/mycomap/blast');
    assert.equal(h.calls[0].body.query, DNA);
    assert.deepEqual(h.queue, [hit]);
    assert.equal(h.run('lastMycomapBlastUrl'), 'https://mycomap.org/mycoblast/123');
    assert.equal(h.run('importFilterDetails.mycomap.counts.contaminant'), 1);
    assert.equal(h.run('importFilterDetails.mycomap.filtered_records[0].name'), 'excluded contaminant');
    assert.equal(h.nodes.sequence_text.value, '');
    assert.equal(h.nodes.mycomap_blast_query.value, '');
    assert.match(h.statuses.at(-1).message, /One hit was unavailable/);
});

test('failure preserves pasted and provider input, restores buttons and permits retry', async () => {
    const h = harness(attempt => attempt === 1
        ? response({ error: 'MycoMap is temporarily unavailable.' }, 503)
        : response({ sequences: [{ name: 'retry hit', sequence: DNA }] }));
    await h.input('sequence_text', DNA);
    await h.nodes.paste_mycomap_blast_btn.click();
    assert.equal(h.nodes.sequence_text.value, DNA);
    assert.equal(h.nodes.mycomap_blast_query.value, DNA);
    assert.equal(h.nodes.mycomap_blast_btn.disabled, false);
    assert.equal(h.nodes.paste_mycomap_blast_btn.disabled, false);
    assert.match(h.statuses.at(-1).message, /MycoMap is temporarily unavailable/);
    await h.nodes.paste_mycomap_blast_btn.click();
    assert.equal(h.calls.length, 2);
    assert.equal(h.queue.length, 1);
});

test('one running request guards both providers and shortcuts even after input changes', async () => {
    let finish;
    const h = harness(() => new Promise(resolve => { finish = resolve; }));
    await h.input('mycomap_blast_query', DNA);
    const pending = h.nodes.mycomap_blast_btn.click();
    await h.input('blast_query', 'OR807397');
    await h.input('sequence_text', 'OR807398');
    assert.equal(h.nodes.mycomap_blast_btn.disabled, true);
    assert.equal(h.nodes.blast_btn.disabled, true);
    assert.equal(h.nodes.paste_blast_btn.disabled, true);
    assert.equal(h.nodes.paste_mycomap_blast_btn.disabled, true);
    await h.run("runBlastSearch('blast')");
    await h.run("startPasteBlast('mycomap_blast')");
    assert.equal(h.calls.length, 1);
    finish(response({ sequences: [] }));
    await pending;
    assert.equal(h.nodes.blast_btn.disabled, false);
    assert.equal(h.nodes.sequence_text.value, 'OR807398');
});

test('Paste shortcuts preserve a different typed destination and make no request', async () => {
    const h = harness();
    await h.input('mycomap_blast_query', DNA);
    await h.input('sequence_text', 'OR807397');
    await h.nodes.paste_mycomap_blast_btn.click();
    assert.equal(h.nodes.mycomap_blast_query.value, DNA);
    assert.equal(h.nodes.sequence_text.value, 'OR807397');
    assert.equal(h.calls.length, 0);
    assert.match(h.statuses.at(-1).message, /already has a query/);
});

test('a completed request preserves replacement input typed while the search runs', async () => {
    let finish;
    const h = harness(() => new Promise(resolve => { finish = resolve; }));
    await h.input('sequence_text', DNA);
    const pending = h.nodes.paste_blast_btn.click();
    await h.input('sequence_text', 'OR807397');
    await h.input('blast_query', 'OR807398');
    finish(response({ sequences: [] }));
    await pending;
    assert.equal(h.nodes.sequence_text.value, 'OR807397');
    assert.equal(h.nodes.blast_query.value, 'OR807398');
});

test('a timed-out NCBI search offers Continue and preserves the same query and settings', async () => {
    const h = harness(attempt => attempt === 1
        ? response({error: 'NCBI is still working.', can_resume: true}, 504)
        : response({sequences: [{name: 'continued result', sequence: DNA}]}));
    await h.input('blast_query', DNA);
    await h.nodes.blast_btn.click();
    assert.match(h.nodes.blast_btn.innerHTML, /Continue NCBI BLAST/);
    assert.equal(h.nodes.blast_query.value, DNA);
    await h.nodes.blast_btn.click();
    assert.deepEqual(h.calls[0].body, h.calls[1].body);
    assert.equal(h.queue[0].name, 'continued result');
});

test('a pending NCBI response returns control without queueing or reporting an error', async () => {
    const h = harness(attempt => attempt === 1
        ? response({status: 'pending', message: 'Your search is saved.', can_resume: true}, 202)
        : response({sequences: [{name: 'continued result', sequence: DNA}]}));
    await h.input('blast_query', DNA);
    await h.nodes.blast_btn.click();
    assert.equal(h.queue.length, 0);
    assert.equal(h.nodes.blast_btn.disabled, false);
    assert.match(h.nodes.blast_btn.innerHTML, /Continue NCBI BLAST/);
    assert.equal(h.statuses.at(-1).level, 'info');
    assert.equal(h.failures.at(-1).error.expected, true);
    await h.nodes.blast_btn.click();
    assert.deepEqual(h.calls[0].body, h.calls[1].body);
    assert.equal(h.queue[0].name, 'continued result');
});

test('known MycoMap refusal disables both entry points and keeps NCBI available', async () => {
    const h = harness();
    await h.input('mycomap_blast_query', DNA);
    await h.input('blast_query', DNA);
    await h.input('sequence_text', DNA);
    h.run('mycomapSearchAvailable = false; updateBlastButtonState(); updateAddSequencesButtonState();');
    assert.equal(h.nodes.mycomap_blast_btn.disabled, true);
    assert.equal(h.nodes.paste_mycomap_blast_btn.disabled, true);
    assert.equal(h.nodes.blast_btn.disabled, false);
    assert.equal(h.nodes.paste_blast_btn.disabled, false);
    await h.run("runBlastSearch('mycomap_blast')");
    assert.equal(h.calls.length, 0);
});
