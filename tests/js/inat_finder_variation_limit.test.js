'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const repo = process.argv[2] || path.resolve(__dirname, '..', '..');
const template = fs.readFileSync(path.join(repo, 'app/templates/inat_finder.html'), 'utf8');
const helpersStart = template.indexOf('    function combinationCount');
const helpersEnd = template.indexOf('    async function resolveCriteria');

if (helpersStart < 0 || helpersEnd < 0) throw new Error('Finder variation helpers were not found');

const context = {result: null, log() {}};
vm.runInNewContext(
    `const MAX_VARIATIONS = 100000;\n${template.slice(helpersStart, helpersEnd)}\n` +
    'result = {estimateVariationCount, buildVariations};',
    context,
);

const normalEstimate = context.result.estimateVariationCount('123456789', 3);
if (normalEstimate !== 64278) {
    throw new Error(`Expected 64,278 variations for a normal nine-digit ID, got ${normalEstimate}`);
}

const hostileId = '1'.repeat(200);
const hostileEstimate = context.result.estimateVariationCount(hostileId, 3);
if (hostileEstimate <= 100000) {
    throw new Error(`Hostile input was estimated below the cap: ${hostileEstimate}`);
}

let rejected = false;
try {
    context.result.buildVariations(hostileId, 3);
} catch (error) {
    rejected = error && error.name === 'RangeError';
}
if (!rejected) throw new Error('buildVariations did not reject hostile input before generation');

const runSearch = template.slice(
    template.indexOf('    async function runSearch'),
    template.indexOf("    form.addEventListener('submit'"),
);
const capCheck = runSearch.indexOf('variationEstimate > MAX_VARIATIONS');
const criteriaLookup = runSearch.indexOf('await resolveCriteria');
if (capCheck < 0 || criteriaLookup < 0 || capCheck > criteriaLookup) {
    throw new Error('Search does not enforce the variation cap before its first API lookup');
}

console.log('PASS iNat Finder variation limit');
