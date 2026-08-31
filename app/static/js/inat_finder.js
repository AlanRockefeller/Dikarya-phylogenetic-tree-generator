(() => {
    'use strict';

    const API = 'https://api.inaturalist.org/v1';
    const BATCH_SIZE = 200;
    const MAX_VARIATIONS = 100000;
    // Alan 8/31/26 - port of inat.finder.py 1.7.5: large-search confirmation threshold.
    // inat.finder.py 1.7.5 asks for confirmation above this many variations.
    const LARGE_SEARCH_THRESHOLD = 5000;
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
    const termInput = document.getElementById('search-term');
    const observationInput = document.getElementById('observation-input');
    const digitsSelect = document.getElementById('digits-off');
    const verboseInput = document.getElementById('verbose');
    const errorBox = document.getElementById('form-error');
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
    const progressCount = document.getElementById('progress-count');
    const progressEta = document.getElementById('progress-eta');
    const suggestionList = document.getElementById('taxon-suggestions');
    const suggestionStatus = document.getElementById('taxon-suggestion-status');
    const pinnedBox = document.getElementById('taxon-pinned');
    const pinnedSwatch = document.getElementById('taxon-pinned-swatch');
    const pinnedLabel = document.getElementById('taxon-pinned-label');
    const pinnedClear = document.getElementById('taxon-pinned-clear');
    let activeSearch = null;

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
        clearTaxonChoices();
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
                document.getElementById('mode-taxon').checked = true;
                updateMode();
                termInput.value = String(taxon.id);
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

    async function resolveCriteria(mode, term, search) {
        if (mode === 'taxon') {
            const match = term.match(/^(?:.*\/taxa\/)?(\d+)/);
            const taxonId = match ? Number(match[1]) : NaN;
            if (!Number.isInteger(taxonId) || taxonId <= 0) {
                throw new Error(`“${term}” is not an iNaturalist taxon ID. Enter a number such as 48419, or an iNaturalist taxon URL.`);
            }
            let data;
            try {
                data = await apiGet(`/taxa/${taxonId}`, {}, search);
            } catch (error) {
                if (error.status === 404) throw new Error(`iNaturalist taxon ID ${taxonId} was not found.`);
                throw error;
            }
            const taxon = (data.results || []).find(item => String(item.id) === String(taxonId));
            if (!taxon) throw new Error(`iNaturalist taxon ID ${taxonId} was not found.`);
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
                const error = new Error(
                    `${rankLabel} “${term}” matches ${candidates.length} taxa in the iNaturalist taxonomy. Choose the one you meant.`
                );
                error.taxonCandidates = candidates;
                throw error;
            }
            const taxon = exact.size === 1 ? [...exact.values()][0] : null;
            if (!taxon) {
                throw new Error(`${rankLabel} “${term}” was not found in the iNaturalist taxonomy.`);
            }
            return {label: `${taxon.name} (taxon ID ${taxon.id})`, taxonId: Number(taxon.id), taxon};
        }
        if (mode === 'user') {
            let data;
            try {
                data = await apiGet(`/users/${encodeURIComponent(term)}`, {}, search);
            } catch (error) {
                if (error.status === 404) throw new Error(`iNaturalist user “${term}” was not found.`);
                throw error;
            }
            const user = (data.results || []).find(item => String(item.login || '').toLowerCase() === term.toLowerCase());
            if (!user) throw new Error(`iNaturalist user “${term}” was not found.`);
            return {label: user.login};
        }

        const projectUrlMatch = term.match(/(?:^|\/)projects\/([^/?#]+)/i);
        if (/^\d+$/.test(term)) {
            let data;
            try {
                data = await apiGet(`/projects/${term}`, {}, search);
            } catch (error) {
                if (error.status === 404) throw new Error(`iNaturalist project ID “${term}” was not found.`);
                throw error;
            }
            const project = (data.results || [])[0];
            if (!project) throw new Error(`iNaturalist project ID “${term}” was not found.`);
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
                throw new Error(`No exact project match for “${term}”. Similar projects: ${suggestions}. Use the project ID or exact slug.`);
            }
            if (exact.length > 1) throw new Error(`More than one project matches “${term}”. Use the numeric project ID.`);
            throw new Error(`iNaturalist project “${term}” was not found.`);
        }
        return {label: exact[0].title, projectId: String(exact[0].id), project: exact[0]};
    }

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

    // Alan 8/31/26 - autocomplete the taxon name against iNaturalist, so a
    // homonym like Lactarius the mushroom and Lactarius the fish is separated
    // before the search runs rather than raising an ambiguity error after it.
    const TAXON_MODES = new Set(['genus', 'family', 'taxon']);
    const SUGGESTION_DEBOUNCE_MS = 250;
    let suggestions = [];
    let activeSuggestion = -1;
    let suggestionRequest = null;
    let suggestionTimer = null;
    let pinnedTaxon = null;

    // Alan 8/31/26 - the one line that tells a searcher which taxon they got.
    function describeTaxonSuggestion(taxon) {
        const parts = [];
        if (taxon.rank) parts.push(taxon.rank);
        if (taxon.iconic_taxon_name) parts.push(taxon.iconic_taxon_name);
        if (taxon.preferred_common_name) parts.push(taxon.preferred_common_name);
        parts.push(`taxon ID ${taxon.id}`);
        return parts.join(' · ');
    }

    function closeSuggestions() {
        suggestions = [];
        activeSuggestion = -1;
        suggestionList.replaceChildren();
        suggestionList.classList.add('hidden');
        termInput.setAttribute('aria-expanded', 'false');
        termInput.removeAttribute('aria-activedescendant');
    }

    function highlightSuggestion(index) {
        activeSuggestion = index;
        [...suggestionList.children].forEach((item, position) => {
            const active = position === index;
            item.classList.toggle('bg-journal-gold/15', active);
            item.setAttribute('aria-selected', active ? 'true' : 'false');
            if (active) item.scrollIntoView({block: 'nearest'});
        });
        if (index >= 0) {
            termInput.setAttribute('aria-activedescendant', `taxon-suggestion-${index}`);
        } else {
            termInput.removeAttribute('aria-activedescendant');
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
        // Taxon-ID mode searches by the number; the name modes keep the name and
        // remember the ID behind it so the search never has to guess again.
        const value = currentMode() === 'taxon' ? String(taxon.id) : String(taxon.name || taxon.id);
        termInput.value = value;
        pinTaxon(taxon, value);
        closeSuggestions();
        clearError();
        termInput.focus();
    }

    function renderSuggestions(results) {
        suggestions = results;
        activeSuggestion = -1;
        suggestionList.replaceChildren();
        if (!results.length) {
            closeSuggestions();
            suggestionStatus.textContent = 'No matching taxa';
            return;
        }
        results.forEach((taxon, index) => {
            const item = document.createElement('li');
            item.id = `taxon-suggestion-${index}`;
            item.role = 'option';
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
            suggestionList.appendChild(item);
        });
        suggestionList.classList.remove('hidden');
        termInput.setAttribute('aria-expanded', 'true');
        suggestionStatus.textContent = `${results.length} matching taxa. Use the arrow keys to review them.`;
    }

    async function requestSuggestions(term) {
        const mode = currentMode();
        if (!TAXON_MODES.has(mode) || term.length < 2 || /^\d+$/.test(term)) {
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
        if (mode !== 'taxon') params.rank = mode;
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

    function appendResultCard(observation, originalId, location) {
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
        if (String(observation.id) === originalId) {
            const badge = document.createElement('span');
            badge.className = 'inline-flex rounded-full bg-green-100 dark:bg-green-950 px-2 py-0.5 text-xs font-semibold text-green-700 dark:text-green-300';
            badge.textContent = 'Original ID matches';
            badges.appendChild(badge);
        }
        if (badges.childElementCount) body.appendChild(badges);
        const title = document.createElement('h3');
        title.className = 'mt-1 truncate font-serif text-xl font-semibold italic text-journal-dark dark:text-white';
        title.textContent = observation.taxon?.name || observation.species_guess || 'Unknown taxon';
        body.appendChild(title);
        const details = document.createElement('p');
        details.className = 'mt-1 text-sm text-gray-500 dark:text-gray-400';
        details.textContent = `Observation #${observation.id} · @${observation.user?.login || 'unknown'}`;
        body.appendChild(details);
        if (location) {
            const place = document.createElement('p');
            place.className = 'mt-1 truncate text-sm text-gray-500 dark:text-gray-400';
            const pin = document.createElement('i');
            pin.className = 'fas fa-location-dot mr-1 text-xs';
            place.append(pin, location);
            body.appendChild(place);
        }
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
        resultsList.appendChild(card);
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
    // Matches are shown as each batch comes back rather than only at the end.
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
            if (element !== cancelButton) element.disabled = running;
        });
        searchButton.classList.toggle('opacity-60', running);
        cancelButton.disabled = !running;
        cancelButton.classList.toggle('hidden', !running);
        searchButton.querySelector('span').textContent = running ? 'Searching…' : 'Find observation';
        searchButton.querySelector('i').className = running ? 'fas fa-spinner fa-spin' : 'fas fa-magnifying-glass';
    }

    async function runSearch(event) {
        event.preventDefault();
        if (activeSearch) return;
        clearError();
        resultsSection.classList.add('hidden');
        resultsList.replaceChildren();
        logOutput.textContent = '';
        logSection.classList.toggle('hidden', !verboseInput.checked);

        const mode = currentMode();
        const term = termInput.value.trim();
        // Alan 8/31/26 - a taxon chosen from autocomplete is searched by its ID,
        // so a name shared by two kingdoms never has to be disambiguated twice.
        const usePinned = pinnedTaxon && TAXON_MODES.has(mode) && term === pinnedTaxon.term;
        const lookupMode = usePinned ? 'taxon' : mode;
        const lookupTerm = usePinned ? String(pinnedTaxon.id) : term;
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
            if (criteria.taxon) pinTaxon(criteria.taxon, term);

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

    form.addEventListener('submit', runSearch);
    form.querySelectorAll('input[name="mode"]').forEach(input => input.addEventListener('change', () => {
        updateMode();
        // Alan 8/31/26 - a genus pinned in one mode means nothing in the next.
        clearPinnedTaxon();
        closeSuggestions();
    }));

    // Alan 8/31/26 - debounced taxon autocomplete wiring.
    termInput.addEventListener('input', () => {
        if (pinnedTaxon && termInput.value.trim() !== pinnedTaxon.term) clearPinnedTaxon();
        window.clearTimeout(suggestionTimer);
        const term = termInput.value.trim();
        suggestionTimer = window.setTimeout(() => requestSuggestions(term), SUGGESTION_DEBOUNCE_MS);
    });
    termInput.addEventListener('keydown', event => {
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
    termInput.addEventListener('blur', () => window.setTimeout(closeSuggestions, 150));
    // Keep the click from blurring the input before the option is chosen.
    suggestionList.addEventListener('mousedown', event => event.preventDefault());
    pinnedClear.addEventListener('click', () => {
        clearPinnedTaxon();
        termInput.focus();
    });
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
})();
