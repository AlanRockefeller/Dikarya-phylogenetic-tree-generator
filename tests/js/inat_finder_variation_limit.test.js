'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const repo = process.argv[2] || path.resolve(__dirname, '..', '..');
const source = fs.readFileSync(path.join(repo, 'app/static/js/inat_finder.js'), 'utf8');

function section(start, end) {
    const from = source.indexOf(start);
    const to = source.indexOf(end, from);
    if (from < 0 || to < 0) throw new Error(`Finder section was not found: ${start}`);
    return source.slice(from, to);
}

function contextWith(code, setup = {}) {
    const context = {
        URL,
        AbortController,
        DOMException,
        setTimeout,
        clearTimeout,
        window: {setTimeout, clearTimeout},
        ...setup,
    };
    vm.runInNewContext(`${code}\nthis.result = {${setup.exports || ''}};`, context);
    return context;
}

async function main() {
    const parsing = contextWith(
        section('    function canonicalObservationId', '    function showError'),
        {exports: 'parseObservationId'},
    ).result.parseObservationId;
    const accepted = new Map([
        ['123456', '123456'],
        [' https://www.inaturalist.org/observations/123456/ ', '123456'],
        ['www.inaturalist.org/observations/123456?foo=bar', '123456'],
        ['/observations/001234#activity', '1234'],
    ]);
    accepted.forEach((expected, input) => {
        if (parsing(input) !== expected) throw new Error(`Did not parse ${input}`);
    });
    ['123-456', '2024 survey notes', 'voucher 123456', '/observations/123/more'].forEach(input => {
        if (parsing(input) !== '') throw new Error(`Incorrectly parsed ${input}`);
    });

    const variationCode = section('    function combinations', '    async function resolveCriteria');
    const variations = contextWith(
        `const MAX_VARIATIONS = 100000;\n` +
        `${section('    function canonicalObservationId', '    function parseObservationId')}\n` +
        variationCode,
        {log() {}, exports: 'estimateVariationCount, buildVariations'},
    ).result;
    const normalEstimate = variations.estimateVariationCount('123456789', 3);
    if (normalEstimate !== 64278) {
        throw new Error(`Expected 64,278 variations for a normal nine-digit ID, got ${normalEstimate}`);
    }
    const generated = variations.buildVariations('12345', 1);
    if (!generated.includes('129345')) throw new Error('An interior missing digit was not generated');
    if (generated.includes('12345')) throw new Error('The original numeric ID was regenerated');
    if (generated.some(value => value.length > 1 && value.startsWith('0'))) {
        throw new Error('A leading-zero candidate survived canonicalization');
    }
    if (new Set(generated).size !== generated.length) throw new Error('Numeric candidates were not deduplicated');

    const hostileId = '1'.repeat(200);
    if (variations.estimateVariationCount(hostileId, 3) <= 100000) {
        throw new Error('Hostile input was estimated below the cap');
    }
    let rejected = false;
    try {
        variations.buildVariations(hostileId, 3);
    } catch (error) {
        rejected = error && error.name === 'RangeError';
    }
    if (!rejected) throw new Error('Variation generation did not enforce its cap');

    const swapped = variations.buildVariations('123456789', 1);
    if (!swapped.includes('123465789')) throw new Error('An adjacent-digit transposition was not generated');
    const twoWrong = variations.buildVariations('123456789', 2);
    if (twoWrong.includes('123465789') !== true) throw new Error('A two-digit search should still cover the swap');
    const interiorPair = variations.buildVariations('1234', 1);
    if (!interiorPair.includes('129934')) throw new Error('Two interior missing digits were not generated');

    const matching = contextWith(
        section('    function observationMatches', '    async function checkBatch'),
        {exports: 'observationMatches'},
    ).result.observationMatches;
    const taxonCriteria = {label: 'Amanita', taxonId: 48419};
    if (!matching({taxon: {id: 48419, ancestor_ids: []}}, 'genus', taxonCriteria)) {
        throw new Error('A genus-level taxon ID did not match');
    }
    if (!matching({taxon: {id: 999, ancestor_ids: [1, 48419, 2]}}, 'genus', taxonCriteria)) {
        throw new Error('A species/section ancestor taxon ID did not match');
    }
    const familyCriteria = {label: 'Amanitaceae', taxonId: 118249};
    if (!matching({taxon: {id: 999, ancestor_ids: [118249]}}, 'family', familyCriteria)) {
        throw new Error('Family mode did not use ancestor taxon IDs');
    }
    if (matching({taxon: {id: 999, name: 'Amanita homonym', ancestor_ids: []}}, 'genus', taxonCriteria)) {
        throw new Error('The removed genus-name heuristic still matched');
    }
    // The CLI's _taxon_id_matches() also consults the expanded `ancestors`
    // objects. The browser used to check ancestor_ids alone, so a record that
    // carried only `ancestors` was a false negative here and a match in the CLI.
    if (!matching({taxon: {id: 999, ancestors: [{id: 1}, {id: 48419}]}}, 'genus', taxonCriteria)) {
        throw new Error('An ancestors[] taxon ID did not match the way the CLI does');
    }
    if (matching({taxon: {id: 999, ancestors: [{id: 1}, null, {}]}}, 'genus', taxonCriteria)) {
        throw new Error('An unrelated ancestors[] list matched');
    }
    // A malformed list must cost one observation at most, never the whole batch:
    // .some() on a non-array throws out of checkBatch().
    [
        {taxon: {id: 999, ancestor_ids: 'not-an-array'}},
        {taxon: {id: 999, ancestors: 'not-an-array'}},
        {taxon: null},
        {},
        null,
    ].forEach(observation => {
        if (matching(observation, 'genus', taxonCriteria)) {
            throw new Error('A malformed observation matched');
        }
    });
    if (matching(null, 'user', {label: 'someone'})) {
        throw new Error('A null observation matched in user mode');
    }

    const criteriaCode = section('    async function resolveCriteria', '    function observationMatches');
    const lookupCalls = [];
    const criteriaContext = contextWith(criteriaCode, {
        apiGet: async (requestPath, params) => {
            lookupCalls.push([requestPath, params]);
            if (requestPath === '/taxa/autocomplete') {
                return {results: [{id: 47157, rank: params.rank, name: params.rank === 'family' ? 'Amanitaceae' : 'Amanita'}]};
            }
            if (requestPath === '/projects/example-slug') {
                const error = new Error('not found');
                error.status = 404;
                throw error;
            }
            if (requestPath === '/projects') {
                return {results: [{id: 42, slug: 'example-slug', title: 'Example Project'}]};
            }
            const error = new Error('not found');
            error.status = 404;
            throw error;
        },
        exports: 'resolveCriteria',
    });
    const family = await criteriaContext.result.resolveCriteria('family', 'Amanitaceae', {});
    if (family.taxonId !== 47157) throw new Error('Family resolution did not retain the numeric taxon ID');
    lookupCalls.length = 0;
    const project = await criteriaContext.result.resolveCriteria('project', 'example-slug', {});
    if (project.projectId !== '42' || lookupCalls[0][0] !== '/projects/example-slug' || lookupCalls[1][0] !== '/projects') {
        throw new Error('Project slug resolution did not try the direct endpoint before fuzzy search');
    }
    for (const [mode, term] of [
        ['user', 'missing-user'],
        ['project', '999999999'],
    ]) {
        try {
            await criteriaContext.result.resolveCriteria(mode, term, {});
            throw new Error(`${mode} lookup unexpectedly succeeded`);
        } catch (error) {
            if (!error.message.includes('was not found')) throw error;
        }
    }

    const requestCode = section('    function sleep', '    function combinations');
    const requestContext = contextWith(`
        const API = 'https://api.inaturalist.org/v1';
        const REQUEST_TIMEOUT_MS = 5;
        ${requestCode}
    `, {
        log() {},
        fetch: async () => ({ok: true, status: 200, json: async () => { throw new SyntaxError('bad json'); }}),
        exports: 'apiGet',
    });
    try {
        await requestContext.result.apiGet('/observations', {}, {cancelled: false}, 1);
        throw new Error('Unreadable JSON unexpectedly succeeded');
    } catch (error) {
        if (error.message !== 'iNaturalist returned an unreadable response') throw error;
    }

    const rateContext = contextWith(`
        const API = 'https://api.inaturalist.org/v1';
        const REQUEST_TIMEOUT_MS = 20;
        ${requestCode}
    `, {
        log() {},
        fetch: async () => ({ok: false, status: 429, headers: {get: () => null}}),
        exports: 'apiGet',
    });
    try {
        await rateContext.result.apiGet('/observations', {}, {cancelled: false}, 1);
        throw new Error('A final 429 unexpectedly succeeded');
    } catch (error) {
        if (!error.message.includes('rate-limiting')) throw error;
    }

    let timeoutFetches = 0;
    const timeoutContext = contextWith(`
        const API = 'https://api.inaturalist.org/v1';
        const REQUEST_TIMEOUT_MS = 5;
        ${requestCode}
    `, {
        log() {},
        fetch: (_url, options) => new Promise((_resolve, rejectFetch) => {
            timeoutFetches += 1;
            options.signal.addEventListener('abort', () => rejectFetch(new DOMException('aborted', 'AbortError')));
        }),
        exports: 'apiGet',
    });
    try {
        await timeoutContext.result.apiGet('/observations', {}, {cancelled: false}, 2);
        throw new Error('Timed-out requests unexpectedly succeeded');
    } catch (error) {
        if (!error.message.includes('20 seconds') || timeoutFetches !== 2) throw error;
    }

    let cancelledFetches = 0;
    const cancelContext = contextWith(`
        const API = 'https://api.inaturalist.org/v1';
        const REQUEST_TIMEOUT_MS = 100;
        ${requestCode}
    `, {
        log() {},
        fetch: (_url, options) => new Promise((_resolve, rejectFetch) => {
            cancelledFetches += 1;
            options.signal.addEventListener('abort', () => rejectFetch(new DOMException('aborted', 'AbortError')));
        }),
        exports: 'apiGet, sleep',
    });
    const fetchSearch = {cancelled: false, controller: null};
    const cancelledRequest = cancelContext.result.apiGet('/observations', {}, fetchSearch, 3);
    setTimeout(() => {
        fetchSearch.cancelled = true;
        fetchSearch.controller.abort();
    }, 5);
    try {
        await cancelledRequest;
        throw new Error('A cancelled fetch unexpectedly succeeded');
    } catch (error) {
        if (error.name !== 'AbortError' || cancelledFetches !== 1) throw error;
    }

    const sleepSearch = {cancelled: false, sleepTimer: null, sleepReject: null};
    const cancelledSleep = cancelContext.result.sleep(1000, sleepSearch);
    const rejectSleep = sleepSearch.sleepReject;
    clearTimeout(sleepSearch.sleepTimer);
    sleepSearch.cancelled = true;
    sleepSearch.sleepTimer = null;
    sleepSearch.sleepReject = null;
    rejectSleep(new DOMException('Search cancelled', 'AbortError'));
    try {
        await cancelledSleep;
        throw new Error('A cancelled between-batch sleep unexpectedly succeeded');
    } catch (error) {
        if (error.name !== 'AbortError' || sleepSearch.sleepTimer !== null || sleepSearch.sleepReject !== null) throw error;
    }
    await cancelContext.result.sleep(1, {
        cancelled: false, sleepTimer: null, sleepReject: null,
    });

    const taxonContext = contextWith(criteriaCode, {
        apiGet: async requestPath => {
            if (requestPath === '/taxa/48419') {
                return {results: [{id: 48419, name: 'Amanita', rank: 'genus', iconic_taxon_name: 'Fungi'}]};
            }
            const error = new Error('not found');
            error.status = 404;
            throw error;
        },
        exports: 'resolveCriteria',
    });
    const byId = await taxonContext.result.resolveCriteria('taxon', 'https://www.inaturalist.org/taxa/48419-Amanita', {});
    if (byId.taxonId !== 48419) throw new Error('A taxon URL did not resolve to its numeric taxon ID');
    try {
        await taxonContext.result.resolveCriteria('taxon', 'Amanita', {});
        throw new Error('A non-numeric taxon ID unexpectedly resolved');
    } catch (error) {
        if (!error.message.includes('not an iNaturalist taxon ID')) throw error;
    }

    const ambiguousContext = contextWith(criteriaCode, {
        apiGet: async () => ({results: [
            {id: 47604, name: 'Prunella', rank: 'genus', iconic_taxon_name: 'Plantae'},
            {id: 9083, name: 'Prunella', rank: 'genus', iconic_taxon_name: 'Aves'},
        ]}),
        exports: 'resolveCriteria',
    });
    try {
        await ambiguousContext.result.resolveCriteria('genus', 'Prunella', {});
        throw new Error('An ambiguous genus name unexpectedly resolved to one taxon');
    } catch (error) {
        if (!Array.isArray(error.taxonCandidates) || error.taxonCandidates.length !== 2) throw error;
    }

    const places = contextWith(
        section('    function formatPlaceLabel', '    async function resolveLocations'),
        {exports: 'formatPlaceLabel'},
    ).result.formatPlaceLabel;
    if (places([
        {admin_level: 0, display_name: 'United States'},
        {admin_level: 20, display_name: 'Mendocino County, California, United States'},
    ]) !== 'Mendocino Co. California US') {
        throw new Error('The most specific administrative place was not formatted');
    }
    if (places([{admin_level: null, display_name: 'Some park'}]) !== '') {
        throw new Error('A non-standard place should fall back to the observation place guess');
    }

    const describe = contextWith(
        section('    function describeTaxonSuggestion', '    function closeSuggestions'),
        {exports: 'describeTaxonSuggestion'},
    ).result.describeTaxonSuggestion;
    const milkcaps = describe({id: 54597, name: 'Lactarius', rank: 'genus', iconic_taxon_name: 'Fungi', preferred_common_name: 'Common Milkcaps'});
    const trevallies = describe({id: 54596, name: 'Lactarius', rank: 'genus', iconic_taxon_name: 'Actinopterygii', preferred_common_name: 'False Trevallies'});
    if (milkcaps === trevallies) throw new Error('Two homonym taxa were described identically');
    if (!milkcaps.includes('Fungi') || !milkcaps.includes('taxon ID 54597')) {
        throw new Error('A suggestion did not name its iconic taxon and taxon ID');
    }
    if (describe({id: 7}) !== 'taxon ID 7') throw new Error('A bare taxon was not described by its ID');

    const runSearch = section('    async function runSearch', "    form.addEventListener('submit'");
    const capCheck = runSearch.indexOf('variationEstimate > MAX_VARIATIONS');
    const criteriaLookup = runSearch.indexOf('await resolveCriteria');
    if (capCheck < 0 || criteriaLookup < 0 || capCheck > criteriaLookup) {
        throw new Error('Search does not enforce the variation cap before its first API lookup');
    }

    console.log('PASS iNat Finder browser regressions');
}

main().catch(error => {
    console.error(error.stack || error);
    process.exit(1);
});
