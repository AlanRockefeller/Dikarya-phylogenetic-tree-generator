'use strict';

// Auto-mode regressions for the browser iNaturalist finder.
//
// The ladder in app/static/js/inat_finder.js is a port of inat.finder.py 1.8.0's
// --auto mode. These tests pin the behaviour that a reader of the JS alone could
// plausibly "simplify" away: the stop rule, the tri-state project evidence, the
// difference between an unusable clue and an outage, and the promise that no
// observation ID is ever requested twice. The candidate ladder itself is checked
// against fixtures generated from the CLI, so the two implementations cannot
// drift apart silently.

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const crypto = require('crypto');

const repo = process.argv[2] || path.resolve(__dirname, '..', '..');
const source = fs.readFileSync(path.join(repo, 'app/static/js/inat_finder.js'), 'utf8');
const fixture = JSON.parse(
    fs.readFileSync(path.join(repo, 'tests/fixtures/inat_finder_candidate_parity.json'), 'utf8')
);

function section(start, end) {
    const from = source.indexOf(start);
    const to = source.indexOf(end, from);
    if (from < 0 || to < 0) throw new Error(`Finder section was not found: ${start}`);
    return source.slice(from, to);
}

// Read the shipped constants out of the implementation rather than restating
// them: a test carrying its own copy keeps passing after the real value moves.
function constant(name) {
    const match = source.match(new RegExp(`const\\s+${name}\\s*=\\s*(\\d+)\\s*;`));
    if (!match) throw new Error(`${name} was not found in inat_finder.js`);
    return Number(match[1]);
}

const CANDIDATES = section('    // ---- section:auto-candidates ----', '    // ---- section:auto-scoring ----');
const SCORING = section('    // ---- section:auto-scoring ----', '    // ---- section:auto-resolve ----');
const RESOLVE = section('    // ---- section:auto-resolve ----', '    // ---- section:auto-ladder ----');
const LADDER = section('    // ---- section:auto-ladder ----', '    // ---- section:autocomplete ----');
const MATCHING = section('    // ---- section:matching ----', '    // ---- section:auto-candidates ----');

// The engine is written against injected dependencies, so it runs headlessly
// with no DOM and no network.
function engine(extra = {}, batchSize = 200) {
    const context = {
        BATCH_SIZE: batchSize,
        LARGE_SEARCH_THRESHOLD: constant('LARGE_SEARCH_THRESHOLD'),
        MAX_CONSECUTIVE_FAILED_BATCHES: constant('MAX_CONSECUTIVE_FAILED_BATCHES'),
        MAX_SEARCH_CANDIDATES: constant('MAX_SEARCH_CANDIDATES'),
        AUTO_DEFAULT_MAX_DIGITS: constant('AUTO_DEFAULT_MAX_DIGITS'),
        EARLY_STOP_MIN_CANDIDATES: constant('EARLY_STOP_MIN_CANDIDATES'),
        apiGet: async () => ({results: []}),
        ...extra
    };
    const code = `${MATCHING}\n${CANDIDATES}\n${SCORING}\n${RESOLVE}\n${LADDER}\n`
        + 'this.out = {buildCandidatePlan, createAutoStage, autoStageLabel, countDigitVariations,'
        + ' scoreObservation, isFullMatch, rankMatches, makeTaxonCriterion, makeUserCriterion,'
        + ' makeProjectCriterion, resolveAutoCriteria, runStagePass, runAutoLadder,'
        + ' estimateStageSeconds, rankMatches, EVIDENCE, CLUE_LABELS, AUTO_CLUE_KINDS};';
    vm.runInNewContext(code, context);
    return context.out;
}

function abortError() {
    return new DOMException('Search cancelled', 'AbortError');
}

// A fake iNaturalist holding a fixed universe of observations, which records
// every ID it was asked about so the tests can assert on the request pattern.
function fakeApi(universe, options = {}) {
    const {members = null, failObservations = () => false, failMembership = () => false} = options;
    const requested = [];
    const membershipCalls = [];
    const api = {
        requested,
        membershipCalls,
        batches: 0,
        async fetchBatch(ids) {
            api.batches += 1;
            const failure = failObservations(ids, api.batches);
            if (failure) throw failure === true ? new Error('iNaturalist is unreachable') : failure;
            ids.forEach(id => requested.push(String(id)));
            return ids.map(id => universe.get(String(id))).filter(Boolean);
        },
        async fetchMembership(ids) {
            membershipCalls.push(ids.map(String));
            const failure = failMembership(ids, membershipCalls.length);
            if (failure) throw failure === true ? new Error('membership lookup failed') : failure;
            return new Set(ids.map(String).filter(id => (members || new Set()).has(id)));
        }
    };
    return api;
}

function observation(id, extra = {}) {
    return {
        id: Number(id),
        taxon: {id: 48419, name: 'Amanita muscaria', ancestor_ids: [48419]},
        user: {login: 'alan'},
        place_ids: [],
        ...extra
    };
}

const failures = [];
function check(label, condition, detail = '') {
    if (!condition) failures.push(`${label}${detail ? `: ${detail}` : ''}`);
}

async function main() {
    const out = engine();
    const genus = out.makeTaxonCriterion('genus', 'Amanita', 'genus Amanita', 48419, null);
    const user = out.makeUserCriterion('alan');
    const project = out.makeProjectCriterion('bioblitz', "project 'BioBlitz'");

    // ---- 12. Candidate plans and stage counts match the Python 1.8.0 semantics.
    // Both the set and the order, for every ID shape in the fixture.
    for (const testCase of fixture.cases) {
        for (const plan of testCase.plans) {
            const built = out.buildCandidatePlan(testCase.number, plan.digits_off);
            const digest = crypto.createHash('sha256');
            let count = 0;
            for (const candidate of built.candidates()) {
                digest.update(`${candidate}\n`);
                count += 1;
            }
            const where = `${testCase.number}/digits ${plan.digits_off}`;
            check(`plan total (${where})`, built.total === plan.total, `${built.total} vs ${plan.total}`);
            check(`plan yielded (${where})`, count === plan.yielded, `${count} vs ${plan.yielded}`);
            check(`plan replacements (${where})`, built.replacementCount === plan.replacement_count);
            check(`plan additions (${where})`, built.additions.length === plan.additions);
            check(`plan removals (${where})`, built.removals.length === plan.removals);
            check(`plan transpositions (${where})`, built.transpositions.length === plan.transpositions);
            check(`plan label (${where})`, out.autoStageLabel(plan.digits_off, built) === plan.label,
                `${out.autoStageLabel(plan.digits_off, built)} vs ${plan.label}`);
            check(`plan candidate order (${where})`, digest.digest('hex') === plan.sha256,
                'the JS ladder no longer yields the CLI\'s candidates in the CLI\'s order');
        }
        // The ladder's stages nest, so stage k is plan k minus everything the
        // earlier stages already yielded - not plan(k).total - plan(k-1).total.
        const seen = new Set();
        for (const stageFixture of testCase.stages) {
            const stage = out.createAutoStage(
                stageFixture.index,
                out.buildCandidatePlan(testCase.number, stageFixture.index),
                seen
            );
            const digest = crypto.createHash('sha256');
            let yielded = 0;
            for (const candidate of stage.candidates()) {
                digest.update(`${candidate}\n`);
                yielded += 1;
            }
            const where = `${testCase.number}/stage ${stageFixture.index}`;
            check(`stage total (${where})`, stage.total === stageFixture.total,
                `${stage.total} vs ${stageFixture.total}`);
            // The count a stage announces must equal the number of new IDs it
            // really searches, or the progress bar is lying.
            check(`stage announced its real size (${where})`, yielded === stage.total,
                `announced ${stage.total}, yielded ${yielded}`);
            check(`stage seen set (${where})`, seen.size === stageFixture.seen_after);
            check(`stage label (${where})`, stage.label === stageFixture.label);
            check(`stage candidate order (${where})`, digest.digest('hex') === stageFixture.sha256);
        }
    }

    // The widest stage of a nine-digit number is the one the UI warns about.
    const nineDigit = fixture.cases.find(item => item.number === '123456789');
    check('nine-digit stage 3 is the ~59k stage', nineDigit.stages[2].total === 58968,
        String(nineDigit.stages[2].total));

    // ---- Scoring: any-of, with UNKNOWN neither matching nor blocking.
    const scoredNoProject = out.scoreObservation(observation(1), {projectMemberIds: null}, [genus, user, project]);
    check('unknown project is not a match', scoredNoProject.matched.join(',') === 'genus,user');
    check('unknown project is reported as unknown', scoredNoProject.unknown.join(',') === 'project');
    check('an unknown clue can never complete a full match',
        out.isFullMatch(scoredNoProject.matched, [genus, user, project]) === false);
    const scoredMember = out.scoreObservation(observation(1), {projectMemberIds: new Set(['1'])}, [genus, user, project]);
    check('a confirmed member is a full match', out.isFullMatch(scoredMember.matched, [genus, user, project]));
    const scoredNonMember = out.scoreObservation(observation(1), {projectMemberIds: new Set(['999'])}, [project]);
    check('a confirmed non-member does not match', scoredNonMember.matched.length === 0);
    check('a confirmed non-member is not unknown either', scoredNonMember.unknown.length === 0);

    // ---- 10. Results rank by clue score, deterministically.
    const ranked = out.rankMatches([
        {observation: observation(30), matched: ['genus'], unknown: [], stage: 1},
        {observation: observation(10), matched: ['genus', 'user'], unknown: [], stage: 2},
        {observation: observation(20), matched: ['genus'], unknown: [], stage: 1},
        // The same observation reached twice keeps its best reading, not its first.
        {observation: observation(30), matched: ['genus', 'user'], unknown: [], stage: 3}
    ]);
    check('ranking puts the best score first', ranked.map(m => m.observation.id).join(',') === '10,30,20',
        ranked.map(m => `${m.observation.id}(${m.matched.length})`).join(','));
    check('ranking deduplicates by observation', ranked.length === 3);

    // ---- 6. A stage-0 full match stops immediately.
    {
        const api = fakeApi(new Map([['123456789', observation(123456789)]]));
        const result = await engine().runAutoLadder({
            number: '123456789', criteria: [genus, user], digitsCap: 3,
            fetchBatch: api.fetchBatch, fetchMembership: api.fetchMembership
        });
        check('stage 0 full match reports match_found', result.status === 'match_found', result.status);
        check('stage 0 full match stops before stage 1', api.requested.length === 1,
            `${api.requested.length} IDs requested`);
        check('stage 0 full match is complete', result.complete === true);
        check('the number as supplied is reported', String(result.original.id) === '123456789');
    }

    // ---- 5. No clues: check the number as supplied and stop.
    {
        const api = fakeApi(new Map([['123456789', observation(123456789)]]));
        const result = await engine().runAutoLadder({
            number: '123456789', criteria: [], digitsCap: 3,
            fetchBatch: api.fetchBatch, fetchMembership: api.fetchMembership
        });
        check('no clues stops after the original', api.requested.join(',') === '123456789',
            `requested ${api.requested.length}`);
        check('no clues explains itself', result.stopReason === 'no_clues', result.stopReason);
        check('no clues still shows what the number points at', String(result.original.id) === '123456789');
        check('no clues is not a match', result.matches.length === 0);
    }

    // ---- 7. A stage-1 full match never reaches stage 2, and
    // ---- 11. no observation ID is requested twice.
    {
        const universe = new Map([['123456788', observation(123456788)]]);
        const api = fakeApi(universe);
        const result = await engine().runAutoLadder({
            number: '123456789', criteria: [genus, user], digitsCap: 3,
            fetchBatch: api.fetchBatch, fetchMembership: api.fetchMembership
        });
        check('a stage-1 hit is found', result.matches.length === 1, String(result.matches.length));
        check('a stage-1 hit is reported as a match', result.status === 'match_found', result.status);
        check('a stage-1 hit stops the ladder', result.matches[0].stage === 1, String(result.matches[0].stage));
        // Stage 1 of a nine-digit number is 133 candidates plus the original. The
        // stage now runs to the end, but the ladder still stops after it.
        check('stage 2 was never entered', api.requested.length <= 134, `${api.requested.length} requested`);
        check('no ID was requested twice', new Set(api.requested).size === api.requested.length,
            `${api.requested.length} requests, ${new Set(api.requested).size} distinct`);
    }

    // ---- 8. An empty stage 1 escalates to stage 2, without repeating stage 1.
    {
        // 123454589 differs from 123456789 in two positions, so only stage 2 finds it.
        const api = fakeApi(new Map([['123454589', observation(123454589)]]));
        const result = await engine().runAutoLadder({
            number: '123456789', criteria: [genus], digitsCap: 3,
            fetchBatch: api.fetchBatch, fetchMembership: api.fetchMembership,
            largeThreshold: 1e9
        });
        check('stage 2 found what stage 1 could not', result.matches.length === 1, String(result.matches.length));
        check('the match is attributed to stage 2', result.matches[0].stage === 2,
            String(result.matches[0] && result.matches[0].stage));
        check('no ID was requested twice across stages',
            new Set(api.requested).size === api.requested.length,
            `${api.requested.length} requests, ${new Set(api.requested).size} distinct`);
    }

    // ---- 9. A partial stage-1 match is kept while the ladder keeps widening.
    {
        // The near neighbour matches the genus only; the two-digit neighbour
        // matches both clues. A partial hit must not end the search.
        const api = fakeApi(new Map([
            ['123456788', observation(123456788, {user: {login: 'someone-else'}})],
            ['123454589', observation(123454589)]
        ]));
        const result = await engine().runAutoLadder({
            number: '123456789', criteria: [genus, user], digitsCap: 3,
            fetchBatch: api.fetchBatch, fetchMembership: api.fetchMembership,
            largeThreshold: 1e9
        });
        check('the partial match is kept', result.matches.length === 2, String(result.matches.length));
        check('the full match is ranked first', result.matches[0].matched.length === 2);
        check('the full match is the two-digit neighbour',
            String(result.matches[0].observation.id) === '123454589');
        check('the partial match is still reported as partial', result.matches[1].matched.length === 1);
    }

    // ---- 19/20/21. A large stage is never started unasked; it stops with a
    // continuation, and continuing picks up without repeating anything.
    {
        const api = fakeApi(new Map());
        const first = await engine({}, 200).runAutoLadder({
            number: '123456789', criteria: [genus], digitsCap: 3,
            fetchBatch: api.fetchBatch, fetchMembership: api.fetchMembership,
            // Stage 1 is 133 candidates and stage 2 is 2,836, so only stage 2 is
            // "large" at this threshold and only it should be held back.
            largeThreshold: 200
        });
        check('a large stage stops rather than running', first.stopReason === 'large_stage', first.stopReason);
        check('a stage that was never searched is not a completed search', first.complete === false);
        check('it says which stage and how big', first.continuation
            && first.continuation.stage === 2 && first.continuation.total === 2836,
            JSON.stringify(first.continuation && {s: first.continuation.stage, t: first.continuation.total}));
        check('it did not run the stage it held back', api.requested.length <= 134,
            `${api.requested.length} requested`);

        const beforeContinue = new Set(api.requested);
        const second = await engine({}, 200).runAutoLadder({
            number: '123456789', criteria: [genus], digitsCap: 3,
            fetchBatch: api.fetchBatch, fetchMembership: api.fetchMembership,
            largeThreshold: 200,
            seenIds: first.continuation.seenIds,
            startStage: first.continuation.stage,
            allowLargeStage: true,
            // As the page does: stage 0 is already checked and on screen.
            skipOriginal: true
        });
        const afterContinue = api.requested.slice(beforeContinue.size);
        check('continuing actually ran the held-back stage', afterContinue.length > 0);
        check('continuing re-requested nothing',
            !afterContinue.some(id => beforeContinue.has(id)),
            'an ID from before the pause was requested again');
        check('continuing repeated nothing within itself',
            new Set(api.requested).size === api.requested.length);
        check('the permission is one-shot: the next large stage stops again',
            second.stopReason === 'large_stage' && second.continuation.stage === 3,
            second.stopReason);
    }

    // A large stage still aborts on the batch that produced a full match, because
    // finishing one costs minutes rather than seconds.
    {
        const api = fakeApi(new Map([['123454589', observation(123454589)]]));
        const result = await engine({}, 200).runAutoLadder({
            number: '123456789', criteria: [genus], digitsCap: 2,
            fetchBatch: api.fetchBatch, fetchMembership: api.fetchMembership,
            largeThreshold: 200, earlyStopMin: 200, allowLargeStage: true
        });
        check('a big stage stopped part-way on a full match',
            result.stopReason === 'full_match' && api.requested.length < 133 + 2836,
            `${api.requested.length} requested`);
        check('and still offers to carry on', Boolean(result.continuation));
    }

    // ---- 1.8.1: a SMALL stage runs to the end instead of stopping at its first
    // hit. This is the bug that mattered: iNaturalist numbers observations in
    // upload order, so with one clue a neighbour of the mistyped number satisfies
    // "every clue" by coincidence, and the real observation sat further down the
    // same stage and was never requested.
    {
        const api = fakeApi(new Map([
            ['123456788', observation(123456788)],
            ['123456709', observation(123456709)]
        ]));
        const result = await engine({}, 50).runAutoLadder({
            number: '123456789', criteria: [user], digitsCap: 1,
            fetchBatch: api.fetchBatch, fetchMembership: api.fetchMembership
        });
        check('the whole stage ran despite an early full match',
            api.requested.length === 1 + 133, `${api.requested.length} requested`);
        check('every equally good candidate was collected', result.matches.length === 2,
            String(result.matches.length));
        check('one clue matching several neighbours is called out',
            result.notices.some(notice => /nearby observations all match the only clue/.test(notice)),
            JSON.stringify(result.notices));
        // Both score 1 of 1, so the tie-break decides: the nearer number wins.
        check('ties break towards the number actually typed',
            String(result.matches[0].observation.id) === '123456788',
            result.matches.map(m => m.observation.id).join(','));
        // digitsCap 1 and the stage finished, so there is genuinely no more
        // ladder. Offering to keep looking here would be a button that does
        // nothing, which is worse than no button.
        check('an exhausted ladder offers nothing further', result.continuation === null,
            JSON.stringify(result.continuation));

        // With room left to climb, the same search does offer the way out.
        const roomy = fakeApi(new Map([
            ['123456788', observation(123456788)],
            ['123456709', observation(123456709)]
        ]));
        const deeper = await engine({}, 50).runAutoLadder({
            number: '123456789', criteria: [user], digitsCap: 3,
            fetchBatch: roomy.fetchBatch, fetchMembership: roomy.fetchMembership
        });
        check('a stage that stopped with rungs left offers to keep looking',
            Boolean(deeper.continuation) && deeper.continuation.reason === 'full_match',
            JSON.stringify(deeper.continuation && {r: deeper.continuation.reason}));
    }

    // Ranking: score first, then stage, then distance, then ID.
    {
        const out2 = engine();
        const ranked = out2.rankMatches([
            {observation: observation(500), matched: ['user'], unknown: [], stage: 2},
            {observation: observation(300), matched: ['user'], unknown: [], stage: 1},
            {observation: observation(120), matched: ['user'], unknown: [], stage: 1},
            {observation: observation(100), matched: ['user', 'genus'], unknown: [], stage: 3}
        ], 110);
        check('score outranks everything', String(ranked[0].observation.id) === '100');
        check('a lower stage outranks a nearer number',
            String(ranked[1].observation.id) === '120',
            ranked.map(m => m.observation.id).join(','));
        check('within a stage the nearer number wins',
            String(ranked[2].observation.id) === '300' && String(ranked[3].observation.id) === '500',
            ranked.map(m => m.observation.id).join(','));
        // An unstaged match (the single-criterion search) sorts last, not first.
        const mixed = out2.rankMatches([
            {observation: observation(900), matched: ['user'], unknown: [], stage: null},
            {observation: observation(800), matched: ['user'], unknown: [], stage: 2}
        ], 100);
        check('an unstaged match sorts after a staged one',
            String(mixed[0].observation.id) === '800',
            mixed.map(m => m.observation.id).join(','));
    }

    // A stage-0 hit still leaves the door open: with one clue the number typed
    // can be both wrong and a coincidental match.
    {
        const api = fakeApi(new Map([['123456789', observation(123456789)]]));
        const result = await engine().runAutoLadder({
            number: '123456789', criteria: [user], digitsCap: 3,
            fetchBatch: api.fetchBatch, fetchMembership: api.fetchMembership
        });
        check('a stage-0 hit still reports a match', result.status === 'match_found');
        check('a stage-0 hit offers to keep looking',
            Boolean(result.continuation) && result.continuation.stage === 1,
            JSON.stringify(result.continuation));
    }

    // ---- 22. Cancelling keeps the matches already found.
    {
        const api = fakeApi(new Map([['123456788', observation(123456788, {user: {login: 'other'}})]]), {
            // Let the original and the first stage-1 batch through, then cancel.
            failObservations: (_ids, call) => (call > 2 ? abortError() : false)
        });
        const result = await engine({}, 100).runAutoLadder({
            number: '123456789', criteria: [genus, user], digitsCap: 3,
            fetchBatch: api.fetchBatch, fetchMembership: api.fetchMembership
        });
        check('cancelling reports a cancelled search', result.status === 'cancelled', result.status);
        check('cancelling is never a clean no-match', result.complete === false);
        check('cancelling kept the partial match found so far', result.matches.length === 1,
            String(result.matches.length));
    }

    // ---- 17/18. A failed project membership request keeps the other evidence,
    // marks the project unknown, and leaves the search incomplete.
    {
        const api = fakeApi(new Map([['123456789', observation(123456789)]]), {
            members: new Set(),
            failMembership: () => true
        });
        const result = await engine().runAutoLadder({
            number: '123456789', criteria: [genus, user, project], digitsCap: 1,
            membershipProjectId: '42',
            fetchBatch: api.fetchBatch, fetchMembership: api.fetchMembership
        });
        check('a stage-0 membership failure makes the search incomplete',
            result.status === 'incomplete', result.status);
        check('a stage-0 membership failure is never complete', result.complete === false);
        check('the genus and user evidence survived the membership failure',
            result.matches.length >= 1 && result.matches[0].matched.join(',') === 'genus,user',
            result.matches.length ? result.matches[0].matched.join(',') : 'no matches');
        check('the project is marked unknown, not absent',
            result.matches[0].unknown.join(',') === 'project');
        check('an unanswered project question never reads as a full match',
            result.matches[0].matched.length !== 3);
    }

    // ---- 16. Project combined with another clue is checked separately, so the
    // main request is never filtered down to project members.
    {
        const api = fakeApi(new Map([['123456789', observation(123456789)]]), {members: new Set(['123456789'])});
        const result = await engine().runAutoLadder({
            number: '123456789', criteria: [genus, project], digitsCap: 1,
            membershipProjectId: '42',
            fetchBatch: api.fetchBatch, fetchMembership: api.fetchMembership
        });
        check('membership was asked as its own request', api.membershipCalls.length >= 1,
            String(api.membershipCalls.length));
        check('a confirmed member scores the project clue',
            result.matches.length === 1 && result.matches[0].matched.join(',') === 'genus,project',
            result.matches.length ? result.matches[0].matched.join(',') : 'no matches');
        check('a full match with a project still stops the ladder', result.status === 'match_found');
    }

    // An API outage across a whole stage leaves the search incomplete rather than
    // reporting a confident "nothing matches".
    {
        const api = fakeApi(new Map(), {failObservations: (_ids, call) => call > 1});
        const result = await engine({}, 50).runAutoLadder({
            number: '123456789', criteria: [genus], digitsCap: 1,
            fetchBatch: api.fetchBatch, fetchMembership: api.fetchMembership
        });
        check('a failed stage is incomplete, not "no match"', result.status === 'incomplete', result.status);
        check('a failed stage is never complete', result.complete === false);
        check('the unchecked candidates are counted', result.unchecked > 0, String(result.unchecked));
    }

    // A stage too large to run is an error, never "no matches found": it was
    // never searched, so the run established nothing about it.
    {
        const api = fakeApi(new Map());
        const result = await engine().runAutoLadder({
            number: '123456789', criteria: [genus], digitsCap: 3,
            fetchBatch: api.fetchBatch, fetchMembership: api.fetchMembership,
            maxStageCandidates: 10
        });
        check('an over-sized stage is an error', result.status === 'error', result.status);
        check('an over-sized stage is not complete', result.complete === false);
    }

    // ---- 3/4/13/14/15. Clue resolution in auto mode.
    const clueResolver = overrides => engine({}).resolveAutoCriteria;
    function resolverWith(behaviour) {
        const context = {
            BATCH_SIZE: 200,
            LARGE_SEARCH_THRESHOLD: 5000,
            MAX_CONSECUTIVE_FAILED_BATCHES: 4,
            MAX_SEARCH_CANDIDATES: 1000000,
            resolveCriteria: behaviour,
            observationMatches: () => false
        };
        vm.runInNewContext(
            `${SCORING}\n${RESOLVE}\nthis.out = {resolveAutoCriteria};`,
            context
        );
        return context.out.resolveAutoCriteria;
    }

    // 3. Several clues at once, all usable.
    {
        const seen = [];
        const resolve = resolverWith(async (kind, term) => {
            seen.push(kind);
            if (kind === 'project') return {label: 'BioBlitz', projectId: '42'};
            if (kind === 'user') return {label: term};
            return {label: term, taxonId: 48419, taxon: {id: 48419}};
        });
        const resolved = await resolve({genus: 'Amanita', user: 'alan', project: 'bioblitz'}, {});
        check('auto accepts several clues at once', resolved.criteria.length === 3,
            String(resolved.criteria.length));
        check('clues are verified in the CLI\'s order', seen.join(',') === 'genus,user,project', seen.join(','));
        check('a shared project is checked per batch, not used as a filter',
            resolved.membershipProjectId === '42' && resolved.projectIdParam === null);
    }

    // 4. Exactly one clue is fine too, and a lone project filters server-side.
    {
        const resolve = resolverWith(async () => ({label: 'BioBlitz', projectId: '42'}));
        const resolved = await resolve({project: 'bioblitz'}, {});
        check('a lone project clue is usable on its own', resolved.criteria.length === 1);
        check('a lone project filters the request server-side',
            resolved.projectIdParam === '42' && resolved.membershipProjectId === null);
    }

    // A project supplied alongside a clue that turned out unusable still takes
    // the per-batch path: the decision follows what was asked for.
    {
        const resolve = resolverWith(async kind => {
            if (kind === 'genus') {
                const error = new Error('Genus “Amanata” was not found in the iNaturalist taxonomy.');
                error.unresolvableClue = true;
                throw error;
            }
            return {label: 'BioBlitz', projectId: '42'};
        });
        const resolved = await resolve({genus: 'Amanata', project: 'bioblitz'}, {});
        check('a supplied-but-unusable clue still rules out the server-side filter',
            resolved.membershipProjectId === '42' && resolved.projectIdParam === null);
    }

    // 13. One unresolvable clue plus one valid clue continues.
    {
        const resolve = resolverWith(async kind => {
            if (kind === 'genus') {
                const error = new Error('Genus “Amanata” was not found in the iNaturalist taxonomy.');
                error.unresolvableClue = true;
                throw error;
            }
            return {label: 'alan'};
        });
        const resolved = await resolve({genus: 'Amanata', user: 'alan'}, {});
        check('an unresolvable clue does not end the search', resolved.criteria.length === 1,
            String(resolved.criteria.length));
        check('the surviving clue is the valid one', resolved.criteria[0].kind === 'user');
        check('the unusable clue is reported, not silently dropped',
            resolved.unusable.length === 1 && resolved.unusable[0].kind === 'genus');
        check('the report says why', /was not found/.test(resolved.unusable[0].reason));
    }

    // 14. Malformed input stays a fatal input error even in auto mode.
    {
        const resolve = resolverWith(async () => {
            const error = new Error('“abc” is not an iNaturalist taxon ID.');
            error.malformedInput = true;
            throw error;
        });
        let raised = null;
        try {
            await resolve({taxon: 'abc'}, {});
        } catch (error) {
            raised = error;
        }
        check('malformed input is still an error', raised !== null && raised.malformedInput === true);
    }

    // 15. An API outage is not evidence that a clue does not exist.
    {
        const resolve = resolverWith(async () => { throw new Error('iNaturalist is unreachable'); });
        let raised = null;
        try {
            await resolve({genus: 'Amanita'}, {});
        } catch (error) {
            raised = error;
        }
        check('an outage is not treated as an unusable clue',
            raised !== null && /unreachable/.test(raised.message));
    }

    // A cancelled lookup is neither an outage nor an unusable clue.
    {
        const resolve = resolverWith(async () => { throw abortError(); });
        let raised = null;
        try {
            await resolve({genus: 'Amanita'}, {});
        } catch (error) {
            raised = error;
        }
        check('cancelling a clue lookup propagates', raised !== null && raised.name === 'AbortError');
    }

    if (failures.length) {
        failures.forEach(failure => console.error(`FAIL ${failure}`));
        throw new Error(`${failures.length} auto-mode assertion(s) failed`);
    }
    console.log('PASS iNat Finder auto mode');
}

main().catch(error => {
    console.error(error.stack || error);
    process.exit(1);
});
