/**
 * The annotation save must clear only the selection that created it.
 *
 * WHY A NODE HARNESS
 * ------------------
 * `saveCurrentAnnotation()` awaits `saveAnnotationsNow()`, which is a network
 * round trip. The user can select a different group while that is in flight,
 * and the completion handler used to clear whatever was selected when it
 * resumed -- silently throwing away the newer selection. The guard is three
 * lines, so the only thing worth testing is that the SHIPPED lines behave, and
 * the only way to be sure of that is to run them: this harness slices the real
 * `sameTipIdSet()` and the real post-save block out of
 * tree_viewer_controller.js and executes them against stand-in viewers.
 *
 * Usage: node annotation_selection_race.test.js <repo-root> [--json]
 */
'use strict';

const fs = require('fs');
const vm = require('vm');
const path = require('path');
const assert = require('assert');

const REPO = process.argv[2] || path.resolve(__dirname, '..', '..');
const AS_JSON = process.argv.includes('--json');
const CONTROLLER = path.join(REPO, 'app', 'static', 'js', 'tree_viewer_controller.js');

// ---------------------------------------------------------------------------
// Slice the shipped source. Both markers are asserted so a rename fails loudly
// here rather than quietly testing nothing.
// ---------------------------------------------------------------------------
function blockAt(source, marker) {
    const start = source.indexOf(marker);
    assert.notStrictEqual(start, -1, `controller marker drifted: ${marker}`);
    let depth = 0;
    for (let i = source.indexOf('{', start); i < source.length; i += 1) {
        if (source[i] === '{') depth += 1;
        else if (source[i] === '}') {
            depth -= 1;
            if (depth === 0) return source.slice(start, i + 1);
        }
    }
    throw new Error(`unbalanced braces after ${marker}`);
}

const SOURCE = fs.readFileSync(CONTROLLER, 'utf8');
const HELPER = blockAt(SOURCE, 'function sameTipIdSet(a, b)');
const GUARD = blockAt(SOURCE, 'if (wasAdd && viewer?.deselectCurrentSelection)');

// The guard must consult the CURRENT selection, not the one captured before the
// await. Pinned separately because a revert would still pass every case below
// if it simply stopped calling this.
assert.ok(GUARD.includes('getSelectedAnnotationLeafIds'),
          'the post-save guard no longer reads the current selection');

const context = vm.createContext({});
vm.runInContext(`
${HELPER}
function runPostSave(viewer, payload, wasAdd, updateButtons) {
${GUARD}
}
globalThis.sameTipIdSet = sameTipIdSet;
globalThis.runPostSave = runPostSave;
`, context, { filename: 'annotation_selection_race (sliced)' });

function makeViewer(currentSelection) {
    const calls = { deselect: 0, buttons: 0 };
    const viewer = {
        deselectCurrentSelection() { calls.deselect += 1; return 1; },
    };
    if (currentSelection !== undefined) {
        viewer.getSelectedAnnotationLeafIds = () => currentSelection.slice();
    }
    return { viewer, calls };
}

function postSave(currentSelection, memberIds, { wasAdd = true } = {}) {
    const { viewer, calls } = makeViewer(currentSelection);
    context.runPostSave(
        viewer, { member_tip_ids: memberIds }, wasAdd, () => { calls.buttons += 1; },
    );
    return calls;
}

// ---------------------------------------------------------------------------
const GROUPS = {};
function group(name, fn) {
    try {
        fn();
        GROUPS[name] = { ok: true };
    } catch (error) {
        GROUPS[name] = { ok: false, error: error.message };
    }
}

group('clears-the-selection-it-annotated', () => {
    const calls = postSave(['A', 'B', 'C'], ['A', 'B', 'C']);
    assert.strictEqual(calls.deselect, 1, 'the annotated selection was not cleared');
    assert.strictEqual(calls.buttons, 1, 'the buttons were not refreshed');
});

group('ordering-is-irrelevant', () => {
    // The viewer's selection order is not meaningful; only membership is.
    const calls = postSave(['C', 'A', 'B'], ['A', 'B', 'C']);
    assert.strictEqual(calls.deselect, 1, 'a reordered but identical selection was kept');
});

group('keeps-a-newer-selection-made-during-the-save', () => {
    const calls = postSave(['X', 'Y'], ['A', 'B', 'C']);
    assert.strictEqual(calls.deselect, 0, "the user's newer selection was cleared");
    assert.strictEqual(calls.buttons, 0, 'the buttons were refreshed for a no-op');
});

group('a-superset-is-a-different-selection', () => {
    const calls = postSave(['A', 'B', 'C', 'D'], ['A', 'B', 'C']);
    assert.strictEqual(calls.deselect, 0, 'an extended selection was cleared');
});

group('a-subset-is-a-different-selection', () => {
    const calls = postSave(['A', 'B'], ['A', 'B', 'C']);
    assert.strictEqual(calls.deselect, 0, 'a narrowed selection was cleared');
});

group('a-selection-cleared-during-the-save-stays-cleared', () => {
    const calls = postSave([], ['A', 'B', 'C']);
    assert.strictEqual(calls.deselect, 0, 'an already-empty selection was re-cleared');
});

group('an-edit-never-touches-the-selection', () => {
    // Edit is not opened from a selection, so it has none to drop.
    const calls = postSave(['A', 'B', 'C'], ['A', 'B', 'C'], { wasAdd: false });
    assert.strictEqual(calls.deselect, 0, 'an edit cleared the selection');
});

group('an-older-viewer-without-the-accessor-still-clears', () => {
    // Backwards compatible: no way to compare means the pre-fix behaviour.
    const calls = postSave(undefined, ['A', 'B', 'C']);
    assert.strictEqual(calls.deselect, 1, 'the fallback stopped clearing entirely');
});

group('set-equality-ignores-duplicates-and-order', () => {
    assert.strictEqual(context.sameTipIdSet(['A', 'A', 'B'], ['B', 'A']), true);
    assert.strictEqual(context.sameTipIdSet(['A', 'B'], ['A', 'C']), false);
    assert.strictEqual(context.sameTipIdSet([], []), true);
    assert.strictEqual(context.sameTipIdSet(null, []), true);
    assert.strictEqual(context.sameTipIdSet(['A'], []), false);
});

if (AS_JSON) {
    process.stdout.write(JSON.stringify(GROUPS));
} else {
    for (const [name, result] of Object.entries(GROUPS)) {
        process.stdout.write(`${result.ok ? 'ok  ' : 'FAIL'} ${name}` +
                             `${result.ok ? '' : ': ' + result.error}\n`);
    }
}
process.exit(Object.values(GROUPS).every(r => r.ok) ? 0 : 1);
