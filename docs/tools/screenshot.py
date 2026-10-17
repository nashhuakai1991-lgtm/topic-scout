"""用 bundled chromium 给报告截图（用于 GitHub README 配图）。"""
import sys, pathlib, asyncio
from playwright.async_api import async_playwright

CR = r"C:\Users\Administrator\AppData\Local\ms-playwright\chromium-1234\chrome-win64\chrome.exe"

async def shot(src, dst, h=1750, w=1280):
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=CR, headless=True)
        pg = await b.new_page(viewport={"width": w, "height": 900},
                              device_scale_factor=1)
        await pg.goto(pathlib.Path(src).as_uri(), wait_until="load", timeout=120000)
        await pg.wait_for_timeout(1500)
        await pg.screenshot(path=dst, clip={"x": 0, "y": 0, "width": w, "height": h})
        await b.close()
        print("OK", dst)

asyncio.run(shot(sys.argv[1], sys.argv[2]))
