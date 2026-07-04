<<<<<<< SEARCH
                except asyncio.TimeoutError:
                    if task.done() and queue.empty():
                        break
                    # Send a keep-alive line to prevent HTTP connection timeout
                    yield ": keep-alive\n\n"
=======
                except asyncio.TimeoutError:
                    if task.done() and queue.empty():
                        break
                    # Send a keep-alive line to prevent HTTP connection timeout
                    yield f"data: {json.dumps({'type': 'ping', 'content': 'ping'})}\n\n"
>>>>>>> REPLACE
