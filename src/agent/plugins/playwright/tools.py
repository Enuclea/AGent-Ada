import uuid
import asyncio
from pathlib import Path
from typing import Optional
from agent.plugins.playwright.routes import SCREENSHOTS_DIR

async def playwright_browse_url(url: str, selector: Optional[str] = None, wait_time: int = 0) -> str:
    """Browse a URL using Playwright, extract clean text, and take a screenshot.
    
    This enables full browser automation, including rendering JavaScript, supporting single-page apps (SPAs),
    and capturing the visual layout.
    
    Args:
        url: The web page URL to load. Must start with http:// or https://.
        selector: Optional CSS selector of a specific element to wait for or capture a screenshot of.
        wait_time: Optional seconds to wait after navigation (useful for animations or lazy-loaded components).
    """
    from playwright.async_api import async_playwright
    if not url.startswith(("http://", "https://")):
        return "Error: Invalid URL. It must start with http:// or https://"

    try:
        async with async_playwright() as p:
            # Launch headless chromium
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            
            # Set a standard desktop viewport
            await page.set_viewport_size({"width": 1280, "height": 800})
            
            # Navigate to the URL
            try:
                await page.goto(url, wait_until="load", timeout=30000)
            except Exception as e:
                await browser.close()
                return f"Error: Failed to load page: {e}"
            
            # Optional additional wait time
            if wait_time > 0:
                await asyncio.sleep(min(wait_time, 15))  # Cap wait time at 15s to prevent timeouts
            
            # Optional selector waiting
            if selector:
                try:
                    await page.wait_for_selector(selector, timeout=5000)
                except Exception:
                    pass  # Proceed even if element did not appear
            
            # Extract metadata and content
            title = await page.title() or "No Title"
            loaded_url = page.url
            
            # Extract clean text using innerText
            try:
                text_content = await page.evaluate("document.body.innerText")
            except Exception:
                text_content = "Failed to extract text content."
                
            # Take screenshot and save to the shared persistent directory
            screenshot_filename = f"screenshot_{uuid.uuid4().hex}.png"
            screenshot_path = SCREENSHOTS_DIR / screenshot_filename
            
            screenshot_success = False
            screenshot_error_msg = ""
            
            try:
                if selector:
                    element = await page.query_selector(selector)
                    if element:
                        await element.screenshot(path=str(screenshot_path))
                        screenshot_success = True
                    else:
                        # Fallback to full page if selector element not found
                        await page.screenshot(path=str(screenshot_path), full_page=False)
                        screenshot_success = True
                else:
                    await page.screenshot(path=str(screenshot_path), full_page=False)
                    screenshot_success = True
            except Exception as se:
                screenshot_error_msg = str(se)
            
            await browser.close()

            # Format result markdown
            output = []
            output.append(f"# {title}")
            output.append(f"**Loaded URL**: {loaded_url}")
            
            if screenshot_success:
                # Expose the relative web server URL for screenshot viewing
                screenshot_url = f"/api/playwright/screenshot/{screenshot_filename}"
                output.append(f"**Screenshot**: [View Screenshot]({screenshot_url})")
                output.append(f"![Screenshot]({screenshot_url})")
            else:
                output.append(f"**Screenshot Status**: Failed to capture screenshot ({screenshot_error_msg})")
            
            output.append("\n## Page Content")
            
            # Truncate content to avoid context token bloat
            max_len = 25000
            if len(text_content) > max_len:
                output.append(text_content[:max_len] + "\n\n... [Content Truncated due to length] ...")
            else:
                output.append(text_content)
                
            return "\n".join(output)
            
    except Exception as e:
        return f"Error running Playwright browser automation: {e}"
