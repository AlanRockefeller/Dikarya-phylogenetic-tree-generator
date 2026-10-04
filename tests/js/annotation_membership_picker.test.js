'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = process.argv[2] || path.resolve(__dirname, '..', '..');
const source = fs.readFileSync(path.join(root, 'app/static/js/tree_viewer_controller.js'), 'utf8');

function functionAt(marker) {
    const start = source.indexOf(marker);
    assert.notEqual(start, -1, `missing ${marker}`);
    let depth = 0;
    for (let i = source.indexOf('{', start); i < source.length; i += 1) {
        if (source[i] === '{') depth += 1;
        if (source[i] === '}' && --depth === 0) return source.slice(start, i + 1);
    }
    throw new Error(`unbalanced ${marker}`);
}

const names = [
    'function sameTipIdSet(a, b)',
    'function updateAnnotationMembershipPicker()',
    'function startAnnotationMembershipPicker(annotationId)',
    'function closeAnnotationMembershipPicker(restoreSelection, force = false)',
    'async function saveAnnotationMembershipPicker()'
];
const code = names.map(functionAt).join('\n');

async function scenario({ type = 'clade_line', initial = ['A', 'B'], selected = ['A', 'B', 'C'],
                          clade = selected, saveResult = true } = {}) {
    const elements = Object.fromEntries([
        'annotation-membership-status', 'btn-annotation-membership-save',
        'btn-annotation-membership-cancel',
        'annotation-membership-label', 'annotation-membership-picker'
    ].map(id => [id, { textContent: '', disabled: false, classList: { add() {}, remove() {} } }]));
    const calls = { saves: 0, statuses: [], managerClosed: 0 };
    let selection = ['X'];
    const original = { id: 'ann', label: 'Group', layer_id: 'layer', annotation_type: type,
        member_tip_ids: initial, font_size: 14 };
    const ctx = vm.createContext({
        annotationMembershipPicker: null,
        cladeAnnotations: [original],
        viewer: {
            selectLeafIds(ids) { selection = ids.slice(); return selection.length; },
            getSelectedAnnotationLeafIds() { return selection.slice(); },
            getSelectedCladeLeafIds() { return clade ? selection.slice() : null; },
            hasIncomingBranchForMemberIds() { return true; }
        },
        annotationsEditable: () => true,
        getEl: id => elements[id],
        closeAnnotationManager: () => { calls.managerClosed += 1; },
        updateButtons: () => {},
        showStatus: message => calls.statuses.push(message),
        canonicalAnnotationType: value => value,
        CLADE_ANNOTATION_TYPES: ['clade_line', 'clade_highlight'],
        saveAnnotationsNow: async () => { calls.saves += 1; return saveResult; }
    });
    vm.runInContext(code, ctx);
    ctx.startAnnotationMembershipPicker('ann');
    assert.deepEqual(selection, initial);
    selection = selected.slice();
    return { ctx, calls, original, elements, selection: () => selection };
}

(async () => {
    const normal = await scenario();
    await normal.ctx.saveAnnotationMembershipPicker();
    assert.equal(normal.calls.saves, 1);
    assert.equal(normal.ctx.cladeAnnotations[0].id, 'ann');
    assert.equal(normal.ctx.cladeAnnotations[0].font_size, 14);
    assert.deepEqual(Array.from(normal.ctx.cladeAnnotations[0].member_tip_ids), ['A', 'B', 'C']);
    assert.equal(normal.ctx.annotationMembershipPicker, null);

    const canceled = await scenario();
    canceled.ctx.closeAnnotationMembershipPicker(true);
    assert.deepEqual(canceled.selection(), ['X']);
    assert.equal(canceled.calls.saves, 0);

    const selectedGroup = await scenario({ clade: null });
    await selectedGroup.ctx.saveAnnotationMembershipPicker();
    assert.equal(selectedGroup.calls.saves, 0);
    assert.equal(selectedGroup.elements['btn-annotation-membership-save'].textContent,
        'Save selected group anyway');
    await selectedGroup.ctx.saveAnnotationMembershipPicker();
    assert.equal(selectedGroup.calls.saves, 1);
    assert.equal(selectedGroup.ctx.cladeAnnotations[0].membership_mode, 'selection');

    const branch = await scenario({ type: 'branch_text', clade: null });
    branch.ctx.updateAnnotationMembershipPicker();
    await branch.ctx.saveAnnotationMembershipPicker();
    assert.equal(branch.calls.saves, 0);
    assert.equal(branch.elements['btn-annotation-membership-save'].disabled, true);

    const failed = await scenario({ saveResult: false });
    await failed.ctx.saveAnnotationMembershipPicker();
    assert.notEqual(failed.ctx.annotationMembershipPicker, null);
    assert.equal(failed.ctx.annotationMembershipPicker.saving, false);

    let finishSave;
    const pending = await scenario({ saveResult: new Promise(resolve => { finishSave = resolve; }) });
    const saving = pending.ctx.saveAnnotationMembershipPicker();
    pending.ctx.closeAnnotationMembershipPicker(true);
    assert.notEqual(pending.ctx.annotationMembershipPicker, null);
    assert.equal(pending.elements['btn-annotation-membership-cancel'].disabled, true);
    finishSave(true);
    await saving;
    assert.equal(pending.ctx.annotationMembershipPicker, null);
    process.stdout.write('annotation membership picker: ok\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
