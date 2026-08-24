/**
 * Runs the browser's voucher-number parser (extracted verbatim from
 * voucher_labels.html) over a set of cases and prints the labels as JSON, so a
 * Python test can compare them against _voucher_format_label().
 *
 * Client and server must agree exactly: the preview and the generated sheet
 * carry the same identifiers.
 *
 * Usage: node voucher_number.test.js <path-to-extracted-script> <cases-json>
 */
// Deliberately NOT 'use strict': a direct eval() in strict mode gets its own
// scope, so the function declarations in the extracted script would not be
// visible here. Non-strict direct eval declares them in this module scope.

const fs = require('fs');

const startInput = { value: '' };
const prefixInput = { value: '' };
const startNumberNotice = null;
const clampValue = (v, fallback, lo, hi) =>
    Math.max(lo, Math.min(hi, Number.isFinite(parseFloat(v)) ? parseFloat(v) : fallback));

// eslint-disable-next-line no-eval
eval(fs.readFileSync(process.argv[2], 'utf8'));

const out = JSON.parse(process.argv[3]).map(([raw, prefix, offset]) => {
    startInput.value = raw;
    prefixInput.value = prefix;
    const parts = startNumberParts();
    return { label: buildPreviewValue(offset), tooLong: parts.tooLong, width: parts.width };
});
console.log(JSON.stringify(out));
