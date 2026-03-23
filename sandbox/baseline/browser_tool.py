"""Browser research tools using Playwright — lazy-initialized, persistent page."""

from __future__ import annotations

import os
from urllib.parse import quote_plus


class BrowserTool:
    """Manages a persistent Playwright browser for web research.

    The browser only launches when first used (lazy init).
    A single page is reused across calls.
    """

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._page = None

    def _ensure_browser(self) -> None:
        """Launch browser on first use."""
        if self._page is not None:
            return
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        proxy_url = os.environ.get("HTTPS_PROXY", "")
        launch_args = ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
        launch_kwargs: dict = {
            "headless": True,
            "args": launch_args,
        }
        if proxy_url:
            launch_kwargs["proxy"] = {"server": proxy_url}
        self._browser = self._playwright.chromium.launch(**launch_kwargs)
        context = self._browser.new_context(ignore_https_errors=True)
        self._page = context.new_page()
        self._page.set_viewport_size({"width": 1280, "height": 800})

    def browse(self, url: str) -> dict:
        """Navigate to a URL and extract readable text."""
        try:
            self._ensure_browser()
            resp = self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
            status = resp.status if resp else 0
            title = self._page.title()
            text = self._extract_readable_text()
            return {"url": self._page.url, "title": title, "status": status, "text": text}
        except Exception as exc:
            return {"url": url, "error": str(exc)}

    def search(self, query: str, engine: str = "duckduckgo") -> dict:
        """Search the web and return structured results."""
        try:
            self._ensure_browser()
            encoded = quote_plus(query)
            if engine == "google":
                url = f"https://www.google.com/search?q={encoded}"
            else:
                url = f"https://duckduckgo.com/?q={encoded}"

            self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Wait for JS rendering
            self._page.wait_for_timeout(2000)

            if engine == "google":
                results = self._extract_google_results()
            else:
                results = self._extract_ddg_results()

            page_text = self._extract_readable_text()
            if len(page_text) > 5000:
                page_text = page_text[:5000]

            return {
                "query": query,
                "engine": engine,
                "url": self._page.url,
                "results": results[:10],
                "page_text": page_text,
            }
        except Exception as exc:
            return {"query": query, "engine": engine, "url": "", "error": str(exc)}

    def screenshot(self, path: str = "/workspace/agent/screenshot.png") -> str:
        """Save a screenshot for debugging."""
        try:
            self._ensure_browser()
            self._page.screenshot(path=path)
            return path
        except Exception as exc:
            return f"ERROR: {exc}"

    def _extract_readable_text(self) -> str:
        """Extract clean readable text from the current page."""
        js = """
        (() => {
            // Remove noisy elements
            const selectors = [
                'script', 'style', 'nav', 'footer', 'header', 'aside',
                'iframe', '.ad', '.ads', '.sidebar',
                '[role="navigation"]', '[role="banner"]'
            ];
            const cloned = document.cloneNode(true);
            selectors.forEach(sel => {
                cloned.querySelectorAll(sel).forEach(el => el.remove());
            });
            // Try content-focused selectors first
            const contentSelectors = ['main', 'article', '[role="main"]', '.content', '#content'];
            for (const sel of contentSelectors) {
                const el = cloned.querySelector(sel);
                if (el && el.innerText && el.innerText.trim().length > 100) {
                    return el.innerText.trim();
                }
            }
            return (cloned.body && cloned.body.innerText) ? cloned.body.innerText.trim() : '';
        })()
        """
        try:
            text = self._page.evaluate(js)
            # Clean whitespace
            lines = [line.strip() for line in text.split("\n")]
            text = "\n".join(line for line in lines if line)
            if len(text) > 15000:
                text = text[:15000]
            return text
        except Exception:
            return ""

    def _extract_ddg_results(self) -> list:
        """Best-effort extraction of DuckDuckGo search results."""
        js = """
        (() => {
            const results = [];
            // DuckDuckGo result selectors
            const items = document.querySelectorAll('[data-result="web"], .result, .web-result, article[data-testid="result"]');
            items.forEach(item => {
                const linkEl = item.querySelector('a[href]');
                const titleEl = item.querySelector('h2, h3, .result__title, [data-testid="result-title-a"]');
                const snippetEl = item.querySelector('.result__snippet, [data-result="snippet"], .E2eLOJl8HctVnDOl, span');
                if (linkEl) {
                    const href = linkEl.href;
                    if (href && !href.startsWith('javascript:') && !href.includes('duckduckgo.com')) {
                        results.push({
                            title: (titleEl ? titleEl.innerText : linkEl.innerText || '').trim(),
                            url: href,
                            snippet: snippetEl ? snippetEl.innerText.trim().slice(0, 300) : ''
                        });
                    }
                }
            });
            return results;
        })()
        """
        try:
            return self._page.evaluate(js) or []
        except Exception:
            return []

    def _extract_google_results(self) -> list:
        """Best-effort extraction of Google search results."""
        js = """
        (() => {
            const results = [];
            const items = document.querySelectorAll('#search .g, #rso .g');
            items.forEach(item => {
                const linkEl = item.querySelector('a[href]');
                const titleEl = item.querySelector('h3');
                const snippetEl = item.querySelector('.VwiC3b, .IsZvec, span[style*="-webkit-line-clamp"]');
                if (linkEl && titleEl) {
                    const href = linkEl.href;
                    if (href && !href.startsWith('javascript:')) {
                        results.push({
                            title: titleEl.innerText.trim(),
                            url: href,
                            snippet: snippetEl ? snippetEl.innerText.trim().slice(0, 300) : ''
                        });
                    }
                }
            });
            return results;
        })()
        """
        try:
            return self._page.evaluate(js) or []
        except Exception:
            return []

    def close(self) -> None:
        """Clean up browser resources. Safe to call multiple times."""
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
            self._page = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
