// App JS for Ada Task Engine Dashboard

function generateUUID() {
    if (typeof self !== 'undefined' && self.crypto) {
        if (typeof self.crypto.randomUUID === 'function') {
            return self.crypto.randomUUID();
        }
        if (typeof self.crypto.getRandomValues === 'function') {
            return ([1e7]+-1e3+-4e3+-8e3+-1e11).replace(/[018]/g, c =>
                (c ^ self.crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16)
            );
        }
    }
    // Fallback UUID v4 generator (works without HTTPS secure context)
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
        const r = Math.random() * 16 | 0;
        return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
    });
}
let currentSessionId = generateUUID();
let currentModel = null;
let currentChatMode = 'build';
let activeTasksMap = new Map(); // Keep track of seen tasks and their status
let delegationPending = false; // Set when a delegation response is received
let delegationHistoryTimer = null; // Timer to refresh chat after delegation completes

/**
 * Maps the Intent dropdown value to the correct API request payload fields.
 * - build:    Mode 1 (Work) — full coordinator with planning/delegation
 * - ada:      Mode 2 (Plain Chat) — casual Ada, tools if asked
 * - lacie/val/kira: Mode 2 + specialist profile — casual persona with tools
 * - roleplay: Mode 3 — pure entertainment LLM, ZERO tools
 */
function buildIntentPayload(mode) {
    switch (mode) {
        case 'ada':
            return { general_chat: true };
        case 'lacie':
            return { general_chat: true, agent_profile: 'lacie' };
        case 'val':
            return { general_chat: true, agent_profile: 'qa_specialist' };
        case 'kira':
            return { general_chat: true, agent_profile: 'ops_runner' };
        case 'roleplay':
            return { roleplay: true };
        case 'build':
        default:
            return {};
    }
}

// DOM Elements
const chatMessages = document.getElementById('chat-messages');
const chatForm = document.getElementById('chat-form');
const promptInput = document.getElementById('prompt-input');
const sendBtn = document.getElementById('send-btn');
const interruptBtn = document.getElementById('interrupt-btn');
const statusBanner = document.getElementById('status-banner');
const connectionStatus = document.getElementById('connection-status');
const headerSessionId = document.getElementById('header-session-id');
const modelSelect = document.getElementById('model-select');
const chatModeSelect = document.getElementById('chat-mode-select');
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

// Switch chat mode
chatModeSelect.addEventListener('change', (e) => {
    currentChatMode = e.target.value;
});

// Switch session
sessionSelect.addEventListener('change', async (e) => {
    const sessionId = e.target.value;
    if (sessionId === "") {
        // Start new session — generate a unique ID immediately
        currentSessionId = generateUUID();
        chatMessages.innerHTML = `
            <div class="message system-message">
                <div class="message-avatar">🌸</div>
                <div class="message-content">
                    <p>Hello! I am <strong>Ada</strong>, your autonomous developer assistant. Ask me to write, test, debug, or manage code in your workspace, or teach me new skills to automate your workflow. What are we working on today?</p>
                </div>
            </div>
        `;
        headerSessionId.querySelector('.id-val').textContent = currentSessionId;
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
                model: currentModel,
                ...buildIntentPayload(currentChatMode)
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
                            if (data.type === 'ping') {
                                // Ignore ping messages, they just keep the connection alive
                            } else if (data.type === 'session_id') {
                                if (currentSessionId !== data.content) {
                                    currentSessionId = data.content;
                                    headerSessionId.querySelector('.id-val').textContent = currentSessionId;
                                    updateSessionListSelection(currentSessionId);
                                }
                            } else if (data.type === 'thought') {
                                appendThoughtItem(thoughtBubble, data.content);
                            } else if (data.type === 'chunk') {
                                lastResponseText += data.content;
                                updateResponseBubble(responseBubble, lastResponseText);
                                // Detect delegation response — start polling for results
                                if (data.content && data.content.includes('🚀')) {
                                    delegationPending = true;
                                    console.log('[UI] Delegation detected, starting history poll interval');
                                    // Start dedicated polling interval to pick up background results
                                    const delegationStartMsgCount = chatMessages.querySelectorAll('.message.assistant-message').length;
                                    if (delegationHistoryTimer) clearInterval(delegationHistoryTimer);
                                    let pollCount = 0;
                                    delegationHistoryTimer = setInterval(async () => {
                                        pollCount++;
                                        console.log(`[UI] Delegation poll #${pollCount}`);
                                        await loadHistory();
                                        const newMsgCount = chatMessages.querySelectorAll('.message.assistant-message').length;
                                        if (newMsgCount > delegationStartMsgCount || pollCount >= 24) {
                                            // Found new messages or timed out (2 min)
                                            clearInterval(delegationHistoryTimer);
                                            delegationHistoryTimer = null;
                                            delegationPending = false;
                                            console.log(`[UI] Delegation poll complete (${newMsgCount > delegationStartMsgCount ? 'new messages' : 'timeout'})`);
                                        }
                                    }, 5000);
                                }
                            }
                        } catch (err) {
                            console.error('Failed to parse SSE JSON:', err, rawData);
                        }
                    }
                }
            }
        }

        // Clean up empty thought or response bubble if none received
        if (!thoughtBubble.querySelector('.thought-item')) {
            thoughtBubble.remove();
        }
        if (!lastResponseText) {
            updateResponseBubble(responseBubble, '_No direct text response. Check logs or tool executions._');
        }

    } catch (error) {
        console.error('Failed to stream response:', error);
        appendMessage('system', `Error streaming agent response: ${error.message}`);
        if (thoughtBubble && !thoughtBubble.querySelector('.thought-item')) thoughtBubble.remove();
        if (responseBubble && !lastResponseText) responseBubble.remove();
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
    contentDiv.className = 'message-content thought-list';
    // Start with a "Thinking..." placeholder that gets removed on first real item
    const placeholder = document.createElement('div');
    placeholder.className = 'thought-placeholder';
    placeholder.textContent = 'Thinking...';
    contentDiv.appendChild(placeholder);
    
    msgDiv.appendChild(avatar);
    msgDiv.appendChild(contentDiv);
    chatMessages.appendChild(msgDiv);
    scrollChatToBottom();
    return msgDiv;
}

function appendThoughtItem(bubbleDiv, content, timestampStr = null) {
    if (!content || !content.trim()) return;
    const contentDiv = bubbleDiv.querySelector('.message-content');
    
    // Remove the "Thinking..." placeholder on first real item
    const placeholder = contentDiv.querySelector('.thought-placeholder');
    if (placeholder) placeholder.remove();
    
    const item = document.createElement('div');
    item.className = 'thought-item';
    
    let displayTime = '';
    if (timestampStr) {
        try {
            const d = new Date(timestampStr);
            displayTime = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        } catch (e) {
            displayTime = timestampStr;
        }
    } else {
        displayTime = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    }
    
    const timeSpan = document.createElement('span');
    timeSpan.className = 'thought-time';
    timeSpan.textContent = displayTime;
    
    const textSpan = document.createElement('span');
    textSpan.className = 'thought-text';
    textSpan.textContent = content.trim();
    
    item.appendChild(timeSpan);
    item.appendChild(textSpan);
    contentDiv.appendChild(item);
    
    // Auto collapse if needed
    updateThoughtCollapsibility(bubbleDiv);
    
    scrollChatToBottom();
}

function updateThoughtBubble(bubbleDiv, content, timestampStr = null) {
    if (!content) return;
    const lines = content.split('\n');
    lines.forEach(line => {
        if (line.trim()) {
            appendThoughtItem(bubbleDiv, line.trim(), timestampStr);
        }
    });
}

function updateThoughtCollapsibility(bubbleDiv) {
    const contentDiv = bubbleDiv.querySelector('.message-content.thought-list');
    if (!contentDiv) return;
    
    const items = contentDiv.querySelectorAll('.thought-item');
    if (items.length <= 3) {
        const toggle = bubbleDiv.querySelector('.thought-toggle');
        if (toggle) toggle.remove();
        contentDiv.classList.remove('collapsed');
        return;
    }
    
    let toggle = bubbleDiv.querySelector('.thought-toggle');
    if (!toggle) {
        toggle = document.createElement('div');
        toggle.className = 'thought-toggle';
        contentDiv.insertBefore(toggle, contentDiv.firstChild);
        contentDiv.classList.add('collapsed');
        
        toggle.addEventListener('click', () => {
            const isCollapsed = contentDiv.classList.toggle('collapsed');
            updateToggleText(toggle, contentDiv.querySelectorAll('.thought-item').length, isCollapsed);
        });
    }
    
    const isCollapsed = contentDiv.classList.contains('collapsed');
    updateToggleText(toggle, items.length, isCollapsed);
}

function updateToggleText(toggleEl, totalCount, isCollapsed) {
    if (isCollapsed) {
        toggleEl.innerHTML = `<i class="fa-solid fa-chevron-down"></i> Show all thoughts (${totalCount})`;
    } else {
        toggleEl.innerHTML = `<i class="fa-solid fa-chevron-up"></i> Collapse thoughts`;
    }
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
    // Keep input area active: promptInput is never disabled
    promptInput.disabled = false;
    
    if (loading) {
        if (interruptBtn) interruptBtn.style.display = 'inline-flex';
        if (statusBanner) statusBanner.style.display = 'flex';
        sendBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
    } else {
        if (interruptBtn) interruptBtn.style.display = 'none';
        if (statusBanner) statusBanner.style.display = 'none';
        sendBtn.innerHTML = '<i class="fa-solid fa-paper-plane"></i>';
    }
}

// Event listener for Interrupt button
if (interruptBtn) {
    interruptBtn.addEventListener('click', async () => {
        try {
            // Append a local system message indicating interruption
            appendMessage('system', 'Sending stop signal to terminate execution...');
            
            const response = await fetch(`/api/sessions/${currentSessionId}/cancel`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            });
            if (response.ok) {
                const data = await response.json();
                let msg = `🛑 **Execution Stopped**: ${data.message || 'All active subagents and background plan tasks for this session have been terminated.'}`;
                if (data.cancelled_subagents && data.cancelled_subagents.length > 0) {
                    msg += `\n- Terminated subagents: ${data.cancelled_subagents.map(s => `\`${s}\``).join(', ')}`;
                }
                appendMessage('system', msg);
            } else {
                appendMessage('system', 'Failed to send stop signal to backend.');
            }
        } catch (error) {
            console.error('Error sending interrupt:', error);
            appendMessage('system', `Error sending interrupt: ${error.message}`);
        } finally {
            setLoadingState(false);
            await pollTasks();
            await loadStatus();
            await pollPlanAndTelemetry();
        }
    });
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

    // Handle images: ![Alt](url)
    escaped = escaped.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (match, alt, url) => {
        let cleanUrl = url.replace(/&amp;/g, '&');
        if (cleanUrl.startsWith('file:///')) {
            cleanUrl = '/api/gemini/file?path=' + encodeURIComponent(cleanUrl.substring(7));
        } else if (cleanUrl.startsWith('/app/') || cleanUrl.startsWith('/data/') || cleanUrl.startsWith('/tmp/')) {
            cleanUrl = '/api/gemini/file?path=' + encodeURIComponent(cleanUrl);
        }
        return `<img src="${cleanUrl}" alt="${alt}" style="max-width: 100%; border-radius: 8px; margin-top: 0.5rem; border: 1px solid var(--card-border); display: block;">`;
    });

    // Handle links: [Text](url)
    escaped = escaped.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (match, label, url) => {
        let cleanUrl = url.replace(/&amp;/g, '&');
        if (cleanUrl.startsWith('file:///')) {
            cleanUrl = '/api/gemini/file?path=' + encodeURIComponent(cleanUrl.substring(7));
        } else if (cleanUrl.startsWith('/app/') || cleanUrl.startsWith('/data/') || cleanUrl.startsWith('/tmp/')) {
            cleanUrl = '/api/gemini/file?path=' + encodeURIComponent(cleanUrl);
        }
        return `<a href="${cleanUrl}" target="_blank" style="color: var(--accent-cyan); text-decoration: underline;">${label}</a>`;
    });

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
                
                card.style.cursor = 'pointer';
                card.addEventListener('click', () => {
                    showTaskDetails(task);
                });
                
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
            
            const progressContainer = document.getElementById('plan-progress-container');
            if (progressContainer) progressContainer.style.display = 'none';
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
                    
                    // Update progress bar
                    const totalSteps = data.plan.steps.length;
                    const completedSteps = data.plan.steps.filter(s => s.status === 'completed').length;
                    const percentage = totalSteps > 0 ? Math.round((completedSteps / totalSteps) * 100) : 0;
                    
                    const progressContainer = document.getElementById('plan-progress-container');
                    const progressBar = document.getElementById('plan-progress-bar');
                    const progressVal = document.getElementById('plan-progress-val');
                    
                    if (progressContainer && progressBar && progressVal) {
                        progressContainer.style.display = 'block';
                        progressBar.style.width = `${percentage}%`;
                        progressVal.textContent = `${percentage}%`;
                    }
                    
                    // Render Goal if present
                    if (data.plan.goal) {
                        const goalSec = document.createElement('div');
                        goalSec.className = 'plan-goal-section';
                        goalSec.innerHTML = `
                            <h3 class="plan-section-title">Goal</h3>
                            <p class="plan-goal-desc">${data.plan.goal}</p>
                        `;
                        container.appendChild(goalSec);
                    }
                    
                    // Render Tasks header & steps list
                    const tasksSec = document.createElement('div');
                    tasksSec.className = 'plan-tasks-section';
                    const tasksTitle = document.createElement('h3');
                    tasksTitle.className = 'plan-section-title';
                    tasksTitle.textContent = 'Tasks';
                    tasksSec.appendChild(tasksTitle);
                    
                    const stepsList = document.createElement('div');
                    stepsList.className = 'plan-steps-list';
                    
                    data.plan.steps.forEach(step => {
                        const stepItem = document.createElement('div');
                        stepItem.className = 'plan-step-item';
                        
                        let iconHtml = '';
                        if (step.status === 'completed') {
                            iconHtml = '<i class="fa-solid fa-circle-check" style="color: var(--accent-mint);"></i>';
                        } else if (step.status === 'running' || step.status === 'delegated') {
                            iconHtml = '<i class="fa-solid fa-circle-notch fa-spin" style="color: var(--accent-orchid);"></i>';
                        } else if (step.status === 'failed') {
                            iconHtml = '<i class="fa-solid fa-circle-xmark" style="color: #ef4444;"></i>';
                        } else {
                            iconHtml = '<i class="fa-regular fa-circle" style="color: var(--text-muted);"></i>';
                        }
                        
                        const left = document.createElement('div');
                        left.className = 'step-left';
                        left.innerHTML = iconHtml;
                        stepItem.appendChild(left);
                        
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
                        stepsList.appendChild(stepItem);
                    });
                    
                    tasksSec.appendChild(stepsList);
                    container.appendChild(tasksSec);
                    
                    // Render Acceptance Criteria if present
                    if (data.plan.acceptance_criteria) {
                        try {
                            const criteria = JSON.parse(data.plan.acceptance_criteria);
                            if (criteria && criteria.length > 0) {
                                const acSec = document.createElement('div');
                                acSec.className = 'plan-ac-section';
                                acSec.style.marginTop = '1rem';
                                
                                const acTitle = document.createElement('h3');
                                acTitle.className = 'plan-section-title';
                                acTitle.textContent = 'Acceptance Criteria';
                                acSec.appendChild(acTitle);
                                
                                const acList = document.createElement('ul');
                                acList.className = 'plan-ac-list';
                                acList.style.listStyle = 'none';
                                acList.style.padding = '0';
                                acList.style.margin = '0.5rem 0 0 0';
                                
                                criteria.forEach(item => {
                                    const li = document.createElement('li');
                                    li.style.display = 'flex';
                                    li.style.alignItems = 'center';
                                    li.style.gap = '0.5rem';
                                    li.style.fontSize = '0.85rem';
                                    li.style.marginBottom = '0.4rem';
                                    
                                    const allDone = data.plan.steps.every(s => s.status === 'completed');
                                    const boxIcon = allDone ? '<i class="fa-solid fa-square-check" style="color: var(--accent-mint);"></i>' : '<i class="fa-regular fa-square" style="color: var(--text-muted);"></i>';
                                    
                                    li.innerHTML = `${boxIcon} <span>${item}</span>`;
                                    acList.appendChild(li);
                                });
                                acSec.appendChild(acList);
                                container.appendChild(acSec);
                            }
                        } catch (e) {
                            console.error("Error parsing AC:", e);
                        }
                    }
                    
                    // Render Non-goals if present
                    if (data.plan.non_goals) {
                        try {
                            const nonGoals = JSON.parse(data.plan.non_goals);
                            if (nonGoals && nonGoals.length > 0) {
                                const ngSec = document.createElement('div');
                                ngSec.className = 'plan-ng-section';
                                ngSec.style.marginTop = '1rem';
                                
                                const ngTitle = document.createElement('h3');
                                ngTitle.className = 'plan-section-title';
                                ngTitle.textContent = 'Non-goals';
                                ngSec.appendChild(ngTitle);
                                
                                const ngList = document.createElement('ul');
                                ngList.className = 'plan-ng-list';
                                ngList.style.paddingLeft = '1.2rem';
                                ngList.style.margin = '0.5rem 0 0 0';
                                
                                nonGoals.forEach(item => {
                                    const li = document.createElement('li');
                                    li.style.fontSize = '0.85rem';
                                    li.style.color = 'var(--text-muted)';
                                    li.style.marginBottom = '0.3rem';
                                    li.textContent = item;
                                    ngList.appendChild(li);
                                });
                                ngSec.appendChild(ngList);
                                container.appendChild(ngSec);
                            }
                        } catch (e) {
                            console.error("Error parsing non-goals:", e);
                        }
                    }
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
                        
                        const progressContainer = document.getElementById('plan-progress-container');
                        if (progressContainer) progressContainer.style.display = 'none';
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

function formatRelativeTime(isoString) {
    if (!isoString) return null;
    const diffMs = new Date(isoString) - new Date();
    if (diffMs <= 0) return null;
    
    const diffMins = Math.floor(diffMs / 60000);
    if (diffMins < 60) {
        return `${diffMins}m`;
    }
    const diffHours = Math.floor(diffMins / 60);
    const mins = diffMins % 60;
    if (diffHours < 24) {
        return `${diffHours}h ${mins}m`;
    }
    const diffDays = Math.floor(diffHours / 24);
    const hours = diffHours % 24;
    return `${diffDays}d ${hours}h`;
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
                const used_pct_5h = Math.max(0, Math.min(100, 100 - pct_5h));
                const used_pct_weekly = Math.max(0, Math.min(100, 100 - pct_weekly));
                
                const prefix = (family === 'gemini') ? 'gemini' : 'claude';
                
                const val5h = document.getElementById(`${prefix}-5h-val`);
                const valWeekly = document.getElementById(`${prefix}-weekly-val`);
                const bar5h = document.getElementById(`${prefix}-5h-bar`);
                const barWeekly = document.getElementById(`${prefix}-weekly-bar`);
                const lbl5h = document.getElementById(`${prefix}-5h-label`);
                const lblWeekly = document.getElementById(`${prefix}-weekly-label`);
                
                const labelPrefix = (family === 'gemini') ? 'Gemini' : 'Claude';
                const base5h = `${labelPrefix} 5h Limit`;
                const baseWeekly = `${labelPrefix} Weekly Limit`;
                
                const rel5h = formatRelativeTime(q.reset_5h);
                const relWeekly = formatRelativeTime(q.reset_weekly);
                
                const labelText5h = rel5h ? `${base5h} (Reset: ${rel5h})` : base5h;
                const labelTextWeekly = relWeekly ? `${baseWeekly} (Reset: ${relWeekly})` : baseWeekly;
                
                if (lbl5h) {
                    lbl5h.textContent = labelText5h;
                    if (q.reset_5h) {
                        lbl5h.title = `Next Reset: ${new Date(q.reset_5h).toLocaleString()}`;
                    } else {
                        lbl5h.removeAttribute('title');
                    }
                }
                if (lblWeekly) {
                    lblWeekly.textContent = labelTextWeekly;
                    if (q.reset_weekly) {
                        lblWeekly.title = `Next Reset: ${new Date(q.reset_weekly).toLocaleString()}`;
                    } else {
                        lblWeekly.removeAttribute('title');
                    }
                }
                
                if (val5h) val5h.textContent = `${used_pct_5h.toFixed(1)}%`;
                if (valWeekly) valWeekly.textContent = `${used_pct_weekly.toFixed(1)}%`;
                
                if (bar5h) {
                    bar5h.style.width = `${used_pct_5h}%`;
                    bar5h.className = 'quota-progress-bar ' + (used_pct_5h > 80.0 ? 'low' : (used_pct_5h > 50.0 ? 'medium' : 'high'));
                    if (q.reset_5h) {
                        bar5h.title = `Next Reset: ${new Date(q.reset_5h).toLocaleString()}`;
                    } else {
                        bar5h.removeAttribute('title');
                    }
                }
                if (barWeekly) {
                    barWeekly.style.width = `${used_pct_weekly}%`;
                    barWeekly.className = 'quota-progress-bar ' + (used_pct_weekly > 80.0 ? 'low' : (used_pct_weekly > 50.0 ? 'medium' : 'high'));
                    if (q.reset_weekly) {
                        barWeekly.title = `Next Reset: ${new Date(q.reset_weekly).toLocaleString()}`;
                    } else {
                        barWeekly.removeAttribute('title');
                    }
                }
            });
        }
    } catch (err) {
        console.error('Error polling quotas:', err);
    }
}

// Poll Subagents Status
async function pollSubagents() {
    try {
        const response = await fetch('/api/subagents');
        if (!response.ok) return;
        const data = await response.json();
        const subagents = data.subagents || [];
        
        const container = document.getElementById('subagents-container');
        const countBadge = document.getElementById('subagents-count');
        
        if (!container) return;
        
        // Count active subagents
        const activeCount = subagents.filter(s => s.status === 'active').length;
        if (countBadge) {
            countBadge.textContent = `${activeCount} active`;
            countBadge.className = 'active-count-badge' + (activeCount > 0 ? ' pulse' : '');
        }
        
        if (subagents.length === 0) {
            container.innerHTML = `
                <div class="subagent-empty-state" id="subagent-empty-state">
                    <p>No subagents spawned yet.</p>
                </div>
            `;
            return;
        }
        
        // Build hierarchy map
        const childrenMap = new Map();
        subagents.forEach(s => {
            if (s.parent_session_id) {
                if (!childrenMap.has(s.parent_session_id)) {
                    childrenMap.set(s.parent_session_id, []);
                }
                childrenMap.get(s.parent_session_id).push(s);
            }
        });

        // Find root nodes for the current active session
        const allIds = new Set(subagents.map(s => s.subagent_id));
        const rootSubagents = subagents.filter(s => {
            return s.parent_session_id === currentSessionId || (!allIds.has(s.parent_session_id) && s.parent_session_id !== currentSessionId);
        });

        function renderSubagentTree(node, depth = 0) {
            const timeStr = new Date(node.started_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
            
            let displayName = node.subagent_id;
            const hyphenIdx = node.subagent_id.indexOf('-');
            if (hyphenIdx !== -1) {
                const prefix = node.subagent_id.substring(0, hyphenIdx);
                const isUUIDPrefix = /^[0-9a-f]{8}$/i.test(prefix);
                if (!isUUIDPrefix) {
                    displayName = prefix;
                }
            }
            if (node.subagent_id.startsWith('grace-timekeeper-subagent')) {
                displayName = 'grace_timekeeper';
            }

            const children = childrenMap.get(node.subagent_id) || [];
            let childrenHtml = '';
            if (children.length > 0) {
                childrenHtml = `
                    <div class="subagent-children" style="margin-left: 1.25rem; border-left: 1px dashed rgba(255, 255, 255, 0.15); padding-left: 0.75rem;">
                        ${children.map(child => renderSubagentTree(child, depth + 1)).join('')}
                    </div>
                `;
            }

            return `
                <div class="subagent-node" style="position: relative; margin-bottom: 0.75rem;">
                    <div class="subagent-item clickable-subagent" onclick="showSubagentDetails('${node.subagent_id}', '${node.status}', \`${node.prompt.replace(/\\/g, '\\\\').replace(/`/g, '\\`').replace(/\$/g, '\\$')}\`, '${node.started_at}', '${node.completed_at || ''}', '${displayName}')" style="display: flex; gap: 0.75rem; align-items: flex-start; cursor: pointer; padding: 4px; border-radius: 4px;">
                        <div class="subagent-status-dot ${node.status}" style="margin-top: 0.25rem;"></div>
                        <div class="subagent-details" style="flex: 1; min-width: 0;">
                            <div class="subagent-prompt" title="${node.prompt}" style="font-size: 0.85rem; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-primary);">${node.prompt}</div>
                            <div class="subagent-meta" style="display: flex; gap: 0.5rem; font-size: 0.75rem; color: var(--text-muted); margin-top: 0.15rem;">
                                <span class="subagent-id-badge" title="${node.subagent_id}">${getSubagentEmoji(displayName, node.status)} ${displayName}</span>
                                <span class="subagent-time">${timeStr}</span>
                            </div>
                        </div>
                    </div>
                    ${childrenHtml}
                </div>
            `;
        }

        let html = '';
        if (rootSubagents.length === 0) {
            container.innerHTML = `
                <div class="subagent-empty-state" id="subagent-empty-state">
                    <p>No subagents spawned for this session yet.</p>
                </div>
            `;
            return;
        }

        rootSubagents.forEach(root => {
            html += renderSubagentTree(root);
        });
        container.innerHTML = html;
    } catch (err) {
        console.error('Error polling subagents:', err);
    }
}

// Setup Collapsible Widgets
function setupCollapsibleWidgets() {
    const headers = document.querySelectorAll('.collapsible-header');
    
    // Load saved states
    let states = {};
    try {
        states = JSON.parse(localStorage.getItem('dashboard_collapsed_states')) || {};
    } catch (e) {
        console.error('Error parsing collapsed states:', e);
    }
    
    // Apply saved states
    Object.keys(states).forEach(id => {
        const card = document.getElementById(id);
        if (card && states[id]) {
            card.classList.add('collapsed');
        }
    });
    
    // Bind click events
    headers.forEach(header => {
        if (header.dataset.listenerBound) return;
        header.dataset.listenerBound = "true";
        header.addEventListener('click', () => {
            const card = header.closest('.widget-card');
            if (!card) return;
            
            const isCollapsed = card.classList.toggle('collapsed');
            
            // Save state
            const id = card.id;
            if (id) {
                states[id] = isCollapsed;
                localStorage.setItem('dashboard_collapsed_states', JSON.stringify(states));
            }
        });
    });
}

// Dynamic Module/Widget Loader System
async function loadDynamicModules() {
    try {
        const response = await fetch('/api/modules');
        if (!response.ok) return;
        const data = await response.json();
        const modules = data.modules || [];

        for (let mod of modules) {
            // 1. Inject CSS if present
            if (mod.widgetCss) {
                const link = document.createElement('link');
                link.rel = 'stylesheet';
                link.href = mod.widgetCss + '?v=' + Date.now();
                document.head.appendChild(link);
            }

            // 2. Inject HTML container
            let parentContainer = document.querySelector('.sidebar-content') || document.querySelector('.sidebar') || document.body;
            if (mod.position === 'main') {
                parentContainer = document.querySelector('.main-content') || document.body;
            }

            const widgetCard = document.createElement('div');
            widgetCard.className = 'widget-card glass-card dynamic-module-card';
            widgetCard.id = `module-${mod.id}-card`;
            
            widgetCard.innerHTML = `
                <div class="card-header collapsible-header">
                    <h2><i class="${mod.iconClass || 'fa-solid fa-puzzle-piece'}" style="color: var(--accent-orchid);"></i> ${mod.name}</h2>
                    <div class="header-right" style="display: flex; align-items: center; gap: 0.5rem;">
                        ${mod.headerActionHtml || ''}
                        <span class="collapse-chevron"><i class="fa-solid fa-chevron-down"></i></span>
                    </div>
                </div>
                <div class="card-content-wrapper">
                    <div class="card-body">
                        <div id="module-${mod.id}-container"></div>
                    </div>
                </div>
            `;
            
            const skillsCard = document.getElementById('skills-card');
            if (skillsCard && mod.position === 'sidebar') {
                skillsCard.parentNode.insertBefore(widgetCard, skillsCard);
            } else {
                parentContainer.appendChild(widgetCard);
            }

            // 3. Inject JS script
            if (mod.widgetJs) {
                const script = document.createElement('script');
                script.type = 'module';
                script.src = mod.widgetJs + '?v=' + Date.now();
                document.body.appendChild(script);
            }
        }
        
        // Re-bind collapsible events to dynamically added cards
        setupCollapsibleWidgets();
    } catch (err) {
        console.error('Failed to load dynamic modules:', err);
    }
}

// Init Setup
async function init() {
    // Display the session ID generated at module scope
    headerSessionId.querySelector('.id-val').textContent = currentSessionId;
    await loadStatus();
    await loadSessions();
    await loadHistory();
    await loadSchedules();
    await pollTasks();
    await pollPlanAndTelemetry();
    await pollQuotas();
    setupCollapsibleWidgets();
    
    // Load dynamic widgets/modules
    await loadDynamicModules();
    
    // Polling schedules and active tasks
    setInterval(pollTasks, 2000);
    setInterval(loadSchedules, 5000);
    setInterval(loadSessions, 10000);
    setInterval(pollPlanAndTelemetry, 3000);
    setInterval(pollQuotas, 30000);
}

document.addEventListener('DOMContentLoaded', init);

// ==========================================
// SKILL STORE & MANAGEMENT INTERFACE
// ==========================================

const skillsModal = document.getElementById('skills-modal');
const openSkillsStoreBtn = document.getElementById('open-skills-store-btn');
const closeSkillsBtn = document.getElementById('close-skills-btn');
const skillSearch = document.getElementById('skill-search');
const repoSkillsList = document.getElementById('repo-skills-list');
const skillDetailPanel = document.getElementById('skill-detail-panel');
const btnShowCreator = document.getElementById('btn-show-creator');

let availableRepoSkills = [];
let currentFilter = 'all';
let currentSearch = '';

// Toggle Modal
if (openSkillsStoreBtn) {
    openSkillsStoreBtn.addEventListener('click', () => {
        skillsModal.classList.add('active');
        loadRepoSkills();
    });
}

if (closeSkillsBtn) {
    closeSkillsBtn.addEventListener('click', () => {
        skillsModal.classList.remove('active');
    });
}

// Close modal on click outside container
if (skillsModal) {
    skillsModal.addEventListener('click', (e) => {
        if (e.target === skillsModal) {
            skillsModal.classList.remove('active');
        }
    });
}

// Search and Filter Handlers
if (skillSearch) {
    skillSearch.addEventListener('input', (e) => {
        currentSearch = e.target.value.toLowerCase();
        renderRepoSkillsList();
    });
}

const filterBtns = document.querySelectorAll('.skill-source-filter .filter-btn');
filterBtns.forEach(btn => {
    btn.addEventListener('click', () => {
        filterBtns.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentFilter = btn.dataset.filter;
        renderRepoSkillsList();
    });
});

// Show Skill Creator Form
if (btnShowCreator) {
    btnShowCreator.addEventListener('click', showSkillCreator);
}

// Fetch Repository Skills
async function loadRepoSkills() {
    repoSkillsList.innerHTML = `
        <div class="loading-state">
            <i class="fa-solid fa-circle-notch fa-spin"></i>
            <span>Fetching repository skills...</span>
        </div>
    `;
    try {
        const res = await fetch('/api/repo-skills');
        if (res.ok) {
            const data = await res.json();
            availableRepoSkills = data.skills || [];
            renderRepoSkillsList();
        } else {
            throw new Error(`Failed to load repository skills (${res.status})`);
        }
    } catch (err) {
        repoSkillsList.innerHTML = `
            <div class="error-state">
                <i class="fa-solid fa-triangle-exclamation" style="color: var(--status-failed); font-size: 1.5rem; margin-bottom: 0.5rem;"></i>
                <span>Error loading skills. Please try again.</span>
            </div>
        `;
        console.error(err);
    }
}

// Render Available Skills List
function renderRepoSkillsList() {
    repoSkillsList.innerHTML = '';
    const filtered = availableRepoSkills.filter(skill => {
        const matchesFilter = currentFilter === 'all' || skill.type === currentFilter;
        const matchesSearch = skill.name.toLowerCase().includes(currentSearch) || 
                              skill.description.toLowerCase().includes(currentSearch);
        return matchesFilter && matchesSearch;
    });

    if (filtered.length === 0) {
        repoSkillsList.innerHTML = `
            <div class="error-state">
                <i class="fa-solid fa-magnifying-glass" style="font-size: 1.5rem; margin-bottom: 0.5rem; color: var(--text-muted)"></i>
                <span>No matching skills found.</span>
            </div>
        `;
        return;
    }

    filtered.forEach(skill => {
        const item = document.createElement('div');
        item.className = 'repo-skill-item';
        item.dataset.name = skill.name;
        item.innerHTML = `
            <div class="repo-skill-item-header">
                <span class="repo-skill-name">${skill.name}</span>
                <span class="repo-skill-badge ${skill.type}">${skill.type}</span>
            </div>
            <div class="repo-skill-desc">${skill.description}</div>
        `;
        item.addEventListener('click', () => {
            document.querySelectorAll('.repo-skill-item').forEach(i => i.classList.remove('selected'));
            item.classList.add('selected');
            viewRepoSkill(skill);
        });
        repoSkillsList.appendChild(item);
    });
}

// View and Safety Audit Repository Skill
async function viewRepoSkill(skill) {
    skillDetailPanel.innerHTML = `
        <div class="loading-state">
            <i class="fa-solid fa-circle-notch fa-spin"></i>
            <span>Fetching skill source code...</span>
        </div>
    `;
    try {
        const res = await fetch(`/api/repo-skills/${encodeURIComponent(skill.name)}/code`);
        if (res.ok) {
            const data = await res.json();
            
            // Build visual detail view
            skillDetailPanel.innerHTML = `
                <div class="detail-header">
                    <div class="detail-meta-info">
                        <div class="detail-title-row">
                            <h2 class="detail-title">${skill.name}</h2>
                            <span class="repo-skill-badge ${skill.type}">${skill.type}</span>
                        </div>
                        <div class="detail-subtitle-row">
                            <span>Repository: <strong>${skill.type === 'hermes' ? 'Hermes Skills' : 'OpenClaw Extensions'}</strong></span>
                        </div>
                    </div>
                    <div class="detail-actions" style="display: flex; align-items: center; gap: 1.25rem;">
                        <label class="paranoid-toggle-label" style="display: flex; align-items: center; gap: 0.5rem; color: var(--text-muted); font-size: 0.85rem; cursor: pointer; user-select: none;">
                            <input type="checkbox" id="chk-paranoid-mode" style="cursor: pointer; accent-color: var(--accent-orchid);">
                            <span>Paranoid Mode (Roundtable)</span>
                        </label>
                        <button class="btn-primary" id="btn-install-skill">
                            <i class="fa-solid fa-download"></i> Install Skill
                        </button>
                    </div>
                </div>
                <div class="detail-body">
                    <div>
                        <h3 class="detail-section-title"><i class="fa-solid fa-circle-info"></i> Description</h3>
                        <p class="detail-description">${skill.description}</p>
                    </div>
                    <div>
                        <h3 class="detail-section-title"><i class="fa-solid fa-shield-halved"></i> Safety Code Audit</h3>
                        <p class="detail-description" style="margin-bottom: 0.75rem; font-size: 0.85rem; color: var(--text-muted);">
                            Review the skill instructions and source files before installation to ensure the code complies with your security guidelines.
                        </p>
                        <div class="code-audit-container">
                            <div class="code-audit-header">
                                <span><i class="fa-solid fa-code"></i> Source File Contents</span>
                                <span>Read-Only</span>
                            </div>
                            <pre class="code-audit-body" id="code-audit-body"></pre>
                        </div>
                    </div>
                </div>
            `;
            
            // Escape HTML and populate code content safely
            const codeBody = document.getElementById('code-audit-body');
            codeBody.textContent = data.code || 'No source files found or empty skill.';
            
            // Add click action to Install button
            const installBtn = document.getElementById('btn-install-skill');
            installBtn.addEventListener('click', () => {
                const isParanoid = document.getElementById('chk-paranoid-mode')?.checked || false;
                installRepoSkill(skill.name, installBtn, isParanoid);
            });
        } else {
            throw new Error(`Failed to load skill code (${res.status})`);
        }
    } catch (err) {
        skillDetailPanel.innerHTML = `
            <div class="error-state" style="flex: 1;">
                <i class="fa-solid fa-triangle-exclamation" style="color: var(--status-failed); font-size: 2.5rem; margin-bottom: 1rem;"></i>
                <h2>Failed to Load Skill Details</h2>
                <p>${err.message}</p>
            </div>
        `;
        console.error(err);
    }
}

// Trigger Skill Installation
async function installRepoSkill(skillName, button, isParanoid = false, confirmOverride = false) {
    button.disabled = true;
    button.innerHTML = `<i class="fa-solid fa-circle-notch fa-spin"></i> Installing...`;
    try {
        const res = await fetch(`/api/repo-skills/${encodeURIComponent(skillName)}/install?paranoid=${isParanoid}&confirm=${confirmOverride}`, {
            method: 'POST'
        });
        if (res.ok) {
            const data = await res.json();
            button.className = 'btn-primary';
            button.style.background = 'var(--status-completed)';
            button.innerHTML = `<i class="fa-solid fa-check"></i> Installed`;
            
            // Alert user & reload main sidebar skills
            alert(`Success: ${data.detail || 'Skill installed successfully!'}`);
            loadStatus();
        } else {
            const errData = await res.json();
            const errMsg = errData.detail || 'Error installing skill.';
            if (errMsg.startsWith("HIL_REQUIRED:")) {
                const reason = errMsg.replace("HIL_REQUIRED:", "").trim();
                const userConfirmed = confirm(`⚠️ SECURITY REVIEW WARNING ⚠️\n\n${reason}\n\nDo you want to override and install this skill anyway?`);
                if (userConfirmed) {
                    return installRepoSkill(skillName, button, isParanoid, true);
                }
            }
            throw new Error(errMsg);
        }
    } catch (err) {
        button.disabled = false;
        button.innerHTML = `<i class="fa-solid fa-download"></i> Try Again`;
        alert(`Installation Failed: ${err.message}`);
    }
}

// Show Custom Skill Creator Form
function showSkillCreator() {
    // Unselect sidebar item
    document.querySelectorAll('.repo-skill-item').forEach(i => i.classList.remove('selected'));
    
    skillDetailPanel.innerHTML = `
        <div class="create-skill-panel">
            <div class="detail-title-row" style="margin-bottom: 1rem;">
                <h2 class="detail-title">Create Custom Skill</h2>
            </div>
            <form id="skill-creator-form" class="create-skill-form">
                <div class="form-row">
                    <div class="form-group">
                        <label for="new-skill-name">Skill Name</label>
                        <input type="text" id="new-skill-name" placeholder="e.g. My Helper Skill" required>
                    </div>
                    <div class="form-group">
                        <label for="new-skill-author">Author (Optional)</label>
                        <input type="text" id="new-skill-author" placeholder="e.g. Developer Dan">
                    </div>
                </div>
                <div class="form-row">
                    <div class="form-group" style="flex: 3;">
                        <label for="new-skill-desc">Description</label>
                        <input type="text" id="new-skill-desc" placeholder="What this skill enables the agent to do" required>
                    </div>
                    <div class="form-group" style="flex: 1;">
                        <label for="new-skill-version">Version</label>
                        <input type="text" id="new-skill-version" placeholder="e.g. 1.0.0">
                    </div>
                </div>
                <div class="form-group">
                    <label for="new-skill-instructions">Instructions &amp; Code (SKILL.md Markdown format)</label>
                    <textarea id="new-skill-instructions" rows="12" placeholder="# Instructions for My Helper Skill&#10;&#10;Describe how the agent should utilize this skill and any specific rules or workflows..." required></textarea>
                </div>
                <div style="display: flex; gap: 1rem; justify-content: flex-end; margin-top: 1rem;">
                    <button type="button" class="btn-secondary" id="btn-cancel-creation">Cancel</button>
                    <button type="submit" class="btn-primary" id="btn-submit-skill">
                        <i class="fa-solid fa-floppy-disk"></i> Save &amp; Register Skill
                    </button>
                </div>
            </form>
        </div>
    `;

    document.getElementById('btn-cancel-creation').addEventListener('click', () => {
        skillDetailPanel.innerHTML = `
            <div class="detail-empty-state">
                <i class="fa-solid fa-graduation-cap"></i>
                <p>Select a skill from the list to preview, safety-audit, and install.</p>
            </div>
        `;
    });

    document.getElementById('skill-creator-form').addEventListener('submit', submitCustomSkill);
}

// Save Custom Skill
async function submitCustomSkill(e) {
    e.preventDefault();
    const submitBtn = document.getElementById('btn-submit-skill');
    submitBtn.disabled = true;
    submitBtn.innerHTML = `<i class="fa-solid fa-circle-notch fa-spin"></i> Saving...`;

    const name = document.getElementById('new-skill-name').value;
    const author = document.getElementById('new-skill-author').value || null;
    const description = document.getElementById('new-skill-desc').value;
    const version = document.getElementById('new-skill-version').value || null;
    const instructions = document.getElementById('new-skill-instructions').value;

    try {
        const res = await fetch('/api/skills/install', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, author, description, version, instructions })
        });

        if (res.ok) {
            alert(`Success: Skill '${name}' saved and registered!`);
            loadStatus(); // refresh dashboard sidebar list
            
            // Clear panel
            skillDetailPanel.innerHTML = `
                <div class="detail-empty-state">
                    <i class="fa-solid fa-graduation-cap"></i>
                    <p>Select a skill from the list to preview, safety-audit, and install.</p>
                </div>
            `;
        } else {
            const errData = await res.json();
            throw new Error(errData.detail || 'Failed to save custom skill.');
        }
    } catch (err) {
        submitBtn.disabled = false;
        submitBtn.innerHTML = `<i class="fa-solid fa-floppy-disk"></i> Save &amp; Register Skill`;
        alert(`Error: ${err.message}`);
    }
}

// ============================================================================
// Modernized UI: Color-Coded Emojis, Status Modals, and Task Details
// ============================================================================

function getSubagentEmoji(displayName, status) {
    let typeEmoji = "🤖";
    const nameLower = displayName.toLowerCase();
    
    // Icon based on purpose/name
    if (nameLower.includes("timekeeper") || nameLower.includes("timer") || nameLower.includes("grace")) {
        typeEmoji = "⏱️";
    } else if (nameLower.includes("gmail") || nameLower.includes("mail") || nameLower.includes("email") || nameLower.includes("sync")) {
        typeEmoji = "📧";
    } else if (nameLower.includes("observer") || nameLower.includes("quiet")) {
        typeEmoji = "🤫";
    } else if (nameLower.includes("eval") || nameLower.includes("meta")) {
        typeEmoji = "🧠";
    } else if (nameLower.includes("trade") || nameLower.includes("stock") || nameLower.includes("portfolio")) {
        typeEmoji = "📈";
    } else if (nameLower.includes("qa") || nameLower.includes("test")) {
        typeEmoji = "🧪";
    } else if (nameLower.includes("secure") || nameLower.includes("security") || nameLower.includes("audit")) {
        typeEmoji = "🛡️";
    }
    
    // Status color-coding
    let statusEmoji = "⚪";
    if (status === "active" || status === "running") {
        statusEmoji = "🟢";
    } else if (status === "completed" || status === "success") {
        statusEmoji = "✅";
    } else if (status === "failed") {
        statusEmoji = "❌";
    }
    
    return `${statusEmoji} ${typeEmoji}`;
}

// Details modal DOM bindings
const detailsModal = document.getElementById('details-modal');
const closeDetailsBtn = document.getElementById('close-details-btn');
const detailsModalTitle = document.getElementById('details-modal-title');
const detailsType = document.getElementById('details-type');
const detailsStatus = document.getElementById('details-status');
const detailsPrompt = document.getElementById('details-prompt');
const detailsLogs = document.getElementById('details-logs');
const detailsLogsSection = document.getElementById('details-logs-section');
const detailsTime = document.getElementById('details-time');

if (closeDetailsBtn && detailsModal) {
    closeDetailsBtn.addEventListener('click', () => {
        detailsModal.classList.remove('active');
    });
    detailsModal.addEventListener('click', (e) => {
        if (e.target === detailsModal) {
            detailsModal.classList.remove('active');
        }
    });
}

function showSubagentDetails(subagentId, status, prompt, startedAt, completedAt, displayName) {
    if (!detailsModal) return;
    
    detailsModalTitle.innerHTML = `<i class="fa-solid fa-robot" style="color: var(--accent-orchid);"></i> Subagent Details`;
    detailsType.textContent = displayName || subagentId;
    
    const emojiStr = getSubagentEmoji(displayName || subagentId, status);
    detailsStatus.innerHTML = `
        <span class="subagent-status-dot ${status}" style="display: inline-block;"></span>
        <span style="font-weight: 600; font-size: 0.9rem;">${emojiStr} ${status.toUpperCase()}</span>
    `;
    
    detailsPrompt.textContent = prompt || "No prompt detail recorded.";
    
    // Load subagent messages
    detailsLogsSection.style.display = 'block';
    detailsLogs.innerHTML = `<div style="color: var(--text-muted);"><i class="fa-solid fa-circle-notch fa-spin"></i> Fetching coordination logs...</div>`;
    
    fetch(`/api/subagents/${subagentId}/messages`)
        .then(res => res.ok ? res.json() : { messages: [] })
        .then(data => {
            const msgs = data.messages || [];
            if (msgs.length === 0) {
                detailsLogs.innerHTML = `<div style="color: var(--text-muted);">No message logs found for this subagent.</div>`;
            } else {
                detailsLogs.innerHTML = msgs.map(m => {
                    const time = new Date(m.timestamp).toLocaleTimeString();
                    const color = m.role === 'subagent' ? 'var(--accent-mint)' : 'var(--accent-orchid)';
                    return `<div style="margin-bottom: 0.35rem; line-height: 1.3;">
                        <span style="color: var(--text-muted); font-size: 0.75rem;">[${time}]</span>
                        <strong style="color: ${color};">${m.role.toUpperCase()}:</strong>
                        <span style="color: var(--text-primary); font-size: 0.8rem;">${m.message}</span>
                    </div>`;
                }).join('');
            }
        })
        .catch(err => {
            detailsLogs.innerHTML = `<div style="color: #ef4444;">Failed to load logs: ${err.message}</div>`;
        });
        
    const start = new Date(startedAt).toLocaleString();
    const end = completedAt ? new Date(completedAt).toLocaleString() : 'Running...';
    detailsTime.innerHTML = `
        <div><strong>Started:</strong> ${start}</div>
        <div><strong>Finished:</strong> ${end}</div>
    `;
    
    detailsModal.classList.add('active');
}

function showTaskDetails(task) {
    if (!detailsModal) return;
    
    detailsModalTitle.innerHTML = `<i class="fa-solid fa-bolt" style="color: var(--accent-orchid);"></i> Task / Tool Details`;
    detailsType.textContent = task.name;
    
    // Status formatting
    let statusColor = "var(--text-muted)";
    let statusEmoji = "⚪";
    if (task.status === "running") {
        statusColor = "var(--accent-orchid)";
        statusEmoji = "🟢";
    } else if (task.status === "completed") {
        statusColor = "var(--accent-mint)";
        statusEmoji = "✅";
    } else if (task.status === "failed" || task.status === "denied") {
        statusColor = "#ef4444";
        statusEmoji = "❌";
    }
    
    detailsStatus.innerHTML = `
        <span class="card-status-dot" style="display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: ${statusColor};"></span>
        <span style="font-weight: 600; font-size: 0.9rem; color: ${statusColor};">${statusEmoji} ${task.status.toUpperCase()}</span>
    `;
    
    detailsPrompt.textContent = task.details || "No details recorded.";
    
    // Load task logs
    detailsLogsSection.style.display = 'block';
    detailsLogs.innerHTML = `<div style="color: var(--text-muted);"><i class="fa-solid fa-circle-notch fa-spin"></i> Fetching task execution logs...</div>`;
    
    fetch(`/api/tasks/${task.id}/logs`)
        .then(res => res.ok ? res.json() : { logs: [] })
        .then(data => {
            const logs = data.logs || [];
            if (logs.length === 0) {
                detailsLogs.innerHTML = `<div style="color: var(--text-muted);">No execution logs recorded yet.</div>`;
            } else {
                detailsLogs.innerHTML = logs.map(l => {
                    const time = new Date(l.timestamp).toLocaleTimeString();
                    return `<div style="margin-bottom: 0.25rem;">
                        <span style="color: var(--text-muted); font-size: 0.75rem;">[${time}]</span>
                        <span style="color: var(--text-primary); font-size: 0.8rem;">> ${l.message}</span>
                    </div>`;
                }).join('');
            }
        })
        .catch(err => {
            detailsLogs.innerHTML = `<div style="color: #ef4444;">Failed to load logs: ${err.message}</div>`;
        });
        
    const start = new Date(task.started_at).toLocaleString();
    const end = task.completed_at ? new Date(task.completed_at).toLocaleString() : 'Active...';
    detailsTime.innerHTML = `
        <div><strong>Started:</strong> ${start}</div>
        <div><strong>Finished:</strong> ${end}</div>
    `;
    
    detailsModal.classList.add('active');
}

// Bind methods globally so inline onclick events can find them
window.showSubagentDetails = showSubagentDetails;
window.showTaskDetails = showTaskDetails;
window.getSubagentEmoji = getSubagentEmoji;

// Tab Navigation Logic
document.querySelectorAll('.nav-tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.nav-tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
        
        btn.classList.add('active');
        const panelId = btn.getAttribute('data-tab');
        const panel = document.getElementById(panelId);
        if (panel) {
            panel.classList.add('active');
            
            if (panelId.includes('config')) {
                loadPlatformConfigUI();
                loadTenantRegistryUI();
                loadRouteTelemetryUI();
            } else if (panelId.includes('marketplace')) {
                loadMarketplaceUI();
            }
        }
    });
});

let currentPlatformConfig = { routes: {}, plugins: {}, skills: {} };

function loadPlatformConfigUI() {
    fetch('/api/config/platform')
        .then(res => res.json())
        .then(data => {
            if (data.status === 'success') {
                currentPlatformConfig = data.config || {};
                currentPlatformConfig.routes = currentPlatformConfig.routes || {};
                currentPlatformConfig.plugins = currentPlatformConfig.plugins || {};
                currentPlatformConfig.skills = currentPlatformConfig.skills || {};
                
                const grid = document.getElementById('routes-config-grid');
                grid.innerHTML = '';
                
                data.available_routes.forEach(route => {
                    const rName = route.name.toLowerCase();
                    const cfg = currentPlatformConfig.routes[rName] || {};
                    const status = (cfg.status || route.default_status || '').toUpperCase();
                    const priority = cfg.priority !== undefined ? cfg.priority : route.default_priority;
                    const weight = cfg.weight !== undefined ? cfg.weight : 100;
                    
                    const card = document.createElement('div');
                    card.className = 'route-card';
                    card.style.background = 'rgba(255, 255, 255, 0.02)';
                    card.style.border = '1px solid var(--card-border)';
                    card.style.borderRadius = '8px';
                    card.style.padding = '1.25rem';
                    card.style.display = 'flex';
                    card.style.flexDirection = 'column';
                    card.style.gap = '0.75rem';
                    
                    card.innerHTML = `
                        <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid rgba(255,255,255,0.05); padding-bottom: 0.5rem;">
                            <span style="font-weight: 700; color: var(--text-primary); font-size: 0.95rem; letter-spacing: 0.5px;">${route.name.toUpperCase()}</span>
                            <span style="background: rgba(255,255,255,0.04); border: 1px solid var(--card-border); padding: 0.15rem 0.45rem; border-radius: 4px; font-size: 0.7rem; color: var(--text-muted); text-transform: uppercase; font-weight: 600;">${route.type}</span>
                        </div>
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; align-items: center;">
                            <div style="display: flex; flex-direction: column; gap: 0.25rem;">
                                <label style="font-size: 0.7rem; color: var(--text-muted); font-weight: 500;">Status</label>
                                <select class="route-status-select" data-route="${rName}" style="background: rgba(0,0,0,0.3); border: 1px solid var(--card-border); color: var(--text-primary); padding: 0.35rem; border-radius: 4px; font-size: 0.8rem; width: 100%;">
                                    <option value="PRIMARY" ${status === 'PRIMARY' ? 'selected' : ''}>Primary</option>
                                    <option value="SECONDARY" ${status === 'SECONDARY' ? 'selected' : ''}>Secondary</option>
                                    <option value="URGENT_ONLY" ${status === 'URGENT_ONLY' ? 'selected' : ''}>Urgent Only</option>
                                    <option value="OFF" ${status === 'OFF' ? 'selected' : ''}>Off</option>
                                </select>
                            </div>
                            <div style="display: flex; flex-direction: column; gap: 0.25rem;">
                                <label style="font-size: 0.7rem; color: var(--text-muted); font-weight: 500;">Priority (1-10)</label>
                                <input type="number" class="route-priority-input" data-route="${rName}" value="${priority}" min="1" max="10" style="background: rgba(0,0,0,0.3); border: 1px solid var(--card-border); color: var(--text-primary); padding: 0.35rem; border-radius: 4px; font-size: 0.8rem; width: 100%; text-align: center;">
                            </div>
                            <div style="display: flex; flex-direction: column; gap: 0.25rem; grid-column: span 2;">
                                <label style="font-size: 0.7rem; color: var(--text-muted); font-weight: 500;">Weight Allocation</label>
                                <div style="display: flex; align-items: center; gap: 0.75rem;">
                                    <input type="range" class="route-weight-range" data-route="${rName}" value="${weight}" min="0" max="100" style="flex: 1; accent-color: var(--accent-orchid); cursor: pointer;">
                                    <span class="weight-val" style="font-size: 0.8rem; color: var(--text-primary); font-weight: 600; width: 35px; text-align: right;">${weight}%</span>
                                </div>
                            </div>
                            <div style="grid-column: span 2; display: flex; justify-content: flex-end; margin-top: 0.25rem;">
                                <button class="btn-save-route" data-route="${rName}" style="background: var(--accent-orchid); border: none; color: white; padding: 0.4rem 0.85rem; border-radius: 4px; cursor: pointer; font-size: 0.8rem; font-weight: 600; transition: background 0.2s ease; display: flex; align-items: center; gap: 0.35rem;">
                                    <i class="fa-solid fa-floppy-disk"></i> Save
                                </button>
                            </div>
                        </div>
                    `;
                    
                    card.querySelector('.route-weight-range').addEventListener('input', (e) => {
                        card.querySelector('.weight-val').textContent = e.target.value + '%';
                    });
                    
                    card.querySelector('.btn-save-route').addEventListener('click', () => {
                        const statusVal = card.querySelector('.route-status-select').value;
                        const priorityVal = parseInt(card.querySelector('.route-priority-input').value);
                        const weightVal = parseInt(card.querySelector('.route-weight-range').value);
                        
                        currentPlatformConfig.routes[rName] = {
                            status: statusVal,
                            priority: priorityVal,
                            weight: weightVal
                        };
                        savePlatformConfig();
                    });
                    
                    grid.appendChild(card);
                });
            }
        });
}

function savePlatformConfig() {
    fetch('/api/config/platform', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(currentPlatformConfig)
    })
    .then(res => res.json())
    .then(data => {
        if (data.status === 'success') {
            console.log("[CONFIG] Saved platform configuration successfully.");
        } else {
            alert('Failed to save config: ' + data.detail);
        }
    })
    .catch(err => alert('Save configuration error: ' + err.message));
}

function loadTenantRegistryUI() {
    const tbody = document.getElementById('tenants-config-body');
    tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; padding: 1.5rem; color: var(--text-muted);"><i class="fa-solid fa-circle-notch fa-spin"></i> Fetching tenants status...</td></tr>`;

    fetch('/api/config/tenants')
        .then(res => res.json())
        .then(data => {
            if (data.status === 'success') {
                tbody.innerHTML = '';
                if (data.tenants.length === 0) {
                    tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; padding: 1.5rem; color: var(--text-muted);">No tenant instances registered.</td></tr>`;
                    return;
                }
                
                data.tenants.forEach(tenant => {
                    const tr = document.createElement('tr');
                    tr.style.borderBottom = '1px solid rgba(255,255,255,0.05)';
                    tr.innerHTML = `
                        <td style="padding: 0.75rem; font-weight: 500; color: var(--text-primary); font-family: monospace;">${tenant.owner_id}</td>
                        <td style="padding: 0.75rem; color: var(--text-muted); font-size: 0.85rem;">Port ${tenant.port}</td>
                        <td style="padding: 0.75rem;">
                            <span style="display: inline-flex; align-items: center; gap: 0.35rem; color: var(--accent-mint); font-weight: 600; font-size: 0.85rem;">
                                <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: var(--accent-mint);"></span>
                                ${tenant.status}
                            </span>
                        </td>
                        <td style="padding: 0.75rem;">
                            <button class="btn-tenant-down" data-owner="${tenant.owner_id}" style="background: rgba(239, 68, 68, 0.15); border: 1px solid rgba(239, 68, 68, 0.3); color: #ef4444; padding: 0.35rem 0.75rem; border-radius: 4px; cursor: pointer; font-size: 0.8rem;">
                                Stop
                            </button>
                        </td>
                    `;
                    
                    tr.querySelector('.btn-tenant-down').addEventListener('click', () => {
                        if (confirm(`Are you sure you want to stop and remove tenant ${tenant.owner_id}?`)) {
                            controlTenant(tenant.owner_id, 'down');
                        }
                    });
                    
                    tbody.appendChild(tr);
                });
            }
        });
}

function controlTenant(ownerId, action) {
    fetch(`/api/config/tenants/${ownerId}/${action}`, { method: 'POST' })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'success') {
                alert(`Tenant ${ownerId} execution successfully requested: ${action}`);
                loadTenantRegistryUI();
            } else {
                alert(`Tenant operation failed: ${data.message || data.detail}`);
            }
        })
        .catch(err => alert('Tenant control API error: ' + err.message));
}

// Bind tenant creator trigger
const provisionBtn = document.getElementById('provision-tenant-btn');
if (provisionBtn) {
    provisionBtn.addEventListener('click', () => {
        const ownerId = prompt("Enter Discord Owner ID / Tenant ID to provision:");
        if (ownerId) {
            controlTenant(ownerId.trim(), 'up');
        }
    });
}

function loadMarketplaceUI() {
    fetch('/api/config/platform')
        .then(res => res.json())
        .then(data => {
            if (data.status === 'success') {
                currentPlatformConfig = data.config;
                
                // Plugins Grid
                const pluginsGrid = document.getElementById('plugins-marketplace-grid');
                pluginsGrid.innerHTML = '';
                data.available_plugins.forEach(plugin => {
                    const pName = plugin.name;
                    const enabled = currentPlatformConfig.plugins[pName] !== false; // default true
                    
                    const card = document.createElement('div');
                    card.style.background = 'rgba(255, 255, 255, 0.02)';
                    card.style.border = '1px solid var(--card-border)';
                    card.style.borderRadius = '8px';
                    card.style.padding = '1rem';
                    card.style.display = 'flex';
                    card.style.flexDirection = 'column';
                    card.style.justifyContent = 'space-between';
                    card.innerHTML = `
                        <div>
                            <h4 style="margin: 0 0 0.5rem 0; font-size: 0.95rem; color: var(--text-primary); font-weight: 600;">${pName}</h4>
                            <p style="font-size: 0.75rem; color: var(--text-muted); margin: 0 0 1rem 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">Path: ${plugin.path}</p>
                        </div>
                        <div style="display: flex; justify-content: space-between; align-items: center; border-top: 1px solid rgba(255,255,255,0.05); padding-top: 0.75rem;">
                            <span class="toggle-status" style="font-size: 0.8rem; color: ${enabled ? 'var(--accent-mint)' : 'var(--text-muted)'}; font-weight: 600;">${enabled ? 'Enabled' : 'Disabled'}</span>
                            <label style="position: relative; display: inline-block; width: 34px; height: 18px; cursor: pointer;">
                                <input type="checkbox" class="plugin-toggle" ${enabled ? 'checked' : ''} style="opacity: 0; width: 0; height: 0;">
                                <span class="slider" style="position: absolute; top: 0; left: 0; right: 0; bottom: 0; background-color: ${enabled ? 'var(--accent-orchid)' : 'rgba(255,255,255,0.1)'}; transition: .3s; border-radius: 18px;"></span>
                            </label>
                        </div>
                    `;
                    
                    const input = card.querySelector('input');
                    const slider = card.querySelector('.slider');
                    const statusText = card.querySelector('.toggle-status');
                    input.addEventListener('change', (e) => {
                        const isChecked = e.target.checked;
                        slider.style.backgroundColor = isChecked ? 'var(--accent-orchid)' : 'rgba(255,255,255,0.1)';
                        statusText.textContent = isChecked ? 'Enabled' : 'Disabled';
                        statusText.style.color = isChecked ? 'var(--accent-mint)' : 'var(--text-muted)';
                        currentPlatformConfig.plugins[pName] = isChecked;
                        savePlatformConfig();
                    });
                    
                    pluginsGrid.appendChild(card);
                });

                // Skills Grid
                const skillsGrid = document.getElementById('skills-marketplace-grid');
                skillsGrid.innerHTML = '';
                data.available_skills.forEach(skill => {
                    const sName = skill.name;
                    const enabled = currentPlatformConfig.skills[sName] !== false; // default true
                    
                    const card = document.createElement('div');
                    card.style.background = 'rgba(255, 255, 255, 0.02)';
                    card.style.border = '1px solid var(--card-border)';
                    card.style.borderRadius = '8px';
                    card.style.padding = '1rem';
                    card.style.display = 'flex';
                    card.style.flexDirection = 'column';
                    card.style.justifyContent = 'space-between';
                    card.innerHTML = `
                        <div>
                            <h4 style="margin: 0 0 0.5rem 0; font-size: 0.95rem; color: var(--text-primary); font-weight: 600;">${sName}</h4>
                            <p style="font-size: 0.75rem; color: var(--text-muted); margin: 0 0 1rem 0; height: 32px; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;">${skill.description}</p>
                        </div>
                        <div style="display: flex; justify-content: space-between; align-items: center; border-top: 1px solid rgba(255,255,255,0.05); padding-top: 0.75rem;">
                            <span class="toggle-status" style="font-size: 0.8rem; color: ${enabled ? 'var(--accent-mint)' : 'var(--text-muted)'}; font-weight: 600;">${enabled ? 'Enabled' : 'Disabled'}</span>
                            <label style="position: relative; display: inline-block; width: 34px; height: 18px; cursor: pointer;">
                                <input type="checkbox" class="skill-toggle" ${enabled ? 'checked' : ''} style="opacity: 0; width: 0; height: 0;">
                                <span class="slider" style="position: absolute; top: 0; left: 0; right: 0; bottom: 0; background-color: ${enabled ? 'var(--accent-orchid)' : 'rgba(255,255,255,0.1)'}; transition: .3s; border-radius: 18px;"></span>
                            </label>
                        </div>
                    `;
                    
                    const input = card.querySelector('input');
                    const slider = card.querySelector('.slider');
                    const statusText = card.querySelector('.toggle-status');
                    input.addEventListener('change', (e) => {
                        const isChecked = e.target.checked;
                        slider.style.backgroundColor = isChecked ? 'var(--accent-orchid)' : 'rgba(255,255,255,0.1)';
                        statusText.textContent = isChecked ? 'Enabled' : 'Disabled';
                        statusText.style.color = isChecked ? 'var(--accent-mint)' : 'var(--text-muted)';
                        currentPlatformConfig.skills[sName] = isChecked;
                        savePlatformConfig();
                    });
                    
                    skillsGrid.appendChild(card);
                });
            }
        });
}

function loadRouteTelemetryUI() {
    const btn = document.getElementById('refresh-telemetry-btn');
    if (btn && !btn.dataset.bound) {
        btn.dataset.bound = 'true';
        btn.addEventListener('click', loadRouteTelemetryUI);
    }

    fetch('/api/telemetry/routes')
        .then(res => res.json())
        .then(data => {
            if (data.status !== 'success') return;
            
            // 1. Render Health Bars / Graph
            const container = document.getElementById('route-chart-container');
            container.innerHTML = '';
            
            const routesData = {};
            data.summary.forEach(item => {
                const name = item.route_name.toUpperCase();
                if (!routesData[name]) {
                    routesData[name] = { success: 0, failed: 0, latencies: [] };
                }
                if (item.status === 'success') {
                    routesData[name].success += item.count;
                } else {
                    routesData[name].failed += item.count;
                }
                if (item.avg_latency) {
                    routesData[name].latencies.push(item.avg_latency);
                }
            });
            
            const routeNames = Object.keys(routesData);
            if (routeNames.length === 0) {
                container.innerHTML = '<p style="font-size: 0.85rem; color: var(--text-muted); margin: 0;">No telemetry data available yet.</p>';
            } else {
                routeNames.forEach(name => {
                    const info = routesData[name];
                    const total = info.success + info.failed;
                    const successPct = total > 0 ? (info.success / total) * 100 : 0;
                    const failedPct = total > 0 ? (info.failed / total) * 100 : 0;
                    const avgLat = info.latencies.length > 0 ? (info.latencies.reduce((a, b) => a + b, 0) / info.latencies.length).toFixed(2) + 's' : 'N/A';
                    
                    const barRow = document.createElement('div');
                    barRow.style.display = 'flex';
                    barRow.style.flexDirection = 'column';
                    barRow.style.gap = '0.35rem';
                    barRow.style.padding = '0.5rem';
                    barRow.style.background = 'rgba(255,255,255,0.01)';
                    barRow.style.border = '1px solid rgba(255,255,255,0.03)';
                    barRow.style.borderRadius = '6px';
                    
                    barRow.innerHTML = `
                        <div style="display: flex; justify-content: space-between; font-size: 0.8rem; font-weight: 600; color: var(--text-primary);">
                            <span>${name} <span style="font-weight: 400; color: var(--text-muted); font-size: 0.75rem;">(Avg: ${avgLat}, Total: ${total})</span></span>
                            <span style="color: var(--accent-mint);">${successPct.toFixed(0)}% Success</span>
                        </div>
                        <div style="display: flex; height: 8px; border-radius: 4px; overflow: hidden; background: rgba(255,255,255,0.05);">
                            <div style="width: ${successPct}%; background: var(--accent-mint); transition: width 0.3s ease;"></div>
                            <div style="width: ${failedPct}%; background: var(--accent-coral) || '#ff5555'; transition: width 0.3s ease;"></div>
                        </div>
                    `;
                    container.appendChild(barRow);
                });
            }
            
            // 2. Render Recent History Table
            const tbody = document.getElementById('route-telemetry-body');
            tbody.innerHTML = '';
            
            if (data.recent.length === 0) {
                tbody.innerHTML = `
                    <tr>
                        <td colspan="6" style="padding: 1rem; text-align: center; color: var(--text-muted);">No recent route executions.</td>
                    </tr>
                `;
            } else {
                data.recent.forEach(item => {
                    const tr = document.createElement('tr');
                    tr.style.borderBottom = '1px solid rgba(255,255,255,0.03)';
                    
                    const timeStr = item.timestamp ? new Date(item.timestamp).toLocaleTimeString() : 'N/A';
                    const statusColor = item.status === 'success' ? 'var(--accent-mint)' : '#ff5555';
                    const statusText = item.status === 'success' ? 'Success' : 'Failed';
                    const latencyStr = item.latency ? item.latency.toFixed(2) + 's' : '0.00s';
                    const detail = item.error_message || '-';
                    
                    tr.innerHTML = `
                        <td style="padding: 0.5rem 0.75rem; color: var(--text-muted); white-space: nowrap;">${timeStr}</td>
                        <td style="padding: 0.5rem 0.75rem; font-weight: 600; color: var(--text-primary); text-transform: uppercase;">${item.route_name}</td>
                        <td style="padding: 0.5rem 0.75rem; color: var(--text-muted);">${item.model_name}</td>
                        <td style="padding: 0.5rem 0.75rem; font-weight: 600; color: ${statusColor};">${statusText}</td>
                        <td style="padding: 0.5rem 0.75rem; color: var(--text-muted);">${latencyStr}</td>
                        <td style="padding: 0.5rem 0.75rem; color: var(--text-muted); font-size: 0.75rem; max-width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${detail}">${detail}</td>
                    `;
                    tbody.appendChild(tr);
                });
            }
        });
}
