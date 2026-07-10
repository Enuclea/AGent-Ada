// D&D Characters Widget Rendering Module
async function loadDndCharacters() {
    const container = document.getElementById('module-dnd-container');
    if (!container) return;

    try {
        const response = await fetch('/api/dnd/characters?t=' + Date.now());
        if (!response.ok) {
            throw new Error(`Failed to fetch D&D characters: ${response.statusText}`);
        }
        const data = await response.json();
        const characters = Array.isArray(data) ? data : (data.characters || []);

        if (characters.length === 0) {
            container.replaceChildren();
            const emptyState = document.createElement('div');
            emptyState.className = 'loading-state';
            emptyState.style.padding = '1rem';
            emptyState.style.textAlign = 'center';
            emptyState.style.color = 'var(--text-muted)';
            emptyState.textContent = 'No character rolls available.';
            container.appendChild(emptyState);
            return;
        }

        container.replaceChildren();

        characters.forEach((char) => {
            const card = document.createElement('div');
            card.className = 'dnd-char-card';

            const header = document.createElement('div');
            header.className = 'dnd-char-header';

            const name = document.createElement('span');
            name.className = 'dnd-char-name';
            name.textContent = char.name || 'Hero';

            const charClass = document.createElement('span');
            charClass.className = `dnd-char-class ${char.suggested_class.toLowerCase()}`;
            charClass.textContent = char.suggested_class;

            header.appendChild(name);
            header.appendChild(charClass);
            card.appendChild(header);

            const grid = document.createElement('div');
            grid.className = 'dnd-stats-grid';

            const stats = char.stats || {};
            const statKeys = ['Strength', 'Dexterity', 'Constitution', 'Intelligence', 'Wisdom', 'Charisma'];

            statKeys.forEach(statName => {
                const val = stats[statName] || 0;
                
                const item = document.createElement('div');
                item.className = 'dnd-stat-item';

                const meta = document.createElement('div');
                meta.className = 'dnd-stat-meta';

                const label = document.createElement('span');
                label.className = 'dnd-stat-label';
                const shortLabel = statName.substring(0, 3).toUpperCase();
                label.textContent = shortLabel;

                const valueSpan = document.createElement('span');
                valueSpan.className = 'dnd-stat-val';
                valueSpan.textContent = val;

                meta.appendChild(label);
                meta.appendChild(valueSpan);
                item.appendChild(meta);

                const barContainer = document.createElement('div');
                barContainer.className = 'dnd-stat-bar-container';

                const bar = document.createElement('div');
                bar.className = 'dnd-stat-bar';
                const percentage = Math.min(100, Math.max(0, (val / 18) * 100));
                bar.style.width = `${percentage}%`;

                barContainer.appendChild(bar);
                item.appendChild(barContainer);
                grid.appendChild(item);
            });

            card.appendChild(grid);
            container.appendChild(card);
        });

    } catch (err) {
        console.error('Error loading D&D characters:', err);
        container.replaceChildren();
        const errorState = document.createElement('div');
        errorState.className = 'loading-state';
        errorState.style.padding = '1rem';
        errorState.style.textAlign = 'center';
        errorState.style.color = 'var(--status-failed)';
        errorState.textContent = 'Error loading character rolls.';
        container.appendChild(errorState);
    }
}

// Bind D&D characters refresh button
const refreshBtn = document.getElementById('refresh-dnd-btn');
if (refreshBtn) {
    refreshBtn.addEventListener('click', (e) => {
        e.stopPropagation(); // Avoid collapsing/expanding card
        
        const container = document.getElementById('module-dnd-container');
        if (container) {
            container.replaceChildren();
            const loader = document.createElement('div');
            loader.className = 'loading-state';
            loader.style.padding = '1rem';
            loader.style.textAlign = 'center';
            loader.style.color = 'var(--text-muted)';
            
            const spinner = document.createElement('i');
            spinner.className = 'fa-solid fa-circle-notch fa-spin';
            spinner.style.marginRight = '0.5rem';
            
            const span = document.createElement('span');
            span.textContent = 'Refreshing rolls...';
            
            loader.appendChild(spinner);
            loader.appendChild(span);
            container.appendChild(loader);
        }
        
        fetch('/api/dnd/regenerate', { method: 'POST' })
            .then(response => {
                if (!response.ok) {
                    throw new Error('Failed to regenerate characters');
                }
                return response.json();
            })
            .then(() => {
                loadDndCharacters();
            })
            .catch(err => {
                console.error('Error during D&D regeneration:', err);
                loadDndCharacters();
            });
    });
}

// Initial load
loadDndCharacters();
