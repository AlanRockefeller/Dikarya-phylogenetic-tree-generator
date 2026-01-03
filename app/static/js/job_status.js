/**
 * Job Status Dashboard - Real-time SSE Client
 * 
 * Connects to the SSE endpoint and updates the UI in real-time.
 * Uses stable step keys: input, blast, align, trim, tree, post
 * States: queued, running, done, skipped, failed
 */

class JobStatusClient {
    constructor(jobId) {
        this.jobId = jobId;
        this.eventSource = null;
        this.autoscroll = true;
        this.startTime = null;
        this.elapsedTimer = null;
        this.lastStatus = null;
        this.isRedirecting = false;

        // DOM elements
        this.elements = {
            connectionIndicator: document.getElementById('connection-indicator'),
            connectionText: document.getElementById('connection-text'),
            statusBadge: document.getElementById('job-status-badge'),
            elapsedTime: document.getElementById('elapsed-time'),
            currentStepCard: document.getElementById('current-step-card'),
            currentStepName: document.getElementById('current-step-name'),
            currentStepDetail: document.getElementById('current-step-detail'),
            currentStepTime: document.getElementById('current-step-time'),
            overviewFeed: document.getElementById('overview-feed'),
            terminalContent: document.getElementById('terminal-content'),
            viewTreeBtn: document.getElementById('view-tree-btn'),
            errorPanel: document.getElementById('error-panel'),
        };

        // Step timeline elements
        this.stepElements = {
            input: document.getElementById('step-input'),
            blast: document.getElementById('step-blast'),
            align: document.getElementById('step-align'),
            trim: document.getElementById('step-trim'),
            tree: document.getElementById('step-tree'),
            post: document.getElementById('step-post'),
        };

        // Step icons
        this.stepIcons = {
            queued: '○',
            running: '●',
            done: '✓',
            skipped: '○',
            failed: '✗',
        };

        // Bind methods
        this.handleSnapshot = this.handleSnapshot.bind(this);
        this.handleEvent = this.handleEvent.bind(this);
        this.updateElapsedTime = this.updateElapsedTime.bind(this);
    }

    connect() {
        const url = `/api/job/${this.jobId}/events`;
        this.eventSource = new EventSource(url);

        // Named event: snapshot
        this.eventSource.addEventListener('snapshot', (e) => {
            try {
                const data = JSON.parse(e.data);
                this.handleSnapshot(data);
            } catch (err) {
                console.error('Failed to parse snapshot:', err);
            }
        });

        // Default event: all other messages
        this.eventSource.onmessage = (e) => {
            try {
                const data = JSON.parse(e.data);
                this.handleEvent(data);
            } catch (err) {
                console.error('Failed to parse event:', err);
            }
        };

        // Named event: ping (keepalive)
        this.eventSource.addEventListener('ping', () => {
            this.updateConnectionIndicator(true);
        });

        this.eventSource.onopen = () => {
            this.updateConnectionIndicator(true);
        };

        this.eventSource.onerror = () => {
            this.updateConnectionIndicator(false);
        };
    }

    disconnect() {
        if (this.eventSource) {
            this.eventSource.close();
            this.eventSource = null;
        }
        if (this.elapsedTimer) {
            clearInterval(this.elapsedTimer);
            this.elapsedTimer = null;
        }
    }

    updateConnectionIndicator(connected) {
        const indicator = this.elements.connectionIndicator;
        const text = this.elements.connectionText;

        if (connected) {
            indicator.classList.remove('disconnected');
            indicator.classList.add('connected');
            text.textContent = 'Connected';
        } else {
            indicator.classList.remove('connected');
            indicator.classList.add('disconnected');
            text.textContent = 'Reconnecting...';
        }
    }

    handleSnapshot(data) {
        console.log('Snapshot received:', data);

        const job = data.job;
        const logTails = data.log_tails;

        // Update status badge
        this.updateStatusBadge(job.status);

        // Start elapsed timer
        if (job.started_at) {
            this.startTime = new Date(job.started_at);
            if (job.status === 'running') {
                this.startElapsedTimer();
            } else if (job.elapsed_seconds) {
                this.elements.elapsedTime.textContent = this.formatDuration(job.elapsed_seconds);
            }
        }

        // Update pipeline timeline
        if (job.meta && job.meta.steps) {
            this.updateTimeline(job.meta.steps);

            // Show/hide trimmed FASTA download based on whether trimming was performed
            const trimStep = job.meta.steps.trim;
            const trimmedLink = document.getElementById('dl-trimmed');
            if (trimmedLink) {
                if (trimStep && trimStep.state && trimStep.state !== 'skipped') {
                    trimmedLink.style.display = '';
                } else {
                    trimmedLink.style.display = 'none';
                }
            }
        }

        // Update current step
        if (job.meta && job.meta.current_step) {
            const stepInfo = job.meta.steps?.[job.meta.current_step] || {};
            this.updateCurrentStep(
                stepInfo.label || job.meta.current_step,
                stepInfo.detail || '',
                job.status
            );
        }

        // Populate logs
        this.populateLogTails(logTails);

        // Sync Overview Feed with historical steps
        // 1. Clear default "Waiting..." message
        this.elements.overviewFeed.innerHTML = '';

        // 2. Add "Job started" if applicable
        if (job.started_at) {
            this.appendOverview({ message: 'Job started', icon: 'running' });
        }

        // 3. Backfill step events
        // We iterate through a logical order of steps to reconstruct the feed
        const stepOrder = ['input', 'blast', 'align', 'trim', 'tree', 'post'];
        stepOrder.forEach(stepKey => {
            const step = job.meta.steps?.[stepKey];
            if (!step) return;

            // Skipping 'skipped' steps in the feed to avoid clutter, or maybe show them as skipped?
            // Let's show done/running/failed
            if (step.state === 'done') {
                this.appendOverview({ message: `${step.label || stepKey} complete`, icon: 'done' });
            } else if (step.state === 'running') {
                this.appendOverview({ message: `Starting ${step.label || stepKey}...`, icon: 'running' });
            } else if (step.state === 'failed') {
                this.appendOverview({ message: `${step.label || stepKey} failed`, icon: 'failed' }); // using 'failed' icon class
            }
        });

        // 4. Handle terminal states
        if (job.status === 'completed') {
            this.showSuccessState(job.result_files);

            // Check for transition from running -> completed via snapshot (missed event case)
            // OR if the job finished very recently (e.g. user refreshed page within 1 minute of completion)
            const oldStatus = this.lastStatus;
            const isTransition = oldStatus && oldStatus !== job.status;
            const endedAt = job.ended_at ? new Date(job.ended_at) : null;
            const isRecent = endedAt ? ((Date.now() - endedAt.getTime()) / 1000) < 60 : false;

            if ((isTransition || isRecent) && !this.isRedirecting) {
                const targetUrl = `/job/${this.jobId}/view`;
                this.triggerRedirect(targetUrl);
            }
        } else if (job.status === 'failed') {
            this.showErrorPanel(job);
        }

        this.lastStatus = job.status;

        // Hide skeleton loading
        document.querySelectorAll('.skeleton').forEach(el => {
            el.classList.remove('skeleton');
        });
    }

    handleEvent(event) {
        switch (event.type) {
            case 'job_state':
                this.handleJobState(event);
                break;
            case 'step_start':
                this.handleStepStart(event);
                break;
            case 'step_done':
                this.handleStepDone(event);
                break;
            case 'step_failed':
                this.handleStepFailed(event);
                break;
            case 'log':
                this.appendLog(event);
                break;
            case 'overview':
                this.appendOverview(event);
                break;
            case 'metric':
                // Optional: could update specific UI elements
                console.log('Metric:', event);
                break;
        }
    }

    handleJobState(event) {
        console.log('Job State Event:', event.status, event);
        this.updateStatusBadge(event.status);

        // Update lastStatus tracking
        const oldStatus = this.lastStatus;
        this.lastStatus = event.status;

        if (event.status === 'completed') {
            console.log('Job Completed Event. Redirecting?', { isRedirecting: this.isRedirecting, url: event.view_url });
            this.showSuccessState(event.result_files);

            // Auto-redirect after delay
            if (!this.isRedirecting) {
                try {
                    const targetUrl = event.view_url || `/job/${this.jobId}/view`;
                    this.triggerRedirect(targetUrl);
                } catch (err) {
                    console.error('Error calling triggerRedirect:', err);
                }
            }

        } else if (event.status === 'failed') {
            this.showErrorPanel(event);
        }
    }

    triggerRedirect(url) {
        if (this.isRedirecting) return;
        this.isRedirecting = true;

        this.appendOverview({
            message: 'Redirecting to tree viewer...',
            icon: 'running'
        });

        setTimeout(() => {
            window.location.replace(url);
        }, 500);
    }

    handleStepStart(event) {
        // Show optional steps when they start (blast, trim)
        const optionalSteps = ['blast', 'trim'];
        if (optionalSteps.includes(event.step)) {
            const stepEl = this.stepElements[event.step];
            if (stepEl) {
                stepEl.style.display = '';
            }
        }

        // Update timeline
        this.updateStepState(event.step, 'running');

        // Update current step card
        this.updateCurrentStep(event.label, event.detail || '', 'running');

        // Add to overview
        this.appendOverview({
            message: `Starting ${event.label}...`,
            icon: 'running'
        });
    }

    handleStepDone(event) {
        // Update timeline
        this.updateStepState(event.step, 'done');

        // Add to overview
        this.appendOverview({
            message: event.detail || `${event.step} complete`,
            icon: 'done'
        });
    }

    handleStepFailed(event) {
        // Update timeline
        this.updateStepState(event.step, 'failed');

        // Update current step card
        this.updateCurrentStepFailed(event);
    }

    updateStatusBadge(status) {
        const badge = this.elements.statusBadge;
        badge.className = `status-badge ${status}`;

        const statusText = {
            queued: 'Queued',
            running: 'Running',
            completed: 'Completed',
            failed: 'Failed',
        };

        badge.textContent = statusText[status] || status;
    }

    updateTimeline(steps) {
        // Optional steps that should only be shown if they're actually being used
        const optionalSteps = ['blast', 'trim'];

        for (const [stepKey, stepInfo] of Object.entries(steps)) {
            const stepEl = this.stepElements[stepKey];
            if (!stepEl) continue;

            // Show optional steps only if they have a non-skipped state
            // (i.e., they're actually part of this job's pipeline)
            if (optionalSteps.includes(stepKey)) {
                if (stepInfo.state && stepInfo.state !== 'skipped') {
                    stepEl.style.display = '';  // Show
                } else {
                    stepEl.style.display = 'none';  // Keep hidden
                }
            }

            this.updateStepState(stepKey, stepInfo.state, stepInfo.label);
        }
    }

    updateStepState(stepKey, state, label) {
        const stepEl = this.stepElements[stepKey];
        if (!stepEl) return;

        const indicator = stepEl.querySelector('.step-indicator');
        const labelEl = stepEl.querySelector('.step-label');

        // Update indicator
        indicator.className = `step-indicator ${state}`;
        indicator.textContent = this.stepIcons[state] || '○';

        // Update label
        if (labelEl) {
            labelEl.className = `step-label ${state}`;
            if (label) {
                labelEl.textContent = label;
            }
        }
    }

    updateCurrentStep(name, detail, status) {
        const card = this.elements.currentStepCard;
        card.className = `current-step-card ${status}`;
        card.style.display = 'block';

        this.elements.currentStepName.textContent = name;
        this.elements.currentStepDetail.textContent = detail;
    }

    updateCurrentStepFailed(event) {
        const card = this.elements.currentStepCard;
        card.className = 'current-step-card failed';

        this.elements.currentStepName.textContent = `${event.step} Failed`;
        this.elements.currentStepDetail.textContent = event.error;
    }

    appendLog(event) {
        const container = this.elements.terminalContent;

        const lineEl = document.createElement('div');
        lineEl.className = `log-line ${event.stream}`;

        // Add step tag
        const tag = `[${event.step.toUpperCase()}]`;
        lineEl.innerHTML = `<span class="log-tag">${tag}</span>${this.escapeHtml(event.line)}`;

        container.appendChild(lineEl);

        // Autoscroll
        if (this.autoscroll) {
            container.scrollTop = container.scrollHeight;
        }
    }

    appendOverview(event) {
        const feed = this.elements.overviewFeed;

        const item = document.createElement('div');
        item.className = 'overview-item';

        const iconClass = event.icon || 'running';
        const iconMap = {
            done: '✓',
            running: '⋯',
            skipped: '○',
            failed: '✗',
        };

        item.innerHTML = `
            <span class="overview-icon ${iconClass}">${iconMap[iconClass] || '•'}</span>
            <span>${this.escapeHtml(event.message)}</span>
        `;

        feed.appendChild(item);

        // Scroll to bottom
        feed.scrollTop = feed.scrollHeight;
    }

    populateLogTails(logTails) {
        const container = this.elements.terminalContent;

        // Combine and sort by time (we don't have timestamps, just show in order)
        for (const [logName, lines] of Object.entries(logTails)) {
            for (const line of lines) {
                if (!line.trim()) continue;

                const lineEl = document.createElement('div');
                lineEl.className = 'log-line';
                lineEl.innerHTML = `<span class="log-tag">[${logName.toUpperCase()}]</span>${this.escapeHtml(line)}`;
                container.appendChild(lineEl);
            }
        }

        // Scroll to bottom
        if (this.autoscroll) {
            container.scrollTop = container.scrollHeight;
        }
    }

    showSuccessState(resultFiles) {
        // Stop elapsed timer
        if (this.elapsedTimer) {
            clearInterval(this.elapsedTimer);
        }

        // Update current step card
        const card = this.elements.currentStepCard;
        card.className = 'current-step-card completed';
        this.elements.currentStepName.textContent = 'Pipeline Complete';
        this.elements.currentStepDetail.textContent = 'Ready to view results';

        // Enable View Tree button
        const btn = this.elements.viewTreeBtn;
        if (btn && resultFiles) {
            btn.classList.remove('disabled');
            btn.setAttribute('aria-disabled', 'false');
            btn.href = resultFiles.tree_newick?.replace('/api/job', '/job').replace('/download/tree/newick', '/view')
                || `/job/${this.jobId}/view`;
        }

        // Add success to overview
        this.appendOverview({
            message: 'Pipeline complete! View your tree below.',
            icon: 'done'
        });
    }

    showErrorPanel(errorInfo) {
        // Stop elapsed timer
        if (this.elapsedTimer) {
            clearInterval(this.elapsedTimer);
        }

        const panel = this.elements.errorPanel;
        panel.style.display = 'block';

        // Fill in error details
        const stepLabel = errorInfo.failed_step_label || errorInfo.failed_step || 'Unknown Step';
        const tool = errorInfo.tool || '';
        const exitCode = errorInfo.exit_code;
        const errorSummary = errorInfo.error_summary || 'An error occurred';
        const stderrTail = errorInfo.stderr_tail || [];

        // Title
        panel.querySelector('.error-title').textContent = `${stepLabel} Failed`;

        // What happened
        let whatHappened = errorSummary;
        if (tool && exitCode !== undefined && exitCode !== null) {
            whatHappened = `${tool.toUpperCase()} exited with code ${exitCode}`;
        }
        panel.querySelector('.error-what').textContent = whatHappened;

        // Why (simple heuristics)
        let why = errorSummary;
        if (errorSummary.includes('at least 2 sequences')) {
            why = 'You need at least 2 sequences to build a tree. If you have a single sequence, enable BLAST to find related sequences.';
        }
        panel.querySelector('.error-why').textContent = why;

        // Relevant output
        if (stderrTail.length > 0) {
            panel.querySelector('.error-stderr').textContent = stderrTail.join('\n');
        }

        // Hide current step card
        this.elements.currentStepCard.style.display = 'none';
    }

    startElapsedTimer() {
        if (this.elapsedTimer) return;

        this.updateElapsedTime();
        this.elapsedTimer = setInterval(this.updateElapsedTime, 1000);
    }

    updateElapsedTime() {
        if (!this.startTime) return;

        const now = new Date();
        const elapsed = (now - this.startTime) / 1000;
        this.elements.elapsedTime.textContent = this.formatDuration(elapsed);
    }

    formatDuration(seconds) {
        const mins = Math.floor(seconds / 60);
        const secs = Math.floor(seconds % 60);

        if (mins > 0) {
            return `${mins}m ${secs}s`;
        }
        return `${secs}s`;
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    toggleAutoscroll() {
        this.autoscroll = !this.autoscroll;
        document.getElementById('autoscroll-checkbox').checked = this.autoscroll;
    }

    clearTerminal() {
        this.elements.terminalContent.innerHTML = '';
    }
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    // Get job ID from data attribute or URL
    const container = document.getElementById('job-status-container');
    const jobId = container?.dataset.jobId || window.location.pathname.split('/').filter(Boolean).pop();

    if (!jobId) {
        console.error('No job ID found');
        return;
    }

    // Create client and connect
    window.jobStatusClient = new JobStatusClient(jobId);
    window.jobStatusClient.connect();

    // Setup terminal controls
    const autoscrollCheckbox = document.getElementById('autoscroll-checkbox');
    if (autoscrollCheckbox) {
        autoscrollCheckbox.addEventListener('change', (e) => {
            window.jobStatusClient.autoscroll = e.target.checked;
        });
    }

    const clearBtn = document.getElementById('clear-terminal-btn');
    if (clearBtn) {
        clearBtn.addEventListener('click', () => {
            window.jobStatusClient.clearTerminal();
        });
    }

    // Setup tab switching
    const tabBtns = document.querySelectorAll('.tab-btn');
    console.log('Found tab buttons:', tabBtns.length);
    tabBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            const tabName = btn.dataset.tab;
            console.log('Tab clicked:', tabName);

            // Update button active states
            tabBtns.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');

            // Update content visibility
            document.querySelectorAll('.tab-content').forEach(pane => {
                pane.classList.remove('active');
                pane.style.display = 'none';
            });

            const targetPane = document.getElementById(`pane-${tabName}`);
            console.log('Target pane:', targetPane);
            if (targetPane) {
                targetPane.classList.add('active');
                targetPane.style.display = 'block';
            }

            // Load content on first switch to each tab
            if (tabName === 'sequences' && !window.sequencesLoaded) {
                loadSequences();
            }
            if (tabName === 'aligned' && !window.alignedLoaded) {
                loadAligned();
            }
            if (tabName === 'pipeline-log' && !window.pipelineLogLoaded) {
                loadLog('pipeline', 'pipeline-log-content');
            }
            if (tabName === 'alignment-log' && !window.alignmentLogLoaded) {
                loadLog('alignment', 'alignment-log-content');
            }
            if (tabName === 'tree-log' && !window.treeLogLoaded) {
                loadLog('tree_builder', 'tree-log-content');
            }
        });
    });

    // Generic function to load log files
    async function loadLog(logName, elementId) {
        const content = document.getElementById(elementId);
        content.textContent = 'Loading log...';

        try {
            const response = await fetch(`/api/job/${jobId}/logs/${logName}`);

            if (!response.ok) {
                content.textContent = `Log not available yet. (Status: ${response.status})`;
                return;
            }

            const logText = await response.text();
            content.textContent = logText || '(Empty log)';

            // Mark as loaded
            if (logName === 'pipeline') window.pipelineLogLoaded = true;
            if (logName === 'alignment') window.alignmentLogLoaded = true;
            if (logName === 'tree_builder') window.treeLogLoaded = true;

        } catch (err) {
            console.error(`Failed to load ${logName} log:`, err);
            content.textContent = 'Failed to load log.';
        }
    }

    // Function to load input sequences
    async function loadSequences() {
        const content = document.getElementById('sequence-content');
        const title = document.getElementById('sequence-title');
        const count = document.getElementById('sequence-count');

        try {
            const response = await fetch(`/api/job/${jobId}/download/fasta/original`);

            if (!response.ok) {
                content.textContent = 'Sequences not available yet.';
                return;
            }

            const fastaText = await response.text();
            content.textContent = fastaText;

            // Count sequences (lines starting with >)
            const seqCount = (fastaText.match(/^>/gm) || []).length;
            count.textContent = `${seqCount} sequence${seqCount !== 1 ? 's' : ''}`;

            title.textContent = 'Input Sequences';

            window.sequencesLoaded = true;
        } catch (err) {
            console.error('Failed to load sequences:', err);
            content.textContent = 'Failed to load sequences.';
        }
    }

    // Function to load aligned sequences
    async function loadAligned() {
        const content = document.getElementById('aligned-content');
        const title = document.getElementById('aligned-title');
        const count = document.getElementById('aligned-count');

        console.log('Loading aligned sequences for job:', jobId);
        content.textContent = 'Loading aligned sequences...';

        try {
            const response = await fetch(`/api/job/${jobId}/download/fasta/aligned`);
            console.log('Aligned response status:', response.status);

            if (!response.ok) {
                const errorText = await response.text();
                console.error('Aligned fetch failed:', response.status, errorText);
                content.textContent = `Aligned sequences not available yet.\n(Status: ${response.status})`;
                return;
            }

            const fastaText = await response.text();

            // Check if it looks like FASTA (starts with >)
            if (!fastaText.startsWith('>')) {
                console.error('Response does not look like FASTA:', fastaText.substring(0, 100));
                content.textContent = 'Aligned sequences not available yet.';
                return;
            }

            content.textContent = fastaText;

            // Count sequences (lines starting with >)
            const seqCount = (fastaText.match(/^>/gm) || []).length;
            count.textContent = `${seqCount} sequence${seqCount !== 1 ? 's' : ''}`;

            title.textContent = 'Aligned Sequences';

            window.alignedLoaded = true;
            console.log('Aligned sequences loaded successfully');
        } catch (err) {
            console.error('Failed to load aligned sequences:', err);
            content.textContent = 'Failed to load aligned sequences.';
        }
    }
});

// Cleanup on page unload
window.addEventListener('beforeunload', () => {
    if (window.jobStatusClient) {
        window.jobStatusClient.disconnect();
    }
});
