// App JS for Ada Task Engine Dashboard

let currentSessionId = null;
let currentModel = null;
let activeTasksMap = new Map(); // Keep track of seen tasks and their status

// DOM Elements
const chatMessages = document.getElementById('chat-messages');
const chatForm = document.getElementById('chat-form');
const promptInput = document.getElementById('prompt-input');
const sendBtn = document.getElementById('send-btn');
const connectionStatus = document.getElementById('connection-status');
const headerSessionId = document.getElementById('header-session-id');
const modelSelect = document.getElementById('model-select');
const sessionSelect = document.getElementById('session-select');
const workspacePath = document.getElementById('workspace-path');
const activeTasksCount = document.getElementById('active-tasks-count');
const activityFeed = document.getElementById('activity-feed');
const feedEmptyState = document.getElementById('feed-empty-state');
const skillsList = document.getElementById('skills-list');
const skillsEmptyState = document.getElementById('skills-empty-state');
const schedulesContainer = document.getElementById('schedules-container');
const scheduleForm = document.getElementById('schedule-form');
const settingsToggle = document.getElementById('settings-toggle');
const settingsCard = document.getElementById('settings-card');

// Toggle Settings Card visibility
settingsToggle.addEventListener('click', () => {
    settingsCard.style.display = settingsCard.style.display === 'none' ? 'block' : 'none';
});

// Auto-resize prompt textarea
promptInput.addEventListener('input', () => {
    promptInput.style.height = 'auto';
    promptInput.style.height = promptInput.scrollHeight + 'px';
});

// Prompt Ctrl+Enter or Command+Enter to send
promptInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        chatForm.requestSubmit();
    }
});

// Copy session ID to clipboard
headerSessionId.addEventListener('click', () => {
    if (currentSessionId) {
        navigator.clipboard.writeText(currentSessionId).then(() => {
            const originalText = headerSessionId.innerHTML;
            headerSessionId.innerHTML = '<i class="fa-solid fa-check"></i> Copied!';
            setTimeout(() => {
                headerSessionId.innerHTML = originalText;
            }, 1500);
        });
    }
});

// Switch active model
modelSelect.addEventListener('change', (e) => {
    currentModel = e.target.value;
});

// Switch session
sessionSelect.addEventListener('change', async (e) => {
    const sessionId = e.target.value;
    if (sessionId === "") {
        // Start new session
        currentSessionId = null;
        chatMessages.innerHTML = `
            <div class="message system-message">
                <div class="message-avatar">🌸</div>
                <div class="message-content">
                    <p>Hello! I am <strong>Ada</strong>, your autonomous developer assistant. Ask me to write, test, debug, or manage code in your workspace, or teach me new skills to automate your workflow. What are we working on today?</p>
                </div>
            </div>
        `;
        headerSessionId.querySelector('.id-val').textContent = 'New Session';
        await pollPlanAndTelemetry();
    } else {
        await resumeSession(sessionId);
        await pollPlanAndTelemetry();
    }
});

// Send Chat Message
chatForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const prompt = promptInput.value.trim();
    if (!prompt) return;

    // Reset input
    promptInput.value = '';
    promptInput.style.height = 'auto';

    // Append user message
    appendMessage('user', prompt);

    // Prepare assistant bubbles
    const thoughtBubble = appendThoughtBubble();
    const responseBubble = appendResponseBubble();

    try {
        setLoadingState(true);
        
        const response = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                prompt: prompt,
                session_id: currentSessionId,
                model: currentModel
            })
        });

        if (!response.ok) {
            throw new Error(`Chat API error: ${response.statusText}`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let isDone = false;

        let lastThoughtText = '';
        let lastResponseText = '';

        while (!isDone) {
            const { value, done } = await reader.read();
            isDone = done;
            if (value) {
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop(); // hold last chunk in buffer

                for (const line of lines) {
                    if (line.startsWith('data: ')) {
                        const rawData = line.slice(6).trim();
                        if (rawData === '[DONE]') {
                            isDone = true;
                            break;
                        }

                        try {
                            const data = JSON.parse(rawData);
                            if (data.type === 'session_id') {
                                if (currentSessionId !== data.content) {
                                    currentSessionId = data.content;
                                    headerSessionId.querySelector('.id-val').textContent = currentSessionId;
                                    updateSessionListSelection(currentSessionId);
                                }
                            } else if (data.type === 'thought') {
                                lastThoughtText += data.content;
                                updateThoughtBubble(thoughtBubble, lastThoughtText);
                            } else if (data.type === 'chunk') {
                                lastResponseText += data.content;
                                updateResponseBubble(responseBubble, lastResponseText);
                            }
                        } catch (err) {
                            console.error('Failed to parse SSE JSON:', err, rawData);
                        }
                    }
                }
            }
        }

        // Clean up empty thought or response bubble if none received
        if (!lastThoughtText) {
            thoughtBubble.remove();
        }
        if (!lastResponseText) {
            updateResponseBubble(responseBubble, '_No direct text response. Check logs or tool executions._');
        }

    } catch (error) {
        console.error('Failed to stream response:', error);
        appendMessage('system', `Error streaming agent response: ${error.message}`);
        if (thoughtBubble) thoughtBubble.remove();
        if (responseBubble) responseBubble.remove();
    } finally {
        setLoadingState(false);
        // Refresh tasks and status
        await pollTasks();
        await loadStatus();
        await pollPlanAndTelemetry();
    }
});

// Add Schedule
scheduleForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const name = document.getElementById('schedule-name').value.trim();
    const prompt = document.getElementById('schedule-prompt').value.trim();
    const cron = document.getElementById('schedule-cron').value.trim();

    try {
        const res = await fetch('/api/schedule', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, prompt, cron_expr: cron })
        });
        if (res.ok) {
            scheduleForm.reset();
            await loadSchedules();
        } else {
            const err = await res.json();
            alert(`Failed to add schedule: ${err.detail}`);
        }
    } catch (error) {
        console.error('Error adding schedule:', error);
    }
});

// Functions
function appendMessage(role, content) {
    const msgDiv = document.createElement('div');
    msgDiv.className = `message ${role}-message`;
    
    const avatar = document.createElement('div');
    avatar.className = 'message-avatar';
    avatar.textContent = role === 'user' ? '👤' : (role === 'system' ? '⚙️' : '🌸');
    
    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';
    contentDiv.innerHTML = formatMarkdown(content);
    
    msgDiv.appendChild(avatar);
    msgDiv.appendChild(contentDiv);
    chatMessages.appendChild(msgDiv);
    scrollChatToBottom();
    return msgDiv;
}

function appendThoughtBubble() {
    const msgDiv = document.createElement('div');
    msgDiv.className = 'message thought-message';
    
    const avatar = document.createElement('div');
    avatar.className = 'message-avatar';
    avatar.textContent = '🧠';
    
    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';
    contentDiv.textContent = 'Thinking...';
    
    msgDiv.appendChild(avatar);
    msgDiv.appendChild(contentDiv);
    chatMessages.appendChild(msgDiv);
    scrollChatToBottom();
    return msgDiv;
}

function updateThoughtBubble(bubbleDiv, content) {
    const contentDiv = bubbleDiv.querySelector('.message-content');
    contentDiv.textContent = content;
    scrollChatToBottom();
}

function appendResponseBubble() {
    const msgDiv = document.createElement('div');
    msgDiv.className = 'message assistant-message';
    
    const avatar = document.createElement('div');
    avatar.className = 'message-avatar';
    avatar.textContent = '🌸';
    
    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';
    contentDiv.innerHTML = '<span class="card-loader"></span>';
    
    msgDiv.appendChild(avatar);
    msgDiv.appendChild(contentDiv);
    chatMessages.appendChild(msgDiv);
    scrollChatToBottom();
    return msgDiv;
}

function updateResponseBubble(bubbleDiv, content) {
    const contentDiv = bubbleDiv.querySelector('.message-content');
    contentDiv.innerHTML = formatMarkdown(content);
    scrollChatToBottom();
}

function setLoadingState(loading) {
    sendBtn.disabled = loading;
    promptInput.disabled = loading;
    if (loading) {
        sendBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
    } else {
        sendBtn.innerHTML = '<i class="fa-solid fa-paper-plane"></i>';
    }
}

function scrollChatToBottom() {
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

function formatMarkdown(text) {
    if (!text) return '';
    // Escape HTML tags to prevent XSS
    let escaped = text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');

    // Handle code blocks
    escaped = escaped.replace(/```([\s\S]*?)```/g, (match, p1) => {
        return `<pre><code>${p1.trim()}</code></pre>`;
    });

    // Handle inline code
    escaped = escaped.replace(/`([^`]+)`/g, '<code>$1</code>');

    // Handle bold
    escaped = escaped.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

    // Handle paragraphs
    const paragraphs = escaped.split('\n\n');
    return paragraphs.map(p => {
        if (p.startsWith('<pre>') || p.startsWith('<ul>') || p.startsWith('<ol>')) {
            return p;
        }
        return `<p>${p.replace(/\n/g, '<br>')}</p>`;
    }).join('');
}

// Resume past session
async function resumeSession(sessionId) {
    try {
        const res = await fetch('/api/sessions/resume', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: sessionId })
        });
        if (res.ok) {
            currentSessionId = sessionId;
            headerSessionId.querySelector('.id-val').textContent = sessionId;
            await loadHistory();
        }
    } catch (error) {
        console.error('Error resuming session:', error);
    }
}

// Load session history
async function loadHistory() {
    try {
        const url = currentSessionId ? `/api/history?session_id=${encodeURIComponent(currentSessionId)}` : '/api/history';
        const res = await fetch(url);
        if (res.ok) {
            const data = await res.json();
            chatMessages.replaceChildren();
            
            if (data.history.length === 0) {
                const doc = new DOMParser().parseFromString(`
                    <div class="message system-message">
                        <div class="message-avatar">🌸</div>
                        <div class="message-content">
                            <p>This session has no recorded history yet. How can I help you?</p>
                        </div>
                    </div>
                `, 'text/html');
                chatMessages.appendChild(doc.body.firstElementChild);
                return;
            }

            data.history.forEach(step => {
                if (step.role === 'user') {
                    appendMessage('user', step.content);
                } else if (step.role === 'thought') {
                    const bubble = appendThoughtBubble();
                    updateThoughtBubble(bubble, step.content);
                } else if (step.role === 'assistant') {
                    appendMessage('assistant', step.content);
                } else if (step.role === 'tool_call') {
                    appendMessage('system', `<strong>Executed tool:</strong> <code>${step.tool_name}</code><br>Arguments: <code>${step.content}</code>`);
                }
            });
            scrollChatToBottom();
        }
    } catch (error) {
        console.error('Error loading session history:', error);
    }
}

// Load global configuration and status
async function loadStatus() {
    try {
        const res = await fetch('/api/status');
        if (res.ok) {
            const data = await res.json();
            connectionStatus.textContent = 'Online';
            workspacePath.textContent = data.workspace;
            workspacePath.title = data.workspace;
            
            // Set current session and update title
            if (!currentSessionId && data.session_id) {
                currentSessionId = data.session_id;
                headerSessionId.querySelector('.id-val').textContent = currentSessionId;
            }

            // Set current model dropdown
            if (!currentModel && data.model) {
                currentModel = data.model;
                modelSelect.value = currentModel;
            }

            // Load Custom Skills
            skillsList.innerHTML = '';
            if (data.skills && data.skills.length > 0) {
                skillsEmptyState.style.display = 'none';
                data.skills.forEach(skill => {
                    const item = document.createElement('div');
                    item.className = 'skill-item';
                    item.innerHTML = `
                        <div class="skill-name">${skill.name}</div>
                        <div class="skill-desc">${skill.description}</div>
                    `;
                    skillsList.appendChild(item);
                });
            } else {
                skillsEmptyState.style.display = 'flex';
            }
        }
    } catch (error) {
        console.error('Error fetching API status:', error);
        connectionStatus.textContent = 'Disconnected';
    }
}

// Load session options
async function loadSessions() {
    try {
        const res = await fetch('/api/sessions');
        if (res.ok) {
            const data = await res.json();
            
            // Keep "Start New Session" option
            sessionSelect.innerHTML = '<option value="">Start New Session</option>';
            
            data.sessions.forEach(sess => {
                const opt = document.createElement('option');
                opt.value = sess.session_id;
                const formattedTime = new Date(sess.last_active).toLocaleString();
                opt.textContent = `${sess.session_id.substring(0, 8)}... (${formattedTime})`;
                sessionSelect.appendChild(opt);
            });

            if (currentSessionId) {
                updateSessionListSelection(currentSessionId);
            }
        }
    } catch (error) {
        console.error('Error loading sessions:', error);
    }
}

function updateSessionListSelection(sessionId) {
    for (let opt of sessionSelect.options) {
        if (opt.value === sessionId) {
            sessionSelect.value = sessionId;
            break;
        }
    }
}

// Load Automation Schedules
async function loadSchedules() {
    try {
        const res = await fetch('/api/schedule');
        if (res.ok) {
            const data = await res.json();
            schedulesContainer.innerHTML = '';
            
            if (data.schedules.length === 0) {
                schedulesContainer.innerHTML = '<div class="tip-text" style="text-align:center; padding:10px;">No scheduled tasks.</div>';
                return;
            }

            data.schedules.forEach(sched => {
                const item = document.createElement('div');
                item.className = 'schedule-item';
                
                const details = document.createElement('div');
                details.className = 'schedule-details';
                
                const title = document.createElement('div');
                title.className = 'schedule-title';
                title.textContent = sched.name;
                title.title = sched.name;
                
                const prompt = document.createElement('div');
                prompt.className = 'schedule-prompt';
                prompt.textContent = sched.prompt;
                prompt.title = sched.prompt;

                const info = document.createElement('div');
                info.className = 'schedule-info';
                
                const nextStr = sched.next_run ? new Date(sched.next_run).toLocaleTimeString() : 'N/A';
                const lastStr = sched.last_run ? new Date(sched.last_run).toLocaleTimeString() : 'never';
                info.textContent = `Interval: ${sched.cron_expr} | Last: ${lastStr} | Next: ${nextStr}`;
                
                details.appendChild(title);
                details.appendChild(prompt);
                details.appendChild(info);
                
                const delBtn = document.createElement('button');
                delBtn.className = 'schedule-delete-btn';
                delBtn.innerHTML = '<i class="fa-solid fa-trash-can"></i>';
                delBtn.title = 'Delete Schedule';
                delBtn.addEventListener('click', async () => {
                    if (confirm(`Delete schedule "${sched.name}"?`)) {
                        await deleteSchedule(sched.id);
                    }
                });
                
                item.appendChild(details);
                item.appendChild(delBtn);
                schedulesContainer.appendChild(item);
            });
        }
    } catch (error) {
        console.error('Error fetching schedules:', error);
    }
}

async function deleteSchedule(id) {
    try {
        const res = await fetch(`/api/schedule/${id}`, { method: 'DELETE' });
        if (res.ok) {
            await loadSchedules();
        }
    } catch (error) {
        console.error('Error deleting schedule:', error);
    }
}

// Poll Active Tasks and Subagents
async function pollTasks() {
    try {
        const res = await fetch('/api/tasks');
        if (res.ok) {
            const data = await res.json();
            const tasks = data.tasks || [];
            
            // Check if Grace is active
            const isGraceActive = tasks.some(t => 
                t.status === 'running' && 
                (
                    t.name.toLowerCase().includes('grace') || 
                    t.details.toLowerCase().includes('grace')
                )
            );
            
            const graceStatusEl = document.getElementById('grace-status');
            const graceStateText = document.getElementById('grace-state-text');
            if (graceStatusEl && graceStateText) {
                if (isGraceActive) {
                    graceStatusEl.classList.add('active');
                    graceStateText.textContent = 'Active';
                } else {
                    graceStatusEl.classList.remove('active');
                    graceStateText.textContent = 'Idle';
                }
            }
            
            const runningCount = tasks.filter(t => t.status === 'running').length;
            activeTasksCount.textContent = `${runningCount} active`;

            const currentTaskIds = new Set(tasks.map(t => t.id));
            
            // Transition cards in activeTasksMap that are no longer returned in the tasks list
            for (let [taskId, cardEl] of activeTasksMap.entries()) {
                if (!currentTaskIds.has(taskId)) {
                    if (!cardEl.classList.contains('completed') && !cardEl.classList.contains('failed')) {
                        cardEl.className = 'activity-card completed';
                        const dot = cardEl.querySelector('.card-status-dot');
                        if (dot) {
                            dot.replaceChildren();
                            const ind = document.createElement('span');
                            ind.className = 'status-indicator-mini';
                            dot.appendChild(ind);
                            dot.appendChild(document.createTextNode(' Completed'));
                        }
                        cardEl.querySelector('.card-loader')?.remove();
                    }
                    
                    // Remove from DOM after 5 seconds to keep dashboard clean
                    if (!cardEl.dataset.timeoutSet) {
                        cardEl.dataset.timeoutSet = "true";
                        setTimeout(() => {
                            cardEl.remove();
                            activeTasksMap.delete(taskId);
                            if (activeTasksMap.size === 0 && tasks.length === 0) {
                                if (!document.getElementById('feed-empty-state')) {
                                    activityFeed.appendChild(feedEmptyState);
                                }
                            }
                        }, 5000);
                    }
                }
            }

            if (tasks.length === 0 && activeTasksMap.size === 0) {
                activityFeed.replaceChildren();
                activityFeed.appendChild(feedEmptyState);
                return;
            }

            if (tasks.length > 0) {
                feedEmptyState.remove();
            }

            // Create or update tasks
            for (let task of tasks) {
                if (activeTasksMap.has(task.id)) {
                    const card = activeTasksMap.get(task.id);
                    // Update classes and status based on task status
                    if (task.status === 'completed' || task.status === 'failed' || task.status === 'denied') {
                        const statusClass = task.status === 'denied' ? 'failed' : task.status;
                        if (!card.classList.contains(statusClass)) {
                            card.className = `activity-card ${statusClass}`;
                            const dot = card.querySelector('.card-status-dot');
                            if (dot) {
                                dot.replaceChildren();
                                const ind = document.createElement('span');
                                ind.className = 'status-indicator-mini';
                                dot.appendChild(ind);
                                const statusLabel = task.status.charAt(0).toUpperCase() + task.status.slice(1);
                                dot.appendChild(document.createTextNode(` ${statusLabel}`));
                            }
                            card.querySelector('.card-loader')?.remove();
                        }
                    } else {
                        // running
                        card.className = 'activity-card running';
                        const dot = card.querySelector('.card-status-dot');
                        if (dot) {
                            dot.replaceChildren();
                            const ind = document.createElement('span');
                            ind.className = 'status-indicator-mini';
                            dot.appendChild(ind);
                            dot.appendChild(document.createTextNode(' Active'));
                        }
                    }
                    // Update logs for this card
                    await updateCardLogs(task.id, card);
                    continue;
                }

                // New card
                const card = document.createElement('div');
                const initialStatusClass = task.status === 'denied' ? 'failed' : task.status;
                card.className = `activity-card ${initialStatusClass}`;
                card.id = `task-card-${task.id}`;
                
                const top = document.createElement('div');
                top.className = 'card-top';
                
                const title = document.createElement('div');
                title.className = 'card-title';
                
                const icon = document.createElement('i');
                icon.className = 'fa-solid fa-code-fork';
                title.appendChild(icon);
                title.appendChild(document.createTextNode(` ${task.name}`));
                
                const status = document.createElement('div');
                status.className = 'card-status-dot';
                
                const indicator = document.createElement('span');
                indicator.className = 'status-indicator-mini';
                status.appendChild(indicator);
                
                const capitalizedStatus = task.status === 'running' ? 'Active' : (task.status.charAt(0).toUpperCase() + task.status.slice(1));
                status.appendChild(document.createTextNode(` ${capitalizedStatus}`));
                
                top.appendChild(title);
                top.appendChild(status);
                
                const details = document.createElement('div');
                details.className = 'card-details';
                details.textContent = task.details;
                
                const logsDiv = document.createElement('div');
                logsDiv.className = 'card-logs';
                logsDiv.id = `task-logs-${task.id}`;
                
                const bottom = document.createElement('div');
                bottom.className = 'card-bottom';
                
                const timeSpan = document.createElement('span');
                const startTime = new Date(task.started_at);
                timeSpan.textContent = `Started: ${startTime.toLocaleTimeString()}`;
                
                bottom.appendChild(timeSpan);
                if (task.status === 'running') {
                    const loader = document.createElement('div');
                    loader.className = 'card-loader';
                    bottom.appendChild(loader);
                }
                
                card.appendChild(top);
                card.appendChild(details);
                card.appendChild(logsDiv);
                card.appendChild(bottom);
                
                // Add to top of feed
                activityFeed.insertBefore(card, activityFeed.firstChild);
                activeTasksMap.set(task.id, card);

                // Fetch initial logs
                await updateCardLogs(task.id, card);
            }
        }
    } catch (error) {
        console.error('Error polling tasks:', error);
    }
}

async function updateCardLogs(taskId, cardEl) {
    try {
        const res = await fetch(`/api/tasks/${taskId}/logs`);
        if (res.ok) {
            const data = await res.json();
            const logsDiv = cardEl.querySelector('.card-logs');
            if (logsDiv && data.logs && data.logs.length > 0) {
                logsDiv.replaceChildren();
                data.logs.forEach(log => {
                    const logEl = document.createElement('div');
                    logEl.className = 'log-item';
                    logEl.textContent = `> ${log.message}`;
                    logsDiv.appendChild(logEl);
                });
            }
        }
    } catch (error) {
        console.error(`Failed to update card logs for task ${taskId}:`, error);
    }
}

async function pollPlanAndTelemetry() {
    if (!currentSessionId) {
        const container = document.getElementById('plan-steps-container');
        if (container) {
            container.replaceChildren();
            const empty = document.createElement('div');
            empty.className = 'plan-empty-state';
            empty.id = 'plan-empty-state';
            const p = document.createElement('p');
            p.textContent = 'No execution plan generated for this session yet.';
            empty.appendChild(p);
            container.appendChild(empty);
        }
        const inTok = document.getElementById('telemetry-input-tokens');
        if (inTok) inTok.textContent = '0';
        const outTok = document.getElementById('telemetry-output-tokens');
        if (outTok) outTok.textContent = '0';
        const costVal = document.getElementById('telemetry-cost');
        if (costVal) costVal.textContent = '$0.000000';
        return;
    }
    
    // 1. Fetch Plan
    try {
        const res = await fetch(`/api/sessions/${currentSessionId}/plan`);
        if (res.ok) {
            const data = await res.json();
            const container = document.getElementById('plan-steps-container');
            if (container) {
                if (data.plan && data.plan.steps && data.plan.steps.length > 0) {
                    container.replaceChildren();
                    data.plan.steps.forEach(step => {
                        const stepItem = document.createElement('div');
                        stepItem.className = 'plan-step-item';
                        
                        const dot = document.createElement('span');
                        dot.className = `plan-step-status-dot ${step.status}`;
                        stepItem.appendChild(dot);
                        
                        const details = document.createElement('div');
                        details.className = 'plan-step-details';
                        
                        const desc = document.createElement('span');
                        desc.className = 'plan-step-desc';
                        desc.textContent = step.description;
                        details.appendChild(desc);
                        
                        const meta = document.createElement('div');
                        meta.className = 'plan-step-meta';
                        
                        const statusText = document.createElement('span');
                        statusText.textContent = step.status.charAt(0).toUpperCase() + step.status.slice(1);
                        meta.appendChild(statusText);
                        
                        if (step.assigned_tool) {
                            const badge = document.createElement('span');
                            badge.className = 'tool-badge';
                            badge.textContent = step.assigned_tool;
                            meta.appendChild(badge);
                        }
                        details.appendChild(meta);
                        
                        if (step.error_message) {
                            const err = document.createElement('div');
                            err.className = 'plan-step-error';
                            err.textContent = step.error_message;
                            details.appendChild(err);
                        }
                        
                        stepItem.appendChild(details);
                        container.appendChild(stepItem);
                    });
                } else {
                    if (!document.getElementById('plan-empty-state')) {
                        container.replaceChildren();
                        const empty = document.createElement('div');
                        empty.className = 'plan-empty-state';
                        empty.id = 'plan-empty-state';
                        const p = document.createElement('p');
                        p.textContent = 'No execution plan generated for this session yet.';
                        empty.appendChild(p);
                        container.appendChild(empty);
                    }
                }
            }
        }
    } catch (err) {
        console.error('Error polling plan:', err);
    }
    
    // 2. Fetch Telemetry
    try {
        const res = await fetch(`/api/sessions/${currentSessionId}/telemetry`);
        if (res.ok) {
            const data = await res.json();
            let totalInput = 0;
            let totalOutput = 0;
            let totalCost = 0.0;
            if (data.telemetry && data.telemetry.length > 0) {
                data.telemetry.forEach(record => {
                    totalInput += record.input_tokens || 0;
                    totalOutput += record.output_tokens || 0;
                    totalCost += record.cost || 0.0;
                });
            }
            const inTok = document.getElementById('telemetry-input-tokens');
            if (inTok) inTok.textContent = totalInput.toLocaleString();
            const outTok = document.getElementById('telemetry-output-tokens');
            if (outTok) outTok.textContent = totalOutput.toLocaleString();
            const costVal = document.getElementById('telemetry-cost');
            if (costVal) costVal.textContent = `$${totalCost.toFixed(6)}`;
        }
    } catch (err) {
        console.error('Error polling telemetry:', err);
    }
}

// Poll Model Quotas
async function pollQuotas() {
    try {
        const res = await fetch('/api/quotas');
        if (!res.ok) throw new Error(`HTTP error ${res.status}`);
        const data = await res.json();
        if (Array.isArray(data)) {
            data.forEach(q => {
                const family = q.model_family;
                const pct_5h = q.pct_5h;
                const pct_weekly = q.pct_weekly;
                
                const prefix = (family === 'gemini') ? 'gemini' : 'claude';
                
                const val5h = document.getElementById(`${prefix}-5h-val`);
                const valWeekly = document.getElementById(`${prefix}-weekly-val`);
                const bar5h = document.getElementById(`${prefix}-5h-bar`);
                const barWeekly = document.getElementById(`${prefix}-weekly-bar`);
                
                if (val5h) val5h.textContent = `${pct_5h.toFixed(1)}%`;
                if (valWeekly) valWeekly.textContent = `${pct_weekly.toFixed(1)}%`;
                
                if (bar5h) {
                    bar5h.style.width = `${pct_5h}%`;
                    bar5h.className = 'quota-progress-bar ' + (pct_5h >= 50.0 ? 'high' : (pct_5h >= 20.0 ? 'medium' : 'low'));
                }
                if (barWeekly) {
                    barWeekly.style.width = `${pct_weekly}%`;
                    barWeekly.className = 'quota-progress-bar ' + (pct_weekly >= 50.0 ? 'high' : (pct_weekly >= 20.0 ? 'medium' : 'low'));
                }
            });
        }
    } catch (err) {
        console.error('Error polling quotas:', err);
    }
}

// Init Setup
async function init() {
    await loadStatus();
    await loadSessions();
    await loadHistory();
    await loadSchedules();
    await pollTasks();
    await pollPlanAndTelemetry();
    await pollQuotas();
    
    // Polling schedules and active tasks
    setInterval(pollTasks, 2000);
    setInterval(loadSchedules, 5000);
    setInterval(loadSessions, 10000);
    setInterval(pollPlanAndTelemetry, 3000);
    setInterval(pollQuotas, 30000);
}

document.addEventListener('DOMContentLoaded', init);
