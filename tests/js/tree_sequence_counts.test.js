const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/templates/sequence_entry.html', 'utf8');
const start = source.indexOf('    const TREE_MIN_SEQUENCES = ');
const end = source.indexOf('    const QUICK_TREE_MAX_SEQUENCE_BP', start);
assert.ok(start >= 0 && end > start);
const statuses = [];
const context = vm.createContext({sequenceQueue: [], showStatus: (...args) => statuses.push(args)});
vm.runInContext(source.slice(start, end), context);
const expected = {iqtree: 3, iqtree_fast: 3, raxml: 4, mrbayes: 4, fasttree: 2, nj: 2};
assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(TREE_MIN_SEQUENCES)', context)), expected);
for (const [method, minimum] of Object.entries(expected)) {
    for (let count = 1; count <= 4; count++) {
        context.sequenceQueue = Array.from({length: count}, (_, index) => ({name: 'seq' + index}));
        statuses.length = 0;
        const allowed = vm.runInContext("treeSequenceCountCheckPasses('" + method + "')", context);
        assert.equal(allowed, count >= minimum, method + ': ' + count);
        assert.equal(statuses.length, allowed ? 0 : 1);
        if (!allowed) assert.match(statuses[0][0], new RegExp('at least ' + minimum + ' sequences'));
        assert.equal(context.sequenceQueue.length, count);
    }
}
console.log('Browser sequence-count validation checks passed');
