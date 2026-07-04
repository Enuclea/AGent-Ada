<<<<<<< SEARCH
                    # Stream thoughts
                    thoughts_str = ""
                    async for thought in response.thoughts:
                        thoughts_str += thought
                        if thought:
                            thoughts_emitted = True
                            await queue.put({"type": "thought", "content": thought})

                    if thoughts_str:
                        memory.log_conversation_step(active_agent.conversation_id, "thought", thoughts_str)

                    # Stream response chunks
                    output_content = ""
                    async for chunk in response:
                        output_content += chunk
                        if chunk:
                            text_chunks_emitted = True
                            await queue.put({"type": "chunk", "content": chunk})

                    if output_content:
=======
                    from agent.tools import yield_requested
                    # Stream thoughts
                    thoughts_str = ""
                    async for thought in response.thoughts:
                        thoughts_str += thought
                        if thought:
                            thoughts_emitted = True
                            await queue.put({"type": "thought", "content": thought})
                        if yield_requested.get():
                            break

                    if thoughts_str:
                        memory.log_conversation_step(active_agent.conversation_id, "thought", thoughts_str)

                    # Stream response chunks
                    output_content = ""
                    async for chunk in response:
                        output_content += chunk
                        if chunk:
                            text_chunks_emitted = True
                            await queue.put({"type": "chunk", "content": chunk})
                        if yield_requested.get():
                            break

                    if output_content:
>>>>>>> REPLACE
