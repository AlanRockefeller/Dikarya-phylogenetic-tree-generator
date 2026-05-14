// Alan 5/14/26 - Add the Alkaloid Content Estimator widget behavior for searchable source-backed calculations.
(function () {
    const GENERIC_STRAIN = "__generic__";
    const COMPOUNDS = [
        ["psilocybin", "psilocybin"],
        ["psilocin", "psilocin"],
        ["baeocystin", "baeocystin"],
        ["aeruginascin", "aeruginascin"],
        ["norpsilocin", "norpsilocin"],
    ];

    const els = {
        form: document.getElementById("dosage-form"),
        speciesSearch: document.getElementById("dosage-species-search"),
        species: document.getElementById("dosage-species"),
        strain: document.getElementById("dosage-strain"),
        grams: document.getElementById("dosage-grams"),
        materialState: document.getElementById("dosage-material-state"),
        dataMode: document.getElementById("dosage-data-mode"),
        calculate: document.getElementById("dosage-calculate"),
        reset: document.getElementById("dosage-reset"),
        error: document.getElementById("dosage-form-error"),
        emptyState: document.getElementById("dosage-empty-state"),
        output: document.getElementById("dosage-output"),
        subtitle: document.getElementById("dosage-result-subtitle"),
        rowCount: document.getElementById("dosage-row-count"),
        compoundGrid: document.getElementById("dosage-compound-grid"),
        formula: document.getElementById("dosage-formula"),
        warnings: document.getElementById("dosage-warnings"),
        citationsCard: document.getElementById("dosage-citations-card"),
        citations: document.getElementById("dosage-citations"),
        tableCard: document.getElementById("dosage-table-card"),
        tableBody: document.getElementById("dosage-table-body"),
    };

    function debounce(fn, delay) {
        let timer = null;
        return function (...args) {
            window.clearTimeout(timer);
            timer = window.setTimeout(() => fn.apply(this, args), delay);
        };
    }

    async function apiGet(path, params = {}) {
        const url = new URL(path, window.location.origin);
        Object.entries(params).forEach(([key, value]) => {
            if (value !== undefined && value !== null && value !== "") {
                url.searchParams.set(key, value);
            }
        });
        const response = await fetch(url.toString(), { headers: { Accept: "application/json" } });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.status === "error") {
            throw new Error(data.error || "Request failed.");
        }
        return data;
    }

    function setLoading(isLoading) {
        els.calculate.disabled = isLoading;
        els.calculate.innerHTML = isLoading
            ? '<i class="fas fa-spinner fa-spin mr-2"></i>Calculating'
            : '<i class="fas fa-calculator mr-2"></i>Calculate';
    }

    function showError(message) {
        els.error.textContent = message;
        els.error.classList.remove("hidden");
    }

    function clearError() {
        els.error.textContent = "";
        els.error.classList.add("hidden");
    }

    function option(label, value) {
        const node = document.createElement("option");
        node.value = value;
        node.textContent = label;
        return node;
    }

    async function loadSpecies(query = "", selectedId = "") {
        els.species.innerHTML = "";
        els.species.appendChild(option("Loading species...", ""));
        try {
            const data = await apiGet("/api/dosage/species", { q: query });
            els.species.innerHTML = "";
            els.species.appendChild(option("Select a species", ""));
            data.species.forEach((row) => {
                const strain = row.strain ? ` - ${row.strain}` : "";
                // Alan 5/14/26 - Hide the redundant gold cap common name for P. cubensis dropdown rows.
                const common = row.common_name && row.scientific_name !== "Psilocybe cubensis" ? ` (${row.common_name})` : "";
                // Alan 5/14/26 - Remove source row counts from species dropdown labels to keep the picker user-focused.
                const label = `${row.scientific_name}${strain}${common}`;
                els.species.appendChild(option(label, row.species_id));
            });
            if (selectedId) {
                els.species.value = selectedId;
            }
            if (!data.species.length) {
                els.species.innerHTML = "";
                els.species.appendChild(option("No matching species with imported data", ""));
            }
        } catch (error) {
            els.species.innerHTML = "";
            els.species.appendChild(option("Species data unavailable", ""));
            showError(error.message);
        }
    }

    async function loadStrains(speciesId, selected = "") {
        els.strain.innerHTML = "";
        // Alan 5/14/26 - Shorten the default strain selector label per UI copy request.
        els.strain.appendChild(option("any", ""));
        if (!speciesId) {
            return;
        }
        try {
            const data = await apiGet(`/api/dosage/species/${speciesId}/strains`);
            data.strains.forEach((row) => {
                els.strain.appendChild(option(row.label, row.value));
            });
            if (selected) {
                els.strain.value = selected;
            }
        } catch (error) {
            showError(error.message);
        }
    }

    function formatNumber(value, digits = 3) {
        if (value === null || value === undefined) {
            return "not reported";
        }
        const abs = Math.abs(Number(value));
        if (abs === 0) {
            return "0";
        }
        if (abs < 0.01) {
            return Number(value).toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
        }
        return Number(value).toFixed(digits).replace(/0+$/, "").replace(/\.$/, "");
    }

    function formatMg(value) {
        if (value === null || value === undefined) {
            return "not reported";
        }
        return `${formatNumber(value, 2)} mg`;
    }

    function qualityClasses(level) {
        if (level === "high") {
            return "bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-200";
        }
        if (level === "medium") {
            return "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-200";
        }
        if (level === "low") {
            return "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200";
        }
        return "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-200";
    }

    function renderCompounds(summary, rowCount) {
        els.compoundGrid.innerHTML = "";
        COMPOUNDS.forEach(([key, label]) => {
            const item = summary[key] || {};
            const value = !item.reported
                ? "not reported"
                : item.estimate_mg !== null && item.estimate_mg !== undefined
                    ? formatMg(item.estimate_mg)
                    : `${formatMg(item.min_mg)} - ${formatMg(item.max_mg)}`;
            // Alan 5/14/26 - Remove low-value per-card helper copy in the result grid.
            const detail = item.reported && rowCount > 1 ? "reported range from selected rows" : "";
            const card = document.createElement("div");
            card.className = "rounded-lg border border-gray-200 p-4 dark:border-gray-700 dark:bg-journal-dark";
            card.innerHTML = `
                <p class="text-xs font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400">${escapeHtml(label)}</p>
                <p class="mt-2 text-2xl font-bold text-journal-dark dark:text-white">${escapeHtml(value)}</p>
                ${detail ? `<p class="mt-1 text-xs text-gray-500 dark:text-gray-400">${escapeHtml(detail)}</p>` : ""}
            `;
            els.compoundGrid.appendChild(card);
        });
    }

    function renderWarnings(warnings) {
        els.warnings.innerHTML = "";
        warnings.forEach((warning) => {
            const item = document.createElement("div");
            item.className = "rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-journal-gold dark:bg-journal-dark dark:text-journal-gold-light";
            item.innerHTML = `<i class="fas fa-triangle-exclamation mr-2"></i>${escapeHtml(warning)}`;
            els.warnings.appendChild(item);
        });
    }

    function renderCitations(rows) {
        els.citations.innerHTML = "";
        const seen = new Set();
        rows.forEach((item) => {
            const row = item.row;
            if (seen.has(row.reference_id)) {
                return;
            }
            seen.add(row.reference_id);
            const citation = document.createElement("article");
            citation.className = "rounded-lg border border-gray-200 p-4 dark:border-gray-700 dark:bg-journal-dark";
            const links = [
                row.doi ? `<a class="text-journal-gold hover:underline" href="${escapeAttr(doiUrl(row.doi))}" target="_blank" rel="noopener noreferrer">DOI</a>` : "",
                row.url ? `<a class="text-journal-gold hover:underline" href="${escapeAttr(row.url)}" target="_blank" rel="noopener noreferrer">source URL</a>` : "",
            ].filter(Boolean).join(" · ");
            citation.innerHTML = `
                <div class="flex flex-wrap items-center gap-2">
                    <span class="font-mono text-xs text-gray-500 dark:text-gray-400">${escapeHtml(row.citation_key || "reference")}</span>
                    <span class="rounded-full px-2 py-1 text-xs font-semibold ${qualityClasses(row.quality.level)}">${escapeHtml(row.quality.label)}</span>
                </div>
                <h3 class="mt-2 font-semibold text-journal-dark dark:text-white">${escapeHtml(row.title || "Untitled source")}</h3>
                <p class="mt-1 text-sm text-gray-600 dark:text-gray-300">${escapeHtml([row.authors, row.year].filter(Boolean).join(" · "))}</p>
                <p class="mt-1 text-sm text-gray-600 dark:text-gray-300">${escapeHtml(row.journal || "journal not reported")}</p>
                ${links ? `<p class="mt-2 text-sm">${links}</p>` : ""}
                <dl class="mt-3 grid gap-2 text-xs text-gray-600 dark:text-gray-300 sm:grid-cols-2">
                    <div><dt class="font-semibold">source_type</dt><dd>${escapeHtml(row.source_type || "not reported")}</dd></div>
                    <div><dt class="font-semibold">part_tested</dt><dd>${escapeHtml(row.part_tested || "not reported")}</dd></div>
                    <div><dt class="font-semibold">material_state</dt><dd>${escapeHtml(row.material_state || "not reported")}</dd></div>
                    <div><dt class="font-semibold">percent_basis</dt><dd>${escapeHtml(row.percent_basis || "not reported")}</dd></div>
                </dl>
                ${row.result_notes ? `<p class="mt-3 text-sm text-gray-600 dark:text-gray-300">${escapeHtml(row.result_notes)}</p>` : ""}
            `;
            els.citations.appendChild(citation);
        });
        els.citationsCard.classList.toggle("hidden", rows.length === 0);
    }

    function renderTable(rows) {
        els.tableBody.innerHTML = "";
        rows.forEach((item) => {
            const row = item.row;
            const tr = document.createElement("tr");
            tr.className = "text-gray-700 dark:text-gray-200";
            tr.innerHTML = `
                <td class="px-3 py-2 italic">${escapeHtml(row.scientific_name || "")}</td>
                <td class="px-3 py-2">${escapeHtml(row.display_strain || "generic / unspecified")}</td>
                <td class="px-3 py-2">${escapeHtml(row.source_type || "not reported")}</td>
                <td class="px-3 py-2">${escapeHtml(row.percent_basis || "not reported")}</td>
                <td class="px-3 py-2">${escapeHtml(row.part_tested || "not reported")}</td>
                <td class="px-3 py-2">${escapeHtml(formatNumber(row.psilocybin_pct))}</td>
                <td class="px-3 py-2">${escapeHtml(formatNumber(row.psilocin_pct))}</td>
                <td class="px-3 py-2">${escapeHtml(formatNumber(row.baeocystin_pct))}</td>
                <td class="px-3 py-2">${escapeHtml(formatNumber(row.aeruginascin_pct))}</td>
                <td class="px-3 py-2">${escapeHtml(formatNumber(row.norpsilocin_pct))}</td>
                <td class="px-3 py-2">${escapeHtml(row.citation_key || "not reported")}</td>
                <td class="px-3 py-2"><span class="rounded-full px-2 py-1 text-xs font-semibold ${qualityClasses(row.quality.level)}">${escapeHtml(row.quality.label)}</span></td>
            `;
            els.tableBody.appendChild(tr);
        });
        els.tableCard.classList.toggle("hidden", rows.length === 0);
    }

    function renderResult(data) {
        const rows = data.selected_rows || [];
        els.emptyState.classList.add("hidden");
        els.output.classList.remove("hidden");
        els.rowCount.textContent = `${rows.length} source row${rows.length === 1 ? "" : "s"}`;
        els.rowCount.classList.remove("hidden");
        els.subtitle.textContent = rows.length
            // Alan 5/14/26 - Remove dose-advice wording from the result subtitle.
            ? rows.length === 1 ? "" : "Range from matching source rows."
            : "No matching source row was found for that selection.";
        els.formula.textContent = data.formula || "mg = grams_material × effective_potency_percent × 10";

        renderCompounds(data.summary || {}, rows.length);
        renderWarnings(data.warnings || []);
        renderCitations(rows);
        renderTable(rows);
    }

    async function calculate() {
        clearError();
        if (!els.species.value) {
            showError("Choose a species before calculating.");
            return;
        }
        setLoading(true);
        try {
            const data = await apiGet("/api/dosage/calculate", {
                species_id: els.species.value,
                strain_or_variety: els.strain.value,
                grams: els.grams.value,
                material_state: els.materialState.value,
                data_mode: els.dataMode.value,
            });
            renderResult(data);
            syncUrl();
        } catch (error) {
            showError(error.message);
        } finally {
            setLoading(false);
        }
    }

    function syncUrl() {
        const params = new URLSearchParams();
        params.set("species_id", els.species.value);
        if (els.strain.value) params.set("strain", els.strain.value);
        params.set("grams", els.grams.value);
        params.set("material_state", els.materialState.value);
        params.set("data_mode", els.dataMode.value);
        window.history.replaceState({}, "", `${window.location.pathname}?${params.toString()}`);
    }

    function reset() {
        clearError();
        els.form.reset();
        els.speciesSearch.value = "";
        els.emptyState.classList.remove("hidden");
        els.output.classList.add("hidden");
        els.citationsCard.classList.add("hidden");
        els.tableCard.classList.add("hidden");
        els.rowCount.classList.add("hidden");
        window.history.replaceState({}, "", window.location.pathname);
        loadSpecies();
        loadStrains("");
    }

    function doiUrl(doi) {
        return doi.startsWith("http") ? doi : `https://doi.org/${doi}`;
    }

    function escapeHtml(value) {
        return String(value === null || value === undefined ? "" : value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function escapeAttr(value) {
        return escapeHtml(value).replace(/`/g, "&#096;");
    }

    async function init() {
        const params = new URLSearchParams(window.location.search);
        const speciesId = params.get("species_id") || "";
        const strain = params.get("strain") || "";
        // Alan 5/14/26 - Default the estimator amount to 1 gram when no shared URL value is present.
        els.grams.value = params.get("grams") || "1";
        els.materialState.value = params.get("material_state") || "dried";
        els.dataMode.value = params.get("data_mode") || "best_available";

        await loadSpecies("", speciesId);
        await loadStrains(speciesId, strain);
        if (speciesId && els.grams.value) {
            calculate();
        }
    }

    els.form.addEventListener("submit", (event) => {
        event.preventDefault();
        calculate();
    });

    els.reset.addEventListener("click", reset);

    els.species.addEventListener("change", () => {
        clearError();
        loadStrains(els.species.value);
    });

    els.speciesSearch.addEventListener("input", debounce(() => {
        clearError();
        loadSpecies(els.speciesSearch.value);
    }, 250));

    init();
})();
