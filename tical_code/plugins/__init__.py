"""
Plugin System Framework
=======================

Provides plugin architecture for tical-code Full edition.
Every plugin MUST integrate Force-Verify and use SkeletonMemory.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Type, Literal
import importlib
import pkgutil

logger = logging.getLogger(__name__)

# =============================================================================
# Plugin Types
# =============================================================================

class PluginEdition(Enum):
    """Which editions a plugin supports."""
    LITE = "lite"
    FULL = "full"
    BOTH = "both"

@dataclass
class ToolResult:
    """Result from a plugin tool execution."""
    success: bool
    data: Any = None
    error: Optional[str] = None
    verified: bool = False
    elapsed_ms: float = 0.0
    
    def to_dict(self) -> Dict:
        return {
            'success': self.success,
            'data': self.data,
            'error': self.error,
            'verified': self.verified,
            'elapsed_ms': self.elapsed_ms,
        }

@dataclass
class PluginMetadata:
    """Metadata for a plugin."""
    name: str
    version: str
    edition: PluginEdition = PluginEdition.BOTH
    description: str = ""
    author: str = ""
    dependencies: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict:
        return {
            'name': self.name,
            'version': self.version,
            'edition': self.edition.value,
            'description': self.description,
            'author': self.author,
            'dependencies': self.dependencies,
        }

# =============================================================================
# Agent Context
# =============================================================================

@dataclass
class AgentContext:
    """Context passed to plugins during initialization."""
    agent_name: str
    platform: str
    edition: str
    worker_name: Optional[str] = None
    config: Dict[str, Any] = field(default_factory=dict)
    anchors: Dict[str, Any] = field(default_factory=dict)
    
    def get_anchor(self, key: str, default: Any = None) -> Any:
        """Get an anchor value."""
        return self.anchors.get(key, default)

# =============================================================================
# Plugin Base Class
# =============================================================================

class TicalPlugin(ABC):
    """
    Base class for all tical-code plugins.
    
    Every plugin MUST:
    1. Inherit from TicalPlugin
    2. Define metadata (name, version, edition)
    3. Implement init() and shutdown()
    4. Use @tool decorator for tools
    5. Use Force-Verify for all operations
    6. Use SkeletonMemory for persistent state
    """
    
    # Plugin metadata - override in subclasses
    metadata: PluginMetadata = PluginMetadata(
        name="base",
        version="0.1.0",
    )
    
    def __init__(self):
        """Initialize plugin."""
        self.context: Optional[AgentContext] = None
        self.tools: Dict[str, Callable] = {}
        self._initialized = False
        self._memory = None
        
        # Auto-register tools
        self._register_tools()
    
    def _register_tools(self):
        """Auto-register methods decorated with @tool."""
        for name in dir(self):
            if name.startswith('_'):
                continue
            attr = getattr(self, name)
            if callable(attr) and hasattr(attr, '_is_tical_tool'):
                self.tools[name] = attr
                logger.debug(f"Registered tool: {self.metadata.name}.{name}")
    
    async def init(self, context: AgentContext) -> None:
        """
        Initialize the plugin.
        
        Called when plugin is loaded.
        Override this method to perform initialization.
        """
        self.context = context
        self._initialized = True
        logger.info(f"Initialized plugin: {self.metadata.name}")
    
    async def shutdown(self) -> None:
        """
        Shutdown the plugin.
        
        Called when plugin is unloaded.
        Override this method to cleanup resources.
        """
        self._initialized = False
        logger.info(f"Shutdown plugin: {self.metadata.name}")
    
    def get_tools(self) -> Dict[str, Callable]:
        """Get all registered tools."""
        return self.tools.copy()
    
    def get_metadata(self) -> PluginMetadata:
        """Get plugin metadata."""
        return self.metadata
    
    def is_available(self) -> bool:
        """Check if plugin is available (dependencies met)."""
        for dep in self.metadata.dependencies:
            try:
                importlib.import_module(dep)
            except ImportError:
                return False
        return True
    
    def use_memory(self, store=None):
        """
        Get or create memory store for this plugin.
        
        Ensures plugin state persists via SkeletonMemory.
        """
        if self._memory is not None:
            return self._memory
        
        if store is not None:
            self._memory = store
            return self._memory
        
        # Create plugin-specific memory store
        from ..core.memory import MemoryStore
        
        plugin_name = self.metadata.name.replace('-', '_')
        memory_file = f"~/.tical/plugins/{plugin_name}/memory.json"
        
        self._memory = MemoryStore(
            store_file=memory_file,
            max_entries=500,
        )
        
        return self._memory

# =============================================================================
# Tool Decorator
# =============================================================================

def tool(func: Callable) -> Callable:
    """
    Decorator to mark a method as a plugin tool.
    
    Tools are automatically registered when the plugin initializes.
    
    Example:
        @tool
        async def my_tool(self, args: dict) -> ToolResult:
            ...
    """
    func._is_tical_tool = True
    return func

# =============================================================================
# Plugin Manager
# =============================================================================

class PluginManager:
    """
    Manages plugin lifecycle.
    
    Handles:
    - Plugin discovery
    - Loading/unloading
    - Tool registration
    """
    
    def __init__(self, plugins_dir: Optional[str] = None):
        """
        Initialize Plugin Manager.
        
        Args:
            plugins_dir: Directory to load plugins from
        """
        self.plugins_dir = plugins_dir
        self.plugins: Dict[str, TicalPlugin] = {}
        self.context: Optional[AgentContext] = None
    
    def set_context(self, context: AgentContext):
        """Set the agent context for plugins."""
        self.context = context
    
    def register_plugin(self, plugin: TicalPlugin) -> bool:
        """
        Register a plugin instance.
        
        Args:
            plugin: Plugin instance to register
            
        Returns:
            True if registered successfully
        """
        if not plugin.is_available():
            logger.warning(f"Plugin {plugin.metadata.name} not available (missing dependencies)")
            return False
        
        self.plugins[plugin.metadata.name] = plugin
        logger.info(f"Registered plugin: {plugin.metadata.name}")
        return True
    
    def unregister_plugin(self, name: str) -> bool:
        """Unregister a plugin."""
        if name in self.plugins:
            del self.plugins[name]
            logger.info(f"Unregistered plugin: {name}")
            return True
        return False
    
    async def load_plugin(self, plugin_class: Type[TicalPlugin]) -> bool:
        """
        Load and initialize a plugin.
        
        Args:
            plugin_class: Plugin class to instantiate
            
        Returns:
            True if loaded successfully
        """
        plugin = plugin_class()
        
        if not plugin.is_available():
            logger.warning(f"Plugin {plugin.metadata.name} not available")
            return False
        
        if self.context:
            await plugin.init(self.context)
        
        return self.register_plugin(plugin)
    
    async def unload_plugin(self, name: str) -> bool:
        """Unload a plugin gracefully."""
        if name not in self.plugins:
            return False
        
        plugin = self.plugins[name]
        await plugin.shutdown()
        return self.unregister_plugin(name)
    
    async def load_all(self, plugin_classes: List[Type[TicalPlugin]]):
        """Load multiple plugins."""
        for plugin_class in plugin_classes:
            await self.load_plugin(plugin_class)
    
    async def shutdown_all(self):
        """Shutdown all plugins."""
        for name in list(self.plugins.keys()):
            await self.unload_plugin(name)
    
    def get_plugin(self, name: str) -> Optional[TicalPlugin]:
        """Get a plugin by name."""
        return self.plugins.get(name)
    
    def list_plugins(self) -> List[PluginMetadata]:
        """List all registered plugins."""
        return [p.get_metadata() for p in self.plugins.values()]
    
    def get_all_tools(self) -> Dict[str, Callable]:
        """Get all tools from all plugins."""
        tools = {}
        for plugin in self.plugins.values():
            for name, tool_func in plugin.get_tools().items():
                tools[f"{plugin.metadata.name}.{name}"] = tool_func
        return tools
    
    def discover_plugins(self, package_name: str) -> List[Type[TicalPlugin]]:
        """
        Discover plugins in a package.
        
        Args:
            package_name: Package to search for plugins
            
        Returns:
            List of discovered plugin classes
        """
        try:
            package = importlib.import_module(package_name)
        except ImportError:
            return []
        
        plugins = []
        
        # Find all TicalPlugin subclasses
        for importer, modname, ispkg in pkgutil.walk_packages(
            package.__path__,
            package.__name__ + '.',
        ):
            try:
                module = importlib.import_module(modname)
                for name in dir(module):
                    obj = getattr(module, name)
                    if (isinstance(obj, type) and 
                        issubclass(obj, TicalPlugin) and 
                        obj != TicalPlugin):
                        plugins.append(obj)
            except Exception as e:
                logger.debug(f"Could not import {modname}: {e}")
        
        return plugins

# =============================================================================
# Global Plugin Manager
# =============================================================================

_global_manager: Optional[PluginManager] = None

def get_plugin_manager(plugins_dir: Optional[str] = None) -> PluginManager:
    """Get or create the global plugin manager."""
    global _global_manager
    if _global_manager is None:
        _global_manager = PluginManager(plugins_dir)
    return _global_manager

def reset_plugin_manager():
    """Reset the global plugin manager."""
    global _global_manager
    if _global_manager:
        asyncio.run(_global_manager.shutdown_all())
    _global_manager = None

# =============================================================================
# Built-in Plugin Registry
# =============================================================================

# , PluginManager.load_all() 
# Load,

def get_builtin_plugins() -> List[Type[TicalPlugin]]:
    """ """
    plugins = []

    # SearchPlugin (Search)
    try:
        from .search_plugin import SearchPlugin
        plugins.append(SearchPlugin)
    except ImportError as e:
        logger.debug(f"SearchPlugin not available: {e}")

    # P1-6:  WebSearchPlugin( requests/beautifulsoup4),
    #  SearchPlugin(search_plugin/)

    # BrowserPlugin →  Browser Bridge( + Bridge Server)
    #  BrowserPlugin(TicalPlugin) , browser_bridge_tool 
    #  Playwright/Selenium , v0.2 
    try:
        from .browser import _AVAILABLE as _browser_available
        if _browser_available:
            logger.debug("Browser Bridge plugin available (tool-registry mode)")
    except ImportError as e:
        logger.debug(f"Browser Bridge not available: {e}")

    # TradingPlugin ( httpx, websockets; FULL edition only)
    try:
        from .trading import TradingPlugin
        plugins.append(TradingPlugin)
    except ImportError as e:
        logger.debug(f"TradingPlugin not available: {e}")

    return plugins
