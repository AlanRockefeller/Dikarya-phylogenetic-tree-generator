/**
 * Drives the Downloads-dropdown script extracted verbatim from job_status.html
 * against a minimal DOM stub.
 *
 * The invariant under test: the menu's visible state and the button's
 * aria-expanded are always equal, through every way the menu can be opened or
 * closed. The earlier version let `group-focus-within:block` force the menu
 * visible while the toggle had already set aria-expanded="false", and the menu
 * could not be closed at all while the trigger kept focus.
 *
 * The desktop hover path is exercised explicitly. Checking click-open/click-close
 * from a cold start passed while the real bug was live: on a real pointer device
 * the pointer is inside the wrapper when you click, `visible = pinned || hovering`
 * kept the menu on screen, and the second click appeared to do nothing.
 *
 * Usage: node downloads_dropdown.test.js <path-to-extracted-script>
 */
'use strict';

const fs = require('fs');

function makeEl(id, classes) {
    const el = {
        id,
        _classes: new Set(classes),
        _attrs: {},
        _handlers: {},
        parent: null,
        focused: false,
        classList: {
            toggle(c, force) { force ? el._classes.add(c) : el._classes.delete(c); },
            contains(c) { return el._classes.has(c); },
        },
        setAttribute(k, v) { el._attrs[k] = v; },
        getAttribute(k) { return el._attrs[k]; },
        addEventListener(t, fn) { (el._handlers[t] = el._handlers[t] || []).push(fn); },
        dispatch(t, ev) { (el._handlers[t] || []).forEach(fn => fn(Object.assign({ target: el }, ev))); },
        contains(other) { let n = other; while (n) { if (n === el) return true; n = n.parent; } return false; },
        closest(sel) { let n = el; while (n) { if (n._tag === sel) return n; n = n.parent; } return null; },
        focus() { el.focused = true; },
    };
    return el;
}

const wrap = makeEl('downloads-menu-wrap', ['relative', 'group']);
const btn = makeEl('downloads-menu-btn', []);
const menu = makeEl('downloads-menu', ['hidden']);
const link = makeEl('dl-original', []);
link._tag = 'a';
btn.parent = wrap;
menu.parent = wrap;
link.parent = menu;
const outside = makeEl('elsewhere', []);

const els = {
    'downloads-menu-wrap': wrap,
    'downloads-menu-btn': btn,
    'downloads-menu': menu,
};
const docHandlers = {};
global.document = {
    getElementById: id => els[id] || null,
    addEventListener: (t, fn) => (docHandlers[t] = docHandlers[t] || []).push(fn),
    fire: (t, ev) => (docHandlers[t] || []).forEach(fn => fn(ev)),
};

// eslint-disable-next-line no-eval
eval(fs.readFileSync(process.argv[2], 'utf8'));

const visible = () => !menu.classList.contains('hidden');
const aria = () => btn.getAttribute('aria-expanded');
const clickBtn = () => btn.dispatch('click', { stopPropagation() {} });

const failures = [];
function check(label, wantVisible) {
    const v = visible();
    const a = aria();
    const ok = v === wantVisible && a === (wantVisible ? 'true' : 'false');
    if (!ok) failures.push(`${label}: visible=${v} aria-expanded=${a} (wanted visible=${wantVisible})`);
}

check('initial state is closed', false);

clickBtn();
check('click opens', true);
clickBtn();
check('second click closes', false);

clickBtn();
btn.focused = false;
document.fire('keydown', { key: 'Escape' });
check('Escape closes', false);
if (!btn.focused) failures.push('Escape did not return focus to the trigger');

clickBtn();
document.fire('click', { target: outside });
check('outside click closes', false);

wrap.dispatch('mouseenter');
check('hover opens and aria follows', true);
wrap.dispatch('mouseleave');
check('unhover closes', false);

// The desktop sequence the previous harness never established: the pointer is
// already inside the wrapper when the clicks happen.
wrap.dispatch('mouseenter');
check('hovered: menu is open', true);
clickBtn();
check('hovered: first click pins it open', true);
clickBtn();
check('hovered: second click CLOSES it', false);
// The pointer has not moved, so nothing may put it back on screen.
check('still inside the wrapper: stays closed', false);
wrap.dispatch('mouseleave');
check('leaving while closed keeps it closed', false);
wrap.dispatch('mouseenter');
check('a new hover cycle can open it again', true);
wrap.dispatch('mouseleave');
check('unhover closes again', false);

// Escape and outside click must also survive a stationary pointer.
wrap.dispatch('mouseenter');
clickBtn();
check('hovered and pinned before Escape', true);
btn.focused = false;
document.fire('keydown', { key: 'Escape' });
check('Escape closes while still hovered', false);
if (!btn.focused) failures.push('Escape did not return focus to the trigger while hovered');
wrap.dispatch('mouseleave');

wrap.dispatch('mouseenter');
clickBtn();
document.fire('click', { target: outside });
check('outside click closes while still hovered', false);
wrap.dispatch('mouseleave');

wrap.dispatch('mouseenter');
clickBtn();
menu.dispatch('click', { target: link });
check('choosing a download closes while still hovered', false);
wrap.dispatch('mouseleave');

// Touch: a tap fires click; mouseleave never fires on touch.
clickBtn();
check('tap opens', true);
clickBtn();
check('second tap closes', false);

clickBtn();
menu.dispatch('click', { target: link });
check('choosing a download closes', false);

clickBtn();
document.fire('click', { target: menu });
check('click on menu chrome keeps it open', true);

wrap.dispatch('mouseenter');
wrap.dispatch('mouseleave');
check('unhover while click-pinned keeps it open', true);
document.fire('click', { target: outside });
check('outside click clears the pin', false);

if (failures.length) {
    failures.forEach(f => console.error('FAIL ' + f));
    process.exit(1);
}
console.log('ok');
