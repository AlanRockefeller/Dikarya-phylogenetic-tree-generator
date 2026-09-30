const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../../app/templates/sequence_entry.html'), 'utf8');
const start = html.indexOf('    function isValidMycomapUrl(url) {');
assert.ok(start >= 0);
const end = html.indexOf('    // Alan 8/14/26 - MycoMap sequence record pages', start);
assert.ok(end > start);
const context = {URL};
vm.runInNewContext(html.slice(start, end), context);

test('Tree Builder accepts the supported org result pages', () => {
    for (const url of [
        'https://mycomap.org/mycoblast/648539',
        'https://www.mycomap.org/blast-results/332221330/761935',
        'https://mycomap.org/admin/blast-results/332221330/761935',
        'https://mycomap.com/genetics/blast-search/r42/',
    ]) assert.equal(context.isValidMycomapUrl(url), true, url);
});

test('Tree Builder rejects org lookalikes and unrelated paths', () => {
    for (const url of [
        'https://mycomap.org.evil.test/mycoblast/1',
        'http://mycomap.org/mycoblast/1',
        'https://mycomap.org/other/1',
        'https://mycomap.org/mycoblast/nope',
        'https://mycomap.org/mycoblast/1?redirect=evil',
    ]) assert.equal(context.isValidMycomapUrl(url), false, url);
});
