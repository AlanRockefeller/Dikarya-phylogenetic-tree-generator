(() => {
    'use strict';

    const API = 'https://api.inaturalist.org/v1';
    const BATCH_SIZE = 200;
    const MAX_VARIATIONS = 100000;
    // Alan 9/10/26 - port of inat.finder.py 1.8.1: large-search confirmation threshold.
    // Auto mode pauses and asks before running a stage bigger than this; the
    // single-criterion search asks before starting one.
    const LARGE_SEARCH_THRESHOLD = 5000;
    // A stage bigger than this is refused outright rather than started. It was
    // never searched, so such a run reports an error and never "no match".
    const MAX_SEARCH_CANDIDATES = 1000000;
    // Alan 9/10/26 - inat.finder.py 1.8.1. Above this many candidates a stage
    // stops at the batch that produced a full match; below it the stage always
    // runs to the end.
    //
    // Finishing is the default because "the first full match" is weak evidence:
    // iNaturalist assigns observation numbers in upload order, so the numbers
    // either side of a mistyped one very often share an uploader and frequently
    // a taxon. With a single clue that coincidence satisfies a full match, and
    // the stage used to abort on it while the observation actually wanted sat
    // further down the same stage, never requested. Finishing a stage this size
    // costs about a second; reporting one confidently wrong observation costs
    // the answer.
    const EARLY_STOP_MIN_CANDIDATES = 5000;
    // 1.8.0 caps the auto ladder at three substituted digits by default; the
    // single-criterion search keeps its historical default of one.
    const AUTO_DEFAULT_MAX_DIGITS = 3;
    const MANUAL_DEFAULT_MAX_DIGITS = 1;
    // Stop pulling new batches after this many consecutive batches fail every
    // attempt, so a sustained outage does not grind through the whole search.
    const MAX_CONSECUTIVE_FAILED_BATCHES = 4;
    const REQUEST_TIMEOUT_MS = 20000;

    // Alan 8/31/26 - colour results by iNaturalist iconic taxon (Fungi magenta, Plantae green, etc).
    // iNaturalist's own iconic-taxon colors, so a fungus reads magenta and a
    // plant reads green here exactly as it does on iNaturalist.
    const ICONIC_TAXON_COLORS = {
        Animalia: '#1E90FF',
        Actinopterygii: '#1E90FF',
        Amphibia: '#1E90FF',
        Reptilia: '#1E90FF',
        Aves: '#1E90FF',
        Mammalia: '#1E90FF',
        Mollusca: '#FF4500',
        Arachnida: '#FF4500',
        Insecta: '#FF4500',
        Plantae: '#73AC13',
        Fungi: '#FF1493',
        Protozoa: '#691776',
        Chromista: '#993300'
    };
    const UNKNOWN_ICONIC_COLOR = '#AAAAAA';

    const form = document.getElementById('finder-form');
    const observationInput = document.getElementById('observation-input');
    const digitsSelect = document.getElementById('digits-off');
    const verboseInput = document.getElementById('verbose');
    const errorBox = document.getElementById('form-error');
    const noticeBox = document.getElementById('form-notices');
    const choiceBox = document.getElementById('taxon-choices');
    const choiceList = document.getElementById('taxon-choice-list');
    const searchButton = document.getElementById('search-button');
    const cancelButton = document.getElementById('cancel-button');
    const logSection = document.getElementById('log-section');
    const logOutput = document.getElementById('search-log');
    const resultsSection = document.getElementById('results-section');
    const resultsList = document.getElementById('results-list');
    const progressFill = document.getElementById('progress-fill');
    const progressTrack = document.getElementById('progress-track');
    const progressPercent = document.getElementById('progress-percent');
    const progressStatus = document.getElementById('progress-status');
    const progressStage = document.getElementById('progress-stage');
    const progressCount = document.getElementById('progress-count');
    const progressTotal = document.getElementById('progress-total');
    const progressEta = document.getElementById('progress-eta');

    // Auto mode: the observation number plus any combination of optional clues.
    const autoPanel = document.getElementById('auto-panel');
    const clueInputs = {
        genus: document.getElementById('clue-genus'),
        family: document.getElementById('clue-family'),
        taxon: document.getElementById('clue-taxon'),
        user: document.getElementById('clue-user'),
        project: document.getElementById('clue-project')
    };

    // Manual mode: the historical single-criterion search, kept intact behind
    // Advanced search options.
    const manualPanel = document.getElementById('manual-panel');
    const termInput = document.getElementById('search-term');
    const suggestionList = document.getElementById('taxon-suggestions');
    const suggestionStatus = document.getElementById('taxon-suggestion-status');
    const pinnedBox = document.getElementById('taxon-pinned');
    const pinnedSwatch = document.getElementById('taxon-pinned-swatch');
    const pinnedLabel = document.getElementById('taxon-pinned-label');
    const pinnedClear = document.getElementById('taxon-pinned-clear');

    const advancedDetails = document.getElementById('advanced-options');
    const keepLooking = document.getElementById('keep-looking');
    const keepLookingMessage = document.getElementById('keep-looking-message');
    const keepLookingCost = document.getElementById('keep-looking-cost');
    const keepLookingButton = document.getElementById('keep-looking-button');
    const keepLookingLabel = document.getElementById('keep-looking-label');

    let activeSearch = null;
    // The maximum-wrong-digits default follows the mode until the searcher sets
    // it themselves, after which their choice is left alone.
    let digitsTouched = false;

    const modeCopy = {
        genus: {label: '2. Expected genus', placeholder: 'e.g. Amanita', icon: 'fa-leaf'},
        family: {label: '2. Expected family', placeholder: 'e.g. Amanitaceae', icon: 'fa-sitemap'},
        taxon: {label: '2. Expected taxon ID', placeholder: 'e.g. 48419 or an iNaturalist taxon URL', icon: 'fa-fingerprint'},
        user: {label: '2. Expected observer', placeholder: 'e.g. alan_rockefeller', icon: 'fa-user'},
        project: {label: '2. Expected project', placeholder: 'Project ID, slug, URL, or exact title', icon: 'fa-people-group'}
    };

    function currentMode() {
        return form.querySelector('input[name="mode"]:checked').value;
    }

    // Alan 9/10/26 - which of the two searches is selected. Automatic is the
    // default and the one nearly everyone wants; the single-criterion search
    // lives on under Advanced search options for expert use and for debugging.
    function currentSearchMode() {
        const checked = form.querySelector('input[name="search-mode"]:checked');
        return checked ? checked.value : 'auto';
    }

    function updateSearchMode() {
        const auto = currentSearchMode() === 'auto';
        autoPanel.classList.toggle('hidden', !auto);
        manualPanel.classList.toggle('hidden', auto);
        // 1.8.0 caps the auto ladder at three substituted digits and leaves the
        // single-criterion search at its historical one.
        if (!digitsTouched) {
            digitsSelect.value = String(auto ? AUTO_DEFAULT_MAX_DIGITS : MANUAL_DEFAULT_MAX_DIGITS);
        }
        document.getElementById('digits-hint').textContent = auto
            ? 'How far the automatic search may widen. Each extra digit multiplies the numbers checked.'
            : 'How many digits of the number may be wrong.';
    }

    function updateMode() {
        const copy = modeCopy[currentMode()];
        document.getElementById('search-term-label').textContent = copy.label;
        termInput.placeholder = copy.placeholder;
        document.getElementById('search-term-icon').className = `fas ${copy.icon} absolute left-4 top-1/2 -translate-y-1/2 text-gray-400`;
    }

    function canonicalObservationId(value) {
        return value.replace(/^0+(?=\d)/, '');
    }

    function parseObservationId(value) {
        const input = value.trim();
        if (/^\d+$/.test(input)) return canonicalObservationId(input);
        const match = input.match(/(?:^|\/)observations\/(\d+)(?=\/?(?:[?#].*)?$)/i);
        return match ? canonicalObservationId(match[1]) : '';
    }

    function showError(message) {
        errorBox.textContent = message;
        errorBox.classList.remove('hidden');
    }

    function clearError() {
        errorBox.textContent = '';
        errorBox.classList.add('hidden');
        clearNotices();
        clearTaxonChoices();
    }

    // Alan 9/10/26 - a clue iNaturalist could not resolve is reported here rather
    // than through showError(): in auto mode it is news, not a failure, and the
    // search carries on with whatever else was supplied.
    function clearNotices() {
        noticeBox.replaceChildren();
        noticeBox.classList.add('hidden');
    }

    function addNotice(message, tone = 'warn') {
        const item = document.createElement('li');
        item.className = 'flex items-start gap-2';
        const icon = document.createElement('i');
        icon.className = tone === 'warn'
            ? 'fas fa-triangle-exclamation mt-0.5 flex-none text-amber-500'
            : 'fas fa-circle-info mt-0.5 flex-none text-journal-gold';
        const text = document.createElement('span');
        text.textContent = message;
        item.append(icon, text);
        noticeBox.appendChild(item);
        noticeBox.classList.remove('hidden');
    }

    function clearTaxonChoices() {
        choiceList.replaceChildren();
        choiceBox.classList.add('hidden');
    }

    // Alan 8/31/26 - resolve an observation or taxon to its iNaturalist iconic-taxon colour.
    function iconicColor(taxon) {
        return ICONIC_TAXON_COLORS[String((taxon || {}).iconic_taxon_name || '')] || UNKNOWN_ICONIC_COLOR;
    }

    // Alan 8/31/26 - 1.7.5 stops on an ambiguous genus/family name instead of guessing.
    // Show the candidates behind an ambiguous name, exactly as inat.finder.py
    // 1.7.5 does, and let one click re-run the search against that taxon ID.
    function showTaxonChoices(candidates) {
        choiceList.replaceChildren();
        candidates.forEach(taxon => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'flex w-full items-center gap-3 rounded-xl border border-gray-200 dark:border-gray-600 px-4 py-3 text-left transition-colors hover:border-journal-gold';
            button.style.borderLeft = `4px solid ${iconicColor(taxon)}`;
            const text = document.createElement('span');
            text.className = 'min-w-0 flex-1';
            const name = document.createElement('span');
            name.className = 'block font-semibold text-journal-dark dark:text-white';
            name.textContent = taxon.name || `Taxon ${taxon.id}`;
            const detail = document.createElement('span');
            detail.className = 'block text-xs text-gray-500 dark:text-gray-400';
            const parts = [`taxon ID ${taxon.id}`];
            if (taxon.iconic_taxon_name) parts.push(taxon.iconic_taxon_name);
            if (taxon.preferred_common_name) parts.push(taxon.preferred_common_name);
            detail.textContent = parts.join(' · ');
            text.append(name, detail);
            const chevron = document.createElement('i');
            chevron.className = 'fas fa-arrow-right text-xs text-gray-400';
            button.append(text, chevron);
            button.addEventListener('click', () => {
                if (currentSearchMode() === 'auto') {
                    // Auto mode already has a taxon-ID box: filling it keeps
                    // every other clue the searcher supplied.
                    clueInputs.taxon.value = String(taxon.id);
                    clueInputs.genus.value = '';
                    clueInputs.family.value = '';
                } else {
                    document.getElementById('mode-taxon').checked = true;
                    updateMode();
                    termInput.value = String(taxon.id);
                }
                clearError();
                form.requestSubmit(searchButton);
            });
            choiceList.appendChild(button);
        });
        choiceBox.classList.remove('hidden');
    }

    function setProgress(percent, status, checked = 0, total = 0, eta = '—') {
        const bounded = Math.max(0, Math.min(100, Math.round(percent)));
        progressFill.style.width = `${bounded}%`;
        progressTrack.setAttribute('aria-valuenow', String(bounded));
        progressPercent.textContent = `${bounded}%`;
        progressStatus.textContent = status;
        progressCount.textContent = `${checked.toLocaleString()} / ${total.toLocaleString()}`;
        progressEta.textContent = eta;
    }

    // Alan 9/10/26 - the ladder needs two numbers, not one: how far through the
    // current stage the search is, and how many observation numbers it has
    // checked altogether. Reporting only the first makes stage 2 look like a
    // restart from zero.
    function setStageLine(text) {
        progressStage.textContent = text;
    }

    function setCumulativeChecked(count) {
        progressTotal.textContent = count.toLocaleString();
    }

    function formatDuration(seconds) {
        if (!Number.isFinite(seconds) || seconds < 0) return '—';
        const rounded = Math.ceil(seconds);
        if (rounded < 60) return `${rounded}s`;
        const minutes = Math.floor(rounded / 60);
        const remainder = rounded % 60;
        return `${minutes}m ${remainder}s`;
    }

    function formatRoughDuration(seconds) {
        if (seconds < 60) return `${Math.max(1, Math.ceil(seconds))} sec`;
        return `${Math.ceil(seconds / 60)} min`;
    }

    function log(message, force = false) {
        if (!verboseInput.checked && !force) return;
        logSection.classList.remove('hidden');
        const stamp = new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
        logOutput.textContent += `[${stamp}] ${message}\n`;
        logOutput.scrollTop = logOutput.scrollHeight;
    }

    function sleep(ms, search) {
        return new Promise((resolve, reject) => {
            const timer = window.setTimeout(() => {
                search.sleepTimer = null;
                search.sleepReject = null;
                resolve();
            }, ms);
            search.sleepTimer = timer;
            search.sleepReject = reject;
            if (search.cancelled) {
                window.clearTimeout(timer);
                search.sleepTimer = null;
                search.sleepReject = null;
                reject(new DOMException('Search cancelled', 'AbortError'));
            }
        });
    }

    function apiError(message, status = null) {
        const error = new Error(message);
        error.status = status;
        error.noRetry = true;
        return error;
    }

    async function apiGet(path, params, search, attempts = 3) {
        const url = new URL(`${API}${path}`);
        Object.entries(params || {}).forEach(([key, value]) => url.searchParams.set(key, value));
        for (let attempt = 1; attempt <= attempts; attempt += 1) {
            if (search.cancelled) throw new DOMException('Search cancelled', 'AbortError');
            const controller = new AbortController();
            search.controller = controller;
            let retryDelay = null;
            let retryMessage = '';
            const timeout = window.setTimeout(() => {
                search.timedOutController = controller;
                controller.abort();
            }, REQUEST_TIMEOUT_MS);
            try {
                const response = await fetch(url, {
                    signal: controller.signal,
                    headers: {'Accept': 'application/json'}
                });
                if (response.ok) {
                    try {
                        return await response.json();
                    } catch (error) {
                        if (error.name === 'AbortError') throw error;
                        throw apiError('iNaturalist returned an unreadable response');
                    }
                }

                const retryable = [429, 500, 502, 503, 504].includes(response.status);
                if (response.status === 429 && attempt === attempts) {
                    throw apiError('iNaturalist is rate-limiting requests — wait a minute and try again.', 429);
                }
                if (!retryable || attempt === attempts) {
                    throw apiError(`iNaturalist returned HTTP ${response.status}`, response.status);
                }
                retryDelay = (Number(response.headers.get('Retry-After')) || attempt) * 1000;
                retryMessage = `iNaturalist is busy (HTTP ${response.status}); retrying in ${retryDelay / 1000}s.`;
            } catch (error) {
                if (error.name === 'AbortError') {
                    if (search.cancelled) throw error;
                    if (search.timedOutController !== controller) throw error;
                    if (attempt === attempts) {
                        throw apiError('iNaturalist did not respond within 20 seconds. Try again.');
                    }
                    retryDelay = attempt * 1000;
                    retryMessage = `iNaturalist request timed out; retrying (${attempt + 1}/${attempts}).`;
                } else if (error.noRetry || attempt === attempts) {
                    throw error;
                } else {
                    retryDelay = attempt * 1000;
                    retryMessage = `Network request failed; retrying (${attempt + 1}/${attempts}).`;
                }
            } finally {
                window.clearTimeout(timeout);
                if (search.controller === controller) search.controller = null;
                if (search.timedOutController === controller) search.timedOutController = null;
            }
            log(retryMessage, true);
            await sleep(retryDelay, search);
        }
        throw new Error('iNaturalist request failed');
    }

    function combinations(length, choose, callback, start = 0, picked = []) {
        if (picked.length === choose) {
            callback(picked);
            return;
        }
        for (let index = start; index <= length - (choose - picked.length); index += 1) {
            picked.push(index);
            combinations(length, choose, callback, index + 1, picked);
            picked.pop();
        }
    }

    function combinationCount(length, choose) {
        if (choose < 0 || choose > length) return 0;
        const smallerSide = Math.min(choose, length - choose);
        let count = 1;
        for (let index = 0; index < smallerSide; index += 1) {
            count = count * (length - index) / (index + 1);
        }
        return count;
    }

    function estimateVariationCount(number, digitsOff) {
        let count = 0;
        for (let changeCount = 1; changeCount <= digitsOff; changeCount += 1) {
            count += combinationCount(number.length, changeCount) * (9 ** changeCount);
        }
        // This intentionally remains an inexpensive upper bound: canonicalization
        // and duplicate insertions make the final set slightly smaller.
        if (number.length < 9) {
            const oneInserted = (number.length + 1) * 10;
            count += oneInserted + (oneInserted * (number.length + 2) * 10);
        }
        if (number.length > 5) {
            count += combinationCount(number.length, 1) + combinationCount(number.length, 2);
        }
        // With two or more wrong digits every adjacent swap is already covered by
        // a two-digit replacement, so transpositions only add candidates below that.
        if (digitsOff < 2) count += Math.max(0, number.length - 1);
        return count;
    }

    function generateChangedDigits(number, digitsOff) {
        const variations = [];
        const maximumChanges = Math.min(digitsOff, number.length);
        for (let changeCount = 1; changeCount <= maximumChanges; changeCount += 1) {
            combinations(number.length, changeCount, positions => {
                const chars = number.split('');
                const expand = depth => {
                    if (depth === positions.length) {
                        variations.push(chars.join(''));
                        return;
                    }
                    const position = positions[depth];
                    const original = number[position];
                    for (let digit = 0; digit <= 9; digit += 1) {
                        if (String(digit) === original) continue;
                        chars[position] = String(digit);
                        expand(depth + 1);
                    }
                    chars[position] = original;
                };
                expand(0);
            });
        }
        return variations;
    }

    // Alan 8/31/26 - 1.7.5 inserts a second missing digit at any position, not only at the ends.
    function insertEverywhere(value) {
        const inserted = [];
        for (let position = 0; position <= value.length; position += 1) {
            for (let digit = 0; digit <= 9; digit += 1) {
                inserted.push(`${value.slice(0, position)}${digit}${value.slice(position)}`);
            }
        }
        return inserted;
    }

    function generateAdditions(number) {
        // One missing digit at any position, then a second missing digit at any
        // position of each of those — interior pairs included, matching 1.7.5.
        const oneInserted = insertEverywhere(number);
        const variations = oneInserted.slice();
        oneInserted.forEach(base => variations.push(...insertEverywhere(base)));
        return variations;
    }

    function generateRemovals(number) {
        const variations = [];
        [1, 2].forEach(removeCount => {
            if (removeCount >= number.length) return;
            combinations(number.length, removeCount, positions => {
                const removed = new Set(positions);
                variations.push([...number].filter((_, index) => !removed.has(index)).join(''));
            });
        });
        return variations;
    }

    // Alan 8/31/26 - 1.7.5 also tries two adjacent digits typed the wrong way round.
    function generateTranspositions(number) {
        const variations = [];
        for (let index = 0; index < number.length - 1; index += 1) {
            if (number[index] === number[index + 1]) continue;
            variations.push(
                number.slice(0, index) + number[index + 1] + number[index] + number.slice(index + 2)
            );
        }
        return variations;
    }

    function buildVariations(number, digitsOff) {
        if (estimateVariationCount(number, digitsOff) > MAX_VARIATIONS) {
            throw new RangeError('Too many observation ID variations requested.');
        }
        const variations = generateChangedDigits(number, digitsOff);
        log(`Generated ${variations.length.toLocaleString()} changed-digit variations.`);
        if (number.length < 9) {
            const additions = generateAdditions(number);
            variations.push(...additions);
            log(`Added ${additions.length.toLocaleString()} variations with one or two missing digits restored at any position.`);
        }
        if (number.length > 5) {
            const removals = generateRemovals(number);
            variations.push(...removals);
            log(`Added ${removals.length.toLocaleString()} variations with one or two extra digits removed.`);
        }
        if (digitsOff < 2) {
            const transpositions = generateTranspositions(number);
            variations.push(...transpositions);
            log(`Added ${transpositions.length.toLocaleString()} variations with two adjacent digits swapped.`);
        }

        const originalNumericId = canonicalObservationId(number);
        const seen = new Set();
        const unique = [];
        variations.forEach(value => {
            if (!/^\d+$/.test(value) || (value.length > 1 && value.startsWith('0'))) return;
            const numericId = canonicalObservationId(value);
            if (numericId === originalNumericId || seen.has(numericId)) return;
            seen.add(numericId);
            unique.push(numericId);
        });
        return unique;
    }

    // Alan 9/10/26 - 1.8.0's auto mode drops a clue iNaturalist cannot resolve
    // and carries on with the rest, but must never do that because iNaturalist
    // was unreachable: an outage is not evidence that a genus does not exist.
    // These two markers are how the caller tells those apart. Manual mode still
    // treats every one of them as fatal, so no existing behaviour or message
    // changes - only auto mode reads the flags.
    function unresolvableClue(message, candidates = null) {
        const error = new Error(message);
        error.unresolvableClue = true;
        if (candidates) error.taxonCandidates = candidates;
        return error;
    }

    // Input that cannot be read at all. Fatal in BOTH modes: "abc" is not a clue
    // that turned out to be wrong, it is a value that is not a taxon ID.
    function malformedClue(message) {
        const error = new Error(message);
        error.malformedInput = true;
        return error;
    }

    async function resolveCriteria(mode, term, search) {
        if (mode === 'taxon') {
            const match = term.match(/^(?:.*\/taxa\/)?(\d+)/);
            const taxonId = match ? Number(match[1]) : NaN;
            if (!Number.isInteger(taxonId) || taxonId <= 0) {
                throw malformedClue(`“${term}” is not an iNaturalist taxon ID. Enter a number such as 48419, or an iNaturalist taxon URL.`);
            }
            let data;
            try {
                data = await apiGet(`/taxa/${taxonId}`, {}, search);
            } catch (error) {
                if (error.status === 404) throw unresolvableClue(`iNaturalist taxon ID ${taxonId} was not found.`);
                throw error;
            }
            const taxon = (data.results || []).find(item => String(item.id) === String(taxonId));
            if (!taxon) throw unresolvableClue(`iNaturalist taxon ID ${taxonId} was not found.`);
            const rank = taxon.rank ? ` (${taxon.rank})` : '';
            return {label: `${taxon.name || `Taxon ${taxonId}`}${rank}`, taxonId, taxon};
        }
        if (mode === 'genus' || mode === 'family') {
            const rank = mode;
            const rankLabel = rank[0].toUpperCase() + rank.slice(1);
            // Verify the name against the taxonomy and search by the taxon ID it
            // resolves to, the way inat.finder.py 1.7.5 does. Two endpoints are
            // consulted so a name that belongs to more than one taxon is caught
            // rather than silently resolved to whichever one ranked first.
            const exact = new Map();
            for (const path of ['/taxa/autocomplete', '/taxa']) {
                let data;
                try {
                    data = await apiGet(path, {q: term, rank, per_page: '30'}, search);
                } catch (error) {
                    // The second endpoint only confirms the first. Failing it when
                    // a name already resolved would turn a hiccup into "not found".
                    if (error.name === 'AbortError' || !exact.size) throw error;
                    break;
                }
                (data.results || []).forEach(item => {
                    if (!item || item.rank !== rank) return;
                    if (String(item.name || '').toLowerCase() !== term.toLowerCase()) return;
                    if (!Number.isInteger(Number(item.id))) return;
                    if (!exact.has(String(item.id))) exact.set(String(item.id), item);
                });
                if (exact.size > 1) break;
            }
            if (exact.size > 1) {
                const candidates = [...exact.values()];
                throw unresolvableClue(
                    `${rankLabel} “${term}” matches ${candidates.length} taxa in the iNaturalist taxonomy. Choose the one you meant.`,
                    candidates
                );
            }
            const taxon = exact.size === 1 ? [...exact.values()][0] : null;
            if (!taxon) {
                throw unresolvableClue(`${rankLabel} “${term}” was not found in the iNaturalist taxonomy.`);
            }
            return {label: `${taxon.name} (taxon ID ${taxon.id})`, taxonId: Number(taxon.id), taxon};
        }
        if (mode === 'user') {
            let data;
            try {
                data = await apiGet(`/users/${encodeURIComponent(term)}`, {}, search);
            } catch (error) {
                if (error.status === 404) throw unresolvableClue(`iNaturalist user “${term}” was not found.`);
                throw error;
            }
            const user = (data.results || []).find(item => String(item.login || '').toLowerCase() === term.toLowerCase());
            if (!user) throw unresolvableClue(`iNaturalist user “${term}” was not found.`);
            return {label: user.login};
        }

        const projectUrlMatch = term.match(/(?:^|\/)projects\/([^/?#]+)/i);
        if (/^\d+$/.test(term)) {
            let data;
            try {
                data = await apiGet(`/projects/${term}`, {}, search);
            } catch (error) {
                if (error.status === 404) throw unresolvableClue(`iNaturalist project ID “${term}” was not found.`);
                throw error;
            }
            const project = (data.results || [])[0];
            if (!project) throw unresolvableClue(`iNaturalist project ID “${term}” was not found.`);
            return {label: project.title, projectId: String(project.id), project};
        }

        const query = projectUrlMatch ? projectUrlMatch[1] : term;
        if (!/\s/.test(query)) {
            try {
                const direct = await apiGet(`/projects/${encodeURIComponent(query)}`, {}, search);
                const project = (direct.results || [])[0];
                if (project) return {label: project.title, projectId: String(project.id), project};
            } catch (error) {
                if (error.status !== 404) throw error;
            }
        }

        const data = await apiGet('/projects', {q: query, per_page: '10'}, search);
        const lower = query.toLowerCase();
        const exact = (data.results || []).filter(item =>
            String(item.slug || '').toLowerCase() === lower || String(item.title || '').toLowerCase() === lower
        );
        if (exact.length !== 1) {
            if (!exact.length && (data.results || []).length) {
                const suggestions = data.results.slice(0, 3).map(item => `${item.title} (ID ${item.id})`).join('; ');
                throw unresolvableClue(`No exact project match for “${term}”. Similar projects: ${suggestions}. Use the project ID or exact slug.`);
            }
            if (exact.length > 1) throw unresolvableClue(`More than one project matches “${term}”. Use the numeric project ID.`);
            throw unresolvableClue(`iNaturalist project “${term}” was not found.`);
        }
        return {label: exact[0].title, projectId: String(exact[0].id), project: exact[0]};
    }

    // ---- section:matching ----
    function observationMatches(observation, mode, criteria) {
        if (mode === 'project') return true;
        const record = observation || {};
        if (mode === 'user') {
            return String(record.user?.login || '').toLowerCase() === criteria.label.toLowerCase();
        }
        const taxon = record.taxon || {};
        if (Number(taxon.id) === criteria.taxonId) return true;
        // Alan 8/31/26 - The same ancestry test as _taxon_id_matches() in
        // inat_finder.py. ancestor_ids is what /v1/observations normally carries,
        // but a record that omits it and supplies the expanded `ancestors` objects
        // instead used to read as "not in this genus" here while the CLI matched
        // it -- and a false negative in this direction is a match the searcher
        // never sees. Both lists are type-checked because .some() on a non-array
        // throws, which would fail the whole batch rather than one observation.
        const ancestorIds = Array.isArray(taxon.ancestor_ids) ? taxon.ancestor_ids : [];
        if (ancestorIds.some(id => Number(id) === criteria.taxonId)) return true;
        const ancestors = Array.isArray(taxon.ancestors) ? taxon.ancestors : [];
        return ancestors.some(
            ancestor => ancestor && Number(ancestor.id) === criteria.taxonId
        );
    }

    async function checkBatch(ids, mode, criteria, search) {
        const params = {id: ids.join(','), per_page: String(BATCH_SIZE)};
        if (criteria.projectId) params.project_id = criteria.projectId;
        const data = await apiGet('/observations', params, search);
        return (data.results || []).filter(observation => observationMatches(observation, mode, criteria));
    }

    // ---- section:auto-candidates ----
    // Alan 9/10/26 - port of inat.finder.py 1.8.1's auto-mode candidate ladder.
    //
    // Order matters here as much as membership. A paused deep search continues
    // from the position it stopped at, so a plan that yielded the same set in a
    // different order would silently skip or re-request candidates, and a plan
    // that yielded a slightly different set would change what the site can find
    // at all. tests/fixtures/inat_finder_candidate_parity.json pins both against
    // the CLI: it carries each stage's totals and a SHA-256 of the ordered
    // candidate sequence, generated by scripts/dikarya_export_finder_parity.py.
    // Regenerate it after syncing inat_finder.py, and expect this code to change
    // in the same commit if the hashes move.

    function isValidCandidate(value) {
        return Boolean(value) && (value.length === 1 || !value.startsWith('0'));
    }

    // Digits that may replace number[index]. Position 0 of a multi-digit number
    // may never become 0, so a substitution never quietly produces a shorter
    // number. This is also why the counts below are exact rather than an
    // upper bound: the excluded digit is excluded from the count too.
    function replacementDigits(number, index, length) {
        const first = index === 0 && length > 1 ? 1 : 0;
        const options = [];
        for (let digit = first; digit <= 9; digit += 1) {
            const value = String(digit);
            if (value !== number[index]) options.push(value);
        }
        return options;
    }

    // itertools.combinations and itertools.product, as generators, in the same
    // orders CPython yields them: combinations lexicographically, product with
    // the rightmost position varying fastest. Both reuse one array, which is safe
    // because every consumer reads it before asking for the next value.
    function* iterCombinations(length, choose) {
        if (choose < 0 || choose > length) return;
        const picked = new Array(choose);
        function* step(start, depth) {
            if (depth === choose) {
                yield picked;
                return;
            }
            for (let index = start; index <= length - (choose - depth); index += 1) {
                picked[depth] = index;
                yield* step(index + 1, depth + 1);
            }
        }
        yield* step(0, 0);
    }

    function* iterProduct(options) {
        const width = options.length;
        const chosen = new Array(width);
        function* step(depth) {
            if (depth === width) {
                yield chosen;
                return;
            }
            for (const value of options[depth]) {
                chosen[depth] = value;
                yield* step(depth + 1);
            }
        }
        yield* step(0);
    }

    // Candidates differing from the original in one to digitsOff positions. A
    // generator on purpose: the replacement space grows combinatorially and must
    // never be materialized in full.
    function* iterDigitVariations(number, digitsOff) {
        if (digitsOff <= 0) {
            yield number;
            return;
        }
        const length = number.length;
        const maximum = Math.min(digitsOff, length);
        for (let changeCount = 1; changeCount <= maximum; changeCount += 1) {
            for (const positions of iterCombinations(length, changeCount)) {
                const options = positions.map(index => replacementDigits(number, index, length));
                for (const replacements of iterProduct(options)) {
                    const chars = number.split('');
                    for (let slot = 0; slot < positions.length; slot += 1) {
                        chars[positions[slot]] = replacements[slot];
                    }
                    const candidate = chars.join('');
                    // Only reachable when the input itself has a leading zero.
                    if (isValidCandidate(candidate)) yield candidate;
                }
            }
        }
    }

    // Count replacement variations exactly, without generating them, with the
    // same small polynomial DP the CLI uses. A stage has to announce its real
    // size before it builds a single candidate, and "the number the progress bar
    // counts down" and "the number of requests this will make" must be the same
    // number. Overflows to Infinity on hostile input, which reads as over-cap.
    function countDigitVariations(number, digitsOff) {
        const length = number.length;
        if (digitsOff <= 0 || length === 0) return 0;
        // Coefficient k of counts is the number of variations changing k digits.
        let counts = [1];
        for (let index = 0; index < length; index += 1) {
            const options = replacementDigits(number, index, length).length;
            // A leading-zero input can only produce valid IDs by changing position 0.
            const forced = index === 0 && length > 1 && number[0] === '0';
            const updated = new Array(counts.length + 1).fill(0);
            for (let changed = 0; changed < counts.length; changed += 1) {
                const total = counts[changed];
                if (!forced) updated[changed] += total;
                updated[changed + 1] += total * options;
            }
            counts = updated;
        }
        let sum = 0;
        for (let changed = 1; changed <= Math.min(digitsOff, length); changed += 1) {
            sum += counts[changed];
        }
        return sum;
    }

    function uniqueByIntegerValue(sequence) {
        const seen = new Set();
        const result = [];
        for (const item of sequence) {
            if (typeof item !== 'string' || !/^\d+$/.test(item)) continue;
            if (item.length > 1 && item.startsWith('0')) continue;
            // Leading zeroes are already excluded, so the string is the numeric
            // value's one canonical spelling and compares as the CLI's int() does.
            if (seen.has(item)) continue;
            seen.add(item);
            result.push(item);
        }
        return result;
    }

    // One digit inserted at every position, then - because a hurried transcription
    // drops two as easily as one - a second digit inserted anywhere in each of
    // those. Bases with a leading zero are still expanded, because a second
    // leading digit can make them valid again ("0123" -> "50123").
    function* iterDigitInsertions(number, maxAddedDigits = 2) {
        const seen = new Set();
        const offer = value => {
            if (!isValidCandidate(value)) return false;
            if (seen.has(value)) return false;
            seen.add(value);
            return true;
        };
        const oneInserted = [];
        for (let position = 0; position <= number.length; position += 1) {
            for (let digit = 0; digit <= 9; digit += 1) {
                oneInserted.push(`${number.slice(0, position)}${digit}${number.slice(position)}`);
            }
        }
        for (const candidate of oneInserted) {
            if (offer(candidate)) yield candidate;
        }
        if (maxAddedDigits >= 2) {
            for (const base of oneInserted) {
                for (let position = 0; position <= base.length; position += 1) {
                    for (let digit = 0; digit <= 9; digit += 1) {
                        const candidate = `${base.slice(0, position)}${digit}${base.slice(position)}`;
                        if (offer(candidate)) yield candidate;
                    }
                }
            }
        }
    }

    function generateDigitRemovals(number, maxRemovedDigits = 2) {
        const length = number.length;
        if (!length) return [];
        const variations = new Set();
        for (let removeCount = 1; removeCount <= Math.min(maxRemovedDigits, length); removeCount += 1) {
            for (const keep of iterCombinations(length, length - removeCount)) {
                let candidate = '';
                for (const index of keep) candidate += number[index];
                if (isValidCandidate(candidate)) variations.add(candidate);
            }
        }
        // Sorted before deduplication so the order matches the CLI's sorted(set).
        return uniqueByIntegerValue([...variations].sort());
    }

    // Two adjacent digits typed the wrong way round (123456789 -> 123465789).
    // Swaps of equal digits produce the original number and are skipped.
    function generateDigitTranspositions(number) {
        const seen = new Set();
        const variations = [];
        for (let index = 0; index < number.length - 1; index += 1) {
            if (number[index] === number[index + 1]) continue;
            const candidate = number.slice(0, index)
                + number[index + 1] + number[index] + number.slice(index + 2);
            if (!isValidCandidate(candidate)) continue;
            if (seen.has(candidate)) continue;
            seen.add(candidate);
            variations.push(candidate);
        }
        return variations;
    }

    // A deduplicated search space, sized up front and streamed on demand.
    //
    // The candidate classes cannot collide with each other: insertions and
    // removals change the number's length, and transpositions - which do not -
    // are only added below two substituted digits, because a two-digit
    // replacement search already contains every adjacent swap. So no observation
    // ID is ever requested twice and `total` is the true number of API-checked
    // candidates rather than an estimate.
    function createCandidatePlan(number, digitsOff, addDigits, removeDigits) {
        const plan = {
            number,
            digitsOff,
            replacementCount: countDigitVariations(number, digitsOff),
            additions: [],
            removals: [],
            transpositions: []
        };
        if (digitsOff > 0) {
            if (addDigits) plan.additions = [...iterDigitInsertions(number, 2)];
            if (removeDigits) plan.removals = generateDigitRemovals(number, 2);
            if (digitsOff < 2) plan.transpositions = generateDigitTranspositions(number);
        }
        const seen = new Set(/^\d+$/.test(number) && number ? [number] : []);
        plan.extras = [];
        for (const candidate of [...plan.additions, ...plan.removals, ...plan.transpositions]) {
            if (seen.has(candidate)) continue;
            seen.add(candidate);
            plan.extras.push(candidate);
        }
        plan.total = plan.replacementCount + plan.extras.length;
        plan.candidates = function* candidates() {
            if (plan.digitsOff > 0) yield* iterDigitVariations(plan.number, plan.digitsOff);
            yield* plan.extras;
        };
        return plan;
    }

    // Insertions only make sense below nine digits and removals only above five,
    // so those switches come from the number, never from digitsOff.
    function buildCandidatePlan(number, digitsOff) {
        return createCandidatePlan(
            number,
            digitsOff,
            digitsOff > 0 && number.length < 9,
            digitsOff > 0 && number.length > 5
        );
    }

    const STAGE_ORDINALS = {1: 'one', 2: 'two', 3: 'three', 4: 'four', 5: 'five'};

    // Describe a rung in terms of what it really searches. Deliberately derived
    // from the plan rather than hard-coded, because a plan does not add one edit
    // class per digitsOff: it always tries up to two inserted and two removed
    // digits when those classes are enabled at all, and contributes adjacent
    // swaps only below two substituted digits. Calling stage 1 "one digit off"
    // would be a plain lie about what was checked.
    function autoStageLabel(index, plan) {
        if (index <= 0) return 'the number exactly as supplied';
        const ordinal = STAGE_ORDINALS[index] || String(index);
        const parts = [`${ordinal} substituted ${index === 1 ? 'digit' : 'digits'}`];
        if (plan.transpositions.length) parts.push('adjacent swaps');
        if (plan.additions.length && plan.removals.length) parts.push('missing or extra digits');
        else if (plan.additions.length) parts.push('missing digits');
        else if (plan.removals.length) parts.push('extra digits');
        if (parts.length === 1) return parts[0];
        if (parts.length === 2) return `${parts[0]} and ${parts[1]}`;
        return `${parts.slice(0, -1).join(', ')}, and ${parts[parts.length - 1]}`;
    }

    // One rung of the ladder: a plan minus every candidate already tried.
    //
    // The ladder works because the plans nest as sets - plan(n,1) < plan(n,2) <
    // plan(n,3) - so stage k is plan k with everything an earlier stage yielded
    // filtered out. The size is plan.total - seenIds.size measured when the stage
    // starts, and not plan(k).total - plan(k-1).total: a stage can end before its
    // candidates run out, which leaves candidates that never entered seenIds for
    // the next stage to pick up. Only this form declares a total equal to what
    // will really be attempted, which is what the progress bar promises.
    function createAutoStage(index, plan, seenIds) {
        const stage = {
            index,
            plan,
            seenIds,
            label: autoStageLabel(index, plan),
            total: Math.max(0, plan.total - seenIds.size),
            // Counts entries pulled from the plan, not candidates yielded,
            // because that is the position a continuation has to resume from.
            planPosition: 0
        };
        stage.candidates = function* candidates() {
            let position = 0;
            for (const candidate of plan.candidates()) {
                position += 1;
                stage.planPosition = position;
                if (seenIds.has(candidate)) continue;
                seenIds.add(candidate);
                yield candidate;
            }
        };
        stage.exhausted = () => stage.planPosition >= plan.total;
        return stage;
    }

    // Combining a project with other clues costs a second request per batch,
    // since the main request cannot be filtered by the project without hiding
    // everything the other clues might have matched.
    function estimateStageSeconds(total, membershipProjectId) {
        const batches = Math.ceil(total / BATCH_SIZE);
        return Math.round((membershipProjectId ? batches * 2 : batches) * 1.5);
    }

    // ---- section:auto-scoring ----
    // What one clue has to say about one observation.
    //
    // UNKNOWN is the important member: project membership is answered by a
    // separate request, and when that request fails the genus, user and taxon
    // evidence for the same observation is still perfectly good. Saying "unknown"
    // keeps that evidence instead of throwing the batch away, and it never counts
    // toward a score, so an unconfirmed clue can never end the search early.
    const EVIDENCE = {MATCH: 'match', NO_MATCH: 'no_match', UNKNOWN: 'unknown'};

    // The label each clue gets in the UI. Kept apart from the CLI's `kind` so the
    // page can say "Observer" where the CLI says "user".
    const CLUE_LABELS = {
        genus: 'Genus',
        family: 'Family',
        taxon: 'Taxon ID',
        user: 'Observer',
        project: 'Project'
    };

    function makeTaxonCriterion(kind, value, label, taxonId, taxon) {
        return {
            kind,
            value,
            label,
            taxonId,
            taxon,
            // Taxonomy is decidable from the observation alone, so the batch
            // context is not consulted. observationMatches() is the same ancestry
            // test the single-criterion search uses, deliberately shared so the
            // two modes can never disagree about what is inside a genus.
            evaluate(observation) {
                return observationMatches(observation, 'genus', {taxonId})
                    ? EVIDENCE.MATCH
                    : EVIDENCE.NO_MATCH;
            }
        };
    }

    function makeUserCriterion(username) {
        return {
            kind: 'user',
            value: username,
            label: `observer @${username}`,
            evaluate(observation) {
                return observationMatches(observation, 'user', {label: username})
                    ? EVIDENCE.MATCH
                    : EVIDENCE.NO_MATCH;
            }
        };
    }

    // A clue answered by the batch, not by the observation. Whether an
    // observation is in a project is something only iNaturalist can say - a
    // collection project's membership is rule-based and does not appear in the
    // observation record - so this reads the answer out of the batch context.
    // When that answer is missing the verdict is UNKNOWN, not NO_MATCH: an
    // unanswered question must never look like a negative one.
    function makeProjectCriterion(value, label) {
        return {
            kind: 'project',
            value,
            label,
            evaluate(observation, context) {
                const members = context && context.projectMemberIds;
                if (!members) return EVIDENCE.UNKNOWN;
                return members.has(String(observation.id)) ? EVIDENCE.MATCH : EVIDENCE.NO_MATCH;
            }
        };
    }

    // Scoring is any-of on purpose. Requiring every clue to agree would hide the
    // real observation whenever one supplied element was itself wrong, which is
    // the common case auto mode exists for; ranking by how many clues agreed
    // keeps the best answer at the top without throwing the near misses away.
    function scoreObservation(observation, context, criteria) {
        const matched = [];
        const unknown = [];
        criteria.forEach(criterion => {
            const verdict = criterion.evaluate(observation, context);
            if (verdict === EVIDENCE.MATCH) matched.push(criterion.kind);
            else if (verdict === EVIDENCE.UNKNOWN) unknown.push(criterion.kind);
        });
        return {matched, unknown};
    }

    // True when every clue agreed. An UNKNOWN clue can never make this true.
    function isFullMatch(matched, criteria) {
        return Boolean(criteria.length) && matched.length === criteria.length;
    }

    // Deduplicate by observation ID and sort best-first. An observation can be
    // scored more than once - the number as supplied may also turn up as a
    // candidate - so the highest-scoring copy wins.
    //
    // Score decides the order first. Everything after it exists because equal
    // scores are the common case rather than the rare one: with a single clue
    // every hit scores 1 of 1, so without a tie-break the "best" match would be
    // whichever candidate happened to have the lowest number. Ties therefore
    // break on the stage that found the match (fewer digits off is a likelier
    // typo), then on how far the number is from the one actually typed, and only
    // then on the number itself, which keeps the order stable between runs -
    // something a list re-ranked in place as results arrive depends on.
    //
    // `origin` is the number as supplied. It sorts first when it matched, having
    // both the lowest stage and a distance of zero.
    function rankMatches(matches, origin = null) {
        const originValue = Number(origin);
        const hasOrigin = Number.isFinite(originValue);
        const best = new Map();
        matches.forEach(match => {
            const id = String(match.observation.id);
            const current = best.get(id);
            if (!current || match.matched.length > current.matched.length) best.set(id, match);
        });
        // A match with no stage - the single-criterion search leaves it null -
        // sorts after every staged match rather than ahead of stage 0.
        const stageOf = match =>
            (match.stage === null || match.stage === undefined ? Infinity : match.stage);
        return [...best.values()].sort((left, right) => {
            const byScore = right.matched.length - left.matched.length;
            if (byScore) return byScore;
            const byStage = stageOf(left) - stageOf(right);
            // Infinity - Infinity is NaN, which is falsy and falls through.
            if (byStage) return byStage;
            const leftId = Number(left.observation.id) || 0;
            const rightId = Number(right.observation.id) || 0;
            if (hasOrigin) {
                const byDistance = Math.abs(leftId - originValue) - Math.abs(rightId - originValue);
                if (byDistance) return byDistance;
            }
            return leftId - rightId;
        });
    }

    // ---- section:auto-resolve ----
    // The order clues are verified in, which is also the order they are scored
    // and listed. It matches the CLI's, so a "2 of 3: genus, user" reads the same
    // in both places.
    const AUTO_CLUE_KINDS = ['genus', 'family', 'taxon', 'user', 'project'];

    // Verify every clue the user supplied and report which ones cannot be used.
    //
    // This is the whole difference between the two modes. A manual search has
    // exactly one criterion and an unresolvable one is a fatal input error,
    // exactly as before. An auto search may have several, and one that cannot be
    // resolved is reported, dropped and left out of scoring while the rest carry
    // on - the point of auto mode being that on a foray the mistaken element is
    // as often the genus as the number.
    //
    // What that does NOT change: malformed input is always fatal, and a network
    // or API failure is always an outage rather than an unresolvable clue. A clue
    // must never be silently discarded because iNaturalist was unreachable.
    async function resolveAutoCriteria(clues, search, hooks = {}) {
        const {onMessage = () => {}, onResolved = () => {}} = hooks;
        const criteria = [];
        const unusable = [];
        let projectIdParam = null;
        let membershipProjectId = null;
        let projectLabel = null;

        for (const kind of AUTO_CLUE_KINDS) {
            const term = String((clues && clues[kind]) || '').trim();
            if (!term) continue;
            onMessage(`Verifying ${CLUE_LABELS[kind].toLowerCase()} “${term}” on iNaturalist…`);
            let resolved;
            try {
                resolved = await resolveCriteria(kind, term, search);
            } catch (error) {
                if (error && error.name === 'AbortError') throw error;
                // "abc" is not a clue that turned out to be wrong, it is input
                // that cannot be read - fatal in both modes.
                if (error && error.malformedInput) throw error;
                // Anything that is not iNaturalist saying "no such thing" is an
                // outage, and an outage is not evidence about the clue.
                if (!error || !error.unresolvableClue) throw error;
                unusable.push({
                    kind,
                    value: term,
                    reason: error.message,
                    candidates: error.taxonCandidates || null
                });
                onMessage(`Ignoring the ${CLUE_LABELS[kind].toLowerCase()} clue and continuing.`);
                continue;
            }
            onMessage(`✓ ${CLUE_LABELS[kind]} verified: ${resolved.label}.`);
            onResolved(kind, resolved);
            if (kind === 'project') {
                projectLabel = resolved.label;
                criteria.push(makeProjectCriterion(term, `project '${resolved.label}'`));
            } else if (kind === 'user') {
                criteria.push(makeUserCriterion(resolved.label));
            } else {
                criteria.push(makeTaxonCriterion(
                    kind, term, `${CLUE_LABELS[kind]} ${resolved.label}`, resolved.taxonId, resolved.taxon
                ));
            }
            if (kind === 'project') {
                // A project on its own can be answered by filtering the main
                // request, which is both cheaper and correct for collection
                // projects. Sharing the search with other clues rules that out -
                // the filter would hide every observation the other clues might
                // have matched - so membership moves to a second request per
                // batch instead. A clue that was SUPPLIED but turned out
                // unusable still counts here: the CLI decides on what was asked
                // for, not on what survived verification.
                const otherSupplied = AUTO_CLUE_KINDS.some(other =>
                    other !== 'project' && String((clues && clues[other]) || '').trim()
                );
                if (criteria.length === 1 && !otherSupplied) projectIdParam = resolved.projectId;
                else membershipProjectId = resolved.projectId;
            }
        }

        return {criteria, unusable, projectIdParam, membershipProjectId, projectLabel};
    }

    // ---- section:auto-ladder ----
    // Check one set of candidates against every clue, and report what matched.
    //
    // This is the single-pass unit the ladder repeats. stopOnFullMatch is what
    // makes "stops at the first hit" literally true: without it, a hit in the
    // first batch of a 59,000-candidate stage would still cost the whole stage.
    // Every match in the batch that triggered the stop is kept; the candidates
    // never requested are reported through `consumed`, not as unchecked, because
    // nothing failed.
    //
    // Cancellation is caught here rather than left to propagate, so that matches
    // already found in this stage are returned instead of being lost with the
    // rejected promise. "Cancelling shows what was found so far" has to include
    // the stage that was running when Cancel was pressed.
    async function runStagePass(options) {
        const {
            candidates,
            total,
            criteria,
            stageIndex,
            projectIdParam = null,
            membershipProjectId = null,
            stopOnFullMatch = true,
            fetchBatch,
            fetchMembership,
            onMatches = () => {},
            onProgress = () => {},
            onMessage = () => {},
            betweenBatches = async () => {},
            retryRounds = 1,
            batchSize = BATCH_SIZE
        } = options;

        const matches = [];
        let unchecked = 0;
        let consumed = 0;
        let membershipUnknown = 0;
        let foundFull = false;
        let stoppedEarly = false;
        let cancelled = false;
        let consecutiveFailures = 0;

        const iterator = candidates[Symbol.iterator]();
        const takeBatch = () => {
            const batch = [];
            while (batch.length < batchSize) {
                const step = iterator.next();
                if (step.done) break;
                batch.push(step.value);
            }
            return batch;
        };

        const attemptBatch = async batch => {
            let results;
            try {
                results = await fetchBatch(batch, projectIdParam);
            } catch (error) {
                if (error && error.name === 'AbortError') throw error;
                onMessage(
                    `Error fetching batch: ${error.message} `
                    + `(${batch.length} candidate(s) not checked yet)`
                );
                return {ok: false};
            }
            let context = {projectMemberIds: null};
            if (projectIdParam) {
                // The request was already filtered server-side, so everything it
                // returned is a member and nothing else in the batch is.
                context = {
                    projectMemberIds: new Set(results
                        .filter(item => item && item.id !== undefined)
                        .map(item => String(item.id)))
                };
            } else if (membershipProjectId && results.length) {
                try {
                    context = {projectMemberIds: await fetchMembership(batch, membershipProjectId)};
                } catch (error) {
                    if (error && error.name === 'AbortError') throw error;
                    // The observations themselves came back fine. Keep that
                    // evidence and mark only the project clue unknown here.
                    membershipUnknown += 1;
                    onMessage(
                        `Could not check project membership: ${error.message} `
                        + `(project membership unknown for ${results.length} observation(s) in this batch)`
                    );
                }
            }
            return {ok: true, results, context};
        };

        const evaluate = (results, context) => {
            const fresh = [];
            results.forEach(observation => {
                if (!observation || typeof observation !== 'object') return;
                const {matched, unknown} = scoreObservation(observation, context, criteria);
                if (!matched.length) return;
                const match = {observation, matched, unknown, stage: stageIndex};
                matches.push(match);
                fresh.push(match);
                if (isFullMatch(matched, criteria)) foundFull = true;
            });
            return fresh;
        };

        try {
            for (;;) {
                const batch = takeBatch();
                if (!batch.length) break;
                let outcome = await attemptBatch(batch);
                for (let round = 0; !outcome.ok && round < retryRounds; round += 1) {
                    onMessage(`Retrying a failed batch of ${batch.length} IDs (attempt ${round + 2}).`);
                    outcome = await attemptBatch(batch);
                }
                consumed += batch.length;
                if (outcome.ok) {
                    consecutiveFailures = 0;
                    onProgress(batch.length, true);
                    const fresh = evaluate(outcome.results, outcome.context);
                    if (fresh.length) await onMatches(fresh);
                    if (stopOnFullMatch && foundFull) {
                        stoppedEarly = true;
                        break;
                    }
                    await betweenBatches();
                    continue;
                }
                unchecked += batch.length;
                consecutiveFailures += 1;
                onProgress(batch.length, false);
                if (consecutiveFailures >= MAX_CONSECUTIVE_FAILED_BATCHES) {
                    // A sustained outage stops the stage rather than grinding
                    // through every remaining batch to fail at each one.
                    const skipped = Math.max(0, total - consumed);
                    if (skipped) {
                        unchecked += skipped;
                        onProgress(skipped, false);
                    }
                    onMessage(
                        `Stopping after ${consecutiveFailures} consecutive batches failed; `
                        + `${skipped.toLocaleString()} planned candidate(s) remain unchecked.`
                    );
                    break;
                }
                await betweenBatches();
            }
        } catch (error) {
            if (!error || error.name !== 'AbortError') throw error;
            cancelled = true;
        }

        return {matches, unchecked, consumed, membershipUnknown, stoppedEarly, cancelled, foundFull};
    }

    // Climb the ladder of typo hypotheses until something matches, or nothing does.
    //
    // The ladder is stage 0 (the number exactly as supplied) then one plan per
    // digitsOff up to digitsCap, each stage yielding only what earlier stages did
    // not already try. It stops at the first FULL match - one where every usable
    // clue agreed - because a partial match usually means one of the clues is
    // itself wrong, and that is worth widening the search to check. With a single
    // clue, full score is one, so this is exactly "stop at the first hit".
    //
    // The CLI hands out an --auto-resume token at every voluntary stop and
    // replays the tried candidates offline to rebuild its seen set. Here the set
    // and the ladder position simply stay in memory across the pause, which gives
    // the same invariant - a continuation begins exactly where the search stopped
    // and never re-requests an ID - without a token to carry.
    async function runAutoLadder(options) {
        const {
            number,
            criteria,
            digitsCap,
            projectIdParam = null,
            membershipProjectId = null,
            fetchBatch,
            fetchMembership,
            onStage = () => {},
            onProgress = () => {},
            onMatches = () => {},
            onMessage = () => {},
            onOriginal = () => {},
            betweenBatches = async () => {},
            largeThreshold = LARGE_SEARCH_THRESHOLD,
            maxStageCandidates = MAX_SEARCH_CANDIDATES,
            earlyStopMin = EARLY_STOP_MIN_CANDIDATES,
            // Continuing a search: the IDs already tried, and the rung to resume
            // on. The set is the whole cursor - a stage skips what it contains,
            // so a stage stopped part-way simply picks up its remainder.
            seenIds = new Set(),
            startStage = 1,
            // Stage 0 is the number as supplied. A continuation has already
            // checked and displayed it, and re-requesting it would break the
            // promise that no observation number is checked twice.
            skipOriginal = false,
            // One-shot permission to run a stage bigger than largeThreshold,
            // spent on the first such stage. A later large stage asks again.
            allowLargeStage = false
        } = options;

        const matches = [];
        const stages = [];
        const notices = [];
        let largeStageAllowance = allowLargeStage;
        let uncheckedTotal = 0;
        let membershipUnknownTotal = 0;
        let cancelled = false;
        let stopReason = 'exhausted';
        let original = null;
        let originalScore = null;
        let lastStage = null;
        let continuation = null;

        // Every exit comes through here so that one rule holds everywhere: if
        // anything went unchecked, the search is incomplete and says so.
        // Reporting "no match", or pausing for confirmation, over a gap left by a
        // failed request would be exactly the false negative this tool exists to
        // avoid.
        const finish = (status, reason, message) => {
            let finalStatus = status;
            let finalReason = reason;
            let finalMessage = message;
            if (status !== 'cancelled' && status !== 'error'
                && (uncheckedTotal || membershipUnknownTotal)) {
                finalStatus = 'incomplete';
                finalReason = 'failures';
                if (membershipUnknownTotal && !finalMessage) {
                    finalMessage = 'Project membership could not be checked for part of this '
                        + 'search, so the project clue is unknown for some results.';
                }
            }
            return {
                status: finalStatus,
                stopReason: finalReason,
                message: finalMessage || null,
                notices,
                matches: rankMatches(matches, number),
                original,
                originalScore,
                stages,
                lastStage,
                // Non-null when there is more ladder to check. The UI turns this
                // into the "keep looking" button; nothing is re-requested,
                // because seenIds carries everything already tried.
                continuation,
                unchecked: uncheckedTotal,
                membershipUnknown: membershipUnknownTotal,
                // True only when the ladder really finished the work it set out
                // to do. A paused or declined stage leaves candidates
                // deliberately unsearched, so it may not claim completeness even
                // though it is not a failure either.
                complete: (finalStatus === 'match_found' || finalStatus === 'no_match')
                    && !['declined', 'too_large', 'large_stage'].includes(finalReason)
            };
        };

        // Stage 0 asks "what is this number?". Unlike the later stages this
        // request is never filtered by project, because a project filter answers
        // a different question: it would hide a real observation that simply is
        // not a member, leaving the run to report it as nonexistent. Membership
        // is asked separately so "not in the project" stays distinct from "not
        // there at all".
        lastStage = 0;
        if (!skipOriginal) {
            try {
                const found = await fetchBatch([number], null);
                if (found.length) {
                    original = found[0];
                    let context = {projectMemberIds: null};
                    const projectId = projectIdParam || membershipProjectId;
                    if (projectId) {
                        try {
                            context = {projectMemberIds: await fetchMembership([number], projectId)};
                        } catch (error) {
                            if (error && error.name === 'AbortError') throw error;
                            // Stage 0 is never repeated when a search continues, so
                            // an unanswered question here would be skipped for good.
                            membershipUnknownTotal += 1;
                            onMessage(
                                'Could not check project membership for the observation number '
                                + `as supplied: ${error.message}`
                            );
                        }
                    }
                    const {matched, unknown} = scoreObservation(original, context, criteria);
                    originalScore = {matched, unknown};
                    stages.push({stage: 0, total: 1, attempted: 1, unchecked: 0});
                    onOriginal(original, matched, unknown);
                    if (matched.length) {
                        const match = {observation: original, matched, unknown, stage: 0};
                        matches.push(match);
                        await onMatches([match]);
                    }
                    if (isFullMatch(matched, criteria)) {
                        onMessage(`✓ The observation number ${number} as supplied matches every clue.`);
                        // Even this is worth offering a way past. With one clue,
                        // "matches every clue" is one agreeing field, and the
                        // number typed can be both wrong and a coincidental hit.
                        if (digitsCap >= 1) {
                            continuation = {reason: 'full_match', stage: 1, of: digitsCap, seenIds};
                        }
                        return finish('match_found', 'full_match');
                    }
                } else {
                    stages.push({stage: 0, total: 1, attempted: 1, unchecked: 0});
                    onOriginal(null, [], []);
                    onMessage(`Observation ${number} as supplied does not exist on iNaturalist.`);
                }
            } catch (error) {
                if (error && error.name === 'AbortError') {
                    return finish('cancelled', 'interrupted');
                }
                // Saying "does not exist" here would turn an outage into a fact
                // about the observation.
                uncheckedTotal += 1;
                stages.push({stage: 0, total: 1, attempted: 0, unchecked: 1});
                onOriginal(null, [], []);
                onMessage(
                    `The observation number ${number} as supplied could not be checked `
                    + `(${error.message}); the search will continue.`
                );
            }
        }

        if (!criteria.length) {
            // Nothing to filter on. Enumerating thousands of neighbouring IDs
            // would return every observation that happens to exist near this
            // number, which is noise rather than an answer.
            return finish(
                'no_match',
                'no_clues',
                'No clue was supplied, so there was nothing to search for beyond the number '
                + 'itself. Add a genus, family, taxon ID, observer or project and the search '
                + 'can widen to numbers close to the one you entered.'
            );
        }

        for (let index = Math.max(1, startStage); index <= digitsCap; index += 1) {
            const plan = buildCandidatePlan(number, index);
            const stage = createAutoStage(index, plan, seenIds);
            lastStage = index;
            if (stage.total <= 0) continue;

            if (stage.total > maxStageCandidates) {
                // This stage was never searched, so the run has not established
                // that there is nothing there. Saying "no match" would be a lie.
                return finish(
                    'error',
                    'too_large',
                    `Stage ${index} would need ${stage.total.toLocaleString()} checks, more than `
                    + `the limit of ${maxStageCandidates.toLocaleString()}. Lower the maximum `
                    + 'number of wrong digits.'
                );
            }

            const seconds = estimateStageSeconds(stage.total, membershipProjectId);
            onStage({
                index,
                of: digitsCap,
                label: stage.label,
                total: stage.total,
                seconds
            });

            if (stage.total > largeThreshold) {
                if (!largeStageAllowance) {
                    // Never start a stage this size unannounced - it is minutes
                    // of requests. Stop and hand back a continuation so the page
                    // can offer it as a choice instead of blocking on a prompt.
                    continuation = {
                        reason: 'large_stage',
                        stage: index,
                        of: digitsCap,
                        label: stage.label,
                        total: stage.total,
                        seconds,
                        seenIds
                    };
                    return finish(
                        matches.length ? 'match_found' : 'no_match',
                        'large_stage',
                        `The next step would check about ${stage.total.toLocaleString()} more `
                        + 'observation numbers. Nothing beyond this point has been checked yet.'
                    );
                }
                // Spent on this stage only. A later large stage asks again, so
                // one click can never authorise the whole rest of the ladder.
                largeStageAllowance = false;
            }

            const result = await runStagePass({
                candidates: stage.candidates(),
                total: stage.total,
                criteria,
                stageIndex: index,
                projectIdParam,
                membershipProjectId,
                // 1.8.1: a small stage runs to the end so every equally good
                // candidate is collected, because with one clue the first hit is
                // often a neighbour by the same uploader rather than the one
                // wanted. Only a stage too big to finish keeps aborting on a hit.
                stopOnFullMatch: stage.total > earlyStopMin,
                fetchBatch,
                fetchMembership,
                onMatches,
                onProgress,
                onMessage,
                betweenBatches
            });

            matches.push(...result.matches);
            uncheckedTotal += result.unchecked;
            membershipUnknownTotal += result.membershipUnknown;
            stages.push({
                stage: index,
                total: stage.total,
                attempted: result.consumed,
                unchecked: result.unchecked
            });

            if (result.cancelled) {
                cancelled = true;
                break;
            }
            const fullHere = result.matches.filter(match => isFullMatch(match.matched, criteria));
            if (fullHere.length) {
                stopReason = 'full_match';
                if (fullHere.length > 1 && criteria.length === 1) {
                    // Worth saying plainly rather than leaving the reader to
                    // infer it from a list: one clue cannot separate these, and
                    // nearby numbers share an uploader far more often than chance.
                    notices.push(
                        `${fullHere.length} nearby observations all match the only clue you gave `
                        + `(${CLUE_LABELS[criteria[0].kind].toLowerCase()}), so the one listed first `
                        + 'is a best guess rather than an answer. iNaturalist numbers observations in '
                        + 'the order they were uploaded, so numbers next to each other often belong to '
                        + 'the same person and the same taxon. Check all of them, or add a second clue.'
                    );
                }
                if (!stage.exhausted()) {
                    onMessage(
                        `Stopped at stage ${index} after ${result.consumed.toLocaleString()} of `
                        + `${stage.total.toLocaleString()} new candidate(s) because a full match was found.`
                    );
                }
                // There is more ladder either way: the rest of this stage when it
                // stopped early, or the next rung when it finished.
                if (index < digitsCap || !stage.exhausted()) {
                    continuation = {reason: 'full_match', stage: index, of: digitsCap, seenIds};
                }
                break;
            }
        }

        if (cancelled) return finish('cancelled', 'interrupted');
        if (matches.length) return finish('match_found', stopReason);
        return finish('no_match', stopReason);
    }

    // ---- section:autocomplete ----
    // Alan 8/31/26 - autocomplete the taxon name against iNaturalist, so a
    // homonym like Lactarius the mushroom and Lactarius the fish is separated
    // before the search runs rather than raising an ambiguity error after it.
    //
    // Alan 9/10/26 - auto mode has its own genus and family clue boxes beside the
    // single-criterion one, so this became a factory rather than a set of module
    // globals. Each instance owns its own listbox, its own pinned taxon and its
    // own in-flight request; nothing is shared, which is what keeps a suggestion
    // for the family box from landing in the genus box.
    const TAXON_MODES = new Set(['genus', 'family', 'taxon']);
    const SUGGESTION_DEBOUNCE_MS = 250;

    // Alan 8/31/26 - the one line that tells a searcher which taxon they got.
    function describeTaxonSuggestion(taxon) {
        const parts = [];
        if (taxon.rank) parts.push(taxon.rank);
        if (taxon.iconic_taxon_name) parts.push(taxon.iconic_taxon_name);
        if (taxon.preferred_common_name) parts.push(taxon.preferred_common_name);
        parts.push(`taxon ID ${taxon.id}`);
        return parts.join(' · ');
    }

    // `rankFor()` returns the rank to search ('genus', 'family' or 'taxon'), or
    // null when this field should not autocomplete at all - which is how the
    // single-criterion box goes quiet in observer and project mode.
    function createTaxonAutocomplete(options) {
        const {
            input, list, status, pinnedBox, pinnedSwatch, pinnedLabel, pinnedClear,
            rankFor, valueFor = taxon => String(taxon.name || taxon.id), onPick = () => {}
        } = options;
        const controller = {};
        let suggestions = [];
        let activeSuggestion = -1;
        let suggestionRequest = null;
        let suggestionTimer = null;
        let pinnedTaxon = null;

        function closeSuggestions() {
            suggestions = [];
            activeSuggestion = -1;
            list.replaceChildren();
            list.classList.add('hidden');
            input.setAttribute('aria-expanded', 'false');
            input.removeAttribute('aria-activedescendant');
        }

        function highlightSuggestion(index) {
            activeSuggestion = index;
            [...list.children].forEach((item, position) => {
                const active = position === index;
                item.classList.toggle('bg-journal-gold/15', active);
                item.setAttribute('aria-selected', active ? 'true' : 'false');
                if (active) item.scrollIntoView({block: 'nearest'});
            });
            if (index >= 0) {
                input.setAttribute('aria-activedescendant', `${list.id}-option-${index}`);
            } else {
                input.removeAttribute('aria-activedescendant');
            }
        }

        function pinTaxon(taxon, term) {
            pinnedTaxon = {id: Number(taxon.id), name: taxon.name, term};
            pinnedSwatch.style.backgroundColor = iconicColor(taxon);
            pinnedLabel.textContent = `${taxon.name || `Taxon ${taxon.id}`} — ${describeTaxonSuggestion(taxon)}`;
            pinnedBox.classList.remove('hidden');
        }

        function clearPinnedTaxon() {
            pinnedTaxon = null;
            pinnedLabel.textContent = '';
            pinnedBox.classList.add('hidden');
        }

        function selectSuggestion(index) {
            const taxon = suggestions[index];
            if (!taxon) return;
            // Taxon-ID mode searches by the number; the name fields keep the name
            // and remember the ID behind it so the search never has to guess again.
            const value = valueFor(taxon);
            input.value = value;
            pinTaxon(taxon, value);
            closeSuggestions();
            clearError();
            onPick(taxon);
            input.focus();
        }

        function renderSuggestions(results) {
            suggestions = results;
            activeSuggestion = -1;
            list.replaceChildren();
            if (!results.length) {
                closeSuggestions();
                status.textContent = 'No matching taxa';
                return;
            }
            results.forEach((taxon, index) => {
                const item = document.createElement('li');
                item.id = `${list.id}-option-${index}`;
                // setAttribute, not the `role` IDL property: ARIA reflection is
                // recent enough that older Safari/Firefox silently drop the
                // assignment, leaving the listbox options with no role at all.
                item.setAttribute('role', 'option');
                item.setAttribute('aria-selected', 'false');
                item.className = 'flex cursor-pointer items-center gap-3 px-3 py-2 hover:bg-journal-gold/15';
                item.style.borderLeft = `4px solid ${iconicColor(taxon)}`;
                const thumbnail = taxon.default_photo?.square_url;
                if (thumbnail) {
                    const image = document.createElement('img');
                    image.src = thumbnail;
                    image.alt = '';
                    image.loading = 'lazy';
                    image.className = 'h-8 w-8 flex-none rounded object-cover';
                    item.appendChild(image);
                }
                const text = document.createElement('span');
                text.className = 'min-w-0 flex-1';
                const name = document.createElement('span');
                name.className = 'block truncate font-semibold italic text-journal-dark dark:text-white';
                name.textContent = taxon.name || `Taxon ${taxon.id}`;
                const detail = document.createElement('span');
                detail.className = 'block truncate text-xs text-gray-500 dark:text-gray-400';
                detail.textContent = describeTaxonSuggestion(taxon);
                text.append(name, detail);
                item.appendChild(text);
                item.addEventListener('click', () => selectSuggestion(index));
                list.appendChild(item);
            });
            list.classList.remove('hidden');
            input.setAttribute('aria-expanded', 'true');
            status.textContent = `${results.length} matching taxa. Use the arrow keys to review them.`;
        }

        async function requestSuggestions(term) {
            const rank = rankFor();
            if (!rank || term.length < 2 || /^\d+$/.test(term)) {
                closeSuggestions();
                return;
            }
            suggestionRequest?.abort();
            const request = {
                cancelled: false,
                controller: null,
                abort() {
                    this.cancelled = true;
                    this.controller?.abort();
                }
            };
            suggestionRequest = request;
            const params = {q: term, per_page: '10', is_active: 'true'};
            if (rank !== 'taxon') params.rank = rank;
            try {
                const data = await apiGet('/taxa/autocomplete', params, request, 1);
                if (suggestionRequest !== request) return;
                renderSuggestions((data.results || []).filter(taxon => taxon && taxon.id !== undefined));
            } catch (error) {
                // A failed suggestion is not worth an error message; the name can
                // still be typed in full and verified when the search starts.
                if (suggestionRequest === request) closeSuggestions();
            }
        }

        input.addEventListener('input', () => {
            if (pinnedTaxon && input.value.trim() !== pinnedTaxon.term) clearPinnedTaxon();
            window.clearTimeout(suggestionTimer);
            const term = input.value.trim();
            suggestionTimer = window.setTimeout(() => requestSuggestions(term), SUGGESTION_DEBOUNCE_MS);
        });
        input.addEventListener('keydown', event => {
            if (event.key === 'Escape') return closeSuggestions();
            if (!suggestions.length) return;
            if (event.key === 'ArrowDown') {
                event.preventDefault();
                highlightSuggestion((activeSuggestion + 1) % suggestions.length);
            } else if (event.key === 'ArrowUp') {
                event.preventDefault();
                highlightSuggestion((activeSuggestion - 1 + suggestions.length) % suggestions.length);
            } else if (event.key === 'Enter' && activeSuggestion >= 0) {
                event.preventDefault();
                selectSuggestion(activeSuggestion);
            }
        });
        input.addEventListener('blur', () => window.setTimeout(closeSuggestions, 150));
        // Keep the click from blurring the input before the option is chosen.
        list.addEventListener('mousedown', event => event.preventDefault());
        pinnedClear.addEventListener('click', () => {
            clearPinnedTaxon();
            input.focus();
        });

        controller.close = closeSuggestions;
        controller.pin = pinTaxon;
        controller.clear = clearPinnedTaxon;
        // The pinned taxon is only usable while the box still reads what was
        // pinned; editing the text by hand drops it.
        controller.pinnedFor = term => (pinnedTaxon && pinnedTaxon.term === term ? pinnedTaxon : null);
        return controller;
    }

    // Alan 8/31/26 - 1.7.5 shows each match's location alongside its taxon.
    // The most specific standard administrative place, formatted the way
    // inat.finder.py 1.7.5 prints it, falling back to the observation's own guess.
    function formatPlaceLabel(places) {
        const administrative = places.filter(place =>
            place && Number.isInteger(place.admin_level) && place.admin_level >= 0
        );
        if (!administrative.length) return '';
        const mostSpecific = administrative.reduce((best, place) =>
            place.admin_level > best.admin_level ? place : best
        );
        const label = mostSpecific.display_name || mostSpecific.name;
        if (typeof label !== 'string' || !label.trim()) return '';
        return label
            .replace(/\bCounty\b/g, 'Co.')
            .replace(/\bUnited States\b/g, 'US')
            .split(',')
            .map(part => part.trim())
            .filter(Boolean)
            .join(' ');
    }

    async function resolveLocations(observations, search) {
        const labels = new Map();
        const placeIds = new Set();
        observations.forEach(observation => {
            (observation.place_ids || []).forEach(id => placeIds.add(String(id)));
        });
        const places = new Map();
        const ids = [...placeIds];
        for (let index = 0; index < ids.length; index += BATCH_SIZE) {
            const batch = ids.slice(index, index + BATCH_SIZE);
            try {
                const data = await apiGet(`/places/${batch.join(',')}`, {per_page: String(BATCH_SIZE)}, search);
                (data.results || []).forEach(place => {
                    if (place && place.id !== undefined) places.set(String(place.id), place);
                });
            } catch (error) {
                if (error.name === 'AbortError') throw error;
                // Locations are cosmetic; a failed lookup falls back to place_guess.
                log(`Could not resolve locations for this batch: ${error.message}`);
            }
        }
        observations.forEach(observation => {
            const observationPlaces = (observation.place_ids || [])
                .map(id => places.get(String(id)))
                .filter(Boolean);
            const label = formatPlaceLabel(observationPlaces)
                || String(observation.place_guess || '').trim()
                || 'Unknown location';
            labels.set(String(observation.id), label);
        });
        return labels;
    }

    const manualAutocomplete = createTaxonAutocomplete({
        input: termInput,
        list: suggestionList,
        status: suggestionStatus,
        pinnedBox,
        pinnedSwatch,
        pinnedLabel,
        pinnedClear,
        rankFor: () => (TAXON_MODES.has(currentMode()) ? currentMode() : null),
        valueFor: taxon => (currentMode() === 'taxon' ? String(taxon.id) : String(taxon.name || taxon.id))
    });

    const clueAutocomplete = {};
    ['genus', 'family'].forEach(kind => {
        clueAutocomplete[kind] = createTaxonAutocomplete({
            input: clueInputs[kind],
            list: document.getElementById(`clue-${kind}-suggestions`),
            status: document.getElementById(`clue-${kind}-suggestion-status`),
            pinnedBox: document.getElementById(`clue-${kind}-pinned`),
            pinnedSwatch: document.getElementById(`clue-${kind}-pinned-swatch`),
            pinnedLabel: document.getElementById(`clue-${kind}-pinned-label`),
            pinnedClear: document.getElementById(`clue-${kind}-pinned-clear`),
            rankFor: () => kind
        });
    });

    // ---- section:results ----
    // Alan 9/10/26 - one card per observation, built once and then updated in
    // place. Auto mode meets the same observation through more than one path -
    // the number as supplied is also reachable as a candidate of a later stage -
    // and a second card for it would read as a second observation.
    const resultCards = new Map();

    function buildResultCard(observation) {
        const card = document.createElement('article');
        card.className = 'flex gap-4 rounded-2xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-journal-green p-4 shadow-sm';
        const accent = iconicColor(observation.taxon);
        card.style.borderLeft = `5px solid ${accent}`;
        const photo = document.createElement('div');
        photo.className = 'flex h-24 w-24 flex-none items-center justify-center overflow-hidden rounded-xl bg-gray-100 dark:bg-journal-dark text-gray-400';
        const photoUrl = observation.photos?.[0]?.url || observation.taxon?.default_photo?.square_url;
        if (photoUrl) {
            const image = document.createElement('img');
            image.src = photoUrl.replace(/square(?=\.[a-z]+(?:\?|$))/i, 'small');
            image.alt = '';
            image.loading = 'lazy';
            image.className = 'h-full w-full object-cover';
            photo.appendChild(image);
        } else {
            const icon = document.createElement('i');
            icon.className = 'fas fa-image text-2xl';
            photo.appendChild(icon);
        }
        const body = document.createElement('div');
        body.className = 'min-w-0 flex-1';
        const badges = document.createElement('div');
        badges.className = 'flex flex-wrap items-center gap-2';
        const iconicName = observation.taxon?.iconic_taxon_name;
        if (iconicName) {
            const iconic = document.createElement('span');
            iconic.className = 'inline-flex items-center rounded-full px-2 py-0.5 text-xs font-semibold';
            iconic.style.color = accent;
            iconic.style.backgroundColor = `${accent}22`;
            iconic.style.border = `1px solid ${accent}66`;
            iconic.textContent = iconicName;
            badges.appendChild(iconic);
        }
        body.appendChild(badges);
        const title = document.createElement('h3');
        title.className = 'mt-1 truncate font-serif text-xl font-semibold italic text-journal-dark dark:text-white';
        title.textContent = observation.taxon?.name || observation.species_guess || 'Unknown taxon';
        body.appendChild(title);
        const details = document.createElement('p');
        details.className = 'mt-1 text-sm text-gray-500 dark:text-gray-400';
        details.textContent = `Observation #${observation.id} · @${observation.user?.login || 'unknown'}`;
        body.appendChild(details);
        const place = document.createElement('p');
        place.className = 'mt-1 hidden truncate text-sm text-gray-500 dark:text-gray-400';
        body.appendChild(place);
        const score = document.createElement('div');
        score.className = 'mt-2 hidden';
        body.appendChild(score);
        const link = document.createElement('a');
        link.href = `https://www.inaturalist.org/observations/${observation.id}`;
        link.target = '_blank';
        link.rel = 'noopener';
        link.className = 'mt-3 inline-flex items-center gap-2 text-sm font-semibold text-journal-green dark:text-journal-gold hover:underline';
        link.append('Open on iNaturalist');
        const external = document.createElement('i');
        external.className = 'fas fa-arrow-up-right-from-square text-xs';
        link.appendChild(external);
        body.appendChild(link);
        card.append(photo, body);
        return {card, badges, place, score};
    }

    // "2 of 3 clues matched — Genus ✓ · Observer ✓ · Project unknown".
    //
    // Every clue is named with its own verdict, and `unknown` is never collapsed
    // into `no`: "we could not check the project" and "it is not in the project"
    // are different answers and the second one is a reason to stop looking.
    // The symbol is always paired with a word, so the verdict never depends on
    // colour or on a glyph a screen reader may skip.
    function renderScore(container, entry, criteria) {
        container.replaceChildren();
        if (!criteria.length) {
            container.classList.add('hidden');
            return;
        }
        const headline = document.createElement('p');
        headline.className = 'text-sm font-semibold text-journal-dark dark:text-white';
        headline.textContent =
            `${entry.matched.length} of ${criteria.length} ${criteria.length === 1 ? 'clue' : 'clues'} matched`;
        container.appendChild(headline);
        const list = document.createElement('ul');
        list.className = 'mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-gray-600 dark:text-gray-300';
        criteria.forEach(criterion => {
            const item = document.createElement('li');
            item.className = 'inline-flex items-center gap-1';
            const matched = entry.matched.includes(criterion.kind);
            const unknown = entry.unknown.includes(criterion.kind);
            const mark = document.createElement('span');
            mark.setAttribute('aria-hidden', 'true');
            if (matched) {
                mark.textContent = '✓';
                mark.className = 'font-bold text-green-600 dark:text-green-400';
            } else if (unknown) {
                mark.textContent = '?';
                mark.className = 'font-bold text-amber-600 dark:text-amber-400';
            } else {
                mark.textContent = '✕';
                mark.className = 'font-bold text-gray-400';
            }
            const text = document.createElement('span');
            const verdict = matched ? 'matched' : (unknown ? 'unknown' : 'did not match');
            text.textContent = `${CLUE_LABELS[criterion.kind] || criterion.kind} ${verdict}`;
            item.append(mark, text);
            list.appendChild(item);
        });
        container.appendChild(list);
        if (entry.stage !== null && entry.stage !== undefined) {
            const provenance = document.createElement('p');
            provenance.className = 'mt-1 text-xs text-gray-500 dark:text-gray-400';
            provenance.textContent = entry.stage === 0
                ? 'Found as the number you entered'
                : `Found at stage ${entry.stage}`;
            container.appendChild(provenance);
        }
        container.classList.remove('hidden');
    }

    function renderBadges(parts, entry, criteria, originalId) {
        parts.badges.replaceChildren();
        const observation = entry.observation;
        const accent = iconicColor(observation.taxon);
        const iconicName = observation.taxon?.iconic_taxon_name;
        if (iconicName) {
            const iconic = document.createElement('span');
            iconic.className = 'inline-flex items-center rounded-full px-2 py-0.5 text-xs font-semibold';
            iconic.style.color = accent;
            iconic.style.backgroundColor = `${accent}22`;
            iconic.style.border = `1px solid ${accent}66`;
            iconic.textContent = iconicName;
            parts.badges.appendChild(iconic);
        }
        if (String(observation.id) === originalId) {
            const badge = document.createElement('span');
            badge.className = 'inline-flex rounded-full bg-blue-100 dark:bg-blue-950 px-2 py-0.5 text-xs font-semibold text-blue-700 dark:text-blue-300';
            badge.textContent = 'The number you entered';
            parts.badges.appendChild(badge);
        }
        if (criteria.length) {
            const full = isFullMatch(entry.matched, criteria);
            const badge = document.createElement('span');
            // A full match and a partial one are told apart by their words, not
            // by their colour alone.
            badge.className = full
                ? 'inline-flex rounded-full bg-green-100 dark:bg-green-950 px-2 py-0.5 text-xs font-semibold text-green-700 dark:text-green-300'
                : 'inline-flex rounded-full bg-gray-100 dark:bg-gray-800 px-2 py-0.5 text-xs font-semibold text-gray-700 dark:text-gray-200';
            badge.textContent = full ? 'Full match' : 'Partial match';
            parts.badges.appendChild(badge);
        }
    }

    // Add or refresh one observation, then put the whole list back into ranked
    // order. Cards are moved rather than rebuilt, so re-ranking never restarts a
    // photo download or loses the scroll position.
    function upsertResult(entry, criteria, originalId, location) {
        const id = String(entry.observation.id);
        let parts = resultCards.get(id);
        if (!parts) {
            parts = buildResultCard(entry.observation);
            parts.entry = entry;
            resultCards.set(id, parts);
            resultsList.appendChild(parts.card);
        } else if (entry.matched.length >= parts.entry.matched.length) {
            // The highest-scoring reading of an observation wins, matching the
            // CLI's rank_matches().
            parts.entry = entry;
        }
        renderBadges(parts, parts.entry, criteria, originalId);
        renderScore(parts.score, parts.entry, criteria);
        if (location) {
            parts.place.replaceChildren();
            const pin = document.createElement('i');
            pin.className = 'fas fa-location-dot mr-1 text-xs';
            parts.place.append(pin, location);
            parts.place.classList.remove('hidden');
        }
    }

    // `origin` must be the same number the ladder ranked against, or the list on
    // screen and the list in the result would disagree about which match is best.
    function reorderResults(criteria, origin = null) {
        const ranked = rankMatches([...resultCards.values()].map(parts => parts.entry), origin);
        ranked.forEach(match => {
            const parts = resultCards.get(String(match.observation.id));
            if (parts) resultsList.appendChild(parts.card);
        });
        return ranked;
    }

    function appendResultCard(observation, originalId, location) {
        // The single-criterion search has no clue scores to show, so it adds a
        // card with an empty criteria list and nothing else changes.
        upsertResult({observation, matched: [], unknown: [], stage: null}, [], originalId, location);
    }

    // Alan 8/31/26 - 1.7.5 reports an incomplete search rather than calling it "no matches".
    function updateResultsHeading(matchCount, incomplete) {
        let heading;
        if (matchCount) {
            heading = `${matchCount.toLocaleString()} potential ${matchCount === 1 ? 'match' : 'matches'} found`;
        } else {
            heading = incomplete ? 'No matches found yet' : 'No matching observations found';
        }
        document.getElementById('results-heading').textContent = heading;
    }

    // Alan 8/31/26 - 1.7.5 shows matches as each batch returns instead of only at the end.
    function showMatches(matches, originalId, locations) {
        matches.forEach(match => appendResultCard(match, originalId, locations.get(String(match.id))));
    }

    function renderSummary(checked, total, unchecked, elapsedSeconds, matchCount, incomplete) {
        resultsSection.classList.remove('hidden');
        updateResultsHeading(matchCount, incomplete);
        const parts = [`${checked.toLocaleString()} of ${total.toLocaleString()} variations checked in ${formatDuration(elapsedSeconds)}`];
        if (unchecked) parts.push(`${unchecked.toLocaleString()} could not be checked`);
        document.getElementById('results-summary').textContent = parts.join(' · ');
        if (matchCount) return;
        const empty = document.createElement('div');
        empty.className = 'md:col-span-2 rounded-2xl border border-dashed border-gray-300 dark:border-gray-600 bg-white/60 dark:bg-journal-green/40 p-8 text-center text-gray-600 dark:text-gray-300';
        empty.textContent = incomplete
            ? 'The search was incomplete because iNaturalist could not be reached for part of it. Try again before concluding that nothing matches.'
            : 'Try checking more digits, confirming the taxon or username spelling, or using a project ID. The observation may also have been removed.';
        resultsList.appendChild(empty);
    }

    function setRunning(running) {
        [...form.elements].forEach(element => {
            if (element === cancelButton) return;
            element.disabled = running;
        });
        searchButton.classList.toggle('opacity-60', running);
        cancelButton.disabled = !running;
        cancelButton.classList.toggle('hidden', !running);
        searchButton.querySelector('span').textContent = running ? 'Searching…' : 'Find observation';
        searchButton.querySelector('i').className = running ? 'fas fa-spinner fa-spin' : 'fas fa-magnifying-glass';
    }

    async function runManualSearch() {
        clearError();
        resultsSection.classList.add('hidden');
        resultsList.replaceChildren();
        logOutput.textContent = '';
        logSection.classList.toggle('hidden', !verboseInput.checked);

        const mode = currentMode();
        const term = termInput.value.trim();
        // Alan 8/31/26 - a taxon chosen from autocomplete is searched by its ID,
        // so a name shared by two kingdoms never has to be disambiguated twice.
        const pinned = TAXON_MODES.has(mode) ? manualAutocomplete.pinnedFor(term) : null;
        const lookupMode = pinned ? 'taxon' : mode;
        const lookupTerm = pinned ? String(pinned.id) : term;
        const originalId = parseObservationId(observationInput.value);
        const digitsOff = Number(digitsSelect.value);
        if (!term) {
            const what = {user: 'observer username', taxon: 'taxon ID', project: 'project'}[mode] || mode;
            return showError(`Enter the expected ${what}.`);
        }
        if (!originalId) return showError('Enter a numeric iNaturalist observation ID or a valid iNaturalist observation URL. Specimen and voucher codes are not observation IDs.');
        if (digitsOff > originalId.length) return showError('The number of wrong digits cannot exceed the length of the observation ID.');
        const variationEstimate = estimateVariationCount(originalId, digitsOff);
        if (variationEstimate > MAX_VARIATIONS) {
            return showError(`That search could generate about ${variationEstimate.toLocaleString()} variations. The limit is ${MAX_VARIATIONS.toLocaleString()}; use fewer wrong digits or check the observation ID.`);
        }

        const search = {
            cancelled: false,
            controller: null,
            timedOutController: null,
            sleepTimer: null,
            sleepReject: null
        };
        activeSearch = search;
        setRunning(true);
        const started = performance.now();
        let checked = 0;
        let unchecked = 0;
        let total = 0;
        let incomplete = false;
        const found = new Map();
        try {
            setProgress(2, `Verifying ${mode} on iNaturalist…`);
            log(`Verifying ${mode} “${term}”.`, true);
            const criteria = await resolveCriteria(lookupMode, lookupTerm, search);
            log(`Verified ${mode}: ${criteria.label}.`, true);
            // Alan 8/31/26 - show which taxon the search actually resolved to.
            if (criteria.taxon) manualAutocomplete.pin(criteria.taxon, term);

            setProgress(5, 'Generating possible observation numbers…');
            await new Promise(resolve => window.setTimeout(resolve, 0));
            const variations = buildVariations(originalId, digitsOff);
            total = variations.length;
            if (!total) throw new Error('No variations could be generated from this observation ID.');
            const batchCount = Math.ceil(total / BATCH_SIZE);
            const roughDuration = formatRoughDuration(batchCount * 1.3);
            const estimate = `Checking ${total.toLocaleString()} variations in ${batchCount.toLocaleString()} batches (~${roughDuration})…`;
            setProgress(7, estimate, 0, total, `~${roughDuration}`);
            log(`Checking ${total.toLocaleString()} unique variations in ${batchCount.toLocaleString()} API batches (~${roughDuration}).`, true);

            // Alan 8/31/26 - confirm a large search up front, using the exact candidate count.
            // inat.finder.py 1.7.5 reports the size of a search and asks before
            // starting a large one. The count here is exact, not the upper bound
            // used for the hard cap above.
            if (total > LARGE_SEARCH_THRESHOLD) {
                const proceed = window.confirm(
                    `This will check ${total.toLocaleString()} observation numbers and take roughly ${roughDuration}.\n\nStart the search?`
                );
                if (!proceed) {
                    search.cancelled = true;
                    throw new DOMException('Search cancelled', 'AbortError');
                }
            }

            const report = async matches => {
                const fresh = matches.filter(item => !found.has(String(item.id)));
                fresh.forEach(item => found.set(String(item.id), item));
                if (!fresh.length) return;
                resultsSection.classList.remove('hidden');
                const locations = await resolveLocations(fresh, search);
                showMatches(fresh, originalId, locations);
                updateResultsHeading(found.size, true);
            };

            const originalMatches = await checkBatch([originalId], lookupMode, criteria, search);
            if (originalMatches.length) log(`The original observation #${originalId} already matches.`, true);
            await report(originalMatches);

            const batchDurations = [];
            const failedBatches = [];
            let consecutiveFailures = 0;
            for (let index = 0; index < variations.length; index += BATCH_SIZE) {
                const batch = variations.slice(index, index + BATCH_SIZE);
                const batchNumber = Math.floor(index / BATCH_SIZE) + 1;
                const batchStarted = performance.now();
                log(`Checking batch ${batchNumber}/${batchCount} (${batch.length} IDs).`);
                log(`IDs: ${batch.join(', ')}`);
                let matches = null;
                try {
                    matches = await checkBatch(batch, lookupMode, criteria, search);
                    consecutiveFailures = 0;
                } catch (error) {
                    if (error.name === 'AbortError' || search.cancelled) throw error;
                    failedBatches.push(batch);
                    consecutiveFailures += 1;
                    log(`Batch ${batchNumber} failed: ${error.message}`, true);
                    if (consecutiveFailures >= MAX_CONSECUTIVE_FAILED_BATCHES) {
                        log(`Stopping early: ${consecutiveFailures} batches in a row failed.`, true);
                        unchecked += Math.max(0, variations.length - (index + BATCH_SIZE));
                        break;
                    }
                }
                if (matches) {
                    checked += batch.length;
                    await report(matches);
                    if (matches.length) log(`Found ${matches.length} matching observation${matches.length === 1 ? '' : 's'} in this batch.`, true);
                }
                batchDurations.push(Math.max(1000, performance.now() - batchStarted));
                if (batchDurations.length > 5) batchDurations.shift();
                const averageMs = batchDurations.reduce((sum, value) => sum + value, 0) / batchDurations.length;
                const remainingBatches = Math.ceil((total - checked - unchecked) / BATCH_SIZE);
                const percent = 7 + ((checked + unchecked) / total) * 92;
                setProgress(percent, `Checking batch ${batchNumber} of ${batchCount}…`, checked, total, formatDuration(remainingBatches * averageMs / 1000));
                if (checked + unchecked < total) await sleep(1000, search);
            }

            // Alan 8/31/26 - retry failed batches once so an isolated hiccup is not reported as unchecked.
            // One retry round over the batches that failed every attempt, so an
            // isolated hiccup does not turn into a permanently unchecked range.
            for (const batch of failedBatches) {
                log(`Retrying a failed batch of ${batch.length} IDs.`, true);
                try {
                    const matches = await checkBatch(batch, lookupMode, criteria, search);
                    checked += batch.length;
                    await report(matches);
                } catch (error) {
                    if (error.name === 'AbortError' || search.cancelled) throw error;
                    unchecked += batch.length;
                    log(`Batch retry failed: ${error.message}`, true);
                }
                await sleep(1000, search);
            }

            incomplete = unchecked > 0;
            const elapsed = (performance.now() - started) / 1000;
            const status = incomplete
                ? 'Search incomplete — some batches could not be checked'
                : (found.size ? 'Search complete — matches found' : 'Search complete — no matches found');
            setProgress(100, status, checked, total, formatDuration(0));
            renderSummary(checked, total, unchecked, elapsed, found.size, incomplete);
            log(`Search finished in ${formatDuration(elapsed)}. Found ${found.size} potential match${found.size === 1 ? '' : 'es'}; ${unchecked.toLocaleString()} variations could not be checked.`, true);
            if (incomplete) {
                showError('iNaturalist could not be reached for part of this search, so it is incomplete. The matches below are everything that was found — try again for the rest.');
            }
        } catch (error) {
            if (error.name === 'AbortError' || search.cancelled) {
                setProgress(0, 'Search cancelled');
                log('Search cancelled by user.', true);
                if (found.size) {
                    renderSummary(checked, total, unchecked, (performance.now() - started) / 1000, found.size, true);
                }
            } else if (error.taxonCandidates) {
                showError(error.message);
                showTaxonChoices(error.taxonCandidates);
                setProgress(0, 'Choose which taxon you meant');
                log(`${error.message}`, true);
            } else {
                showError(error.message || 'The search could not be completed.');
                setProgress(0, 'Search stopped because of an error');
                log(`Error: ${error.message || error}`, true);
                if (window.reportClientError) window.reportClientError('finder.search', error);
            }
        } finally {
            activeSearch = null;
            setRunning(false);
        }
    }

    // ---- section:auto-ui ----
    // Alan 9/10/26 - the browser side of 1.8.1's auto mode. The ladder itself is
    // in runAutoLadder(); everything here is the page it reports to.
    // The state a "keep looking" click continues from. Held here rather than
    // rebuilt from the form, so a continuation uses the clues the results on
    // screen were actually found with even if the boxes have since been edited.
    let autoSession = null;

    async function runAutoSearch(continuing = null) {
        clearError();
        if (!continuing) {
            resultsSection.classList.add('hidden');
            resultsList.replaceChildren();
            resultCards.clear();
            logOutput.textContent = '';
            autoSession = null;
        }
        hideKeepLooking();
        logSection.classList.toggle('hidden', !verboseInput.checked);

        const originalId = continuing
            ? continuing.number
            : parseObservationId(observationInput.value);
        if (!originalId) {
            return showError(
                'Enter a numeric iNaturalist observation ID or a valid iNaturalist observation URL. '
                + 'Specimen and voucher codes are not observation IDs.'
            );
        }
        const digitsCap = continuing
            ? continuing.digitsCap
            : Math.min(Number(digitsSelect.value) || AUTO_DEFAULT_MAX_DIGITS, originalId.length);
        const clues = {};
        if (!continuing) {
            Object.entries(clueInputs).forEach(([kind, input]) => { clues[kind] = input.value.trim(); });
            // A genus or family chosen from the autocomplete is searched by the
            // taxon ID behind it, so a name shared by two kingdoms is never
            // ambiguous twice.
            ['genus', 'family'].forEach(kind => {
                const pinned = clueAutocomplete[kind].pinnedFor(clues[kind]);
                if (pinned && !clues.taxon) {
                    clues.taxon = String(pinned.id);
                    clues[kind] = '';
                }
            });
        }

        const search = {
            cancelled: false,
            controller: null,
            timedOutController: null,
            sleepTimer: null,
            sleepReject: null
        };
        activeSearch = search;
        setRunning(true);
        const started = performance.now();
        // Continuing keeps the running totals from the searches before it, so
        // "checked in total" counts the whole hunt rather than the last leg.
        let checkedTotal = continuing ? continuing.checkedTotal : 0;
        let stageChecked = 0;
        let stageTotal = 0;
        let uncheckedTotal = continuing ? continuing.uncheckedTotal : 0;
        let criteria = continuing ? continuing.criteria : [];

        try {
            let resolved;
            if (continuing) {
                resolved = continuing.resolved;
            } else {
                setProgress(2, 'Checking the clues you supplied on iNaturalist…');
                setStageLine('Verifying clues');
                setCumulativeChecked(0);
                resolved = await resolveAutoCriteria(clues, search, {
                    onMessage: message => log(message, true),
                    onResolved: (kind, result) => {
                        if ((kind === 'genus' || kind === 'family') && result.taxon) {
                            clueAutocomplete[kind].pin(result.taxon, clues[kind]);
                        }
                    }
                });
                criteria = resolved.criteria;

                // A clue iNaturalist could not resolve is reported and dropped;
                // the rest of the search carries on without it. On a foray the
                // mistaken element is as often the genus as the number.
                resolved.unusable.forEach(clue => {
                    addNotice(`${CLUE_LABELS[clue.kind]}: ${clue.reason} This clue was left out of the search.`);
                    if (clue.candidates) showTaxonChoices(clue.candidates);
                });
            }

            const fetchBatch = async (ids, projectId) => {
                const params = {id: ids.join(','), per_page: String(BATCH_SIZE)};
                if (projectId) params.project_id = projectId;
                const data = await apiGet('/observations', params, search);
                return data.results || [];
            };
            // Project membership needs its own request whenever the project
            // shares the search with another clue, because filtering the main
            // request would hide everything the other clues might have matched.
            const fetchMembership = async (ids, projectId) => {
                const results = await fetchBatch(ids, projectId);
                return new Set(results
                    .filter(item => item && item.id !== undefined)
                    .map(item => String(item.id)));
            };

            const paintMatches = async fresh => {
                resultsSection.classList.remove('hidden');
                const observations = fresh.map(match => match.observation);
                let locations = new Map();
                try {
                    locations = await resolveLocations(observations, search);
                } catch (error) {
                    if (error.name === 'AbortError') throw error;
                }
                fresh.forEach(match => {
                    upsertResult(match, criteria, originalId, locations.get(String(match.observation.id)));
                });
                const ranked = reorderResults(criteria, originalId);
                updateResultsHeading(ranked.length, true);
            };

            const outcome = await runAutoLadder({
                number: originalId,
                criteria,
                digitsCap,
                projectIdParam: resolved.projectIdParam,
                membershipProjectId: resolved.membershipProjectId,
                fetchBatch,
                fetchMembership,
                onMessage: message => log(message, true),
                skipOriginal: Boolean(continuing),
                onOriginal: (observation, matched) => {
                    if (!observation) return;
                    log(
                        `Observation #${observation.id} exists and matched ${matched.length} of `
                        + `${criteria.length} clue(s).`,
                        true
                    );
                },
                onStage: info => {
                    stageChecked = 0;
                    stageTotal = info.total;
                    setStageLine(`Stage ${info.index} of ${info.of}: ${info.label}`);
                    setProgress(
                        stageProgressPercent(0, info.total),
                        `Stage ${info.index} of ${info.of}: checking ${info.label}…`,
                        0,
                        info.total,
                        formatRoughDuration(info.seconds)
                    );
                    log(
                        `Stage ${info.index}: ${info.label} — ${info.total.toLocaleString()} new `
                        + `candidate(s), about ${formatRoughDuration(info.seconds)}.`,
                        true
                    );
                },
                onProgress: (count, wasChecked) => {
                    stageChecked += count;
                    if (wasChecked) checkedTotal += count;
                    else uncheckedTotal += count;
                    setCumulativeChecked(checkedTotal);
                    setProgress(
                        stageProgressPercent(stageChecked, stageTotal),
                        progressStatus.textContent,
                        Math.min(stageChecked, stageTotal),
                        stageTotal,
                        progressEta.textContent
                    );
                },
                onMatches: paintMatches,
                betweenBatches: () => sleep(1000, search),
                // A continuation carries the tried set forward, so the ladder
                // picks up exactly where it stopped and re-requests nothing.
                seenIds: continuing ? continuing.seenIds : new Set(),
                startStage: continuing ? continuing.stage : 1,
                allowLargeStage: Boolean(continuing && continuing.allowLargeStage)
            });

            autoSession = {
                number: originalId,
                digitsCap,
                criteria,
                resolved,
                checkedTotal,
                uncheckedTotal,
                continuation: outcome.continuation
            };
            finishAutoSearch(outcome, criteria, originalId, checkedTotal, uncheckedTotal, started);
        } catch (error) {
            if (error.name === 'AbortError' || search.cancelled) {
                // Cancelling keeps whatever was found rather than throwing it
                // away, and says the result is partial rather than a clean miss.
                setProgress(0, 'Search cancelled — showing what was found so far');
                setStageLine('Cancelled');
                log('Search cancelled by user.', true);
                const ranked = reorderResults(criteria, originalId);
                if (ranked.length) {
                    renderSummary(checkedTotal, checkedTotal, uncheckedTotal,
                        (performance.now() - started) / 1000, ranked.length, true);
                }
            } else if (error.malformedInput) {
                showError(error.message);
                setProgress(0, 'Check the clue you entered');
                log(`Error: ${error.message}`, true);
            } else {
                showError(error.message || 'The search could not be completed.');
                setProgress(0, 'Search stopped because of an error');
                setStageLine('Stopped');
                log(`Error: ${error.message || error}`, true);
                if (window.reportClientError) window.reportClientError('finder.search', error);
            }
        } finally {
            activeSearch = null;
            setRunning(false);
        }
    }

    function stageProgressPercent(checked, total) {
        if (!total) return 100;
        return Math.min(100, (checked / total) * 100);
    }

    function finishAutoSearch(outcome, criteria, originalId, checkedTotal, uncheckedTotal, started) {
        const elapsed = (performance.now() - started) / 1000;
        const ranked = reorderResults(criteria, originalId);
        resultsSection.classList.remove('hidden');
        updateResultsHeading(ranked.length, !outcome.complete);

        const full = ranked.filter(match => isFullMatch(match.matched, criteria)).length;
        let status;
        if (outcome.status === 'cancelled') status = 'Search cancelled — showing what was found so far';
        else if (outcome.status === 'incomplete') status = 'Search incomplete — some observations could not be checked';
        else if (outcome.stopReason === 'declined') status = 'Stopped before the deeper search';
        else if (outcome.stopReason === 'no_clues') status = 'Checked the number as supplied';
        else if (full) status = 'Full match found — search stopped';
        else if (ranked.length) status = 'No full match — showing the closest matches';
        else status = 'Search complete — no matching observations found';

        // A search that is waiting on a decision, or that could not check
        // everything, must never read as 100% complete.
        setProgress(outcome.complete ? 100 : 92, status, checkedTotal, checkedTotal, '—');
        setStageLine(outcome.status === 'incomplete' ? 'Incomplete' : 'Finished');
        setCumulativeChecked(checkedTotal);

        const parts = [
            `${checkedTotal.toLocaleString()} observation ${checkedTotal === 1 ? 'number' : 'numbers'} `
            + `checked in ${formatDuration(elapsed)}`
        ];
        if (uncheckedTotal) parts.push(`${uncheckedTotal.toLocaleString()} could not be checked`);
        if (full) parts.push(`${full.toLocaleString()} full ${full === 1 ? 'match' : 'matches'}`);
        document.getElementById('results-summary').textContent = parts.join(' · ');

        if (outcome.message) addNotice(outcome.message, outcome.status === 'error' ? 'warn' : 'info');
        // 1.8.1: when one clue cannot separate several neighbours, say so rather
        // than letting the top of the list read as an answer.
        (outcome.notices || []).forEach(notice => addNotice(notice, 'info'));
        showKeepLooking(outcome);
        if (outcome.status === 'incomplete') {
            showError(
                'iNaturalist could not be reached for part of this search, so it is incomplete. '
                + 'Whatever was found is below — try again for the rest.'
            );
        }
        if (!ranked.length) {
            const empty = document.createElement('div');
            empty.className = 'md:col-span-2 rounded-2xl border border-dashed border-gray-300 dark:border-gray-600 bg-white/60 dark:bg-journal-green/40 p-8 text-center text-gray-600 dark:text-gray-300';
            empty.textContent = outcome.stopReason === 'no_clues'
                ? 'Nothing was searched beyond the number itself. Add a genus, family, taxon ID, observer or project so the search has something to recognise, and it will widen to nearby numbers.'
                : (outcome.complete
                    ? 'Nothing matched any of your clues. One of the clues may itself be wrong — try removing the one you are least sure of, or raise the maximum number of wrong digits under Advanced search options.'
                    : 'The search did not finish, so this is not a conclusive answer. Try again.');
            resultsList.appendChild(empty);
        }
        log(
            `Search finished in ${formatDuration(elapsed)}: ${ranked.length} potential match(es), `
            + `${full} full. ${uncheckedTotal.toLocaleString()} could not be checked.`,
            true
        );
    }

    // Alan 9/10/26 - the way out when the answer is not in the list.
    //
    // The ladder stops after the first rung that matched everything, and 1.8.1
    // is explicit about why that is not the same as being right: iNaturalist
    // numbers observations in upload order, so with a single clue a neighbour of
    // the mistyped number satisfies "every clue" by coincidence far more often
    // than chance. The reader is the only one who can tell. So the search always
    // leaves a door open, and this is it - one button, at the top of the results,
    // which continues from exactly where the search stopped.
    function hideKeepLooking() {
        keepLooking.classList.add('hidden');
    }

    function showKeepLooking(outcome) {
        const next = outcome.continuation;
        if (!next) return;
        const large = next.reason === 'large_stage';
        keepLookingLabel.textContent = large ? 'Continue the deeper search' : 'Keep looking';
        keepLookingMessage.textContent = large
            ? 'Nothing has matched every clue yet. The next step is a big one, so it has not '
                + 'been started - it is ready when you are.'
            : 'The search stopped because something matched everything you told it. If none of '
                + 'the results below is the observation you want, keep going: numbers next to a '
                + 'mistyped one often belong to the same person, so an early match can be a '
                + 'coincidence rather than the answer.';
        keepLookingCost.textContent = large && next.total
            ? `Checks about ${next.total.toLocaleString()} more observation numbers `
                + `(around ${formatRoughDuration(next.seconds)}).`
            : 'Picks up exactly where it stopped — nothing already checked is checked again.';
        keepLooking.classList.remove('hidden');
    }

    async function continueAutoSearch() {
        // Guard rather than assume: the button is inside the form, so a stray
        // submit or a double click must not start a second ladder.
        if (activeSearch || !autoSession || !autoSession.continuation) return;
        const next = autoSession.continuation;
        await runAutoSearch({
            number: autoSession.number,
            digitsCap: autoSession.digitsCap,
            criteria: autoSession.criteria,
            resolved: autoSession.resolved,
            checkedTotal: autoSession.checkedTotal,
            uncheckedTotal: autoSession.uncheckedTotal,
            seenIds: next.seenIds,
            stage: next.stage,
            // Clicking through a large-stage stop is the permission to run it.
            allowLargeStage: next.reason === 'large_stage'
        });
    }

    keepLookingButton.addEventListener('click', continueAutoSearch);

    async function runSearch(event) {
        event.preventDefault();
        if (activeSearch) return;
        if (currentSearchMode() === 'auto') return runAutoSearch();
        return runManualSearch();
    }

    // ---- section:wiring ----
    form.addEventListener('submit', runSearch);
    form.querySelectorAll('input[name="search-mode"]').forEach(input =>
        input.addEventListener('change', () => {
            updateSearchMode();
            clearError();
        }));
    form.querySelectorAll('input[name="mode"]').forEach(input => input.addEventListener('change', () => {
        updateMode();
        // Alan 8/31/26 - a genus pinned in one mode means nothing in the next.
        manualAutocomplete.clear();
        manualAutocomplete.close();
    }));
    digitsSelect.addEventListener('change', () => { digitsTouched = true; });
    observationInput.addEventListener('input', () => {
        const id = parseObservationId(observationInput.value);
        document.getElementById('short-id-note').classList.toggle('hidden', !id || id.length > 5);
    });
    cancelButton.addEventListener('click', () => {
        if (!activeSearch) return;
        activeSearch.cancelled = true;
        activeSearch.controller?.abort();
        if (activeSearch.sleepTimer !== null) {
            window.clearTimeout(activeSearch.sleepTimer);
            const rejectSleep = activeSearch.sleepReject;
            activeSearch.sleepTimer = null;
            activeSearch.sleepReject = null;
            rejectSleep?.(new DOMException('Search cancelled', 'AbortError'));
        }
    });
    document.getElementById('clear-log').addEventListener('click', () => { logOutput.textContent = ''; });
    updateMode();
    updateSearchMode();
})();
