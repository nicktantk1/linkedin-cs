import asyncio
from playwright.async_api import async_playwright

async def debug():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,  # <-- shows the browser window
            slow_mo=500,     # slows actions down so you can follow along
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = await context.new_page()
        await page.goto("https://www.linkedin.com/jobs/view/4382290134/")
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(2000)

        input("Browser is open — inspect it now, press Enter to close...")
        await browser.close()

asyncio.run(debug())