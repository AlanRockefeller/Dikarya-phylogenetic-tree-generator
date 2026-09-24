/**
 * Runs the Quick Tree per-sequence ceiling as the browser runs it.
 *
 * quickTreeLengthCheckPasses() is extracted verbatim from sequence_entry.html
 * and executed here against a stub queue, so the test cannot drift away from
 * what is actually served. The backend enforces the same limit from
 * QUICK_TREE_MAX_SEQUENCE_BP in app/services/tree_parameter_validation.py; the
 * Python side of this harness asserts the two constants agree.
 *
 * Usage: node quick_tree_limit.test.js <path-to-extracted-script> <queue-json>
 * Prints {"passes": bool, "status": [[message, level], ...]} on stdout.
 */
// Deliberately NOT 'use strict': a direct eval() in strict mode gets its own
// scope, so the function declarations in the extracted script would not be
// visible here. Non-strict direct eval declares them in this module scope.

const fs = require('fs');

let sequenceQueue = JSON.parse(process.argv[3]);

const statuses = [];
function showStatus(message, level) {
    statuses.push([message, level]);
}

eval(fs.readFileSync(process.argv[2], 'utf8'));

const passes = quickTreeLengthCheckPasses();
process.stdout.write(JSON.stringify({ passes, status: statuses }));
