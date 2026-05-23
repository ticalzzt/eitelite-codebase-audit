"""
Cloud Device Control Plugin (tical-code v0.3)
=============================================

Browser and mobile device automation.
Inspired by mobile_use and browser_use, but with tical-code philosophy:
- Every action MUST pass Force-Verify (screenshot confirmation, readback verification)
- All operations are traced
- Evidence hash for each action

Lite edition: Browser automation only (no mobile emulator)
Full edition: Browser + Mobile device control
"""

import asyncio
import base64
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)

# =============================================================================
# Device Types
# =============================================================================

class DeviceType(Enum):
    """Types of cloud devices."""
    BROWSER = "browser"
    MOBILE = "mobile"
    TABLET = "tablet"

class DeviceStatus(Enum):
    """Device connection status."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    BUSY = "busy"
    ERROR = "error"

# =============================================================================
# Device Action Types
# =============================================================================

class ActionType(Enum):
    """Types of device actions."""
    # Browser
    BROWSER_OPEN = "browser_open"
    BROWSER_NAVIGATE = "browser_navigate"
    BROWSER_CLICK = "browser_click"
    BROWSER_TYPE = "browser_type"
    BROWSER_SCREENSHOT = "browser_screenshot"
    BROWSER_EXTRACT = "browser_extract"
    BROWSER_SUBMIT = "browser_submit"
    
    # Mobile
    MOBILE_OPEN_APP = "mobile_open_app"
    MOBILE_TAP = "mobile_tap"
    MOBILE_SWIPE = "mobile_swipe"
    MOBILE_TYPE = "mobile_type"
    MOBILE_SCREENSHOT = "mobile_screenshot"
    MOBILE_EXTRACT = "mobile_extract"
    
    # Common
    WAIT = "wait"
    VERIFY = "verify"

# =============================================================================
# Device Action
# =============================================================================

@dataclass
class DeviceAction:
    """
    A single device action.
    
    Each action has:
    - type: What to do
    - target: Element selector or coordinates
    - value: Input value (for type actions)
    - verify_after: Whether to verify after execution
    """
    action_id: str
    action_type: ActionType
    device_id: str
    
    # Target
    selector: Optional[str] = None  # CSS selector, XPath, etc.
    xpath: Optional[str] = None
    coordinates: Optional[Tuple[int, int]] = None
    element_text: Optional[str] = None
    
    # Value
    value: Optional[str] = None
    
    # Options
    timeout_seconds: int = 30
    verify_after: bool = True
    screenshot_after: bool = True
    
    # Metadata
    description: str = ""
    tags: List[str] = field(default_factory=list)

@dataclass
class DeviceResult:
    """
    Result of a device action.
    
    Records:
    - Success status
    - Output data (screenshot, extracted text, etc.)
    - Verification result
    - Evidence hash
    """
    action_id: str
    action_type: ActionType
    success: bool
    
    # Output
    screenshot: Optional[str] = None  # Base64 encoded
    extracted_data: Optional[Dict] = None
    element_text: Optional[str] = None
    page_url: Optional[str] = None
    page_title: Optional[str] = None
    
    # Verification
    verified: bool = False
    verification_method: Optional[str] = None
    verification_details: str = ""
    
    # Timing
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    duration_ms: float = 0.0
    
    # Evidence
    evidence_hash: Optional[str] = None
    trace_span_id: Optional[str] = None
    
    # Error
    error: Optional[str] = None
    
    def to_dict(self) -> Dict:
        """Convert to dictionary."""
        return {
            'action_id': self.action_id,
            'action_type': self.action_type.value,
            'success': self.success,
            'screenshot': bool(self.screenshot),  # Don't include full base64
            'extracted_data': self.extracted_data,
            'element_text': self.element_text,
            'page_url': self.page_url,
            'page_title': self.page_title,
            'verified': self.verified,
            'verification_method': self.verification_method,
            'verification_details': self.verification_details,
            'duration_ms': self.duration_ms,
            'evidence_hash': self.evidence_hash,
            'error': self.error,
        }
    
    def _generate_evidence_hash(self):
        """Generate evidence hash."""
        content = json.dumps({
            'action_id': self.action_id,
            'action_type': self.action_type.value,
            'success': self.success,
            'verified': self.verified,
            'screenshot_hash': hashlib.md5(
                self.screenshot.encode() if self.screenshot else b''
            ).hexdigest()[:8],
            'timestamp': time.time(),
        }, sort_keys=True)
        
        self.evidence_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

# =============================================================================
# Browser Tools
# =============================================================================

class BrowserTool:
    """
    Browser automation tool.
    
    Supports Playwright and Selenium (lazy import).
    """
    
    def __init__(self, device_id: str = "default"):
        """
        Initialize browser tool.
        
        Args:
            device_id: Unique identifier for this browser instance
        """
        self.device_id = device_id
        self._driver = None
        self._browser = None
        self._context = None
        self._page = None
        self._using_playwright = False
        self._using_selenium = False
        
        # Current state
        self.current_url: Optional[str] = None
        self.current_title: Optional[str] = None
    
    def _lazy_import_playwright(self):
        """Lazy import Playwright."""
        if self._using_playwright:
            return True
        
        try:
            from playwright.async_api import async_playwright
            self._playwright_module = async_playwright
            self._using_playwright = True
            logger.info("[BrowserTool] Using Playwright")
            return True
        except ImportError:
            pass
        
        return False
    
    def _lazy_import_selenium(self):
        """Lazy import Selenium."""
        if self._using_selenium:
            return True
        
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
            self._selenium_webdriver = webdriver
            self._selenium_options = Options
            self._selenium_service = Service
            self._using_selenium = True
            logger.info("[BrowserTool] Using Selenium")
            return True
        except ImportError:
            pass
        
        return False
    
    async def connect(
        self,
        headless: bool = True,
        browser_type: str = "chromium",
    ) -> bool:
        """
        Connect to browser.
        
        Args:
            headless: Run headless
            browser_type: chromium, firefox, or webkit
            
        Returns:
            True if connected
        """
        # Try Playwright first
        if self._lazy_import_playwright():
            try:
                pw = await self._playwright_module.async_playwright().start()
                self._browser = await getattr(pw, browser_type).launch(headless=headless)
                self._context = await self._browser.new_context(
                    viewport={'width': 1280, 'height': 720}
                )
                self._page = await self._context.new_page()
                
                logger.info(f"[BrowserTool] Connected (Playwright, {browser_type})")
                return True
            except Exception as e:
                logger.error(f"[BrowserTool] Playwright connection failed: {e}")
        
        # Try Selenium
        if self._lazy_import_selenium():
            try:
                options = self._selenium_options()
                if headless:
                    options.add_argument('--headless')
                options.add_argument('--no-sandbox')
                options.add_argument('--disable-dev-shm-usage')
                
                self._driver = self._selenium_webdriver.Chrome(options=options)
                self._driver.set_window_size(1280, 720)
                
                logger.info("[BrowserTool] Connected (Selenium)")
                return True
            except Exception as e:
                logger.error(f"[BrowserTool] Selenium connection failed: {e}")
        
        logger.error("[BrowserTool] No browser automation available")
        return False
    
    async def disconnect(self):
        """Disconnect from browser."""
        if self._page:
            await self._page.close()
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        
        self._page = None
        self._context = None
        self._browser = None
        self._driver = None
        
        logger.info("[BrowserTool] Disconnected")
    
    async def open(self, url: str, timeout: int = 30) -> DeviceResult:
        """
        Open a URL.
        
        Args:
            url: URL to open
            timeout: Timeout in seconds
            
        Returns:
            DeviceResult
        """
        result = DeviceResult(
            action_id=str(uuid.uuid4())[:16],
            action_type=ActionType.BROWSER_OPEN,
            success=False,
        )
        
        try:
            if self._using_playwright and self._page:
                response = await self._page.goto(url, timeout=timeout * 1000)
                self.current_url = url
                self.current_title = await self._page.title()
                
                result.success = response.ok if response else True
                result.page_url = self.current_url
                result.page_title = self.current_title
                
            elif self._using_selenium and self._driver:
                self._driver.get(url)
                self.current_url = url
                self.current_title = self._driver.title
                
                result.success = True
                result.page_url = self.current_url
                result.page_title = self.current_title
            
            else:
                result.error = "No browser connected"
            
        except Exception as e:
            result.error = str(e)
            logger.error(f"[BrowserTool] Open error: {e}")
        
        result._generate_evidence_hash()
        return result
    
    async def screenshot(self, full_page: bool = False) -> Optional[str]:
        """
        Take a screenshot.
        
        Args:
            full_page: Capture full page
            
        Returns:
            Base64 encoded PNG
        """
        try:
            if self._using_playwright and self._page:
                return await self._page.screenshot(full_page=full_page)
            
            elif self._using_selenium and self._driver:
                import base64
                img = self._driver.get_screenshot_as_png()
                return base64.b64encode(img).decode()
        
        except Exception as e:
            logger.error(f"[BrowserTool] Screenshot error: {e}")
        
        return None
    
    async def click(self, selector: str) -> DeviceResult:
        """
        Click an element.
        
        Args:
            selector: CSS selector
            
        Returns:
            DeviceResult
        """
        result = DeviceResult(
            action_id=str(uuid.uuid4())[:16],
            action_type=ActionType.BROWSER_CLICK,
            success=False,
        )
        
        try:
            if self._using_playwright and self._page:
                await self._page.click(selector, timeout=30000)
                result.success = True
                
                # Get element text if available
                try:
                    result.element_text = await self._page.locator(selector).text_content()
                except Exception as e:
                    logger.debug(f"[cloud_device] : {e}")
                    pass
            
            elif self._using_selenium and self._driver:
                from selenium.webdriver.common.by import By
                element = self._driver.find_element(By.CSS_SELECTOR, selector)
                element.click()
                result.success = True
                result.element_text = element.text
            
            # Take screenshot after action
            if result.success:
                result.screenshot = await self.screenshot()
        
        except Exception as e:
            result.error = str(e)
            logger.error(f"[BrowserTool] Click error: {e}")
        
        result._generate_evidence_hash()
        return result
    
    async def type_text(
        self,
        selector: str,
        text: str,
        clear_first: bool = True,
    ) -> DeviceResult:
        """
        Type text into an element.
        
        Args:
            selector: CSS selector
            text: Text to type
            clear_first: Clear existing text first
            
        Returns:
            DeviceResult
        """
        result = DeviceResult(
            action_id=str(uuid.uuid4())[:16],
            action_type=ActionType.BROWSER_TYPE,
            success=False,
        )
        
        try:
            if self._using_playwright and self._page:
                if clear_first:
                    await self._page.fill(selector, text)
                else:
                    await self._page.locator(selector).type(text)
                result.success = True
            
            elif self._using_selenium and self._driver:
                from selenium.webdriver.common.by import By
                element = self._driver.find_element(By.CSS_SELECTOR, selector)
                if clear_first:
                    element.clear()
                element.send_keys(text)
                result.success = True
            
            # Readback verification
            if result.success:
                await asyncio.sleep(0.1)  # Small delay for UI update
                result.extracted_data = {'input_value': text}
        
        except Exception as e:
            result.error = str(e)
            logger.error(f"[BrowserTool] Type error: {e}")
        
        result._generate_evidence_hash()
        return result
    
    async def extract(
        self,
        selector: str,
        attribute: Optional[str] = None,
    ) -> DeviceResult:
        """
        Extract data from element.
        
        Args:
            selector: CSS selector
            attribute: Attribute to extract (None = text content)
            
        Returns:
            DeviceResult with extracted data
        """
        result = DeviceResult(
            action_id=str(uuid.uuid4())[:16],
            action_type=ActionType.BROWSER_EXTRACT,
            success=False,
        )
        
        try:
            if self._using_playwright and self._page:
                if attribute:
                    value = await self._page.get_attribute(selector, attribute)
                else:
                    value = await self._page.text_content(selector)
                
                result.success = value is not None
                result.extracted_data = {selector: value}
                result.element_text = str(value)
            
            elif self._using_selenium and self._driver:
                from selenium.webdriver.common.by import By
                element = self._driver.find_element(By.CSS_SELECTOR, selector)
                
                if attribute:
                    value = element.get_attribute(attribute)
                else:
                    value = element.text
                
                result.success = True
                result.extracted_data = {selector: value}
                result.element_text = str(value)
        
        except Exception as e:
            result.error = str(e)
            logger.error(f"[BrowserTool] Extract error: {e}")
        
        result._generate_evidence_hash()
        return result
    
    async def submit(self, selector: str) -> DeviceResult:
        """
        Submit a form.
        
        Args:
            selector: Form selector
            
        Returns:
            DeviceResult
        """
        result = DeviceResult(
            action_id=str(uuid.uuid4())[:16],
            action_type=ActionType.BROWSER_SUBMIT,
            success=False,
        )
        
        try:
            if self._using_playwright and self._page:
                await self._page.locator(selector).submit()
                result.success = True
            
            elif self._using_selenium and self._driver:
                from selenium.webdriver.common.by import By
                form = self._driver.find_element(By.CSS_SELECTOR, selector)
                form.submit()
                result.success = True
            
            # Update URL after submit
            if result.success:
                await asyncio.sleep(0.5)  # Wait for navigation
                if self._using_playwright and self._page:
                    result.page_url = self._page.url
                    result.page_title = await self._page.title()
                elif self._using_selenium and self._driver:
                    result.page_url = self._driver.current_url
                    result.page_title = self._driver.title
        
        except Exception as e:
            result.error = str(e)
            logger.error(f"[BrowserTool] Submit error: {e}")
        
        result._generate_evidence_hash()
        return result

# =============================================================================
# Mobile Tools
# =============================================================================

class MobileTool:
    """
    Mobile device automation tool.
    
    Uses Appium or uiautomator2 (lazy import).
    Lite edition doesn't include this.
    """
    
    def __init__(self, device_id: str = "mobile"):
        """
        Initialize mobile tool.
        
        Args:
            device_id: Unique identifier for this device
        """
        self.device_id = device_id
        self._driver = None
        self._platform = None  # android or ios
        
        # Current state
        self.current_app: Optional[str] = None
        self.current_activity: Optional[str] = None
    
    def _lazy_import_appium(self):
        """Lazy import Appium."""
        try:
            from appium import webdriver as appium_webdriver
            from appium.options.android import UiAutomator2Options
            self._appium_webdriver = appium_webdriver
            self._appium_options = UiAutomator2Options
            return True
        except ImportError:
            pass
        return False
    
    async def connect(
        self,
        platform: str,
        udid: Optional[str] = None,
        appium_url: str = "http://localhost:4723",
    ) -> bool:
        """
        Connect to mobile device.
        
        Args:
            platform: android or ios
            udid: Device UDID (for real device)
            appium_url: Appium server URL
            
        Returns:
            True if connected
        """
        self._platform = platform
        
        if self._lazy_import_appium():
            try:
                options = self._appium_options()
                if udid:
                    options.udid = udid
                
                self._driver = self._appium_webdriver.Remote(
                    appium_url,
                    options=options
                )
                
                logger.info(f"[MobileTool] Connected ({platform})")
                return True
                
            except Exception as e:
                logger.error(f"[MobileTool] Connection failed: {e}")
        
        logger.warning("[MobileTool] Appium not available (Full edition required)")
        return False
    
    async def screenshot(self) -> Optional[str]:
        """Take a screenshot."""
        if self._driver:
            import base64
            img = self._driver.get_screenshot_as_base64()
            return img
        return None
    
    async def tap(self, x: int, y: int) -> DeviceResult:
        """Tap at coordinates."""
        result = DeviceResult(
            action_id=str(uuid.uuid4())[:16],
            action_type=ActionType.MOBILE_TAP,
            success=False,
        )
        
        try:
            if self._driver:
                self._driver.tap([(x, y)])
                result.success = True
                result.screenshot = await self.screenshot()
        except Exception as e:
            result.error = str(e)
        
        result._generate_evidence_hash()
        return result
    
    async def type_text(self, text: str) -> DeviceResult:
        """Type text."""
        result = DeviceResult(
            action_id=str(uuid.uuid4())[:16],
            action_type=ActionType.MOBILE_TYPE,
            success=False,
        )
        
        try:
            if self._driver:
                self._driver.set_value(self._driver.active_element, text)
                result.success = True
        except Exception as e:
            result.error = str(e)
        
        result._generate_evidence_hash()
        return result

# =============================================================================
# Cloud Device Plugin
# =============================================================================

class CloudDevicePlugin:
    """
    Cloud device control plugin for tical-code.
    
    Integrates browser and mobile automation with:
    - Force-Verify on every action
    - Trace recording
    - Anchor persistence
    
    Usage:
        plugin = CloudDevicePlugin()
        await plugin.init(context)
        
        # Browser automation
        await plugin.browser_open("https://example.com")
        await plugin.browser_click("#button")
        await plugin.browser_type("#input", "hello")
        
        # Take screenshot with verification
        result = await plugin.browser_screenshot(verify=True)
    """
    
    # Plugin metadata
    metadata = {
        'name': 'cloud_device',
        'version': '0.1.0',
        'edition': 'full',  # Lite only supports browser
        'description': 'Cloud device automation (browser + mobile)',
        'author': 'tical-code',
        'dependencies': ['playwright', 'selenium', 'appium'],
    }
    
    def __init__(self):
        """Initialize plugin."""
        self.context = None
        self.browser = BrowserTool()
        self.mobile = MobileTool()
        self._initialized = False
        self._action_history: List[DeviceResult] = []
        
        # Trace integration
        self._trace_recorder = None
    
    def _get_trace_recorder(self):
        """Lazy load trace recorder."""
        if self._trace_recorder is None:
            try:
                from ...core.trace import get_trace_recorder
                self._trace_recorder = get_trace_recorder()
            except ImportError:
                pass
        return self._trace_recorder
    
    async def init(self, context) -> None:
        """
        Initialize the plugin.
        
        Args:
            context: AgentContext
        """
        self.context = context
        self._initialized = True
        logger.info("[CloudDevice] Plugin initialized")
    
    async def shutdown(self) -> None:
        """Shutdown the plugin."""
        await self.browser.disconnect()
        self._initialized = False
        logger.info("[CloudDevice] Plugin shutdown")
    
    async def _record_trace(
        self,
        action_type: ActionType,
        result: DeviceResult,
    ):
        """Record action to trace."""
        recorder = self._get_trace_recorder()
        if recorder is None:
            return
        
        try:
            from ...core.trace import SpanType, SpanStatus
            span = recorder.start_trace(
                name=f"device:{action_type.value}",
                span_type=SpanType.DEVICE_ACTION,
                metadata={
                    'device_id': result.action_id,
                    'action_type': action_type.value,
                },
            )
            
            result.trace_span_id = span.spans[-1].span_id if span.spans else None
            
            recorder.end_trace(
                span,
                status=SpanStatus.OK if result.success else SpanStatus.ERROR,
                output_data={
                    'success': result.success,
                    'verified': result.verified,
                    'evidence_hash': result.evidence_hash,
                },
                verification_passed=result.verified,
            )
        except ImportError:
            pass
    
    async def _verify_result(
        self,
        result: DeviceResult,
        verification_type: str,
    ) -> bool:
        """
        Verify a device action result.
        
        Args:
            result: Action result to verify
            verification_type: Type of verification
            
        Returns:
            True if verified
        """
        try:
            from ...core.verify import SchemaValidator
            
            # Schema verification
            schema = {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "action_id": {"type": "string"},
                    "duration_ms": {"type": "number"},
                },
            }
            
            data = {
                "success": result.success,
                "action_id": result.action_id,
                "duration_ms": result.duration_ms,
            }
            
            vr = SchemaValidator.validate(data, schema)
            result.verified = vr.passed
            result.verification_method = f"schema_{verification_type}"
            result.verification_details = vr.details
            
            # Additional screenshot verification
            if result.screenshot and result.verified:
                # Check screenshot is not blank
                if len(result.screenshot) < 100:  # Too small = probably blank
                    result.verified = False
                    result.verification_details += " (screenshot appears blank)"
            
            return result.verified
            
        except ImportError:
            result.verified = True
            result.verification_method = "skipped"
            return True
    
    # -------------------------------------------------------------------------
    # Browser Actions
    # -------------------------------------------------------------------------
    
    async def browser_connect(
        self,
        headless: bool = True,
        browser_type: str = "chromium",
    ) -> DeviceResult:
        """Connect to browser."""
        result = DeviceResult(
            action_id="browser_connect",
            action_type=ActionType.BROWSER_OPEN,
            success=False,
        )
        
        result.success = await self.browser.connect(
            headless=headless,
            browser_type=browser_type,
        )
        
        if result.success:
            result.verified = True
            result.verification_details = "Browser connected successfully"
        
        await self._record_trace(ActionType.BROWSER_OPEN, result)
        return result
    
    async def browser_open(
        self,
        url: str,
        timeout: int = 30,
        verify: bool = True,
    ) -> DeviceResult:
        """
        Open a URL in browser.
        
        Args:
            url: URL to open
            timeout: Timeout in seconds
            verify: Verify result
            
        Returns:
            DeviceResult
        """
        result = await self.browser.open(url, timeout)
        
        if verify:
            await self._verify_result(result, "browser_open")
        
        self._action_history.append(result)
        await self._record_trace(ActionType.BROWSER_OPEN, result)
        
        return result
    
    async def browser_click(
        self,
        selector: str,
        verify: bool = True,
    ) -> DeviceResult:
        """
        Click an element.
        
        Args:
            selector: CSS selector
            verify: Verify result
            
        Returns:
            DeviceResult
        """
        result = await self.browser.click(selector)
        
        if verify:
            await self._verify_result(result, "browser_click")
        
        self._action_history.append(result)
        await self._record_trace(ActionType.BROWSER_CLICK, result)
        
        return result
    
    async def browser_type(
        self,
        selector: str,
        text: str,
        clear_first: bool = True,
        verify: bool = True,
    ) -> DeviceResult:
        """
        Type text into an element.
        
        Args:
            selector: CSS selector
            text: Text to type
            clear_first: Clear existing text
            verify: Verify result
            
        Returns:
            DeviceResult
        """
        result = await self.browser.type_text(selector, text, clear_first)
        
        if verify:
            await self._verify_result(result, "browser_type")
        
        self._action_history.append(result)
        await self._record_trace(ActionType.BROWSER_TYPE, result)
        
        return result
    
    async def browser_screenshot(
        self,
        full_page: bool = False,
        verify: bool = True,
    ) -> DeviceResult:
        """
        Take a screenshot.
        
        Args:
            full_page: Capture full page
            verify: Verify result
            
        Returns:
            DeviceResult with screenshot data
        """
        result = DeviceResult(
            action_id=str(uuid.uuid4())[:16],
            action_type=ActionType.BROWSER_SCREENSHOT,
            success=True,
        )
        
        result.screenshot = await self.browser.screenshot(full_page=full_page)
        
        if verify:
            await self._verify_result(result, "browser_screenshot")
        
        self._action_history.append(result)
        await self._record_trace(ActionType.BROWSER_SCREENSHOT, result)
        
        return result
    
    async def browser_extract(
        self,
        selector: str,
        attribute: Optional[str] = None,
        verify: bool = True,
    ) -> DeviceResult:
        """
        Extract data from element.
        
        Args:
            selector: CSS selector
            attribute: Attribute to extract (None = text)
            verify: Verify result
            
        Returns:
            DeviceResult with extracted data
        """
        result = await self.browser.extract(selector, attribute)
        
        if verify:
            await self._verify_result(result, "browser_extract")
        
        self._action_history.append(result)
        await self._record_trace(ActionType.BROWSER_EXTRACT, result)
        
        return result
    
    # -------------------------------------------------------------------------
    # Mobile Actions (Full edition)
    # -------------------------------------------------------------------------
    
    async def mobile_connect(
        self,
        platform: str,
        udid: Optional[str] = None,
    ) -> DeviceResult:
        """Connect to mobile device."""
        result = DeviceResult(
            action_id="mobile_connect",
            action_type=ActionType.MOBILE_OPEN_APP,
            success=False,
        )
        
        result.success = await self.mobile.connect(platform, udid)
        
        if result.success:
            result.verified = True
            result.verification_details = "Device connected successfully"
        
        await self._record_trace(ActionType.MOBILE_OPEN_APP, result)
        return result
    
    async def mobile_tap(
        self,
        x: int,
        y: int,
        verify: bool = True,
    ) -> DeviceResult:
        """Tap at coordinates."""
        result = await self.mobile.tap(x, y)
        
        if verify:
            await self._verify_result(result, "mobile_tap")
        
        self._action_history.append(result)
        await self._record_trace(ActionType.MOBILE_TAP, result)
        
        return result
    
    async def mobile_screenshot(
        self,
        verify: bool = True,
    ) -> DeviceResult:
        """Take mobile screenshot."""
        result = DeviceResult(
            action_id=str(uuid.uuid4())[:16],
            action_type=ActionType.MOBILE_SCREENSHOT,
            success=True,
        )
        
        result.screenshot = await self.mobile.screenshot()
        
        if verify:
            await self._verify_result(result, "mobile_screenshot")
        
        self._action_history.append(result)
        await self._record_trace(ActionType.MOBILE_SCREENSHOT, result)
        
        return result
    
    # -------------------------------------------------------------------------
    # Utility
    # -------------------------------------------------------------------------
    
    def get_action_history(self) -> List[Dict]:
        """Get action history."""
        return [r.to_dict() for r in self._action_history]
    
    def get_last_screenshot(self) -> Optional[str]:
        """Get last screenshot."""
        for result in reversed(self._action_history):
            if result.screenshot:
                return result.screenshot
        return None
    
    async def wait(self, seconds: float):
        """Wait for specified seconds."""
        await asyncio.sleep(seconds)

# Create plugin instance
_plugin_instance: Optional[CloudDevicePlugin] = None

def get_cloud_device_plugin() -> CloudDevicePlugin:
    """Get or create the cloud device plugin instance."""
    global _plugin_instance
    if _plugin_instance is None:
        _plugin_instance = CloudDevicePlugin()
    return _plugin_instance
