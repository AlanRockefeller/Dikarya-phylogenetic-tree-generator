// Alan 8/24/26 - Lint config for the browser-side viewer code.
//
// This exists because of one specific production outage: a `const` in
// tree_viewer_controller.js was referenced ~16 lines above its declaration, so
// the whole DOMContentLoaded bootstrap threw a temporal-dead-zone
// ReferenceError and the tree viewer hung on "loading" forever. `node --check`
// cannot see that - TDZ is a runtime error, not a syntax error - but
// `no-use-before-define` catches it statically, for free, on every file.
//
// The rule set is deliberately narrow. These are large, long-lived files that
// were never written against a linter, so turning on a stylistic preset would
// bury the one class of defect that actually takes the site down.

const browserGlobals = {
    window: 'readonly', document: 'readonly', console: 'readonly',
    fetch: 'readonly', navigator: 'readonly', location: 'readonly',
    setTimeout: 'readonly', clearTimeout: 'readonly',
    setInterval: 'readonly', clearInterval: 'readonly',
    requestAnimationFrame: 'readonly', cancelAnimationFrame: 'readonly',
    URLSearchParams: 'readonly', URL: 'readonly', Blob: 'readonly',
    FormData: 'readonly', FileReader: 'readonly', Image: 'readonly',
    CustomEvent: 'readonly', Event: 'readonly', DOMParser: 'readonly',
    XMLSerializer: 'readonly', localStorage: 'readonly',
    sessionStorage: 'readonly', alert: 'readonly', confirm: 'readonly',
    getComputedStyle: 'readonly', MutationObserver: 'readonly',
    ResizeObserver: 'readonly', IntersectionObserver: 'readonly',
    AbortController: 'readonly', EventSource: 'readonly', btoa: 'readonly',
    atob: 'readonly', structuredClone: 'readonly', queueMicrotask: 'readonly',
    performance: 'readonly', history: 'readonly', screen: 'readonly',
    HTMLElement: 'readonly', Node: 'readonly', SVGElement: 'readonly',
    WheelEvent: 'readonly', MouseEvent: 'readonly', KeyboardEvent: 'readonly',
    // Cross-file globals the viewer scripts publish to each other via window.
    d3: 'readonly', _: 'readonly', phylotree: 'readonly',
};

module.exports = [
    {
        // Vendored bundles are not ours to lint, and phylotree.js is a
        // locally-modified vendor drop already covered by its own invariant
        // harness (tests/test_phylotree_invariants.py).
        ignores: [
            'node_modules/**',
            'app/static/vendor/**',
            'app/static/js/phylotree.js',
            'var/**',
            '.venv/**',
        ],
    },
    {
        files: ['app/static/js/**/*.js'],
        languageOptions: {
            ecmaVersion: 2022,
            sourceType: 'script',
            globals: browserGlobals,
        },
        rules: {
            // The outage rule. `variables: true` is the part that matters:
            // it flags a const/let/class read above its declaration.
            // `functions: false` because hoisted function declarations are
            // legitimately called before their definition throughout these
            // files, and flagging those would be pure noise.
            'no-use-before-define': ['error', {
                variables: true,
                functions: false,
                classes: true,
                allowNamedExports: false,
            }],
            // Assigning to a const is always a bug and always fatal.
            'no-const-assign': 'error',
            // Redeclaring a let/const in the same scope is a SyntaxError that
            // only surfaces when the file is actually parsed by the browser.
            'no-redeclare': 'error',
            'no-dupe-keys': 'error',
            'no-dupe-args': 'error',
            'no-unreachable': 'error',
        },
    },
];
