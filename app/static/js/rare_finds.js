// Alan 10/21/26 - Rare Finds: rank the taxa an iNaturalist search returns by how
// rarely each is observed worldwide, and build a .pptx of the chosen ones.
// Everything runs in the browser against api.inaturalist.org; the only Dikarya
// request is /rare-finds/photo/..., which relays all-rights-reserved photos
// because static.inaturalist.org sends no CORS header.
(() => {
    'use strict';

    const API = 'https://api.inaturalist.org/v1';
    const SITE = 'https://www.inaturalist.org';
    // iNaturalist asks API clients to stay around one request per second.
    const REQUEST_GAP_MS = 1000;
    const REQUEST_TIMEOUT_MS = 30000;
    const TAXA_PER_PAGE = 500;
    // Pages are read rarest-in-search first (order=asc), so a capped search
    // still covers the taxa most likely to be rare worldwide.
    const MAX_TAXA_PAGES = 20;
    const SHOW_COUNT = 50;
    const DEFAULT_SELECTED = 15;
    const VERIFY_BATCH = 50;
    const OBSERVATION_BATCH = 30;
    // species, subspecies, variety, form and hybrid; a genus count says nothing
    // about rarity.
    const MAX_RANK_LEVEL = 10;
    const COMMERCIAL_LICENSES = new Set(['cc0', 'cc-by', 'cc-by-sa', 'cc-by-nd']);
    // Website-only parameters that change the page, not the result set.
    const DROP_PARAMS = new Set([
        'page', 'per_page', 'view', 'subview', 'order', 'order_by', 'tab', 'locale',
        'ttl', 'return_bounds', 'only_id', 'fields', 'preferred_place_id',
    ]);
    const OBS_FIELDS = '(id:!t,observed_on:!t,place_guess:!t,quality_grade:!t,faves_count:!t,'
        + 'user:(login:!t,name:!t),taxon:(id:!t,ancestor_ids:!t),'
        + 'photos:(id:!t,url:!t,license_code:!t,attribution:!t))';
    const COLORS = {dark: '1A2F23', green: '2D4A3E', gold: 'C9A962', ink: '7A5F1A', cream: 'F8F6F0', grey: '5B6660'};

    const $ = (id) => document.getElementById(id);
    const el = {
        form: $('rare-form'), url: $('rare-url'), run: $('rare-run'), cancel: $('rare-cancel'),
        formError: $('rare-form-error'), progress: $('rare-progress'), progressLabel: $('rare-progress-label'),
        progressCount: $('rare-progress-count'), progressFill: $('rare-progress-fill'),
        results: $('rare-results'), summary: $('rare-summary'), tbody: $('rare-tbody'),
        csv: $('rare-csv'), pptx: $('rare-pptx'), pptxLabel: $('rare-pptx-label'), pptxStatus: $('rare-pptx-status'),
        deckTitle: $('rare-deck-title'), deckSubtitle: $('rare-deck-subtitle'), commercial: $('rare-commercial'),
        selectTop: $('rare-select-top'), selectNone: $('rare-select-none'), describedOnly: $('rare-described-only'),
        selectedCount: $('rare-selected-count'),
    };

    let run = null;          // the active search: {cancelled, controller}
    let state = null;        // the finished search's results
    let lastRequestAt = 0;

    // ---------------------------------------------------------------- helpers

    function cancelledError() {
        return new DOMException('Search cancelled', 'AbortError');
    }

    function sleep(ms) {
        return new Promise((resolve) => window.setTimeout(resolve, ms));
    }

    async function pace() {
        const wait = lastRequestAt + REQUEST_GAP_MS - Date.now();
        if (wait > 0) await sleep(wait);
        lastRequestAt = Date.now();
    }

    async function apiGet(path, params, attempts = 4) {
        const url = new URL(`${API}${path}`);
        Object.entries(params || {}).forEach(([key, value]) => url.searchParams.set(key, value));
        for (let attempt = 1; attempt <= attempts; attempt += 1) {
            if (run && run.cancelled) throw cancelledError();
            await pace();
            const controller = new AbortController();
            if (run) run.controller = controller;
            const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
            let retryAfter = attempt * 2000;
            try {
                const response = await fetch(url, {signal: controller.signal, headers: {Accept: 'application/json'}});
                if (response.ok) return await response.json();
                if (![429, 500, 502, 503, 504].includes(response.status) || attempt === attempts) {
                    throw Object.assign(new Error(response.status === 429
                        ? 'iNaturalist is rate-limiting requests. Wait a minute and try again.'
                        : `iNaturalist returned HTTP ${response.status}.`), {noRetry: true});
                }
                retryAfter = Math.max(retryAfter, (Number(response.headers.get('Retry-After')) || 0) * 1000);
            } catch (error) {
                if (run && run.cancelled) throw cancelledError();
                if (error.noRetry || attempt === attempts) {
                    throw error.name === 'AbortError' ? new Error('iNaturalist did not respond. Try again.') : error;
                }
            } finally {
                window.clearTimeout(timer);
            }
            await sleep(retryAfter);
        }
        throw new Error('iNaturalist did not respond. Try again.');
    }

    function setProgress(label, done = null, total = null) {
        el.progress.classList.remove('hidden');
        el.progressLabel.textContent = label;
        if (done !== null && total) {
            el.progressCount.textContent = `${done} / ${total}`;
            el.progressFill.style.width = `${Math.min(100, Math.round((done / total) * 100))}%`;
        } else {
            el.progressCount.textContent = '';
        }
    }

    function showFormError(message) {
        el.formError.textContent = message;
        el.formError.classList.toggle('hidden', !message);
    }

    function isProvisional(name) {
        return name.includes("'") || name.includes('"');
    }

    function plural(n, one, many) {
        return `${n.toLocaleString()} ${n === 1 ? one : many}`;
    }

    // ------------------------------------------------------------ URL parsing

    // Turns an iNaturalist page URL into API search parameters. Throws an
    // Error with a user-facing message when the URL is not a search.
    function parseSearchUrl(text) {
        let raw = (text || '').trim();
        if (!raw) throw new Error('Paste an iNaturalist search URL.');
        if (!/^https?:\/\//i.test(raw)) raw = `https://${raw}`;
        let url;
        try {
            url = new URL(raw);
        } catch (error) {
            throw new Error('That does not look like a URL.');
        }
        const params = {};
        const parts = url.pathname.split('/').filter(Boolean).map((part) => decodeURIComponent(part));
        const head = (parts[0] || '').toLowerCase();
        let kind = 'observations';
        if (head === 'observations') {
            const second = parts[1] || '';
            if (/^\d+$/.test(second)) {
                throw new Error('That is a single observation. Paste a search (the Explore page) instead.');
            }
            if (second && !['identify', 'export', 'map'].includes(second.toLowerCase())) {
                params.user_id = second;
            }
        } else if (head === 'projects' && parts[1]) {
            params.project_id = parts[1];
            kind = 'project';
        } else if (head === 'people' && parts[1]) {
            params.user_id = parts[1];
        } else if (head === 'taxa' && /^\d+/.test(parts[1] || '')) {
            params.taxon_id = parts[1].match(/^\d+/)[0];
        } else {
            throw new Error('Paste an iNaturalist observation search, project or person URL.');
        }
        url.searchParams.forEach((value, key) => {
            if (DROP_PARAMS.has(key)) return;
            if (value === '' && !key.startsWith('field:')) return;
            params[key] = value;
        });
        if (!Object.keys(params).length) {
            throw new Error('That search has no filters, so it would cover all of iNaturalist. Add a place, taxon, project or observer.');
        }
        // The Explore page shows only verifiable observations unless told otherwise.
        if (kind === 'observations' && !('verifiable' in params) && !('quality_grade' in params)) {
            params.verifiable = 'true';
        }
        return {params, source: url.toString()};
    }

    async function describeSearch(params) {
        const parts = [];
        let taxonName = '';
        let placeName = '';
        try {
            if (params.taxon_id && /^[\d,]+$/.test(params.taxon_id)) {
                const data = await apiGet(`/taxa/${params.taxon_id.split(',').slice(0, 5).join(',')}`, {});
                taxonName = data.results.map((t) => t.name).join(', ');
            }
            if (params.place_id && /^[\d,]+$/.test(params.place_id)) {
                const data = await apiGet(`/places/${params.place_id.split(',').slice(0, 5).join(',')}`, {});
                placeName = data.results.map((p) => p.display_name || p.name).join(', ');
            }
            if (params.project_id) {
                const data = await apiGet(`/projects/${encodeURIComponent(params.project_id.split(',')[0])}`, {});
                if (data.results && data.results[0]) parts.push(data.results[0].title);
            }
        } catch (error) {
            if (error.name === 'AbortError') throw error;
            // A description is a nicety; the search itself can still run.
        }
        if (params.user_id) parts.push(`observed by ${params.user_id}`);
        Object.keys(params).filter((key) => key.startsWith('field:')).forEach((key) => {
            const field = key.slice(6);
            parts.push(params[key] ? `${field} = ${params[key]}` : `with ${field}`);
        });
        if (params.d1 || params.d2) parts.push(`${params.d1 || '…'} to ${params.d2 || 'now'}`);
        if (params.year) parts.push(`year ${params.year}`);
        if (params.quality_grade) parts.push(params.quality_grade.replace(/_/g, ' ').replace(/,/g, ' + '));
        return {taxonName, placeName, parts};
    }

    // --------------------------------------------------------------- search

    async function fetchSearchTaxa(params) {
        const taxa = [];
        let total = null;
        let pages = 1;
        for (let page = 1; page <= pages; page += 1) {
            setProgress('Reading the taxa in this search…', page - 1, pages);
            const data = await apiGet('/observations/species_counts',
                {...params, per_page: TAXA_PER_PAGE, page, order: 'asc'});
            if (total === null) {
                total = data.total_results || 0;
                pages = Math.min(MAX_TAXA_PAGES, Math.ceil(total / TAXA_PER_PAGE));
            }
            (data.results || []).forEach((row) => {
                const taxon = row.taxon || {};
                taxa.push({
                    id: taxon.id,
                    name: taxon.name || `Taxon ${taxon.id}`,
                    rank: taxon.rank || '',
                    rankLevel: taxon.rank_level,
                    common: taxon.preferred_common_name || '',
                    cachedWorld: taxon.observations_count || 0,
                    world: null,
                    local: row.count || 0,
                    taxonPhoto: taxon.default_photo ? taxon.default_photo.square_url : null,
                    observations: [],
                });
            });
            if (!data.results || data.results.length < TAXA_PER_PAGE) break;
        }
        return {taxa, total: total || 0, capped: total > MAX_TAXA_PAGES * TAXA_PER_PAGE};
    }

    function worldOf(taxon) {
        return taxon.world === null ? taxon.cachedWorld : taxon.world;
    }

    function byRarity(a, b) {
        return worldOf(a) - worldOf(b) || b.local - a.local || a.name.localeCompare(b.name);
    }

    // The taxon objects carry a cached count that can be badly stale (a renamed
    // taxon can read 0 while holding hundreds), so the counts that decide the
    // ranking are re-read live. Live counts only grow, so verifying until the
    // whole shown list is live leaves nothing unverified that could outrank it.
    // Both views ("all" and "hide provisional names") show their own top
    // SHOW_COUNT, so both are verified.
    function topTaxa(candidates, describedOnly) {
        const list = describedOnly ? candidates.filter((t) => !isProvisional(t.name)) : candidates;
        return list.slice(0, SHOW_COUNT);
    }

    function shownInEitherView(candidates) {
        return [...new Set([...topTaxa(candidates, false), ...topTaxa(candidates, true)])];
    }

    async function verifyWorldCounts(candidates) {
        let verified = 0;
        for (;;) {
            candidates.sort(byRarity);
            const need = shownInEitherView(candidates).filter((t) => t.world === null).slice(0, VERIFY_BATCH);
            if (!need.length) break;
            setProgress('Checking worldwide counts…', verified, verified + need.length);
            const data = await apiGet('/observations/species_counts',
                {taxon_id: need.map((t) => t.id).join(','), verifiable: 'any', per_page: TAXA_PER_PAGE});
            const counts = new Map((data.results || []).map((row) => [row.taxon.id, row.count]));
            for (const taxon of need) {
                if (counts.has(taxon.id)) {
                    taxon.world = counts.get(taxon.id);
                } else {
                    // Not a leaf worldwide (it has observations at a lower rank),
                    // so species_counts leaves it out; count it directly.
                    const one = await apiGet('/observations', {taxon_id: taxon.id, verifiable: 'any', per_page: 0});
                    taxon.world = one.total_results || 0;
                }
                verified += 1;
            }
        }
        return verified;
    }

    function representativeOrder(a, b) {
        const score = (o) => (o.quality_grade === 'research' ? 4 : 0) + ((o.photos || []).length ? 2 : 0);
        return score(b) - score(a) || (b.faves_count || 0) - (a.faves_count || 0) || a.id - b.id;
    }

    async function fetchObservations(params, shown) {
        for (let i = 0; i < shown.length; i += OBSERVATION_BATCH) {
            const chunk = shown.slice(i, i + OBSERVATION_BATCH);
            setProgress('Finding the observations…', i, shown.length);
            const byId = new Map(chunk.map((t) => [t.id, t]));
            for (let page = 1; page <= 3; page += 1) {
                const data = await apiGet('/observations', {
                    ...params, taxon_id: chunk.map((t) => t.id).join(','), per_page: 200, page, fields: OBS_FIELDS,
                });
                (data.results || []).forEach((obs) => {
                    const lineage = [obs.taxon && obs.taxon.id, ...((obs.taxon && obs.taxon.ancestor_ids) || [])];
                    const owner = lineage.map((id) => byId.get(id)).find(Boolean);
                    if (owner) owner.observations.push(obs);
                });
                if ((data.results || []).length < 200) break;
            }
        }
        shown.forEach((t) => t.observations.sort(representativeOrder));
    }

    async function runSearch(event) {
        event.preventDefault();
        if (run) return;
        showFormError('');
        let parsed;
        try {
            parsed = parseSearchUrl(el.url.value);
        } catch (error) {
            showFormError(error.message);
            return;
        }
        run = {cancelled: false, controller: null};
        el.run.disabled = true;
        el.cancel.classList.remove('hidden');
        el.results.classList.add('hidden');
        try {
            setProgress('Reading the search…');
            const description = await describeSearch(parsed.params);
            const {taxa, total, capped} = await fetchSearchTaxa(parsed.params);
            if (!total) throw new Error('iNaturalist found no observations for that search.');
            const candidates = taxa.filter((t) => t.id && t.rankLevel !== undefined && t.rankLevel <= MAX_RANK_LEVEL);
            if (!candidates.length) {
                throw new Error('None of the observations in that search is identified to species, so there is nothing to rank.');
            }
            const verified = await verifyWorldCounts(candidates);
            candidates.sort(byRarity);
            await fetchObservations(parsed.params, shownInEitherView(candidates));
            state = {
                params: parsed.params, source: parsed.source, description, total, capped, verified,
                speciesLevel: candidates.length, candidates, selected: new Set(), date: new Date(),
            };
            selectTop();
            fillDeckDefaults();
            render();
            el.progress.classList.add('hidden');
            el.results.classList.remove('hidden');
            el.results.scrollIntoView({behavior: 'smooth', block: 'start'});
        } catch (error) {
            el.progress.classList.add('hidden');
            showFormError(error.name === 'AbortError' ? 'Search cancelled.' : error.message);
        } finally {
            run = null;
            el.run.disabled = false;
            el.cancel.classList.add('hidden');
        }
    }

    // --------------------------------------------------------------- render

    function visibleTaxa() {
        return state ? topTaxa(state.candidates, el.describedOnly.checked) : [];
    }

    function selectTop() {
        state.selected = new Set(visibleTaxa().slice(0, DEFAULT_SELECTED).map((t) => t.id));
    }

    function fillDeckDefaults() {
        const {taxonName, placeName, parts} = state.description;
        el.deckTitle.value = `Rare ${taxonName || 'finds'}${placeName ? ` of ${placeName.split(',')[0]}` : ''}`;
        el.deckSubtitle.value = [...parts, 'ranked by worldwide iNaturalist observations'].join(' · ');
    }

    function link(href, text, className = '') {
        const a = document.createElement('a');
        a.href = href;
        a.target = '_blank';
        a.rel = 'noopener';
        a.textContent = text;
        a.className = className || 'text-journal-green dark:text-journal-gold hover:underline';
        return a;
    }

    function nameNode(taxon) {
        const span = document.createElement('span');
        formatName(taxon.name, taxon.rankLevel).forEach((part) => {
            const node = part.italic ? document.createElement('em') : document.createTextNode(part.text);
            if (part.italic) node.textContent = part.text;
            span.appendChild(node);
        });
        return span;
    }

    function cell(content, className = '') {
        const td = document.createElement('td');
        td.className = `px-3 py-2 align-top ${className}`;
        if (content instanceof Node) td.appendChild(content);
        else if (content !== undefined && content !== null) td.textContent = String(content);
        return td;
    }

    function render() {
        const shown = visibleTaxa();
        el.tbody.replaceChildren();
        shown.forEach((taxon, index) => {
            const tr = document.createElement('tr');
            const box = document.createElement('input');
            box.type = 'checkbox';
            box.className = 'h-4 w-4 rounded border-gray-300 text-journal-green focus:ring-journal-gold';
            box.checked = state.selected.has(taxon.id);
            box.setAttribute('aria-label', `Include ${taxon.name} in the slides`);
            box.addEventListener('change', () => {
                if (box.checked) state.selected.add(taxon.id);
                else state.selected.delete(taxon.id);
                updateSelectedCount();
            });
            tr.appendChild(cell(box));
            tr.appendChild(cell(index + 1, 'font-mono text-gray-500 dark:text-gray-400'));

            const obs = taxon.observations[0];
            const photo = obs && obs.photos && obs.photos[0];
            const thumb = document.createElement('img');
            thumb.alt = '';
            thumb.loading = 'lazy';
            thumb.className = 'h-14 w-14 rounded-lg object-cover bg-gray-100 dark:bg-gray-800';
            if (photo && photo.url) thumb.src = photo.url;
            else if (taxon.taxonPhoto) thumb.src = taxon.taxonPhoto;
            tr.appendChild(cell(thumb));

            const nameCell = document.createElement('div');
            const nameLink = link(`${SITE}/taxa/${taxon.id}`, '', 'font-semibold text-journal-dark dark:text-white hover:underline');
            nameLink.appendChild(nameNode(taxon));
            nameCell.appendChild(nameLink);
            if (taxon.common) {
                const common = document.createElement('div');
                common.className = 'text-xs text-gray-500 dark:text-gray-400';
                common.textContent = taxon.common;
                nameCell.appendChild(common);
            }
            if (isProvisional(taxon.name)) {
                const tag = document.createElement('div');
                tag.className = 'text-xs text-gray-500 dark:text-gray-400';
                tag.textContent = 'provisional name';
                nameCell.appendChild(tag);
            }
            if (el.commercial.checked && !pickPhoto(taxon, true)) {
                const tag = document.createElement('div');
                tag.className = 'text-xs text-amber-700 dark:text-amber-300';
                tag.textContent = 'no photo licensed for commercial use';
                nameCell.appendChild(tag);
            }
            tr.appendChild(cell(nameCell));

            tr.appendChild(cell(taxon.world.toLocaleString(), 'text-right font-mono font-semibold'));
            const share = taxon.world ? Math.round((taxon.local / taxon.world) * 100) : 0;
            const localLink = link(searchUrlForTaxon(taxon), taxon.local.toLocaleString());
            const localCell = document.createElement('div');
            localCell.appendChild(localLink);
            const shareNode = document.createElement('div');
            shareNode.className = 'text-xs text-gray-500 dark:text-gray-400';
            shareNode.textContent = `${share}% of world`;
            localCell.appendChild(shareNode);
            tr.appendChild(cell(localCell, 'text-right'));

            const obsCell = document.createElement('div');
            if (obs) {
                obsCell.appendChild(link(`${SITE}/observations/${obs.id}`, `#${obs.id}`));
                const meta = document.createElement('div');
                meta.className = 'text-xs text-gray-500 dark:text-gray-400';
                meta.textContent = [obs.user && obs.user.login, obs.observed_on].filter(Boolean).join(' · ');
                obsCell.appendChild(meta);
            }
            tr.appendChild(cell(obsCell));
            el.tbody.appendChild(tr);
        });
        const s = state;
        const pieces = [
            `${plural(s.total, 'taxon', 'taxa')} in this search, ${s.speciesLevel.toLocaleString()} at species level or below`,
            `showing the ${shown.length} least observed worldwide`,
        ];
        if (s.capped) pieces.push(`only the ${(MAX_TAXA_PAGES * TAXA_PER_PAGE).toLocaleString()} taxa least observed in this search were checked`);
        el.summary.textContent = `${pieces.join('; ')}. Counts as of ${s.date.toLocaleString()}.`;
        updateSelectedCount();
    }

    function updateSelectedCount() {
        const n = visibleTaxa().filter((t) => state.selected.has(t.id)).length;
        el.selectedCount.textContent = `${plural(n, 'slide', 'slides')} selected`;
        el.pptx.disabled = n === 0;
    }

    function searchUrlForTaxon(taxon) {
        const url = new URL(`${SITE}/observations`);
        Object.entries(state.params).forEach(([key, value]) => url.searchParams.set(key, value));
        url.searchParams.set('taxon_id', taxon.id);
        return url.toString();
    }

    // ----------------------------------------------------------------- names

    // Italic for the scientific name, roman for rank words and the quoted part
    // of a provisional name, as a taxonomist would set it.
    function formatName(name, rankLevel) {
        const parts = [];
        let inQuote = false;
        name.split(' ').forEach((token, index) => {
            let italic = rankLevel <= 20 && !inQuote;
            if (/^(var\.|subsp\.|ssp\.|f\.|sp\.|spp\.|cf\.|aff\.|×)$/.test(token)) italic = false;
            if (/^['"‘“]/.test(token)) {
                italic = false;
                inQuote = !/['"’”]$/.test(token.slice(1));
            } else if (inQuote) {
                italic = false;
                if (/['"’”]$/.test(token)) inQuote = false;
            }
            const text = (index ? ' ' : '') + token;
            const last = parts[parts.length - 1];
            if (last && last.italic === italic) last.text += text;
            else parts.push({text, italic});
        });
        return parts;
    }

    // ------------------------------------------------------------------ CSV

    function csvEscape(value) {
        const text = value === null || value === undefined ? '' : String(value);
        return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
    }

    function downloadBlob(blob, filename) {
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 10000);
    }

    function fileStem() {
        const stem = (el.deckTitle.value || 'rare-finds').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
        return stem.slice(0, 80) || 'rare-finds';
    }

    function downloadCsv() {
        const rows = [['rank', 'taxon', 'taxon_rank', 'common_name', 'worldwide_observations', 'observations_in_search',
            'share_of_world', 'provisional_name', 'taxon_url', 'search_url', 'observation_url', 'observer', 'observed_on']];
        visibleTaxa().forEach((t, i) => {
            const obs = t.observations[0];
            rows.push([i + 1, t.name, t.rank, t.common, t.world, t.local,
                t.world ? (t.local / t.world).toFixed(3) : '', isProvisional(t.name) ? 'yes' : 'no',
                `${SITE}/taxa/${t.id}`, searchUrlForTaxon(t), obs ? `${SITE}/observations/${obs.id}` : '',
                obs && obs.user ? obs.user.login : '', obs ? obs.observed_on : '']);
        });
        const csv = rows.map((row) => row.map(csvEscape).join(',')).join('\n');
        downloadBlob(new Blob([csv], {type: 'text/csv'}), `${fileStem()}.csv`);
    }

    // ---------------------------------------------------------------- photos

    function pickPhoto(taxon, commercialOnly) {
        for (const obs of taxon.observations) {
            const photo = (obs.photos || []).find((p) => p.url && (!commercialOnly || COMMERCIAL_LICENSES.has(p.license_code)));
            if (photo) return {obs, photo};
        }
        return null;
    }

    function photoSources(photoUrl) {
        const large = photoUrl.replace(/\/square\.(\w+)/, '/large.$1');
        const match = large.match(/^https:\/\/static\.inaturalist\.org\/photos\/(\d+)\/large\.(jpe?g|png|gif)/i);
        // static.inaturalist.org sends no CORS header, so go through Dikarya.
        return match ? [`/rare-finds/photo/${match[1]}/large.${match[2].toLowerCase()}`] : [large];
    }

    function blobToDataUrl(blob) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result);
            reader.onerror = () => reject(reader.error);
            reader.readAsDataURL(blob);
        });
    }

    function imageSize(dataUrl) {
        return new Promise((resolve, reject) => {
            const img = new Image();
            img.onload = () => resolve({w: img.naturalWidth, h: img.naturalHeight});
            img.onerror = () => reject(new Error('unreadable image'));
            img.src = dataUrl;
        });
    }

    async function loadPhoto(photoUrl) {
        for (const src of photoSources(photoUrl)) {
            for (let attempt = 1; attempt <= 2; attempt += 1) {
                try {
                    const response = await fetch(src, {mode: 'cors'});
                    if (response.status === 429 && attempt === 1) {
                        await sleep(5000);
                        continue;
                    }
                    if (!response.ok) break;
                    const data = await blobToDataUrl(await response.blob());
                    return {data, ...(await imageSize(data))};
                } catch (error) {
                    break;
                }
            }
        }
        return null;
    }

    // ----------------------------------------------------------------- slides

    function licenseLabel(code) {
        if (!code) return 'all rights reserved';
        return code === 'cc0' ? 'CC0' : code.toUpperCase().replace(/^CC-/, 'CC ');
    }

    function addTitleSlide(pptx, count) {
        const slide = pptx.addSlide();
        slide.background = {color: COLORS.dark};
        slide.addShape(pptx.ShapeType.rect, {x: 0.8, y: 2.35, w: 1.2, h: 0.08, fill: {color: COLORS.gold}, line: {color: COLORS.gold}});
        slide.addText(el.deckTitle.value || 'Rare finds', {
            x: 0.8, y: 2.55, w: 11.7, h: 1.4, fontFace: 'Georgia', fontSize: 44, bold: true, color: COLORS.gold, valign: 'top', fit: 'shrink',
        });
        slide.addText(el.deckSubtitle.value || '', {
            x: 0.8, y: 3.95, w: 11.7, h: 1.2, fontFace: 'Arial', fontSize: 20, color: COLORS.cream, valign: 'top', fit: 'shrink',
        });
        slide.addText(`${plural(count, 'taxon', 'taxa')} · worldwide counts from iNaturalist, ${state.date.toLocaleDateString()}`, {
            x: 0.8, y: 6.5, w: 11.7, h: 0.4, fontFace: 'Arial', fontSize: 12, color: 'A7B3AA',
        });
    }

    function addTaxonSlide(pptx, taxon, rank, picked, image) {
        const slide = pptx.addSlide();
        slide.background = {color: COLORS.cream};
        const box = {x: 0.4, y: 0.4, w: 7.5, h: 6.7};
        if (image) {
            const scale = Math.min(box.w / image.w, box.h / image.h);
            const w = image.w * scale;
            const h = image.h * scale;
            slide.addShape(pptx.ShapeType.rect, {...box, fill: {color: COLORS.dark}, line: {color: COLORS.dark}});
            slide.addImage({data: image.data, x: box.x + (box.w - w) / 2, y: box.y + (box.h - h) / 2, w, h});
        } else {
            slide.addShape(pptx.ShapeType.rect, {...box, fill: {color: 'E7E3D8'}, line: {color: 'E7E3D8'}});
            slide.addText(el.commercial.checked ? 'No photo licensed for commercial use' : 'No photo available', {
                ...box, align: 'center', valign: 'middle', fontFace: 'Arial', fontSize: 16, color: COLORS.grey,
            });
        }

        const x = 8.25;
        const w = 4.7;
        slide.addText(`#${rank} RAREST`, {x, y: 0.45, w, h: 0.35, fontFace: 'Arial', fontSize: 12, bold: true, color: COLORS.ink, charSpacing: 2});
        slide.addText(formatName(taxon.name, taxon.rankLevel).map((part) => ({
            text: part.text, options: {italic: part.italic},
        })), {x, y: 0.85, w, h: 1.25, fontFace: 'Georgia', fontSize: 28, bold: true, color: COLORS.dark, valign: 'top', fit: 'shrink'});
        let y = 2.1;
        if (taxon.common) {
            slide.addText(taxon.common, {x, y, w, h: 0.45, fontFace: 'Arial', fontSize: 15, color: COLORS.grey, valign: 'top', fit: 'shrink'});
            y += 0.5;
        }
        slide.addText(taxon.world.toLocaleString(), {x, y: y + 0.1, w, h: 1.1, fontFace: 'Georgia', fontSize: 66, bold: true, color: COLORS.ink, valign: 'top'});
        slide.addText(`${taxon.world === 1 ? 'observation' : 'observations'} on iNaturalist worldwide`, {
            x, y: y + 1.2, w, h: 0.4, fontFace: 'Arial', fontSize: 14, color: COLORS.dark,
        });
        const share = taxon.world ? Math.round((taxon.local / taxon.world) * 100) : 0;
        slide.addText(`${taxon.local.toLocaleString()} of them in this search (${share}%)`, {
            x, y: y + 1.6, w, h: 0.4, fontFace: 'Arial', fontSize: 14, bold: true, color: COLORS.green,
        });

        const obs = taxon.observations[0];
        if (obs) {
            const who = obs.user ? (obs.user.name ? `${obs.user.name} (${obs.user.login})` : obs.user.login) : '';
            const lines = [];
            if (who) lines.push({text: `Observed by ${who}`, options: {breakLine: true}});
            const when = [obs.observed_on, obs.place_guess].filter(Boolean).join(' · ');
            if (when) lines.push({text: when, options: {breakLine: true}});
            lines.push({text: `inaturalist.org/observations/${obs.id}`, options: {hyperlink: {url: `${SITE}/observations/${obs.id}`}, color: COLORS.green}});
            slide.addText(lines, {x, y: y + 2.25, w, h: 1.3, fontFace: 'Arial', fontSize: 12, color: COLORS.dark, valign: 'top', fit: 'shrink'});
        }
        if (isProvisional(taxon.name)) {
            slide.addText('Provisional name: known from sequenced collections, not yet formally described.', {
                x, y: 6.15, w, h: 0.45, fontFace: 'Arial', fontSize: 10, italic: true, color: COLORS.grey, valign: 'top', fit: 'shrink',
            });
        }
        if (picked && image) {
            let credit = picked.photo.attribution || `Photo: ${licenseLabel(picked.photo.license_code)}`;
            if (obs && picked.obs.id !== obs.id) credit += ` · photo from observation ${picked.obs.id}`;
            slide.addText(credit, {x, y: 6.65, w, h: 0.45, fontFace: 'Arial', fontSize: 9, color: COLORS.grey, valign: 'bottom', fit: 'shrink'});
        }
    }

    function addMethodsSlide(pptx) {
        const slide = pptx.addSlide();
        slide.background = {color: COLORS.dark};
        slide.addText('About these numbers', {x: 0.8, y: 0.6, w: 11.7, h: 0.8, fontFace: 'Georgia', fontSize: 32, bold: true, color: COLORS.gold});
        const bullets = [
            `Worldwide counts are every iNaturalist observation of each taxon, any quality grade, on ${state.date.toLocaleDateString()}.`,
            'Taxa identified only above species (for example a genus) are left out.',
            'Provisional names exist only where collections were sequenced, so their low counts partly reflect how little sequencing is done elsewhere.',
            'Photos remain the property of their photographers and are credited on each slide.',
        ];
        slide.addText(bullets.map((text) => ({text, options: {bullet: true, breakLine: true}})), {
            x: 0.8, y: 1.6, w: 11.7, h: 3.4, fontFace: 'Arial', fontSize: 18, color: COLORS.cream, valign: 'top', paraSpaceAfter: 10,
        });
        slide.addText([
            {text: 'Search: ', options: {color: 'A7B3AA'}},
            {text: state.source, options: {hyperlink: {url: state.source}, color: COLORS.gold}},
        ], {x: 0.8, y: 5.3, w: 11.7, h: 0.9, fontFace: 'Arial', fontSize: 11, valign: 'top', fit: 'shrink'});
        slide.addText('Made with Dikarya Rare Finds · dikarya.us/rare-finds', {
            x: 0.8, y: 6.5, w: 11.7, h: 0.4, fontFace: 'Arial', fontSize: 12, color: 'A7B3AA',
        });
    }

    async function downloadPptx() {
        if (!state || typeof window.PptxGenJS !== 'function') {
            el.pptxStatus.textContent = 'The slide builder has not loaded yet. Reload the page and try again.';
            el.pptxStatus.classList.remove('hidden');
            return;
        }
        const chosen = visibleTaxa().map((t, i) => ({taxon: t, rank: i + 1})).filter(({taxon}) => state.selected.has(taxon.id));
        if (!chosen.length) return;
        el.pptx.disabled = true;
        el.pptxStatus.classList.remove('hidden');
        try {
            const pptx = new window.PptxGenJS();
            pptx.layout = 'LAYOUT_WIDE';
            pptx.title = el.deckTitle.value || 'Rare finds';
            pptx.company = 'Dikarya';
            addTitleSlide(pptx, chosen.length);
            let missing = 0;
            for (let i = 0; i < chosen.length; i += 1) {
                const {taxon, rank} = chosen[i];
                el.pptxStatus.textContent = `Adding photo ${i + 1} of ${chosen.length}…`;
                const picked = pickPhoto(taxon, el.commercial.checked);
                const image = picked ? await loadPhoto(picked.photo.url) : null;
                if (!image) missing += 1;
                addTaxonSlide(pptx, taxon, rank, picked, image);
            }
            addMethodsSlide(pptx);
            el.pptxStatus.textContent = 'Building the file…';
            await pptx.writeFile({fileName: `${fileStem()}.pptx`});
            el.pptxStatus.textContent = `Downloaded ${plural(chosen.length + 2, 'slide', 'slides')}`
                + (missing ? `; ${plural(missing, 'slide has', 'slides have')} no photo.` : '.');
        } catch (error) {
            el.pptxStatus.textContent = `Could not build the presentation: ${error.message}`;
        } finally {
            el.pptx.disabled = false;
        }
    }

    // ------------------------------------------------------------------ wiring

    el.form.addEventListener('submit', runSearch);
    el.cancel.addEventListener('click', () => {
        if (!run) return;
        run.cancelled = true;
        if (run.controller) run.controller.abort();
    });
    el.csv.addEventListener('click', downloadCsv);
    el.pptx.addEventListener('click', downloadPptx);
    el.selectTop.addEventListener('click', () => { selectTop(); render(); });
    el.selectNone.addEventListener('click', () => { state.selected.clear(); render(); });
    el.describedOnly.addEventListener('change', () => { if (state) render(); });
    el.commercial.addEventListener('change', () => { if (state) render(); });

    // A search URL in ?url= runs straight away, so a result can be shared as a link.
    const preset = new URLSearchParams(window.location.search).get('url');
    if (preset) {
        el.url.value = preset;
        el.form.requestSubmit();
    }
})();
