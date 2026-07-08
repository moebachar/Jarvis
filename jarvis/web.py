"""A lazily-launched, visible Chrome that Jarvis can drive as a web-browsing fallback.

When WebFetch / WebSearch can't retrieve what Jarvis needs — a JavaScript-rendered page, a
page that blocks simple fetches, or anything he must see fully rendered — the `browse` tool
loads it here in a real Chrome window the user can watch, then hands the rendered text back
to the brain. One persistent browser + one tab are reused across calls. A fresh browser
profile is used (NOT the user's signed-in Chrome), which is why logins don't carry over.

playwright is imported lazily (inside `_ensure`) so nothing here is required unless the tool
is actually used; install it with the `web` extra.
"""

from __future__ import annotations

import asyncio

_MAX_TEXT = 8000  # cap the text handed back to the brain (chars)


class BrowserSession:
    def __init__(self, headless: bool = False) -> None:
        self._headless = headless
        self._pw = None
        self._browser = None
        self._page = None
        self._lock = asyncio.Lock()  # serialize navigations (one shared tab)

    async def _ensure(self) -> None:
        if self._page is not None:
            return
        from playwright.async_api import async_playwright  # lazy — only when actually browsing

        self._pw = await async_playwright().start()
        # Prefer the user's real (visible) Chrome; fall back to Playwright's bundled Chromium.
        try:
            self._browser = await self._pw.chromium.launch(channel="chrome", headless=self._headless)
        except Exception:
            self._browser = await self._pw.chromium.launch(headless=self._headless)
        self._page = await self._browser.new_page()

    async def browse(self, url: str, wait_seconds: float = 0.0) -> dict:
        """Navigate to `url` in the live browser and return its rendered text."""
        url = (url or "").strip()
        if not url:
            return {"ok": False, "error": "no URL given"}
        if not url.startswith(("http://", "https://", "data:", "file:")):
            url = "https://" + url
        async with self._lock:
            try:
                await self._ensure()
            except Exception as exc:
                return {"ok": False, "error": f"couldn't start the browser: {exc}"}
            page = self._page
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                return {"ok": False, "error": f"navigation failed: {exc}"}
            try:
                if wait_seconds and wait_seconds > 0:
                    await page.wait_for_timeout(min(float(wait_seconds), 15.0) * 1000)
                else:
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass  # best-effort; many pages never fully idle
                title = await page.title()
                text = await page.evaluate("() => document.body ? document.body.innerText : ''")
                final_url = page.url
            except Exception as exc:
                return {"ok": False, "error": f"couldn't read the page: {exc}"}
            text = (text or "").strip()
            truncated = len(text) > _MAX_TEXT
            return {
                "ok": True, "title": title or "", "url": final_url,
                "text": text[:_MAX_TEXT], "truncated": truncated,
            }

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._page = None
