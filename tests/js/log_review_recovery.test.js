const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/static/js/tree_viewer_controller.js', 'utf8');
const undoScript = source.slice(source.indexOf('    let serverUndoState = null;'), source.indexOf('    // Apply whatever the single Undo slot'));
function undoContext(fetchState) {
    const timers = new Map(), events = {};
    let next = 0;
    const button = {classList: {toggle() {}}};
    const context = vm.createContext({
        JOB_ID: 'test', viewer: null, isProcessing: false, btnUndo: button, btnMobileUndo: null,
        TreeEditActions: {getUndoState: fetchState}, navigator: {onLine: true},
        document: {hidden: false, addEventListener: (k, fn) => events[k] = fn},
        window: {addEventListener: (k, fn) => events[k] = fn},
        setTimeout: (fn, ms) => {const id = ++next; timers.set(id, {fn, ms}); return id;},
        clearTimeout: id => timers.delete(id),
    });
    vm.runInContext(undoScript, context);
    return {context, timers, events, button, refresh: () => vm.runInContext('refreshServerUndoState()', context)};
}
const flush = () => new Promise(resolve => setImmediate(resolve));
async function undoTests() {
    let calls = 0;
    const u = undoContext(async () => {if (++calls === 1) throw new TypeError('offline'); return {can_undo: true};});
    await u.refresh();
    assert.equal(u.button.disabled, false);
    assert.equal(vm.runInContext('currentUndoTarget()', u.context), null);
    const [id, timer] = [...u.timers][0]; u.timers.delete(id);
    await timer.fn();
    assert.equal(u.button.disabled, false);
    assert.equal(u.timers.size, 0);
    const forbidden = undoContext(async () => {throw Object.assign(new Error('forbidden'), {status: 403});});
    await forbidden.refresh(); assert.equal(forbidden.timers.size, 0);
    let rejectOld;
    const race = undoContext(() => !rejectOld ? new Promise((_, reject) => rejectOld = reject) : Promise.resolve({can_undo: true}));
    const old = race.refresh(); await race.refresh(); rejectOld(new TypeError('stale')); await old;
    assert.equal(race.button.disabled, false); assert.equal(race.timers.size, 0);
    let offline = true;
    const reconnect = undoContext(async () => {if (offline) throw new TypeError('offline'); return {can_undo: true};});
    reconnect.context.document.hidden = true;
    await reconnect.refresh(); assert.equal(reconnect.timers.size, 0);
    offline = false; reconnect.context.document.hidden = false;
    reconnect.events.visibilitychange(); await flush(); assert.equal(reconnect.button.disabled, false);
    let boundedCalls = 0;
    const bounded = undoContext(async () => {
        if (++boundedCalls <= 4) throw new TypeError('offline');
        return {can_undo: true};
    });
    await bounded.refresh();
    for (let n = 0; n < 3; n++) {const [id, timer] = [...bounded.timers][0]; bounded.timers.delete(id); await timer.fn();}
    assert.equal(bounded.timers.size, 0);
    assert.equal(bounded.button.disabled, false);
    assert.match(bounded.button.title, /retry/i);
    assert.equal(vm.runInContext('currentUndoTarget()', bounded.context), null);
    await bounded.refresh();
    assert.equal(bounded.button.disabled, false);
    assert.equal(vm.runInContext('currentUndoTarget().kind', bounded.context), 'server');
}
async function pollTests() {
    const html = fs.readFileSync('app/templates/sequence_entry.html', 'utf8');
    const script = html.slice(html.indexOf('    const BACKGROUND_POLL_MS'), html.indexOf('    function reportWorkflowFailure'));
    const delays = [];
    const started = {status: 202, clone: () => ({json: async () => ({task_id: 'task'})})};
    const limited = {status: 429, headers: {get: () => '12'}};
    const ready = {status: 200};
    const replies = [started, limited, ready];
    const ctx = vm.createContext({
        fetch: async () => replies.shift(), Date, Number,
        setTimeout: (fn, ms) => {delays.push(ms); queueMicrotask(fn); return delays.length;}, clearTimeout() {},
        document: {hidden: false, addEventListener() {}, removeEventListener() {}},
    });
    vm.runInContext(script, ctx);
    assert.equal(await vm.runInContext("fetchInBackground('/preview')", ctx), ready);
    assert.deepEqual(delays, [2000, 12000]);
    // Without Retry-After, repeated throttling backs off instead of hammering.
    limited.headers.get = () => null;
    replies.push(started, limited, limited, ready); delays.length = 0;
    await vm.runInContext("fetchInBackground('/preview')", ctx);
    assert.deepEqual(delays, [2000, 4000, 8000]);
}
(async () => {await undoTests(); await pollTests(); console.log('Undo recovery and background polling checks passed');})().catch(e => {console.error(e); process.exitCode = 1;});
