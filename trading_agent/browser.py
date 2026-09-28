"""Everything that touches Playwright: launching the profile, reading the page, performing actions."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup
from playwright.async_api import BrowserContext, Locator, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .config import MAX_CLICKABLES, MAX_ELEMENT_DESCRIPTION, MAX_INPUT_FIELDS, AgentConfig

log = logging.getLogger("agent")

# Collected from the target element so guardrails see what a CSS selector like "#btn-3" really is.
# Password values are never read.
DESCRIBE_ELEMENT_JS = """el => {
  const form = el.closest('form');
  return [
    el.innerText,
    el.type === 'password' ? '' : el.value,
    el.getAttribute('aria-label'),
    el.getAttribute('title'),
    el.getAttribute('name'),
    el.getAttribute('placeholder'),
    el.id,
    el.getAttribute('href'),
    form && form.getAttribute('action'),
  ].filter(Boolean).join(' | ');
}"""

# Current values of the fillable fields in the target element's form.
FORM_VALUES_JS = """el => {
  const form = el.form || el.closest('form');
  if (!form) return [];
  return Array.from(form.elements)
    .filter(f => !['hidden', 'submit', 'button', 'image', 'reset', 'password'].includes(f.type))
    .filter(f => f.name || f.id || f.placeholder)
    .map(f => [f.name || f.id || f.placeholder, f.value]);
}"""

CLICKABLE_SELECTOR = "a[href], button, [role=button], [role=link], input[type=submit], input[type=button]"

# Only rendered elements with a readable label; hidden links are excluded like hidden text.
CLICKABLES_JS = """(els, limit) => els
  .filter(el => el.checkVisibility ? el.checkVisibility() : el.offsetParent !== null)
  .map(el => [(el.innerText || el.value || el.getAttribute('aria-label') || el.title || '').trim(),
              el.tagName === 'A' ? el.href : ''])
  .filter(([label]) => label)
  .slice(0, limit)"""


@dataclass
class PageState:
    url: str
    clickables: str
    inputs: str
    text: str


@dataclass
class Target:
    locator: Locator
    description: str
    form_fields: list[tuple[str, str]]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def launch_context(pw: Playwright, cfg: AgentConfig) -> BrowserContext:
    """Headed Chromium on a persistent profile so extension state (wallet vaults) survives restarts."""
    cfg.profile_dir.mkdir(parents=True, exist_ok=True)
    args: list[str] = []
    if cfg.extension_dirs:
        paths = ",".join(str(path) for path in cfg.extension_dirs)
        args += [f"--disable-extensions-except={paths}", f"--load-extension={paths}"]
    return await pw.chromium.launch_persistent_context(
        user_data_dir=str(cfg.profile_dir),
        headless=False,
        args=args,
        ignore_default_args=["--disable-extensions"],
        no_viewport=True,
    )


async def run_setup(cfg: AgentConfig) -> None:
    """Open the persistent profile for manual wallet import/unlock; the agent takes no actions."""
    async with async_playwright() as pw:
        context = await launch_context(pw, cfg)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(cfg.start_url, wait_until="domcontentloaded", timeout=cfg.navigation_timeout_ms)
            print(f"Setup mode: profile at {cfg.profile_dir}")
            print("Install/unlock your wallet extension in the browser window now.")
            try:
                await asyncio.to_thread(input, "Press Enter here when finished to save the profile and exit... ")
            except EOFError:
                pass
        finally:
            await context.close()


async def wait_for_network_idle(page: Page, timeout_ms: int) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        log.info("Network not idle after %d ms; continuing.", timeout_ms)
    except PlaywrightError as exc:
        log.warning("Load-state wait failed: %s", exc.message.splitlines()[0])


class WalletPopups:
    """Finds wallet extension windows (e.g. MetaMask confirmations) so the agent can pause while you use them.
    The agent only watches whether these windows are open; it never reads or clicks inside them."""

    def __init__(self, context: BrowserContext) -> None:
        self.context = context
        self._baseline: set[int] = set()

    def mark(self) -> None:
        """Remember the windows open right now; extension windows opened after this count as popups."""
        self._baseline = {id(p) for p in self.context.pages}

    def open_popups(self) -> list[Page]:
        """Wallet windows opened since mark(). Windows the wallet opened on its own at browser start are ignored."""
        return [p for p in self.context.pages
                if not p.is_closed() and p.url.startswith("chrome-extension://") and id(p) not in self._baseline]

    async def bring_to_front(self) -> None:
        """Raise the wallet window above everything so it is obvious where to act. Focus only: nothing is read or clicked."""
        for popup in self.open_popups():
            try:
                await popup.bring_to_front()
            except PlaywrightError:
                pass

    async def wait_for_user(self, appear_s: float, max_wait_s: float, should_stop, on_open, on_closed) -> bool:
        """Wait up to appear_s for a popup; while any is open, wait (up to max_wait_s) for you to close it.
        Popups that open one after another (unlock, connect, switch network, confirm) are all waited for.
        Returns True if any popup was seen."""
        seen = False
        loop = asyncio.get_running_loop()
        give_up = loop.time() + max_wait_s
        while True:
            appear_by = loop.time() + appear_s
            while not self.open_popups() and loop.time() < appear_by:
                await asyncio.sleep(0.25)
            if not self.open_popups():
                return seen
            seen = True
            on_open()
            await self.bring_to_front()
            while self.open_popups() and loop.time() < give_up and not should_stop():
                await asyncio.sleep(0.5)
            on_closed(timed_out=bool(self.open_popups()))
            if self.open_popups() or should_stop():
                return seen
            appear_s = min(appear_s, 3.0) or 1.0  # a follow-up popup usually opens within a second or two


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------

def clean_text(text: str, max_chars: int) -> str:
    lines = (re.sub(r"\s+", " ", line).strip() for line in text.splitlines())
    cleaned = "\n".join(line for line in lines if line)
    return cleaned if len(cleaned) <= max_chars else cleaned[:max_chars] + "\n[...truncated]"


def css_selector_for(element) -> str | None:
    for attr in ("id", "name", "placeholder", "aria-label"):
        value = element.get(attr)
        if isinstance(value, str) and value.strip():
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            return f'{element.name}[{attr}="{escaped}"]'
    return None


def extract_input_fields(html: str, limit: int = MAX_INPUT_FIELDS) -> str:
    """Fillable form fields with stable CSS selectors so the LLM can issue TYPE actions."""
    soup = BeautifulSoup(html, "html.parser")
    fields: list[str] = []
    for element in soup.find_all(["input", "textarea", "select"]):
        input_type = str(element.get("type") or element.name).lower()
        if input_type in {"hidden", "submit", "button", "image", "reset"}:
            continue
        if element.has_attr("hidden") or element.has_attr("disabled"):
            continue
        selector = css_selector_for(element)
        if selector is None:
            continue
        label = element.get("aria-label") or element.get("placeholder") or element.get("name") or ""
        fields.append(f"- {selector} (type={input_type}, label={label!r})")
        if len(fields) >= limit:
            break
    return "\n".join(fields) or "(none)"


async def extract_clickables(page: Page, limit: int = MAX_CLICKABLES) -> str:
    """Visible links and buttons so the LLM knows which page text is clickable."""
    try:
        items = await page.eval_on_selector_all(CLICKABLE_SELECTOR, CLICKABLES_JS, limit)
    except PlaywrightError:
        return "(unavailable)"
    lines = []
    for label, href in items:
        label = re.sub(r"\s+", " ", label).strip()[:80]
        lines.append(f'- "{label}" -> {href}' if href else f'- "{label}" (button)')
    return "\n".join(lines) or "(none)"


async def observe(page: Page, max_chars: int) -> PageState:
    try:
        html = await page.content()
    except PlaywrightError:
        html = ""
    try:
        # innerText only returns rendered text, so CSS-hidden prompt-injection text is excluded.
        visible = await page.locator("body").inner_text(timeout=5000)
    except PlaywrightError:
        visible = BeautifulSoup(html, "html.parser").get_text("\n")
    return PageState(page.url, await extract_clickables(page), extract_input_fields(html), clean_text(visible, max_chars))


# ---------------------------------------------------------------------------
# Targets and actions
# ---------------------------------------------------------------------------

CONTAINS_RE = re.compile(r""":contains\(\s*(?:(['"])(.*?)\1|([^)'"]*?))\s*\)""")


def normalize_selector(selector: str) -> str:
    """Rewrite jQuery's :contains('text'), which browsers reject, as Playwright's :has-text("text")."""
    def to_has_text(match: re.Match) -> str:
        text = match.group(2) if match.group(1) else match.group(3)
        return ':has-text("' + text.replace("\\", "\\\\").replace('"', '\\"') + '")'
    return CONTAINS_RE.sub(to_has_text, selector)


async def resolve_locator(page: Page, selector: str) -> Locator | None:
    """Resolve a CSS selector or visible text/label/placeholder to the first visible element."""
    css = normalize_selector(selector)
    builders = (
        lambda: page.locator(css),
        lambda: page.get_by_text(selector, exact=True),
        lambda: page.get_by_text(selector),
        lambda: page.get_by_label(selector),
        lambda: page.get_by_placeholder(selector),
    )
    for build in builders:
        try:
            locator = build().filter(visible=True)
            if await locator.count():
                return locator.first
        except PlaywrightError:
            continue
    return None


async def resolve_target(page: Page, selector: str) -> Target | None:
    locator = await resolve_locator(page, selector)
    if locator is None:
        return None
    try:
        description = await locator.evaluate(DESCRIBE_ELEMENT_JS)
        fields = await locator.evaluate(FORM_VALUES_JS)
    except PlaywrightError:
        description, fields = "", []
    description = re.sub(r"\s+", " ", str(description)).strip()[:MAX_ELEMENT_DESCRIPTION]
    return Target(locator, description, [(str(name), str(value)) for name, value in fields])


async def perform(page: Page, action: dict, target: Target | None, cfg: AgentConfig) -> str | None:
    """Run one action. Returns None on success or a FAILED message."""
    kind = action["action"]
    try:
        if kind == "GOTO":
            await page.goto(action["url"], wait_until="domcontentloaded", timeout=cfg.navigation_timeout_ms)
        elif kind == "CLICK":
            await target.locator.click(timeout=cfg.action_timeout_ms)
        elif kind == "TYPE":
            await target.locator.fill(action["text"], timeout=cfg.action_timeout_ms)
        elif kind == "WAIT":
            await asyncio.sleep(action["seconds"])
    except PlaywrightTimeoutError:
        return f"FAILED: {kind} timed out."
    except PlaywrightError as exc:
        return f"FAILED: {kind} raised: {exc.message.splitlines()[0]}"
    await wait_for_network_idle(page, cfg.network_idle_timeout_ms)
    return None
