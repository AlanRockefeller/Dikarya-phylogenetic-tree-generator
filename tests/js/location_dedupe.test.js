/**
 * Runs the queue's location de-duplication as the browser runs it.
 *
 * Two shipped code paths decide whether a record's location is already in its
 * header: the queue row rendered by updateQueueDisplay(), and the FASTA header
 * built by buildFastaHeaderFromQueueItem(). Both are extracted verbatim from
 * sequence_entry.html and executed here against a stub DOM, so the test cannot
 * drift away from what is actually served.
 *
 * Usage: node location_dedupe.test.js <path-to-extracted-script> <cases-json>
 */
// Deliberately NOT 'use strict': a direct eval() in strict mode gets its own
// scope, so the function declarations in the extracted script would not be
// visible here. Non-strict direct eval declares them in this module scope.

const fs = require('fs');

// --- A DOM stub with exactly the surface updateQueueDisplay() touches.
function stubElement() {
    return {
        classList: { add() {}, remove() {}, toggle() {} },
        dataset: {},
        textContent: '',
        innerHTML: '',
        disabled: false,
        value: '',
    };
}

const nodes = {};
const document = {
    getElementById(id) {
        if (!nodes[id]) nodes[id] = stubElement();
        return nodes[id];
    },
};

// --- Collaborators of the render path that are not under test here.
let sequenceQueue = [];
let selectedQueueOutgroup = '';
function getSelectedOutgroupName() { return selectedQueueOutgroup; }
function hasFilterableSequences() { return false; }
function updateFilterButtonVisuals() {}
function populateOutgroupDropdown() {}
function escapeHtml(text) { return text === undefined || text === null ? '' : String(text); }

// eslint-disable-next-line no-eval
eval(fs.readFileSync(process.argv[2], 'utf8'));

// The location the queue row actually displays, or '' when it shows none.
function renderedLocation(seq) {
    sequenceQueue = [seq];
    nodes.sequence_queue_list = stubElement();
    updateQueueDisplay();
    const match = /<span class="queue-location">([^<]*)<\/span>/.exec(
        nodes.sequence_queue_list.innerHTML
    );
    return match ? match[1] : '';
}

const out = JSON.parse(process.argv[3]).map(seq => ({
    shown: renderedLocation(seq),
    header: buildFastaHeaderFromQueueItem(seq),
}));
console.log(JSON.stringify(out));
