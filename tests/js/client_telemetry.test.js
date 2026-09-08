'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const html = fs.readFileSync(path.join(__dirname, '../../app/templates/base_modern.html'), 'utf8');
const source = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)]
    .map(match => match[1]).find(script => script.includes("const endpoint = '/api/log/client'"));
assert.ok(source, 'Execute the telemetry script shipped in the base template');

function browser(fetchResult, collectorError = null, collectorResponse = () => new Response('{}')) {
    const reports = [];
    const collectorRequests = [];
    const requests = [];
    const window = {
        addEventListener() {},
        async fetch(...args) {
            if (args[0] === '/api/log/client') {
                reports.push(JSON.parse(args[1].body));
                collectorRequests.push(args);
                if (collectorError) throw collectorError;
                return collectorResponse();
            }
            requests.push(args);
            return fetchResult(...args);
        },
    };
    let time = 100;
    vm.runInNewContext(source, {
        window, URL, performance: {now: () => (time += 250)},
        document: {
            querySelector: selector => ({content: selector.includes('release') ? 'git:old-page' : 'csrf-token'}),
            visibilityState: 'hidden',
        },
        navigator: {onLine: false},
        location: new URL('https://dikarya.us/tree?token=private-page-query'),
    });
    return {window, reports, requests, collectorRequests};
}

test('collector refreshes CSRF once and retries the same bounded report', async () => {
    let attempt = 0;
    const b = browser(
        () => new Response(JSON.stringify({csrf_token: 'fresh-token'})), null,
        () => new Response('{}', {status: ++attempt === 1 ? 400 : 200})
    );
    b.window.reportClientError('test', new Error('failure'));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(b.requests.length, 1);
    assert.equal(b.requests[0][0], '/api/log/client/csrf');
    assert.equal(b.reports.length, 2);
    assert.deepEqual(b.reports[0], b.reports[1]);
    assert.equal(b.collectorRequests[1][1].headers['X-CSRFToken'], 'fresh-token');
});

test('persistent collector rejection has a bounded retry and does not recurse', async () => {
    const b = browser(
        () => new Response(JSON.stringify({csrf_token: 'fresh-token'})), null,
        () => new Response('{}', {status: 400})
    );
    b.window.reportClientError('test', new Error('failure'));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(b.requests.length, 1);
    assert.equal(b.reports.length, 2);
});

test('HTTP failures link to the server without reading or replacing the response', async () => {
    const response = new Response('private response body', {
        status: 500, headers: {'X-Request-Id': 'abcdef123456'},
    });
    const b = browser(() => response);
    const init = {method: 'POST', body: 'private sequence', headers: {'Authorization': 'Bearer private-token'}};
    const returned = await b.window.fetch('/api/job?token=private-query', init);
    assert.equal(returned, response);
    assert.equal(response.bodyUsed, false);
    assert.equal(b.requests[0][1], init);
    assert.equal(b.reports.length, 1);
    const report = b.reports[0];
    assert.equal(report.event, 'api_non_2xx');
    assert.equal(report.server_request_id, 'abcdef123456');
    assert.equal(report.http_status, 500);
    assert.equal(report.method, 'POST');
    assert.equal(report.duration_ms, 250);
    assert.equal(report.release, 'git:old-page');
    assert.equal(report.online, false);
    assert.equal(report.visibility, 'hidden');
    assert.equal(report.action, '/api/job');
    assert.ok(!JSON.stringify(report).includes('private'));
});

test('URL and Request inputs preserve method overrides and correlation', async () => {
    for (const input of [
        new URL('https://dikarya.us/api/job'),
        new Request('https://dikarya.us/api/job', {method: 'POST'}),
    ]) {
        const b = browser(() => new Response('', {status: 503}));
        await b.window.fetch(input, {method: 'PATCH'});
        assert.equal(b.requests[0][0], input);
        assert.equal(b.reports[0].method, 'PATCH');
        assert.equal(b.reports[0].action, '/api/job');
    }
    const b = browser(() => new Response('', {status: 503}));
    await b.window.fetch(new Request('https://dikarya.us/api/job', {method: 'POST'}));
    assert.equal(b.reports[0].method, 'POST');
});

test('network failures keep the original exception and carry connection state', async () => {
    const error = new TypeError('Failed to fetch');
    const b = browser(() => { throw error; });
    await assert.rejects(b.window.fetch('/api/job', {method: 'POST'}), err => err === error);
    assert.equal(b.reports[0].event, 'ui_action_failed');
    assert.equal(b.reports[0].action, 'network:/api/job');
    assert.equal(b.reports[0].online, false);
    assert.equal(b.reports[0].duration_ms, 250);
    assert.equal(b.reports[0].server_request_id, '');
});

test('cancellation, external APIs, successful requests and expected 4xx stay quiet', async () => {
    const abort = new DOMException('cancelled', 'AbortError');
    const b = browser(() => { throw abort; });
    await assert.rejects(b.window.fetch('/api/job'), err => err === abort);
    assert.equal(b.reports.length, 0);
    for (const [url, status] of [
        ['/api/job', 200], ['/api/job', 400], ['/api/job', 401], ['/api/job', 403],
        ['/api/job', 404], ['/api/job', 409], ['/api/job', 422], ['/api/job', 429],
        ['https://api.inaturalist.org/api/search?query=private', 500],
    ]) {
        const b = browser(() => new Response('', {status}));
        assert.equal((await b.window.fetch(url)).status, status);
        assert.equal(b.reports.length, 0);
    }
    const external = browser(() => { throw new TypeError('offline'); });
    await assert.rejects(external.window.fetch('https://api.inaturalist.org/v1/observations?search=private'));
    assert.equal(external.reports.length, 0);
});

test('a failing telemetry request does not recurse or affect the original response', async () => {
    const response = new Response('', {status: 500});
    const collectorError = new TypeError('collector unavailable');
    const b = browser(() => response, collectorError);
    assert.equal(await b.window.fetch('/api/job'), response);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(b.reports.length, 1);
    const count = b.reports.length;
    await assert.rejects(b.window.fetch('/api/log/client', {body: '{}'}), err => err === collectorError);
    assert.equal(b.reports.length, count + 1);
});
