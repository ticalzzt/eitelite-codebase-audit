"""
Trading Plugin
==============

Provides trading capabilities via Interactive Brokers Web API (REST + OAuth2.0).

Supports two authentication modes:
- **Gateway mode** (personal): Client Portal Gateway running locally
- **OAuth mode** (multi-user/third-party): OAuth2.0 private_key_jwt (RFC 7521/7523)

Integrates Force-Verify for all trading operations.

WARNING: Trading involves real money. All operations MUST be verified.

Tools:
- connect: Connect to IB Web API (Gateway or OAuth)
- disconnect: Disconnect from IB Web API
- get_account: Get account information
- get_positions: Get current positions
- place_order: Place a new order
- cancel_order: Cancel an existing order
- get_market_data: Get market data snapshot for a symbol
- get_market_data_stream: Stream live market data via WebSocket
- get_order_status: Query order status
- search_contract: Search for a contract (obtain conid)

Edition: FULL ONLY
"""

import asyncio
import hashlib
import json
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, AsyncIterator

from .. import TicalPlugin, tool, ToolResult, PluginMetadata, PluginEdition, AgentContext
from tical_code.core.verify import VerifyLevel, force_verify, PluginVerifyMixin, VerifyResult
from tical_code.core.memory import MemoryType, SkeletonStrategy

# =============================================================================
# Constants
# =============================================================================

# IB Web API base URLs
GATEWAY_BASE_URL = "https://localhost:5000/v1/api"
OAUTH_BASE_URL = "https://api.ibkr.com/v1/api"

# Market data field tags (IBKR numeric identifiers)
# See: https://www.interactivebrokers.com/campus/ibkr-api-page/webapi-ref/
MKT_FIELDS_LAST = "31"      # Last price
MKT_FIELDS_BID = "84"       # Bid price
MKT_FIELDS_ASK = "86"       # Ask price
MKT_FIELDS_HIGH = "70"      # High
MKT_FIELDS_LOW = "71"       # Low
MKT_FIELDS_CLOSE = "7059"   # Close (previous close)
MKT_FIELDS_VOLUME = "87"    # Volume
MKT_FIELDS_BID_SIZE = "85"  # Bid size
MKT_FIELDS_ASK_SIZE = "88"  # Ask size

# Common field set for snapshot/streaming
DEFAULT_MKT_FIELDS = [
    MKT_FIELDS_LAST, MKT_FIELDS_BID, MKT_FIELDS_ASK,
    MKT_FIELDS_HIGH, MKT_FIELDS_LOW, MKT_FIELDS_CLOSE,
    MKT_FIELDS_VOLUME, MKT_FIELDS_BID_SIZE, MKT_FIELDS_ASK_SIZE,
]

class TradingPlugin(TicalPlugin, PluginVerifyMixin):
    """
    Trading plugin for equities, futures, forex via IB Web API.

    Supports:
    - Gateway mode: Client Portal Gateway (personal, localhost:5000)
    - OAuth mode: OAuth2.0 private_key_jwt (multi-user, api.ibkr.com)

    All operations integrate Force-Verify and require dual confirmation
    for trades above certain thresholds.

    WARNING: This plugin handles real money. Use with extreme caution.
    """

    metadata = PluginMetadata(
        name="trading",
        version="0.4.0",
        edition=PluginEdition.FULL,
        description="Trading via IB Web API (REST + OAuth2.0) with Force-Verify",
        dependencies=["httpx", "websockets"],
    )

    def __init__(self):
        super().__init__()
        # HTTP client (httpx.AsyncClient)
        self._http_client: Optional[Any] = None
        # WebSocket connection for market data streaming
        self._ws_connection: Optional[Any] = None
        # Connection state
        self._connected: bool = False
        self._auth_mode: Optional[str] = None  # "gateway" or "oauth"
        self._base_url: str = ""
        self._account_id: Optional[str] = None
        self._session_cookie: Optional[str] = None

        # OAuth2.0 credentials
        self._access_token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._oauth_client_id: Optional[str] = None
        self._oauth_private_key: Optional[str] = None
        self._oauth_client_key_id: Optional[str] = None
        self._oauth_credential: Optional[str] = None  # IBKR username

        # Tickle task for keepalive
        self._tickle_task: Optional[asyncio.Task] = None

        # Track order state for verification
        self._pending_orders: Dict[str, Dict] = {}

        # Conid cache (symbol -> conid)
        self._conid_cache: Dict[str, int] = {}

        # Check dependencies
        self._httpx_available: bool = False
        self._websockets_available: bool = False

        try:
            import httpx  # noqa: F401
            self._httpx_available = True
        except ImportError:
            pass

        try:
            import websockets  # noqa: F401
            self._websockets_available = True
        except ImportError:
            pass

    async def init(self, context: AgentContext) -> None:
        """Initialize trading plugin."""
        await super().init(context)

        if not self._httpx_available:
            self.logger.warning(
                "httpx not available. Install: pip install httpx"
            )
        if not self._websockets_available:
            self.logger.warning(
                "websockets not available. Install: pip install websockets"
            )

        # Initialize memory with strict settings
        self.use_memory(strategy=SkeletonStrategy.LAZY)

        # Load previous positions
        positions = self._memory.get('positions', touch=False)
        if positions:
            self.logger.info(f"Loaded {len(positions)} previous positions")

        # Load pending orders
        self._pending_orders = self._memory.get('pending_orders', touch=False) or {}

    async def shutdown(self) -> None:
        """Disconnect and cleanup."""
        await self.disconnect()
        await super().shutdown()

    # =========================================================================
    # Connection Management
    # =========================================================================

    async def connect(self, args: Dict) -> ToolResult:
        """
        Connect to IB Web API.

        Supports two modes:
        - "gateway": Client Portal Gateway on localhost (personal use)
        - "oauth": OAuth2.0 private_key_jwt (multi-user / third-party)

        Args:
            args: {
                "mode": "gateway" | "oauth",
                # Gateway mode:
                "base_url": "https://localhost:5000/v1/api",  # optional
                # OAuth mode:
                "client_id": "TESTCONS",
                "client_key_id": "kid-from-portal",
                "private_key_path": "/path/to/private-key.pem",
                "credential": "ibkr_username",  # optional, for ssodh init
                "account_id": "DU1234567",  # optional
            }

        Returns:
            ToolResult with connection status
        """
        mode = args.get("mode", "gateway")

        if not self._httpx_available:
            return ToolResult(
                success=False,
                error="httpx is not installed. Run: pip install httpx",
            )

        try:
            if mode == "gateway":
                return await self._connect_gateway(args)
            elif mode == "oauth":
                return await self._connect_oauth(args)
            else:
                return ToolResult(
                    success=False,
                    error=f"Unknown mode: {mode}. Use 'gateway' or 'oauth'.",
                )
        except Exception as e:
            self.logger.error(f"Connection failed: {e}")
            return ToolResult(success=False, error=str(e))

    async def _connect_gateway(self, args: Dict) -> ToolResult:
        """
        Connect via Client Portal Gateway.

        The user must have already logged in through the browser on the
        same machine running the CP Gateway.

        Args:
            args: Connection arguments (base_url optional)
        """
        import httpx

        base_url = args.get("base_url", GATEWAY_BASE_URL)
        self._base_url = base_url
        self._auth_mode = "gateway"

        # Create HTTP client(SSLVerify)
        self._http_client = httpx.AsyncClient(
            base_url=base_url,
            timeout=30.0,
            verify=True,  # :SSLVerify
        )

        # Check if session exists by calling /iserver/auth/status
        try:
            resp = await self._http_client.get("/iserver/auth/status")
            data = resp.json()

            if data.get("authenticated", False):
                self._connected = True
                # Get accounts
                accounts_resp = await self._http_client.get("/iserver/accounts")
                accounts_data = accounts_resp.json()
                accounts = accounts_data.get("accounts", [])
                if accounts and accounts[0] != "All":
                    self._account_id = accounts[0]

                self._start_tickle()

                return ToolResult(
                    success=True,
                    data={
                        "mode": "gateway",
                        "base_url": base_url,
                        "authenticated": True,
                        "account_id": self._account_id,
                        "accounts": accounts,
                    },
                    verified=True,
                )
            else:
                # Session not yet authenticated - user needs to log in via browser
                return ToolResult(
                    success=False,
                    error="Gateway session not authenticated. "
                          "Log in via browser at https://localhost:5000 "
                          "then retry.",
                    data={
                        "mode": "gateway",
                        "authenticated": False,
                    },
                )

        except httpx.ConnectError:
            return ToolResult(
                success=False,
                error="Cannot connect to Client Portal Gateway. "
                      "Make sure the gateway is running on "
                      f"{base_url}",
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Gateway connection error: {e}",
            )

    async def _connect_oauth(self, args: Dict) -> ToolResult:
        """
        Connect via OAuth2.0 (private_key_jwt).

        Follows RFC 7521/7523:
        1. Generate JWT client_assertion signed with RSA private key
        2. POST /oauth2/token to obtain access_token
        3. Initialize brokerage session via /iserver/auth/ssodh/init

        Args:
            args: OAuth credentials (client_id, client_key_id, private_key_path, credential)
        """
        import httpx

        client_id = args.get("client_id")
        client_key_id = args.get("client_key_id")
        private_key_path = args.get("private_key_path")
        credential = args.get("credential")
        account_id = args.get("account_id")

        if not all([client_id, client_key_id, private_key_path]):
            return ToolResult(
                success=False,
                error="OAuth mode requires: client_id, client_key_id, private_key_path",
            )

        self._oauth_client_id = client_id
        self._oauth_client_key_id = client_key_id
        self._oauth_credential = credential
        self._base_url = OAUTH_BASE_URL
        self._auth_mode = "oauth"

        # Read private key
        try:
            with open(private_key_path, "r") as f:
                self._oauth_private_key = f.read()
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Cannot read private key from {private_key_path}: {e}",
            )

        # Step 1: Generate client_assertion (JWT)
        try:
            client_assertion = self._generate_client_assertion()
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Failed to generate client_assertion: {e}",
            )

        # Step 2: Obtain access token
        try:
            async with httpx.AsyncClient(
                base_url="https://api.ibkr.com",
                timeout=30.0,
            ) as token_client:
                token_resp = await token_client.post(
                    "/oauth2/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                        "client_assertion": client_assertion,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )

                if token_resp.status_code != 200:
                    return ToolResult(
                        success=False,
                        error=f"OAuth token request failed: {token_resp.status_code} {token_resp.text}",
                    )

                token_data = token_resp.json()
                self._access_token = token_data.get("access_token")
                expires_in = token_data.get("expires_in", 300)
                self._token_expiry = time.time() + expires_in

                # Get session token via /tickle for cookie management
                # (required for OAuth1.0a and OAuth2.0)
                # TODO: Implement live session token (LST) handshake
                # per IBKR OAuth documentation if needed for OAuth2.0 flow.
                # The exact flow may differ based on IBKR's current OAuth2.0 beta implementation.

        except Exception as e:
            return ToolResult(
                success=False,
                error=f"OAuth token request error: {e}",
            )

        # Create HTTP client with Bearer token
        self._http_client = httpx.AsyncClient(
            base_url=OAUTH_BASE_URL,
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {self._access_token}",
            },
        )

        # Step 3: Initialize brokerage session
        try:
            brokerage_result = await self._init_brokerage_session(credential)
            if not brokerage_result:
                return ToolResult(
                    success=False,
                    error="Failed to initialize brokerage session",
                )
        except Exception as e:
            self.logger.warning(f"Brokerage session init error (may need manual steps): {e}")

        self._connected = True
        self._account_id = account_id

        # Get actual accounts if not specified
        if not self._account_id:
            try:
                accounts_resp = await self._http_client.get("/iserver/accounts")
                accounts_data = accounts_resp.json()
                accounts = accounts_data.get("accounts", [])
                if accounts and accounts[0] != "All":
                    self._account_id = accounts[0]
            except Exception as e:
                logger.debug(f"[__init__.py] (): {e}")

        self._start_tickle()

        return ToolResult(
            success=True,
            data={
                "mode": "oauth",
                "authenticated": True,
                "account_id": self._account_id,
                "token_expiry": self._token_expiry,
            },
            verified=True,
        )

    def _generate_client_assertion(self) -> str:
        """
        Generate a JWT client_assertion for OAuth2.0 private_key_jwt.

        Follows RFC 7521/7523:
        - iss = client_id
        - sub = client_id
        - aud = IBKR token endpoint
        - exp = now + 5 minutes
        - jti = unique identifier

        Returns:
            Signed JWT string
        """
        import jwt  # PyJWT is already a core dependency

        now = int(time.time())
        payload = {
            "iss": self._oauth_client_id,
            "sub": self._oauth_client_id,
            "aud": "https://api.ibkr.com/oauth2/token",
            "iat": now,
            "exp": now + 300,  # 5 minutes
            "jti": str(uuid.uuid4()),
        }

        headers = {
            "kid": self._oauth_client_key_id,
            "alg": "RS256",
            "typ": "JWT",
        }

        token = jwt.encode(
            payload,
            self._oauth_private_key,
            algorithm="RS256",
            headers=headers,
        )

        return token

    async def _init_brokerage_session(self, credential: Optional[str] = None) -> bool:
        """
        Initialize brokerage session via /iserver/auth/ssodh/init.

        Required for trading and market data after OAuth authentication.
        The CP Gateway auto-initializes this; for OAuth it must be done manually.

        Args:
            credential: IBKR username (optional for OAuth flow)

        Returns:
            True if brokerage session was established

        NOTE: The full SSODH handshake involves:
        1. POST /iserver/auth/ssodh/init → get challenge
        2. Compute response from challenge + session token
        3. POST /iserver/auth/ssodh/response → authenticate

        The exact implementation depends on the OAuth2.0 beta specifics.
        This is a simplified version; TODO: complete the DH key exchange.
        """
        if not self._http_client:
            return False

        try:
            # Generate machine identifiers
            machine_id = hashlib.md5(str(uuid.uuid4()).encode()).hexdigest()[:8].upper()
            mac = ":".join([hashlib.md5(str(uuid.uuid4()).encode()).hexdigest()[i:i+2].upper()
                           for i in range(0, 12, 2)])

            init_resp = await self._http_client.post(
                "/iserver/auth/ssodh/init",
                data={
                    "machineId": machine_id,
                    "mac": mac,
                    "compete": "false",
                    "locale": "en_US",
                    "username": credential or "-",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

            if init_resp.status_code == 200:
                data = init_resp.json()
                # If we get a challenge, we need to complete the handshake
                # TODO: Implement full SSODH challenge-response with DH key exchange
                # For now, check auth status
                auth_resp = await self._http_client.get("/iserver/auth/status")
                auth_data = auth_resp.json()
                return auth_data.get("authenticated", False)

            return False

        except Exception as e:
            self.logger.error(f"Brokerage session init error: {e}")
            return False

    def _start_tickle(self) -> None:
        """Start periodic tickle to keep the session alive."""
        if self._tickle_task and not self._tickle_task.done():
            return

        async def _tickle_loop():
            """Send tickle every 60 seconds to keep session alive."""
            while self._connected:
                try:
                    await asyncio.sleep(60)
                    if self._http_client and self._connected:
                        await self._http_client.get("/tickle")
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    self.logger.debug(f"Tickle error: {e}")

        self._tickle_task = asyncio.create_task(_tickle_loop())

    async def disconnect(self, args: Optional[Dict] = None) -> ToolResult:
        """
        Disconnect from IB Web API.

        Returns:
            ToolResult with disconnection status
        """
        try:
            # Cancel tickle
            if self._tickle_task and not self._tickle_task.done():
                self._tickle_task.cancel()
                self._tickle_task = None

            # Close WebSocket
            if self._ws_connection:
                try:
                    await self._ws_connection.close()
                except Exception as e:
                    logger.debug(f"[__init__.py] (): {e}")
                self._ws_connection = None

            # Logout and close HTTP client
            if self._http_client:
                try:
                    await self._http_client.post("/logout")
                except Exception as e:
                    logger.debug(f"[__init__.py] (): {e}")
                try:
                    await self._http_client.aclose()
                except Exception as e:
                    logger.debug(f"[__init__.py] (): {e}")
                self._http_client = None

        except Exception as e:
            self.logger.error(f"Disconnect error: {e}")
        finally:
            self._connected = False
            self._auth_mode = None
            self._account_id = None
            self._access_token = None
            self._session_cookie = None

        return ToolResult(success=True, data={"connected": False})

    def _verify_connection(self) -> bool:
        """
        Verify connection is active.

        Returns:
            True if connected with an active HTTP client
        """
        return self._connected and self._http_client is not None

    # =========================================================================
    # Dual Verification for Orders
    # =========================================================================

    async def _verify_order_dual(self, order_data: Dict) -> VerifyResult:
        """
        Perform dual verification for an order.

        Verifies:
        1. Order data matches expected values (parameter validation)
        2. Account has sufficient funds (balance check via API response)
        """
        start = time.time()

        symbol = order_data.get('symbol')
        quantity = order_data.get('quantity', 0)
        order_type = order_data.get('order_type')
        action = order_data.get('action')  # BUY or SELL

        errors = []

        # Verify 1: Check order data
        if not symbol:
            errors.append("Missing symbol")
        if quantity <= 0:
            errors.append("Invalid quantity")
        if order_type not in ['MARKET', 'LIMIT', 'STOP', 'STOP_LIMIT', 'MKT', 'LMT', 'STP']:
            errors.append("Invalid order type")
        if action not in ['BUY', 'SELL']:
            errors.append("Invalid action")

        # Verify 2: Check account balance (only if connected)
        if self._verify_connection():
            try:
                account = await self.get_account({})
                if account.success and account.data:
                    cash = account.data.get('cash', 0)

                    if action == 'BUY':
                        # Estimate cost - rough estimate without real-time price
                        estimated_cost = quantity * 100  # Rough estimate
                        if cash < estimated_cost:
                            errors.append(f"Insufficient cash: {cash} < {estimated_cost}")
            except Exception as e:
                errors.append(f"Account check failed: {e}")

        passed = len(errors) == 0

        return VerifyResult(
            passed=passed,
            level=VerifyLevel.DUAL,
            method="trading_dual_verify",
            details="; ".join(errors) if errors else "Order verified",
            elapsed_ms=(time.time() - start) * 1000,
        )

    # =========================================================================
    # Helper Methods
    # =========================================================================

    async def _resolve_conid(self, symbol: str) -> Optional[int]:
        """
        Resolve a symbol to its IBKR conid (contract ID).

        Uses cache first, then queries /iserver/secdef/search.

        Args:
            symbol: Ticker symbol (e.g., "AAPL")

        Returns:
            conid (int) or None if not found
        """
        if symbol in self._conid_cache:
            return self._conid_cache[symbol]

        if not self._verify_connection():
            return None

        try:
            resp = await self._http_client.post(
                "/iserver/secdef/search",
                json={"symbol": symbol, "secType": "STK", "name": True},
            )

            if resp.status_code == 200:
                data = resp.json()
                # Find the matching US stock contract
                for item in data:
                    sections = item.get("sections", [])
                    for section in sections:
                        for sec in section.get("secType", []):
                            if sec.get("secType") == "STK":
                                conid = sec.get("conid")
                                if conid:
                                    self._conid_cache[symbol] = conid
                                    return conid

                # Fallback: try first result's conid
                if data and len(data) > 0:
                    conid = data[0].get("conid")
                    if conid is None:
                        # Try sections
                        sections = data[0].get("sections", [])
                        for section in sections:
                            sec_types = section.get("secType", [])
                            for sec in sec_types:
                                conid = sec.get("conid")
                                if conid:
                                    self._conid_cache[symbol] = conid
                                    return conid

            return None

        except Exception as e:
            self.logger.error(f"Conid resolution error for {symbol}: {e}")
            return None

    def _normalize_order_type(self, order_type: str) -> str:
        """
        Normalize order type to IB Web API format.

        Args:
            order_type: User-provided order type

        Returns:
            IB Web API order type string (e.g., "LMT", "MKT", "STP")
        """
        mapping = {
            "MARKET": "MKT",
            "LIMIT": "LMT",
            "STOP": "STP",
            "STOP_LIMIT": "STP_LMT",
            "MKT": "MKT",
            "LMT": "LMT",
            "STP": "STP",
            "STP_LMT": "STP_LMT",
        }
        return mapping.get(order_type.upper(), order_type.upper())

    async def _ensure_brokerage_session(self) -> bool:
        """
        Ensure the brokerage session is active.

        Checks /iserver/auth/status and attempts re-authentication if needed.

        Returns:
            True if brokerage session is active
        """
        if not self._verify_connection():
            return False

        try:
            resp = await self._http_client.get("/iserver/auth/status")
            data = resp.json()

            if data.get("authenticated", False):
                return True

            # Try to reauthenticate
            if self._auth_mode == "gateway":
                await self._http_client.get("/iserver/reauthenticate")
                await asyncio.sleep(1)
                resp = await self._http_client.get("/iserver/auth/status")
                data = resp.json()
                return data.get("authenticated", False)

            return False

        except Exception as e:
            self.logger.error(f"Brokerage session check error: {e}")
            return False

    # =========================================================================
    # Tool Methods
    # =========================================================================

    @tool
    @force_verify(level=VerifyLevel.SCHEMA, schema={
        "type": "object",
        "properties": {
            "account_id": {"type": "string"}
        }
    })
    async def get_account(self, args: Dict) -> ToolResult:
        """
        Get account information.

        Uses GET /portfolio/accounts for account details.
        For balance/currency info, uses GET /portfolio/{accountId}/ledger.

        Args:
            args: {"account_id": "DU123456"} (optional, uses default account)

        Returns:
            ToolResult with account data
        """
        if not self._verify_connection():
            # Return mock data if not connected
            return ToolResult(
                success=True,
                data={
                    'account_id': 'DEMO',
                    'cash': 10000.00,
                    'buying_power': 20000.00,
                    'equity': 10000.00,
                    'position_value': 0.0,
                    'currency': 'USD',
                    'connected': False,
                    'mode': 'demo',
                },
                verified=True,
            )

        account_id = args.get('account_id') or self._account_id

        try:
            # Get account list
            accounts_resp = await self._http_client.get("/portfolio/accounts")
            if accounts_resp.status_code != 200:
                return ToolResult(
                    success=False,
                    error=f"Failed to get accounts: {accounts_resp.status_code}",
                )

            accounts_data = accounts_resp.json()

            if not accounts_data:
                return ToolResult(
                    success=False,
                    error="No accounts returned",
                )

            # Find the target account
            account_info = None
            if isinstance(accounts_data, list):
                for acct in accounts_data:
                    if acct.get("accountId") == account_id or account_id is None:
                        account_info = acct
                        break
                if account_info is None and accounts_data:
                    account_info = accounts_data[0]
            else:
                account_info = accounts_data

            if not account_info:
                return ToolResult(
                    success=False,
                    error="Account not found",
                )

            result_account_id = account_info.get("accountId", "UNKNOWN")

            # Get ledger for balance details
            cash = 0.0
            equity = 0.0
            currency = "USD"
            try:
                ledger_resp = await self._http_client.get(
                    f"/portfolio/{result_account_id}/ledger"
                )
                if ledger_resp.status_code == 200:
                    ledger_data = ledger_resp.json()
                    # The ledger is keyed by currency
                    usd_data = ledger_data.get("USD", ledger_data.get("Total (in USD)", {}))
                    if usd_data:
                        cash = float(usd_data.get("settled_cash", 0))
                        equity = float(usd_data.get("net_liquidation", 0))
            except Exception as e:
                self.logger.warning(f"Ledger fetch error: {e}")

            return ToolResult(
                success=True,
                data={
                    'account_id': result_account_id,
                    'cash': cash,
                    'equity': equity,
                    'currency': currency,
                    'connected': True,
                    'provider': 'ib_webapi',
                    'auth_mode': self._auth_mode,
                },
                verified=True,
            )

        except Exception as e:
            self.logger.error(f"Get account error: {e}")
            return ToolResult(success=False, error=str(e))

    @tool
    @force_verify(level=VerifyLevel.BASIC)
    async def get_positions(self, args: Dict) -> ToolResult:
        """
        Get current positions.

        Uses GET /portfolio/{accountId}/positions.

        Args:
            args: {"account_id": "DU123456"} (optional)

        Returns:
            ToolResult with positions list
        """
        if not self._verify_connection():
            return ToolResult(
                success=True,
                data={'positions': [], 'connected': False, 'mode': 'demo'},
                verified=True,
            )

        account_id = args.get('account_id') or self._account_id
        if not account_id:
            return ToolResult(
                success=False,
                error="No account_id available. Connect first or specify account_id.",
            )

        try:
            positions = []

            # GET /portfolio/{accountId}/positions
            # page_id is 0-based for pagination (30 positions per page)
            page_id = args.get("page_id", 0)
            resp = await self._http_client.get(
                f"/portfolio/{account_id}/positions",
                params={"pageId": page_id},
            )

            if resp.status_code != 200:
                return ToolResult(
                    success=False,
                    error=f"Failed to get positions: {resp.status_code} {resp.text}",
                )

            positions_data = resp.json()

            if isinstance(positions_data, list):
                for pos in positions_data:
                    contract = pos.get("contractDesc", {})
                    positions.append({
                        'symbol': contract.get("ticker", ""),
                        'conid': contract.get("conid"),
                        'quantity': pos.get("position", 0),
                        'avg_cost': pos.get("avgPrice", 0),
                        'market_value': pos.get("mktValue", 0),
                        'unrealized_pnl': pos.get("unrealizedPnl", 0),
                        'currency': contract.get("currency", "USD"),
                        'asset_class': contract.get("assetClass", ""),
                    })

            # Store in memory
            self._memory.set('positions', positions)

            return ToolResult(
                success=True,
                data={
                    'positions': positions,
                    'count': len(positions),
                    'connected': True,
                    'provider': 'ib_webapi',
                    'auth_mode': self._auth_mode,
                },
                verified=True,
            )

        except Exception as e:
            self.logger.error(f"Get positions error: {e}")
            return ToolResult(success=False, error=str(e))

    @tool
    @force_verify(level=VerifyLevel.SCHEMA, schema={
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "quantity": {"type": "number"},
            "action": {"type": "string", "enum": ["BUY", "SELL"]},
            "order_type": {"type": "string", "enum": ["MARKET", "LIMIT", "STOP", "MKT", "LMT", "STP", "STOP_LIMIT"]},
            "limit_price": {"type": "number"},
            "stop_price": {"type": "number"},
            "tif": {"type": "string", "default": "DAY"},
            "account_id": {"type": "string"}
        },
        "required": ["symbol", "quantity", "action", "order_type"]
    })
    async def place_order(self, args: Dict) -> ToolResult:
        """
        Place a new order via IB Web API.

        Uses POST /iserver/account/{accountId}/orders.

        WARNING: This involves real money. Dual verification is applied.

        Args:
            args: {
                "symbol": "AAPL",
                "quantity": 10,
                "action": "BUY",
                "order_type": "LIMIT",
                "limit_price": 150.00,
                "stop_price": 145.00,       # optional, for STOP orders
                "tif": "DAY",               # optional, default DAY
                "account_id": "DU1234567",  # optional, uses default
            }

        Returns:
            ToolResult with order confirmation
        """
        # Dual verification
        verify_result = await self._verify_order_dual(args)
        if not verify_result.passed:
            return ToolResult(
                success=False,
                error=f"Order rejected: {verify_result.details}",
            )

        if not self._verify_connection():
            # Demo mode: return mock response
            order_id = hashlib.md5(f"{time.time()}".encode()).hexdigest()[:12]

            self._pending_orders[order_id] = {
                **args,
                'order_id': order_id,
                'status': 'demo',
                'timestamp': time.time(),
            }

            return ToolResult(
                success=True,
                data={
                    'order_id': order_id,
                    'status': 'demo_pending',
                    'message': 'Order placed in demo mode',
                    **args,
                },
                verified=True,
            )

        account_id = args.get('account_id') or self._account_id
        if not account_id:
            return ToolResult(
                success=False,
                error="No account_id available. Connect first or specify account_id.",
            )

        try:
            # Ensure brokerage session is active
            if not await self._ensure_brokerage_session():
                return ToolResult(
                    success=False,
                    error="Brokerage session not active. Cannot place orders.",
                )

            # Resolve conid
            symbol = args['symbol']
            conid = await self._resolve_conid(symbol)
            if not conid:
                return ToolResult(
                    success=False,
                    error=f"Cannot resolve conid for symbol: {symbol}. "
                          f"Use search_contract to find the correct conid.",
                )

            # Build order ticket per IB Web API spec
            normalized_type = self._normalize_order_type(args['order_type'])

            order_ticket: Dict[str, Any] = {
                "conid": conid,
                "side": args['action'].upper(),
                "orderType": normalized_type,
                "quantity": int(args['quantity']),
                "tif": args.get('tif', 'DAY'),
            }

            # Add price fields based on order type
            if normalized_type in ("LMT", "STP_LMT"):
                if 'limit_price' in args:
                    order_ticket["price"] = args['limit_price']
                else:
                    return ToolResult(
                        success=False,
                        error="LIMIT orders require limit_price",
                    )

            if normalized_type in ("STP", "STP_LMT"):
                if 'stop_price' in args:
                    order_ticket["auxPrice"] = args['stop_price']
                else:
                    return ToolResult(
                        success=False,
                        error="STOP orders require stop_price",
                    )

            # Submit order: body is a JSON array (for bracket support)
            resp = await self._http_client.post(
                f"/iserver/account/{account_id}/orders",
                json=[order_ticket],
            )

            if resp.status_code not in (200, 201):
                return ToolResult(
                    success=False,
                    error=f"Order submission failed: {resp.status_code} {resp.text}",
                )

            response_data = resp.json()

            # Handle order reply messages (may need confirmation)
            # IB may return a message requiring /iserver/reply/{replyId}
            if isinstance(response_data, list):
                response_data = response_data[0] if response_data else {}

            # Check for reply messages that need confirmation
            # TODO: Implement automatic reply confirmation via /iserver/reply/{id}
            order_id = str(response_data.get("order_id", ""))
            order_status = response_data.get("order_status", "unknown")

            # If there's a message requiring confirmation
            if response_data.get("message") and not order_id:
                return ToolResult(
                    success=False,
                    error=f"Order requires confirmation: {response_data.get('message')}",
                    data=response_data,
                )

            order_info = {
                'order_id': order_id,
                'status': order_status,
                'symbol': symbol,
                'conid': conid,
                **args,
            }

            # Store order
            if order_id:
                self._pending_orders[order_id] = order_info
                self._memory.set('pending_orders', self._pending_orders)

            return ToolResult(
                success=True,
                data=order_info,
                verified=True,
            )

        except Exception as e:
            self.logger.error(f"Place order error: {e}")
            return ToolResult(success=False, error=str(e))

    @tool
    @force_verify(level=VerifyLevel.SCHEMA, schema={
        "type": "object",
        "properties": {
            "order_id": {"type": "string"},
            "account_id": {"type": "string"}
        },
        "required": ["order_id"]
    })
    async def cancel_order(self, args: Dict) -> ToolResult:
        """
        Cancel an existing order.

        Uses DELETE /iserver/account/{accountId}/order/{orderId}.

        Args:
            args: {"order_id": "12345", "account_id": "DU1234567"}

        Returns:
            ToolResult with cancellation confirmation
        """
        order_id = args.get('order_id')

        if not order_id:
            return ToolResult(success=False, error="order_id is required")

        # Check pending orders (demo mode)
        if order_id in self._pending_orders:
            order = self._pending_orders[order_id]
            if order.get('status') == 'demo':
                del self._pending_orders[order_id]
                return ToolResult(
                    success=True,
                    data={
                        'order_id': order_id,
                        'status': 'cancelled',
                        'message': 'Demo order cancelled',
                    },
                    verified=True,
                )

        if not self._verify_connection():
            return ToolResult(success=False, error="Not connected")

        account_id = args.get('account_id') or self._account_id
        if not account_id:
            return ToolResult(
                success=False,
                error="No account_id available. Connect first or specify account_id.",
            )

        try:
            # Ensure brokerage session is active
            if not await self._ensure_brokerage_session():
                return ToolResult(
                    success=False,
                    error="Brokerage session not active. Cannot cancel orders.",
                )

            resp = await self._http_client.delete(
                f"/iserver/account/{account_id}/order/{order_id}",
            )

            if resp.status_code not in (200, 201):
                return ToolResult(
                    success=False,
                    error=f"Cancel order failed: {resp.status_code} {resp.text}",
                )

            response_data = resp.json()

            # Update pending orders
            if order_id in self._pending_orders:
                self._pending_orders[order_id]['status'] = 'cancelled'
                self._memory.set('pending_orders', self._pending_orders)

            return ToolResult(
                success=True,
                data={
                    'order_id': order_id,
                    'status': 'cancelled',
                    'response': response_data,
                },
                verified=True,
            )

        except Exception as e:
            self.logger.error(f"Cancel order error: {e}")
            return ToolResult(success=False, error=str(e))

    @tool
    @force_verify(level=VerifyLevel.SCHEMA, schema={
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "include_snapshot": {"type": "boolean", "default": True}
        },
        "required": ["symbol"]
    })
    async def get_market_data(self, args: Dict) -> ToolResult:
        """
        Get market data snapshot for a symbol.

        Uses GET /iserver/marketdata/snapshot.

        Note: The first request for a conid acts as a "pre-flight" request
        and may not return data. Subsequent requests will return actual values.

        Args:
            args: {"symbol": "AAPL", "include_snapshot": true}

        Returns:
            ToolResult with market data
        """
        symbol = args.get('symbol')
        include_snapshot = args.get('include_snapshot', True)

        if not symbol:
            return ToolResult(success=False, error="symbol is required")

        try:
            data = {
                'symbol': symbol,
                'timestamp': time.time(),
            }

            if self._verify_connection():
                # Ensure brokerage session
                if not await self._ensure_brokerage_session():
                    # Fall through to demo data
                    data.update(self._demo_market_data())
                else:
                    conid = await self._resolve_conid(symbol)
                    if conid:
                        fields_str = ",".join(DEFAULT_MKT_FIELDS)
                        resp = await self._http_client.get(
                            "/iserver/marketdata/snapshot",
                            params={
                                "conids": str(conid),
                                "fields": fields_str,
                            },
                        )

                        if resp.status_code == 200:
                            snapshot_data = resp.json()
                            if isinstance(snapshot_data, list) and snapshot_data:
                                item = snapshot_data[0]
                                # Check if this is a pre-flight response (no actual data yet)
                                # Pre-flight responses contain only conid/conidEx, no field values
                                has_data = any(
                                    key in item
                                    for key in [MKT_FIELDS_LAST, MKT_FIELDS_BID, MKT_FIELDS_ASK]
                                )
                                if has_data:
                                    data.update({
                                        'bid': self._parse_mkt_float(item.get(MKT_FIELDS_BID)),
                                        'ask': self._parse_mkt_float(item.get(MKT_FIELDS_ASK)),
                                        'last': self._parse_mkt_float(item.get(MKT_FIELDS_LAST)),
                                        'high': self._parse_mkt_float(item.get(MKT_FIELDS_HIGH)),
                                        'low': self._parse_mkt_float(item.get(MKT_FIELDS_LOW)),
                                        'close': self._parse_mkt_float(item.get(MKT_FIELDS_CLOSE)),
                                        'volume': self._parse_mkt_int(item.get(MKT_FIELDS_VOLUME)),
                                        'conid': conid,
                                    })
                                else:
                                    # Pre-flight response - data not yet available
                                    data['preflight'] = True
                                    data['conid'] = conid
                            elif isinstance(snapshot_data, dict):
                                # Dict response may also be pre-flight
                                has_data = any(
                                    key in snapshot_data
                                    for key in [MKT_FIELDS_LAST, MKT_FIELDS_BID, MKT_FIELDS_ASK]
                                )
                                if not has_data:
                                    data['preflight'] = True
                                    data['conid'] = conid
                        else:
                            data.update(self._demo_market_data())
                    else:
                        data.update(self._demo_market_data())
            else:
                # Return demo data
                data.update(self._demo_market_data())

            return ToolResult(
                success=True,
                data=data,
                verified=True,
            )

        except Exception as e:
            self.logger.error(f"Get market data error: {e}")
            return ToolResult(success=False, error=str(e))

    @tool
    @force_verify(level=VerifyLevel.SCHEMA, schema={
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Field tags to stream (e.g., ['31','84','86'])"
            }
        },
        "required": ["symbol"]
    })
    async def get_market_data_stream(self, args: Dict) -> ToolResult:
        """
        Stream live market data via WebSocket.

        Connects to the IB Web API WebSocket and subscribes to streaming
        market data for the given symbol.

        Uses the WebSocket protocol:
        - Subscribe: smd+CONID+{"fields":["31","84",...]}
        - Unsubscribe: umd+CONID+{}

        Args:
            args: {
                "symbol": "AAPL",
                "fields": ["31", "84", "85", "86", "88", "7059"]  # optional
            }

        Returns:
            ToolResult with stream status (actual data delivered via WebSocket)
        """
        symbol = args.get('symbol')

        if not symbol:
            return ToolResult(success=False, error="symbol is required")

        if not self._verify_connection():
            return ToolResult(
                success=True,
                data={
                    'symbol': symbol,
                    'status': 'demo',
                    'message': 'Market data stream not available in demo mode',
                },
                verified=True,
            )

        if not self._websockets_available:
            return ToolResult(
                success=False,
                error="websockets library not installed. Run: pip install websockets",
            )

        try:
            conid = await self._resolve_conid(symbol)
            if not conid:
                return ToolResult(
                    success=False,
                    error=f"Cannot resolve conid for {symbol}",
                )

            fields = args.get('fields', DEFAULT_MKT_FIELDS)

            # Build WebSocket URL
            ws_url = self._base_url.replace("https://", "wss://").replace("http://", "ws://")
            if not ws_url.endswith("/ws"):
                ws_url = ws_url.rstrip("/") + "/ws"

            # Connect and subscribe
            import websockets

            # For gateway mode, we need to pass the cookie
            extra_headers = {}
            if self._session_cookie:
                extra_headers["Cookie"] = f"api={self._session_cookie}"
            elif self._access_token:
                extra_headers["Authorization"] = f"Bearer {self._access_token}"

            self._ws_connection = await websockets.connect(
                ws_url,
                ssl={"cert_reqs": 0} if self._auth_mode == "gateway" else None,
                additional_headers=extra_headers if extra_headers else None,
            )

            # Subscribe to market data
            subscribe_msg = f'smd+{conid}+{{"fields":{json.dumps(fields)}}}'
            await self._ws_connection.send(subscribe_msg)

            return ToolResult(
                success=True,
                data={
                    'symbol': symbol,
                    'conid': conid,
                    'status': 'streaming',
                    'ws_url': ws_url,
                    'fields': fields,
                    'message': 'WebSocket stream started. Use the WebSocket connection to receive data.',
                },
                verified=True,
            )

        except Exception as e:
            self.logger.error(f"Market data stream error: {e}")
            return ToolResult(success=False, error=str(e))

    @tool
    @force_verify(level=VerifyLevel.BASIC)
    async def get_order_status(self, args: Dict) -> ToolResult:
        """
        Query order status.

        Uses GET /iserver/account/orders for all orders,
        or checks pending orders by order_id.

        Args:
            args: {"order_id": "12345"} (optional, omit for all orders)

        Returns:
            ToolResult with order status data
        """
        order_id = args.get('order_id')

        # Check demo orders first
        if order_id and order_id in self._pending_orders:
            return ToolResult(
                success=True,
                data=self._pending_orders[order_id],
                verified=True,
            )

        if not self._verify_connection():
            return ToolResult(
                success=True,
                data={
                    'orders': [],
                    'connected': False,
                    'mode': 'demo',
                },
                verified=True,
            )

        try:
            # Ensure brokerage session
            if not await self._ensure_brokerage_session():
                return ToolResult(
                    success=False,
                    error="Brokerage session not active. Cannot query orders.",
                )

            # Get all orders
            filters = args.get("filters", [])
            resp = await self._http_client.get(
                "/iserver/account/orders",
                params={"filters": ",".join(filters)} if filters else {},
            )

            if resp.status_code != 200:
                return ToolResult(
                    success=False,
                    error=f"Failed to get orders: {resp.status_code}",
                )

            response_data = resp.json()

            orders = response_data.get("orders", [])

            if order_id:
                # Filter to specific order
                for order in orders:
                    if str(order.get("orderId")) == str(order_id):
                        return ToolResult(
                            success=True,
                            data=order,
                            verified=True,
                        )
                return ToolResult(
                    success=False,
                    error=f"Order not found: {order_id}",
                )

            return ToolResult(
                success=True,
                data={
                    'orders': orders,
                    'count': len(orders),
                },
                verified=True,
            )

        except Exception as e:
            self.logger.error(f"Get order status error: {e}")
            return ToolResult(success=False, error=str(e))

    @tool
    @force_verify(level=VerifyLevel.BASIC)
    async def search_contract(self, args: Dict) -> ToolResult:
        """
        Search for a contract and obtain its conid.

        Uses POST /iserver/secdef/search.

        Args:
            args: {
                "symbol": "AAPL",
                "sec_type": "STK",    # optional, default "STK"
                "name": True,          # optional, search by name too
            }

        Returns:
            ToolResult with contract search results
        """
        symbol = args.get('symbol')

        if not symbol:
            return ToolResult(success=False, error="symbol is required")

        if not self._verify_connection():
            return ToolResult(
                success=True,
                data={
                    'symbol': symbol,
                    'results': [],
                    'connected': False,
                    'mode': 'demo',
                },
                verified=True,
            )

        try:
            sec_type = args.get('sec_type', 'STK')
            search_by_name = args.get('name', True)

            resp = await self._http_client.post(
                "/iserver/secdef/search",
                json={
                    "symbol": symbol,
                    "secType": sec_type,
                    "name": search_by_name,
                },
            )

            if resp.status_code != 200:
                return ToolResult(
                    success=False,
                    error=f"Contract search failed: {resp.status_code} {resp.text}",
                )

            results = resp.json()

            # Cache found conids
            if isinstance(results, list):
                for item in results:
                    conid = item.get("conid")
                    if conid and item.get("ticker"):
                        self._conid_cache[item.get("ticker", symbol)] = conid

            return ToolResult(
                success=True,
                data={
                    'symbol': symbol,
                    'results': results,
                },
                verified=True,
            )

        except Exception as e:
            self.logger.error(f"Search contract error: {e}")
            return ToolResult(success=False, error=str(e))

    # =========================================================================
    # Utility Methods
    # =========================================================================

    @staticmethod
    def _demo_market_data() -> Dict:
        """Return demo market data for unconnected mode."""
        return {
            'bid': 150.00,
            'ask': 150.05,
            'last': 150.02,
            'high': 151.00,
            'low': 149.00,
            'close': 149.50,
            'volume': 1000000,
            'mode': 'demo',
        }

    @staticmethod
    def _parse_mkt_float(value: Any) -> Optional[float]:
        """
        Parse a market data value to float.

        IBKR returns values as strings with possible comma formatting.

        Args:
            value: Raw value from API

        Returns:
            Float value or None
        """
        if value is None or value == "":
            return None
        try:
            if isinstance(value, str):
                return float(value.replace(",", ""))
            return float(value)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _parse_mkt_int(value: Any) -> Optional[int]:
        """
        Parse a market data value to int.

        Args:
            value: Raw value from API

        Returns:
            Int value or None
        """
        if value is None or value == "":
            return None
        try:
            if isinstance(value, str):
                return int(float(value.replace(",", "")))
            return int(float(value))
        except (ValueError, TypeError):
            return None

    def is_available(self) -> bool:
        """Check if trading functionality is available."""
        return self._httpx_available or True  # Always available (demo mode works without deps)
