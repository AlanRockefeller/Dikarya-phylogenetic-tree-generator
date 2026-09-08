/**
 * Runs the Tree Builder queue's duplicate detection as the browser runs it.
 *
 * addSequences() and the dedup-key helpers are extracted verbatim from
 * sequence_entry.html and executed here against stubs for the render path, so
 * the test cannot drift away from what is actually served.
 *
 * Usage: node queue_duplicate_merge.test.js <path-to-extracted-script> <batches-json>
 */
// Deliberately NOT 'use strict': a direct eval() in strict mode gets its own
// scope, so the function declarations in the extracted script would not be
// visible here.

const fs = require('fs');

// Location normalization tables the extracted slice reads but does not define.
const US_STATE_TO_ABBR = { california: 'CA', oregon: 'OR' };
const US_STATE_ABBRS = new Set(Object.values(US_STATE_TO_ABBR));

// Collaborators of the render/diagnostics paths that are not under test here.
let sequenceQueue = [];
const duplicateRows = [];
function updateQueueDisplay() {}
function recordQueueDuplicate(row) { duplicateRows.push(row); }

// eslint-disable-next-line no-eval
eval(fs.readFileSync(process.argv[2], 'utf8'));

const batches = JSON.parse(process.argv[3]);
const addedPerBatch = batches.map(batch => addSequences(batch));

process.stdout.write(JSON.stringify({
    added_per_batch: addedPerBatch,
    queue: sequenceQueue.map(seq => ({
        name: String(seq.name || ''),
        sequence: String(seq.sequence || ''),
        merged_ids: seq.merged_ids || null,
        dedup_primary_id: seq.dedup_primary_id || null,
    })),
    duplicate_rows: duplicateRows,
}));
