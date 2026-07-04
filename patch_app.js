<<<<<<< SEARCH
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
=======
                            if (data.type === 'ping') {
                                // Ignore ping messages, they just keep the connection alive
                            } else if (data.type === 'session_id') {
                                if (currentSessionId !== data.content) {
                                    currentSessionId = data.content;
                                    headerSessionId.querySelector('.id-val').textContent = currentSessionId;
                                    updateSessionListSelection(currentSessionId);
                                }
                            } else if (data.type === 'thought') {
                                lastThoughtText += data.content;
                                updateThoughtBubble(thoughtBubble, lastThoughtText);
                            } else if (data.type === 'chunk') {
>>>>>>> REPLACE
