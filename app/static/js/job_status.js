/**
 * Job Status Dashboard - Real-time SSE Client
 * 
 * Connects to the SSE endpoint and updates the UI in real-time.
 * Uses stable step keys: input, blast, align, trim, tree, post
 * States: queued, running, done, skipped, failed
 */

class JobStatusClient {
    // Upper bound on retained terminal rows (see _trimTerminal).
    static MAX_LOG_LINES = 2000;

    // Alan 8/23/26 - The only stream values that may become a CSS class. 'cmd' is
    // what publish_command() emits and is what makes the "$ mafft ..." lines green;
    // whitelisting rather than passing event.stream through keeps a hostile value
    // from turning into an arbitrary class.
    static LOG_STREAM_CLASSES = new Set(['stdout', 'stderr', 'cmd']);

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
            orient: document.getElementById('step-orient'),
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
        // Alan 8/23/26 - Never leave a second stream open: each one holds a server
        // request slot, and the pageshow reconnect below can call this again.
        if (this.eventSource) {
            this.eventSource.close();
            this.eventSource = null;
        }
        const url = `/api/job/${this.jobId}/events`;
        this.eventSource = new EventSource(url);

        // Named event: snapshot
        this.eventSource.addEventListener('snapshot', (e) => {
            try {
                const data = JSON.parse(e.data);
                this.handleSnapshot(data);
            } catch (err) {
                console.error('Failed to parse snapshot:', err);
                window.reportClientError?.('job_status.snapshot_parse', err);
            }
        });

        // Default event: all other messages
        this.eventSource.onmessage = (e) => {
            try {
                const data = JSON.parse(e.data);
                this.handleEvent(data);
            } catch (err) {
                console.error('Failed to parse event:', err);
                window.reportClientError?.('job_status.event_parse', err);
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
        // Alan 7/18/26 - Count elapsed time from enqueue so queue wait no longer displays as "--".
        const elapsedStartedAt = job.enqueued_at || job.started_at;
        // Alan 7/18/26 - Start the live counter for both queued and running jobs when the timestamp is valid.
        if (elapsedStartedAt) {
            // Alan 7/18/26 - Store the queue timestamp used by the one-second elapsed-time updater.
            // Alan 8/23/26 - A malformed timestamp yields a truthy Invalid Date, which the
            // updater would render as "NaNs"; drop it and keep the server-rendered value.
            const parsedStart = new Date(elapsedStartedAt);
            this.startTime = Number.isNaN(parsedStart.getTime()) ? null : parsedStart;
            // Alan 7/18/26 - Keep counting while a job waits for a worker as well as while its pipeline runs.
            if (job.status === 'queued' || job.status === 'running') {
                this.startElapsedTimer();
            // Alan 7/18/26 - Render a server-calculated terminal duration even when it is zero seconds.
            } else if (job.elapsed_seconds !== null && job.elapsed_seconds !== undefined) {
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

        // Alan 7/10/26 - Restore MycoMap refresh fallback warnings when a user opens or reloads the status page.
        const mycomapRefreshWarnings = Array.isArray(job.meta?.mycomap_refresh_warnings)
            ? job.meta.mycomap_refresh_warnings
            : [];
        mycomapRefreshWarnings.forEach(message => {
            this.appendOverview({ message, icon: 'failed' });
        });

        // Alan 8/5/26 - Tell the user when a job was auto-resubmitted after a server
        // restart. Without this the job silently starts over from step one and looks
        // like it stalled or lost progress.
        if (job.interrupted_notice && !this._noticedRequeue) {
            this._noticedRequeue = true;
            this.appendOverview({ message: job.interrupted_notice, icon: 'running' });
        }

        // 2. Add "Job started" if applicable
        if (job.started_at) {
            this.appendOverview({ message: 'Job started', icon: 'running' });
        }

        // 3. Backfill step events
        // We iterate through a logical order of steps to reconstruct the feed
        const stepOrder = ['input', 'orient', 'blast', 'its', 'align', 'trim', 'tree', 'post'];
        stepOrder.forEach(stepKey => {
            const step = job.meta?.steps?.[stepKey];
            if (!step) return;

            // Skipping 'skipped' steps in the feed to avoid clutter, or maybe show them as skipped?
            // Let's show done/running/failed
            if (step.state === 'done') {
                // Use step.detail if available (e.g., "2 sequence(s) reverse complemented")
                const doneMessage = step.detail || `${step.label || stepKey} complete`;
                this.appendOverview({ message: doneMessage, icon: 'done' });
            } else if (step.state === 'running') {
                this.appendOverview({ message: `Starting ${step.label || stepKey}...`, icon: 'running' });
            } else if (step.state === 'failed') {
                this.appendOverview({ message: `${step.label || stepKey} failed`, icon: 'failed' }); // using 'failed' icon class
            }
        });

        // Alan 7/18/26 - Explain the otherwise silent wait before a queued Mushroom Observer job starts.
        if (job.status === 'queued' && this.elements.overviewFeed.children.length === 0) {
            // Alan 7/18/26 - Identify the high-priority Mushroom Observer lane without claiming the current task can be preempted.
            const waitingMessage = job.meta?.source === 'mushroom_observer_single_tree'
                ? 'Mushroom Observer tree queued in the high-priority lane; waiting for the worker to finish its current task.'
                : 'Job queued; waiting for a worker to start it.';
            // Alan 7/18/26 - Keep the queue state visible until normal worker overview events arrive.
            this.appendOverview({ message: waitingMessage, icon: 'running' });
        }

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

        // Alan 8/5/26 - Close the stream once the job is terminal. Nothing further can
        // arrive, and leaving EventSource open held a server request slot (the pool is
        // only workers x threads) for as long as the tab stayed open. The server now
        // also closes terminal streams, so without this the browser would just keep
        // reconnecting every few seconds.
        if (job.status === 'completed' || job.status === 'failed') {
            this.disconnect();
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
                    window.reportClientError?.('job_status.redirect', err);
                }
            }

        } else if (event.status === 'failed') {
            this.showErrorPanel(event);
        }

        // Alan 8/5/26 - Same as the snapshot path: release the stream (and the server
        // request slot behind it) as soon as the job reaches a terminal state.
        if (event.status === 'completed' || event.status === 'failed') {
            this.disconnect();
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

    // Alan 8/23/26 - Build one terminal row from text only. Everything here arrives
    // over SSE, so nothing is interpolated into markup: the tag and the line become
    // text nodes and the stream only ever contributes a known class name.
    _buildLogLine(tagText, lineText, stream) {
        const lineEl = document.createElement('div');
        lineEl.className = 'log-line';
        if (JobStatusClient.LOG_STREAM_CLASSES.has(stream)) {
            lineEl.classList.add(stream);
        }

        const tagEl = document.createElement('span');
        tagEl.className = 'log-tag';
        tagEl.textContent = tagText;
        lineEl.appendChild(tagEl);
        lineEl.appendChild(document.createTextNode(lineText == null ? '' : String(lineText)));
        return lineEl;
    }

    // Alan 8/23/26 - A long alignment or tree run emits thousands of lines; without a
    // cap the node count grows for the whole lifetime of the tab.
    _trimTerminal(container) {
        while (container.childElementCount > JobStatusClient.MAX_LOG_LINES) {
            container.removeChild(container.firstElementChild);
        }
    }

    appendLog(event) {
        const container = this.elements.terminalContent;

        // Alan 8/23/26 - event.step is absent on some worker log events; String() keeps
        // this from throwing the way `event.step.toUpperCase()` did.
        const step = String(event.step || 'log').toUpperCase();
        container.appendChild(this._buildLogLine(`[${step}]`, event.line, event.stream));
        this._trimTerminal(container);

        // Autoscroll
        if (this.autoscroll) {
            container.scrollTop = container.scrollHeight;
        }
    }

    appendOverview(event) {
        const feed = this.elements.overviewFeed;

        const item = document.createElement('div');
        item.className = 'overview-item';

        const iconMap = {
            done: '✓',
            running: '⋯',
            skipped: '○',
            failed: '✗',
        };
        // Alan 8/23/26 - Only the four known keys may reach the class attribute.
        const iconClass = Object.prototype.hasOwnProperty.call(iconMap, event.icon) ? event.icon : 'running';

        const iconEl = document.createElement('span');
        iconEl.className = `overview-icon ${iconClass}`;
        iconEl.textContent = iconMap[iconClass];

        const msgEl = document.createElement('span');
        msgEl.textContent = event.message == null ? '' : String(event.message);

        item.appendChild(iconEl);
        item.appendChild(msgEl);
        feed.appendChild(item);

        // Scroll to bottom
        feed.scrollTop = feed.scrollHeight;
    }

    populateLogTails(logTails) {
        const container = this.elements.terminalContent;

        // Alan 8/23/26 - A reconnect (bfcache restore, dropped stream) re-delivers the
        // snapshot; without this the same tail would be appended to the terminal again.
        if (this._logTailsPopulated) return;
        this._logTailsPopulated = true;

        // Combine and sort by time (we don't have timestamps, just show in order)
        for (const [logName, lines] of Object.entries(logTails || {})) {
            for (const line of lines) {
                if (!line.trim()) continue;
                container.appendChild(
                    this._buildLogLine(`[${String(logName).toUpperCase()}]`, line, null)
                );
            }
        }
        this._trimTerminal(container);

        // Scroll to bottom
        if (this.autoscroll) {
            container.scrollTop = container.scrollHeight;
        }
    }

    showSuccessState(resultFiles) {
        // Stop elapsed timer
        if (this.elapsedTimer) {
            clearInterval(this.elapsedTimer);
            // Alan 8/23/26 - startElapsedTimer() returns early while this is truthy, so a
            // stale id here would make the timer unrestartable.
            this.elapsedTimer = null;
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

        // Alan 7/15/26 - Offer raw MrBayes command and trace files only when this completed job produced them.
        const mrbayesLink = document.getElementById('dl-mrbayes');
        // Alan 7/15/26 - Keep the matching dropdown heading synchronized with the conditional analysis download.
        const mrbayesHeading = document.getElementById('dl-mrbayes-heading');
        // Alan 7/15/26 - Use the completion payload to avoid showing a dead MrBayes link for other tree methods.
        if (mrbayesLink && mrbayesHeading && resultFiles?.mrbayes) {
            // Alan 7/15/26 - Point the visible link at the access-controlled archive endpoint supplied by the server.
            mrbayesLink.href = resultFiles.mrbayes;
            // Alan 7/15/26 - Reveal both Bayesian download elements together after successful completion.
            mrbayesLink.style.display = '';
            mrbayesHeading.style.display = '';
        }

        // Alan 7/15/26 - Find the optional bundle that pairs before/after FASTA files with the trimmer's marked report.
        const alignmentInspectionLink = document.getElementById('dl-alignment-inspection');
        // Alan 7/15/26 - Show the inspection download only when this completed job actually produced a trimming report.
        if (alignmentInspectionLink && resultFiles?.alignment_inspection) {
            // Alan 7/15/26 - Use the access-controlled archive URL supplied by the completion payload.
            alignmentInspectionLink.href = resultFiles.alignment_inspection;
            // Alan 7/15/26 - Reveal the inspection bundle alongside the aligned and trimmed FASTA downloads.
            alignmentInspectionLink.style.display = '';
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
            this.elapsedTimer = null;
        }

        const panel = this.elements.errorPanel;
        panel.style.display = 'block';

        // Fill in error details
        const stepLabel = errorInfo.failed_step_label || errorInfo.failed_step || 'Unknown Step';
        const tool = errorInfo.tool || '';
        const exitCode = errorInfo.exit_code;
        const errorSummary = errorInfo.error_summary || 'An error occurred';
        const stderrTail = errorInfo.stderr_tail || [];

        // Alan 8/5/26 - A job killed mid-run (server restart, OOM) never reaches the
        // failure handler, so it has no step or error of its own. Report the
        // interruption plainly instead of "Unknown Step Failed / An error occurred".
        const wasInterrupted = !!errorInfo.interrupted;
        const wasRequeued = !!errorInfo.requeued_after_interrupt;

        // Title
        panel.querySelector('.error-title').textContent = wasInterrupted
            ? (wasRequeued ? 'Interrupted - Restarted Automatically' : 'Job Interrupted')
            : `${stepLabel} Failed`;

        // What happened
        let whatHappened = errorSummary;
        if (wasInterrupted) {
            whatHappened = stepLabel && stepLabel !== 'Unknown Step'
                ? `The server restarted while this job was in "${stepLabel}".`
                : 'The server restarted while this job was running.';
        } else if (tool && exitCode !== undefined && exitCode !== null) {
            whatHappened = `${tool.toUpperCase()} exited with code ${exitCode}`;
        } else if (tool) {
            whatHappened = `${tool.toUpperCase()} failed during ${stepLabel}`;
        }
        panel.querySelector('.error-what').textContent = whatHappened;

        // Alan 8/5/26 - Widened the "why" heuristics. Previously only one case was
        // recognised and everything else echoed the raw exception, which for tool
        // failures is often unreadable.
        let why = errorSummary;
        const haystack = `${errorSummary} ${stderrTail.join(' ')}`.toLowerCase();
        if (wasInterrupted) {
            why = wasRequeued
                ? 'Nothing is wrong with your data. The job was resubmitted automatically and will run again from the start - you can leave this page open.'
                : 'Nothing is wrong with your data - the server restarted mid-run. Please submit the job again.';
        } else if (errorSummary.includes('at least 2 sequences')) {
            why = 'You need at least 2 sequences to build a tree. If you have a single sequence, enable BLAST to find related sequences.';
        } else if (haystack.includes('cpu-time allowance') || haystack.includes('sigxcpu') || haystack.includes('cpu limit')) {
            why = 'This analysis needed more processing time than the server allowance. Dikarya stopped only this job so the website and other queued work could remain available. Try MAFFT or FastTree; if it happens again, contact Alan below.';
        } else if (haystack.includes('out of memory') || haystack.includes('bad_alloc') || haystack.includes('cannot allocate') || haystack.includes('memory pressure')) {
            why = 'The analysis exceeded the memory available to one job, so it was stopped before it could take down the site. Try removing some sequences; if the dataset should fit, contact Alan below.';
        } else if (haystack.includes('timeout') || haystack.includes('timed out')) {
            why = 'The job exceeded its time limit and was stopped without crashing the site. Try fewer sequences or a faster method such as MAFFT or FastTree; if it happens again, contact Alan below.';
        } else if (haystack.includes('invalid dna') || haystack.includes('invalid symbol') || haystack.includes('contains invalid')) {
            why = 'One or more sequences contain characters that are not valid DNA. Check for pasted labels or punctuation on sequence lines.';
        } else if (haystack.includes('binary not found') || haystack.includes('no such file or directory')) {
            why = 'A required tool could not be run on the server. This is a server-side problem, not a problem with your data - please report it.';
        } else if (haystack.includes('empty') && haystack.includes('alignment')) {
            why = 'The alignment came out empty. This usually means the sequences had no overlapping region, or trimming removed everything.';
        // Alan 8/5/26 - MycoMap queue failures had no "why" of their own, so the
        // whole backlog paragraph was echoed into both columns.
        } else if (haystack.includes('mycomap')) {
            why = 'Nothing is wrong with your data or your observation. MycoMap runs the BLAST search that supplies this tree\'s sequences, and its queue is shared with every other MycoMap user - when it is busy, results can take much longer than usual to appear.';
        }

        // Alan 8/5/26 - Both fields default to the raw error summary, so any failure
        // that matched no heuristic printed the identical text under "What happened"
        // and again under "Why". Show the explanation once and give it the full width.
        const whyEl = panel.querySelector('.error-why');
        const whyColumn = whyEl.parentElement;
        const whatColumn = panel.querySelector('.error-what').parentElement;
        if (why === whatHappened) {
            if (whyColumn) whyColumn.style.display = 'none';
            if (whatColumn) whatColumn.classList.add('md:col-span-2');
        } else {
            whyEl.textContent = why;
            if (whyColumn) whyColumn.style.display = '';
            if (whatColumn) whatColumn.classList.remove('md:col-span-2');
        }

        // Relevant output. stderr is the most useful thing we have; fall back to
        // the traceback tail so the panel is never blank.
        const output = stderrTail.length > 0
            ? stderrTail
            : (errorInfo.traceback_tail || []);
        const stderrEl = panel.querySelector('.error-stderr');
        // Alan 8/5/26 - Hide the output section when the only thing we could put in
        // it is the error summary that is already displayed above.
        if (output.length > 0) {
            stderrEl.textContent = output.join('\n');
            if (stderrEl.parentElement) stderrEl.parentElement.style.display = '';
        } else {
            stderrEl.textContent = '';
            if (stderrEl.parentElement) stderrEl.parentElement.style.display = 'none';
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

    toggleAutoscroll() {
        this.autoscroll = !this.autoscroll;
        document.getElementById('autoscroll-checkbox').checked = this.autoscroll;
    }

    clearTerminal() {
        this.elements.terminalContent.replaceChildren();
        // Alan 8/23/26 - A manual clear should not stop a later snapshot restoring the tail.
        this._logTailsPopulated = false;
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
    // Alan 8/23/26 - A tab can be clicked repeatedly before the first response lands.
    // Without an in-flight marker each click started another full download.
    const inFlightLoads = new Set();

    async function loadLog(logName, elementId) {
        if (inFlightLoads.has(logName)) return;
        inFlightLoads.add(logName);
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
            // logName is one of three fixed identifiers, never user text.
            window.reportClientError?.(`job_status.load_log.${logName}`, err);
            content.textContent = 'Failed to load log.';
        } finally {
            // Cleared either way so a failed load stays retryable.
            inFlightLoads.delete(logName);
        }
    }

    // Function to load input sequences
    async function loadSequences() {
        if (inFlightLoads.has('sequences')) return;
        inFlightLoads.add('sequences');
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
            window.reportClientError?.('job_status.load_sequences', err);
            content.textContent = 'Failed to load sequences.';
        } finally {
            inFlightLoads.delete('sequences');
        }
    }

    // Function to load aligned sequences
    async function loadAligned() {
        if (inFlightLoads.has('aligned')) return;
        inFlightLoads.add('aligned');
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
            window.reportClientError?.('job_status.load_alignment', err);
            content.textContent = 'Failed to load aligned sequences.';
        } finally {
            inFlightLoads.delete('aligned');
        }
    }
});

// Alan 8/23/26 - Cleanup when the page is hidden. `pagehide` rather than
// `beforeunload`, which disqualifies the page from the back/forward cache in
// Firefox; the paired `pageshow` below reconnects a restored page so it cannot sit
// on stale status with a dead stream.
window.addEventListener('pagehide', () => {
    if (window.jobStatusClient) {
        window.jobStatusClient.disconnect();
    }
});

window.addEventListener('pageshow', (event) => {
    if (!event.persisted) return;
    const client = window.jobStatusClient;
    // A terminal job has nothing more to stream, and the snapshot it already
    // rendered is still correct.
    if (!client || client.eventSource) return;
    if (client.lastStatus === 'completed' || client.lastStatus === 'failed') return;
    client.connect();
});
