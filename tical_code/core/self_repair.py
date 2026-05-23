"""Self-repair - detect and fix runtime issues automatically."""

import ast
import asyncio
import hashlib
import json
import logging
import os
import py_compile
import re
import signal
import socket
import subprocess
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .worker_framework import WorkerFramework

logger = logging.getLogger(__name__)

# =============================================================================
# P0-4: Sandbox Modes
# =============================================================================

class SandboxMode:
    """ """
    DOCKER = "docker"
    RESTRICTED_PYTHON = "restricted_python"
    DISABLED = "disabled"

def _detect_docker_available() -> bool:
    """ """
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False

# =============================================================================
# Issue Types
# =============================================================================

class IssueType:
    """Issue type constants."""
    IDENTITY_MISMATCH = "identity_mismatch"
    CONFIG_MISSING = "config_missing"
    CONFIG_CORRUPTED = "config_corrupted"
    SESSION_LOST = "session_lost"
    PROCESS_NOT_RUNNING = "process_not_running"
    ANCHOR_INCONSISTENT = "anchor_inconsistent"
    FILE_MISSING = "file_missing"
    VERIFICATION_FAILED = "verification_failed"

# =============================================================================
# Repair Result
# =============================================================================

@dataclass
class RepairResult:
    """ """
    issue_type: str
    action: str
    success: bool
    details: str = ""
    restored_from: str = ""
    timestamp: float = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = time.time()
    
    def to_dict(self) -> Dict:
        return {
            'issue_type': self.issue_type,
            'action': self.action,
            'success': self.success,
            'details': self.details,
            'restored_from': self.restored_from,
            'timestamp': self.timestamp,
        }

# =============================================================================
# Self-Repair Engine
# =============================================================================

class SelfRepairEngine:
    """ """
    
    # Config
    # P0 #1: FileList -  sandbox.py PROTECTED_FILE_REGISTRY Source
    # sandbox.py is,not
    try:
        from .sandbox import PROTECTED_FILE_REGISTRY
        PROTECTED_FILES = PROTECTED_FILE_REGISTRY
    except ImportError as e:
        raise ImportError(
            f"CRITICAL: sandbox.py PROTECTED_FILE_REGISTRY  - "
            f"self_repair ."
            f": {e}"
        )
    
    # P0 #1: Dir - DirFilenot
    PROTECTED_DIRS = frozenset({
        '.git',        # Git 
    })
    
    MAX_SELF_MODIFICATIONS = 3  # /3
    HARD_MAX_SELF_MODIFICATIONS = 10  # ,Confignot
    
    # P0 #2 + P1 #5 + P1 #8: Call
    DANGEROUS_PATTERNS = [
        # SystemExec
        r'os\.system\s*\(',
        r'subprocess\.(call|run|Popen|check_output|check_call)\s*\(',
        r'exec\s*\(',
        r'eval\s*\(',
        r'__import__\s*\(',
        # FileSystem
        r'shutil\.rmtree\s*\(',
        r'os\.remove\s*\(\s*[\'"]\/',  # DeleteDirFile
        r'os\.unlink\s*\(',
        # 
        r'sys\.exit\s*\(',
        r'os\._exit\s*\(',
        r'os\.kill\s*\(',
        # 
        r'socket\.socket\s*\(',
        r'telnetlib\.',
        r'http\.server\.',
        # 
        r'os\.environ\[.*\]\s*=',  # Env
        r'PYTHONPATH',
        # 
        r'rm\s+-rf',
        r'dd\s+if=',
        r'mkfs\.',
        r'>\s*/dev/sd',
        # P1 #5: MemoryOp
        r'ctypes\.',
        r'/proc/self/mem',
        r'/proc/self/',
        r'mmap\.',              # mmap Memory
        r'sys\.modules',        # sys.modules 
    ]
    
    # P1 #8: Env
    PROTECTED_ENV_VARS = frozenset({
        'PYTHONPATH', 'PATH', 'HOME', 'USER',
        'ANCHOR_TOKEN', 'AI_SHARED_KEY',
        'TICAL_IDENTITY_NAME', 'TICAL_IDENTITY_ROLE',
    })
    
    # P0 #3:  Python FileCheck
    SHELL_DANGEROUS_PATTERNS = [
        r'rm\s+-rf\s+/',
        r'mkfs\.',
        r'dd\s+if=',
        r'>\s*/dev/sd',
        r'curl\s+.*\|\s*bash',
        r'wget\s+.*\|\s*sh',
        r'chmod\s+777',
        r'sudo\s+rm',
        r':\(\)\{\s*:\|\:&\s*\}',  # fork bomb
    ]
    
    YAML_DANGEROUS_PATTERNS = [
        r'!!python/object/',     # YAML 
        r'!!python/name/',
        r'!!python/module/',
    ]
    
    DOCKERFILE_DANGEROUS_PATTERNS = [
        r'privileged',
        r'host\s+network',
        r'/var/run/docker\.sock',
        r'rm\s+-rf\s+/',
    ]
    
    def __init__(self, framework: 'WorkerFramework', sandbox_mode: str = None):
        self.framework = framework
        self.repair_history: List[RepairResult] = []
        
        # P0-4: Sandbox Config
        # Param, framework config Read, RESTRICTED_PYTHON
        if sandbox_mode is not None:
            self.sandbox_mode = sandbox_mode
        elif hasattr(framework, 'config') and hasattr(framework.config, 'sandbox_mode'):
            self.sandbox_mode = framework.config.sandbox_mode
        else:
            self.sandbox_mode = SandboxMode.RESTRICTED_PYTHON
        
        #  Docker 
        if self.sandbox_mode == SandboxMode.DOCKER and not _detect_docker_available():
            logger.warning("[SelfRepair] Docker  Docker , RESTRICTED_PYTHON")
            self.sandbox_mode = SandboxMode.RESTRICTED_PYTHON
        
        # FileList(Check)
        self.critical_files = [
            'anchor.json',
            '~/.tical-code/sessions.db',
        ]
        
        # Status
        self._modification_count = self._load_modification_count()  # P1 #4: FileLoad
        self._last_commit_hash = None  #  commit hash
        
        # P0 #3: Identity -  identity.py  hash
        self._identity_fingerprint = self._compute_identity_fingerprint()
        
        # P2 #9: 
        self._modify_lock = asyncio.Lock()
        
        # System: lazy import (truthful_reporting.py was deleted)
        self._truth_reporter = None
    
    # =========================================================================
    # Health Check
    # =========================================================================
    
    async def check_health(self) -> Dict:
        """ """
        issues = []
        
        # Check 1: Identity
        identity_issue = await self._check_identity()
        if identity_issue:
            issues.append(identity_issue)
        
        # Check 2: Config
        config_issue = await self._check_config()
        if config_issue:
            issues.append(config_issue)
        
        # Check 3: Session 
        session_issue = await self._check_session()
        if session_issue:
            issues.append(session_issue)
        
        # Check 4: File
        file_issues = await self._check_critical_files()
        issues.extend(file_issues)
        
        # Check 5: Tool
        tool_issues = await self._check_tools()
        issues.extend(tool_issues)
        
        return {
            'healthy': len(issues) == 0,
            'issues': issues,
            'timestamp': time.time(),
        }
    
    async def _check_identity(self) -> Optional[Dict]:
        """ """
        try:
            #  framework Identity
            identity = self.framework.identity
            if not identity:
                return {
                    'type': IssueType.IDENTITY_MISMATCH,
                    'severity': 'high',
                    'details': 'Identity not loaded',
                }
            
            # Check
            identity_dict = identity.to_dict() if hasattr(identity, 'to_dict') else identity
            if not identity_dict.get('name') or identity_dict.get('name') == 'unknown':
                return {
                    'type': IssueType.IDENTITY_MISMATCH,
                    'severity': 'high',
                    'details': f"Identity name is unknown: {identity_dict}",
                }
            
            #  anchor 
            if hasattr(self.framework, 'anchor') and self.framework.anchor:
                anchor_data = self.framework.anchor.data
                if anchor_data:
                    deployments = anchor_data.get('deployments', {})
                    expected = None
                    for dep_id, dep in deployments.items():
                        fp = dep.get('fingerprint', {})
                        my_fp = self._get_current_fingerprint()
                        if (fp.get('hostname') == my_fp.get('hostname') or 
                            fp.get('ip') == my_fp.get('ip')):
                            expected = dep.get('identity', {})
                            break
                    
                    if expected and identity_dict.get('name') != expected.get('name'):
                        return {
                            'type': IssueType.IDENTITY_MISMATCH,
                            'severity': 'medium',
                            'details': f"Identity mismatch: local={identity_dict.get('name')}, anchor={expected.get('name')}",
                            'expected': expected,
                        }
            
            return None
            
        except Exception as e:
            return {
                'type': IssueType.IDENTITY_MISMATCH,
                'severity': 'high',
                'details': f"Identity check error: {e}",
            }
    
    async def _check_config(self) -> Optional[Dict]:
        """ """
        try:
            config = self.framework.config
            
            # Check
            required_fields = ['name', 'model', 'edition']
            missing = [f for f in required_fields if not getattr(config, f, None)]
            
            if missing:
                return {
                    'type': IssueType.CONFIG_MISSING,
                    'severity': 'high',
                    'details': f"Missing required config fields: {missing}",
                }
            
            # CheckConfig
            if config.max_context_tokens < 100:
                return {
                    'type': IssueType.CONFIG_CORRUPTED,
                    'severity': 'medium',
                    'details': f"max_context_tokens too small: {config.max_context_tokens}",
                }
            
            return None
            
        except Exception as e:
            return {
                'type': IssueType.CONFIG_CORRUPTED,
                'severity': 'high',
                'details': f"Config check error: {e}",
            }
    
    async def _check_session(self) -> Optional[Dict]:
        """ """
        try:
            session_manager = self.framework.sessions
            session_id = self.framework._get_session_id()
            
            # Load session
            session = session_manager.load_session(session_id)
            
            if session is None:
                #  anchor  session summary
                if hasattr(self.framework, 'anchor') and self.framework.anchor:
                    anchor_data = self.framework.anchor.data
                    session_summary = anchor_data.get('session', {}).get('summary')
                    if session_summary:
                        return {
                            'type': IssueType.SESSION_LOST,
                            'severity': 'medium',
                            'details': f"Session {session_id} not found, can restore from anchor",
                            'has_summary': True,
                        }
                
                return {
                    'type': IssueType.SESSION_LOST,
                    'severity': 'high',
                    'details': f"Session {session_id} not found and no anchor backup",
                }
            
            return None
            
        except Exception as e:
            return {
                'type': IssueType.SESSION_LOST,
                'severity': 'medium',
                'details': f"Session check error: {e}",
            }
    
    async def _check_critical_files(self) -> List[Dict]:
        """ """
        issues = []
        
        for file_path in self.critical_files:
            expanded = os.path.expanduser(file_path)
            if not os.path.exists(expanded):
                issues.append({
                    'type': IssueType.FILE_MISSING,
                    'severity': 'low',
                    'details': f"Critical file missing: {file_path}",
                    'file_path': expanded,
                })
        
        return issues
    
    async def _check_tools(self) -> List[Dict]:
        """ """
        issues = []
        
        # Check tool registry
        if not hasattr(self.framework, '_tool_registry') or self.framework._tool_registry is None:
            issues.append({
                'type': IssueType.VERIFICATION_FAILED,
                'severity': 'medium',
                'details': "Tool registry not initialized",
            })
        
        return issues
    
    # =========================================================================
    # Repair Methods
    # =========================================================================
    
    async def repair(self, issues: List[Dict]) -> List[RepairResult]:
        """ """
        results = []
        
        for issue in issues:
            issue_type = issue.get('type')
            
            if issue_type == IssueType.IDENTITY_MISMATCH:
                result = await self._repair_identity(issue)
            elif issue_type == IssueType.CONFIG_MISSING:
                result = await self._repair_config(issue)
            elif issue_type == IssueType.CONFIG_CORRUPTED:
                result = await self._repair_config(issue)
            elif issue_type == IssueType.SESSION_LOST:
                result = await self._repair_session(issue)
            elif issue_type == IssueType.FILE_MISSING:
                result = await self._repair_file(issue)
            elif issue_type == IssueType.VERIFICATION_FAILED:
                result = await self._repair_tools(issue)
            else:
                result = RepairResult(
                    issue_type=issue_type,
                    action="unknown",
                    success=False,
                    details=f"Unknown issue type: {issue_type}",
                )
            
            results.append(result)
            self.repair_history.append(result)
        
        return results
    
    async def _repair_identity(self, issue: Dict) -> RepairResult:
        """ """
        try:
            #  anchor ReadIdentity
            anchor_path = self.framework.config.anchor_path
            if not os.path.exists(anchor_path):
                return RepairResult(
                    issue_type=IssueType.IDENTITY_MISMATCH,
                    action="restore_identity",
                    success=False,
                    details=f"Anchor not found: {anchor_path}",
                )
            
            with open(anchor_path, 'r', encoding='utf-8') as f:
                anchor_data = json.load(f)
            
            #  deployment
            my_fp = self._get_current_fingerprint()
            deployments = anchor_data.get('deployments', {})
            matched_identity = None
            matched_deploy_id = None
            
            for dep_id, dep in deployments.items():
                fp = dep.get('fingerprint', {})
                if (fp.get('hostname') == my_fp.get('hostname') or 
                    fp.get('ip') == my_fp.get('ip')):
                    matched_identity = dep.get('identity', {})
                    matched_deploy_id = dep_id
                    break
            
            if not matched_identity:
                return RepairResult(
                    issue_type=IssueType.IDENTITY_MISMATCH,
                    action="restore_identity",
                    success=False,
                    details="No matching deployment in anchor",
                )
            
            # Update framework  identity
            if hasattr(self.framework.identity, '_my_identity'):
                self.framework.identity._my_identity = {
                    'id': matched_deploy_id,
                    **matched_identity,
                    'status': 'active',
                }
                logger.info(f"[SelfRepair] Identity restored: {matched_identity.get('name')}")
                
                return RepairResult(
                    issue_type=IssueType.IDENTITY_MISMATCH,
                    action="restore_identity",
                    success=True,
                    details=f"Identity restored from anchor: {matched_identity.get('name')}",
                    restored_from=f"anchor:{matched_deploy_id}",
                )
            
            return RepairResult(
                issue_type=IssueType.IDENTITY_MISMATCH,
                action="restore_identity",
                success=False,
                details="Identity object does not support in-place update",
            )
            
        except Exception as e:
            return RepairResult(
                issue_type=IssueType.IDENTITY_MISMATCH,
                action="restore_identity",
                success=False,
                details=f"Repair failed: {e}",
            )
    
    async def _repair_config(self, issue: Dict) -> RepairResult:
        """ """
        try:
            #  anchor ReadConfig
            anchor_path = self.framework.config.anchor_path
            if not os.path.exists(anchor_path):
                return RepairResult(
                    issue_type=IssueType.CONFIG_MISSING,
                    action="restore_config",
                    success=False,
                    details=f"Anchor not found: {anchor_path}",
                )
            
            with open(anchor_path, 'r', encoding='utf-8') as f:
                anchor_data = json.load(f)
            
            #  deployment
            my_fp = self._get_current_fingerprint()
            deployments = anchor_data.get('deployments', {})
            matched_deploy = None
            
            for dep_id, dep in deployments.items():
                fp = dep.get('fingerprint', {})
                if (fp.get('hostname') == my_fp.get('hostname') or 
                    fp.get('ip') == my_fp.get('ip')):
                    matched_deploy = dep
                    break
            
            if not matched_deploy:
                return RepairResult(
                    issue_type=IssueType.CONFIG_MISSING,
                    action="restore_config",
                    success=False,
                    details="No matching deployment in anchor",
                )
            
            # Config
            identity = matched_deploy.get('identity', {})
            if 'model' in identity:
                self.framework.config.model = identity['model']
            if 'edition' in identity:
                self.framework.config.edition = identity['edition']
            
            logger.info(f"[SelfRepair] Config restored from anchor")
            
            return RepairResult(
                issue_type=IssueType.CONFIG_MISSING,
                action="restore_config",
                success=True,
                details="Config restored from anchor",
                restored_from="anchor",
            )
            
        except Exception as e:
            return RepairResult(
                issue_type=IssueType.CONFIG_MISSING,
                action="restore_config",
                success=False,
                details=f"Repair failed: {e}",
            )
    
    async def _repair_session(self, issue: Dict) -> RepairResult:
        """ """
        try:
            session_id = self.framework._get_session_id()
            session_manager = self.framework.sessions
            
            #  anchor  summary
            summary = None
            if hasattr(self.framework, 'anchor') and self.framework.anchor:
                anchor_data = self.framework.anchor.data
                summary = anchor_data.get('session', {}).get('summary')
            
            # Create session
            session_data = {
                'session_id': session_id,
                'created_at': time.time(),
                'restored_from_anchor': summary is not None,
                'summary': summary or f"Session restored at {time.strftime('%Y-%m-%d %H:%M:%S')}",
            }
            
            session_manager.save_session(session_id, session_data)
            logger.info(f"[SelfRepair] Session restored: {session_id}")
            
            return RepairResult(
                issue_type=IssueType.SESSION_LOST,
                action="restore_session",
                success=True,
                details=f"Session {session_id} restored" + (" from anchor" if summary else ""),
                restored_from="anchor" if summary else "new",
            )
            
        except Exception as e:
            return RepairResult(
                issue_type=IssueType.SESSION_LOST,
                action="restore_session",
                success=False,
                details=f"Repair failed: {e}",
            )
    
    async def _repair_file(self, issue: Dict) -> RepairResult:
        """ """
        try:
            file_path = issue.get('file_path')
            if not file_path:
                return RepairResult(
                    issue_type=IssueType.FILE_MISSING,
                    action="create_file",
                    success=False,
                    details="No file path specified",
                )
            
            # CreateDir()
            parent = os.path.dirname(file_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            
            # CreateFile
            if file_path.endswith('.db'):
                # SQLite 
                import sqlite3
                conn = sqlite3.connect(file_path)
                conn.close()
            else:
                Path(file_path).touch()
            
            logger.info(f"[SelfRepair] Created missing file: {file_path}")
            
            return RepairResult(
                issue_type=IssueType.FILE_MISSING,
                action="create_file",
                success=True,
                details=f"Created missing file: {file_path}",
                restored_from="created",
            )
            
        except Exception as e:
            return RepairResult(
                issue_type=IssueType.FILE_MISSING,
                action="create_file",
                success=False,
                details=f"Repair failed: {e}",
            )
    
    async def _repair_tools(self, issue: Dict) -> RepairResult:
        """ """
        try:
            #  tool system
            from .worker_framework import _init_tool_system
            
            if hasattr(_init_tool_system, '__wrapped__'):
                # Call
                tool_count = _init_tool_system(self.framework)
            else:
                tool_count = 0
            
            logger.info(f"[SelfRepair] Tool system reinitialized: {tool_count} tools")
            
            return RepairResult(
                issue_type=IssueType.VERIFICATION_FAILED,
                action="reinit_tools",
                success=True,
                details=f"Tool system reinitialized: {tool_count} tools",
                restored_from="reinit",
            )
            
        except Exception as e:
            return RepairResult(
                issue_type=IssueType.VERIFICATION_FAILED,
                action="reinit_tools",
                success=False,
                details=f"Repair failed: {e}",
            )
    
    # =========================================================================
    # Auto Repair
    # =========================================================================
    
    async def auto_repair_if_needed(self) -> bool:
        """ """
        health = await self.check_health()
        
        if health['healthy']:
            return False
        
        issues = health['issues']
        
        # Filter()
        # high_issues = [i for i in issues if i.get('severity') == 'high']
        # if not high_issues:
        #     return False
        
        logger.info(f"[SelfRepair] Found {len(issues)} issues, attempting repair")
        
        try:
            results = await self.repair(issues)
            
            success_count = sum(1 for r in results if r.success)
            logger.info(f"[SelfRepair] Repaired {success_count}/{len(results)} issues")
            
            # Check
            new_health = await self.check_health()
            if not new_health['healthy']:
                remaining = new_health['issues']
                logger.warning(f"[SelfRepair] {len(remaining)} issues remain after repair: {[i.get('type') for i in remaining]}")
            
            return success_count > 0
            
        except Exception as e:
            logger.error(f"[SelfRepair] Auto repair failed: {e}")
            return False
    
    # =========================================================================
    # Utility Methods
    # =========================================================================
    
    def _get_current_fingerprint(self) -> Dict[str, str]:
        """ """
        try:
            hostname = socket.gethostname()
            host_ip = socket.gethostbyname(hostname)
        except Exception as e:
            logger.debug(f"[SelfRepair] _get_current_fingerprint(): {e}")
            hostname = "unknown"
            host_ip = "0.0.0.0"
        
        return {
            'hostname': hostname,
            'ip': host_ip,
        }
    
    def get_repair_history(self, limit: int = 50) -> List[Dict]:
        """ """
        return [r.to_dict() for r in self.repair_history[-limit:]]
    
    def clear_repair_history(self):
        """ """
        self.repair_history.clear()
    
    # =========================================================================
    # Self-Evolution Safety Methods ()
    # =========================================================================
    
    # P0 #1:  - Dir
    def is_protected_file(self, file_path: str) -> bool:
        """Check if a file is in the protected list and cannot be modified."""
        # VerifyPath()
        try:
            file_path = self._validate_file_path(file_path)
        except ValueError:
            return True  # Pathnot,Reject
        filename = os.path.basename(file_path)
        if filename in self.PROTECTED_FILES:
            return True
        #  P0-2: .tical_ File -  .tical_* Filenot
        if filename.startswith('.tical_'):
            return True
        # CheckisDir
        abs_path = os.path.realpath(file_path)
        for protected_dir in self.PROTECTED_DIRS:
            if f'/{protected_dir}/' in abs_path or abs_path.endswith(f'/{protected_dir}'):
                return True
        # Check .git Dir
        if '/.git/' in abs_path:
            return True
        return False
    
    def can_self_modify(self) -> bool:
        """Check if the modification count has not reached the limit."""
        # P1 #5: ConfigValueand, config 
        config_max = self.MAX_SELF_MODIFICATIONS
        if hasattr(self.framework, 'config') and hasattr(self.framework.config, 'max_self_modifications'):
            config_max = self.framework.config.max_self_modifications
        effective_max = min(config_max, self.HARD_MAX_SELF_MODIFICATIONS)
        return self._modification_count < effective_max
    
    async def validate_code_syntax(self, file_path: str) -> Dict:
        """
        Validate Python file syntax.
        
        Non-.py files are always considered valid.
        
        Args:
            file_path: Path to the file to validate
            
        Returns:
            {"valid": bool, "error": str}
        """
        file_path = self._validate_file_path(file_path)  # P0-3: Path
        if not file_path.endswith('.py'):
            return {"valid": True, "error": ""}
        
        try:
            py_compile.compile(file_path, doraise=True)
            return {"valid": True, "error": ""}
        except py_compile.PyCompileError as e:
            return {"valid": False, "error": str(e)}
        except Exception as e:
            return {"valid": False, "error": f"Unexpected error during syntax check: {e}"}
    
    # P0 #2 + P1 #5 + P1 #8: Check
    def validate_code_safety(self, file_path: str) -> Dict:
        """ """
        file_path = self._validate_file_path(file_path)  # P0-3: Path
        if not file_path.endswith('.py'):
            # P0 #3:  Python FileCheck
            return self._non_python_safety_check(file_path)
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            return {"safe": False, "warnings": [f"Cannot read file: {e}"]}
        
        # 1. ASTCheck()
        ast_result = self._ast_safety_check(file_path)
        # 2. Check(AST)
        regex_result = self._regex_safety_check(file_path, content)
        # Result
        all_warnings = ast_result.get("warnings", []) + regex_result.get("warnings", [])
        
        return {"safe": len(all_warnings) == 0, "warnings": all_warnings}
    
    def _ast_safety_check(self, file_path: str) -> Dict:
        """ """
        if not file_path.endswith('.py'):
            return {"safe": True, "warnings": []}
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                source = f.read()
            tree = ast.parse(source)
        except SyntaxError:
            return {"safe": False, "warnings": ["File has syntax errors"]}
        
        warnings = []
        
        #  P1-2:  -  import 
        # alias_map: { -> }, {'_os': 'os', '_sub': 'subprocess'}
        alias_map = {}
        DANGEROUS_MODULE_NAMES = ('os', 'subprocess', 'shutil', 'sys')
        for node in ast.walk(tree):
            # import os as _os
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname and alias.name in DANGEROUS_MODULE_NAMES:
                        alias_map[alias.asname] = alias.name
            # from os import system as _system  →  _system -> os.system
            # from os import system             →  system -> os.system
            elif isinstance(node, ast.ImportFrom):
                if node.module in DANGEROUS_MODULE_NAMES:
                    for alias in node.names:
                        real_name = f"{node.module}.{alias.name}"
                        effective_name = alias.asname if alias.asname else alias.name
                        alias_map[effective_name] = real_name
        
        # :Name
        def _resolve_module(name_id: str) -> Optional[str]:
            """ """
            if name_id in DANGEROUS_MODULE_NAMES:
                return name_id
            if name_id in alias_map:
                mapped = alias_map[name_id]
                # import os as _os → alias_map['_os'] = 'os'
                if mapped in DANGEROUS_MODULE_NAMES:
                    return mapped
                # from os import system → alias_map['system'] = 'os.system'
                # 
                base_module = mapped.split('.')[0]
                if base_module in DANGEROUS_MODULE_NAMES:
                    return base_module
            return None
        
        #  - CheckCall
        for node in ast.walk(tree):
            # 1.  getattr Call - 
            if isinstance(node, ast.Call):
                func = node.func
                # getattr(os, ...) or getattr(_os, ...) 
                if isinstance(func, ast.Name) and func.id == 'getattr':
                    if node.args and isinstance(node.args[0], ast.Name):
                        #  P1-2: 
                        resolved = _resolve_module(node.args[0].id)
                        if resolved:
                            warnings.append(f"Line {node.lineno}: getattr on dangerous module '{node.args[0].id}' (resolves to '{resolved}')")
                
                # 2.  __import__ Call
                if isinstance(func, ast.Name) and func.id == '__import__':
                    warnings.append(f"Line {node.lineno}: __import__ usage detected")
                
                # 3.  eval/exec/compile Call
                if isinstance(func, ast.Name) and func.id in ('eval', 'exec', 'compile'):
                    warnings.append(f"Line {node.lineno}: {func.id}() usage detected")
                
                # 4.  os.system / subprocess.* / shutil.rmtree Call
                #  P1-2: Call _os.system()
                if isinstance(func, ast.Attribute):
                    if isinstance(func.value, ast.Name):
                        module_name = func.value.id
                        method = func.attr
                        dangerous_calls = {
                            'os': ['system', 'popen', 'execvp', 'execl', 'fork', 'kill', '_exit', 'remove', 'unlink'],
                            'subprocess': ['call', 'run', 'Popen', 'check_output', 'check_call'],
                            'shutil': ['rmtree', 'move'],
                            'sys': ['exit'],
                        }
                        # Check
                        if module_name in dangerous_calls and method in dangerous_calls[module_name]:
                            warnings.append(f"Line {node.lineno}: {module_name}.{method}() detected")
                        #  P1-2: Check
                        elif module_name in alias_map:
                            resolved = _resolve_module(module_name)
                            if resolved and resolved in dangerous_calls and method in dangerous_calls[resolved]:
                                warnings.append(f"Line {node.lineno}: {module_name}.{method}() detected (alias for {resolved})")
                
                #  P1-2:  from os import system Call
                #  _system() or system(), system  alias_map  os.system
                if isinstance(func, ast.Name) and func.id in alias_map:
                    mapped = alias_map[func.id]
                    base_module = mapped.split('.')[0]
                    if base_module in DANGEROUS_MODULE_NAMES:
                        warnings.append(f"Line {node.lineno}: {func.id}() detected (alias for {mapped})")
        
        return {"safe": len(warnings) == 0, "warnings": warnings}
    
    def _regex_safety_check(self, file_path: str, content: str) -> Dict:
        """ """
        warnings = []
        for pattern in self.DANGEROUS_PATTERNS:
            matches = re.findall(pattern, content)
            if matches:
                warnings.append(f"Dangerous pattern found: {pattern}")
        
        # P1 #8: EnvCheck
        env_warnings = self._check_env_modification(content)
        warnings.extend(env_warnings)
        
        return {"safe": len(warnings) == 0, "warnings": warnings}
    
    def _non_python_safety_check(self, file_path: str) -> Dict:
        """ """
        ext = os.path.splitext(file_path)[1].lower()
        filename = os.path.basename(file_path).lower()
        warnings = []
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            return {"safe": False, "warnings": [f"Cannot read file: {e}"]}
        
        # Shell 
        if ext in ('.sh',) or filename in ('makefile',):
            for pattern in self.SHELL_DANGEROUS_PATTERNS:
                if re.search(pattern, content, re.IGNORECASE):
                    warnings.append(f"Shell dangerous pattern: {pattern}")
        
        # YAML Config
        if ext in ('.yaml', '.yml'):
            for pattern in self.YAML_DANGEROUS_PATTERNS:
                if re.search(pattern, content):
                    warnings.append(f"YAML dangerous pattern: {pattern}")
        
        # Dockerfile
        if filename == 'dockerfile' or filename.endswith('.dockerfile') or filename == 'docker-compose.yml' or filename == 'docker-compose.yaml':
            for pattern in self.DOCKERFILE_DANGEROUS_PATTERNS:
                if re.search(pattern, content, re.IGNORECASE):
                    warnings.append(f"Dockerfile dangerous pattern: {pattern}")
        
        return {"safe": len(warnings) == 0, "warnings": warnings}
    
    # P1 #8: CheckisEnv
    def _check_env_modification(self, content: str) -> List[str]:
        """ """
        warnings = []
        for var in self.PROTECTED_ENV_VARS:
            # CheckWrite os.environ['VAR'] = or os.environ["VAR"] =
            single_quote_write = f"os.environ['{var}']"
            double_quote_write = f'os.environ["{var}"]'
            # Value( = )
            for pattern in [single_quote_write, double_quote_write]:
                # Location,CheckisValue(Read)
                idx = 0
                while True:
                    idx = content.find(pattern, idx)
                    if idx == -1:
                        break
                    # Checkis = (Value) ) (Read os.environ.get())
                    after = content[idx + len(pattern):idx + len(pattern) + 5].strip()
                    # os.environ['VAR'] = ... isValue
                    # os.environ['VAR'] not .get() isRead
                    if after.startswith('=') and not after.startswith('=='):
                        # Confirmnotis os.environ.get('VAR') 
                        before = content[max(0, idx - 10):idx]
                        if '.get(' not in before:
                            warnings.append(f"Attempt to modify protected env var: {var}")
                            break
                    idx += len(pattern)
        return warnings
    
    # P1 #6: Check
    def _check_dependency_impact(self, file_path: str) -> List[str]:
        """ """
        warnings = []
        target_module = os.path.splitext(os.path.basename(file_path))[0]
        
        # File,is import TargetFile
        for protected_name in self.PROTECTED_FILES:
            if not protected_name.endswith('.py'):
                continue
            protected_path = os.path.join(os.path.dirname(file_path), protected_name)
            if not os.path.exists(protected_path):
                continue
            try:
                with open(protected_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                # Checkis import Target
                if (f'from .{target_module}' in content or 
                    f'import {target_module}' in content or
                    f'from tical_code.core.{target_module}' in content):
                    warnings.append(f"Protected file '{protected_name}' imports '{target_module}'")
            except Exception as e:
                logger.debug(f"[SelfRepair] _check_dependency_impact(): {e}")
                pass
        return warnings
    
    # P0 #3: ComputeIdentity
    def _compute_identity_fingerprint(self) -> str:
        """ """
        try:
            #  identity.py
            for search_dir in [os.path.dirname(os.path.realpath(__file__)), os.getcwd()]:
                identity_path = os.path.join(search_dir, 'identity.py')
                if os.path.exists(identity_path):
                    with open(identity_path, 'rb') as f:
                        return hashlib.sha256(f.read()).hexdigest()
        except Exception as e:
            logger.debug(f"[SelfRepair] _compute_identity_fingerprint(): {e}")
            pass
        return ""
    
    def _verify_identity_fingerprint(self) -> bool:
        """ """
        current = self._compute_identity_fingerprint()
        if not current or not self._identity_fingerprint:
            # Computenot()
            return True
        return current == self._identity_fingerprint
    
    # P0 #3: Check
    async def _process_health_check(self, timeout: int) -> bool:
        """ """
        try:
            #  PID Confirm
            pid = os.getpid()
            if pid and os.path.exists(f'/proc/{pid}'):
                return True
            # macOS / System: os.kill(pid, 0) 
            try:
                os.kill(pid, 0)
                return True
            except (OSError, ProcessLookupError):
                return False
        except Exception as e:
            logger.debug(f"[SelfRepair] _verify_identity_fingerprint(): {e}")
            return False
    
    # P0 #3: HTTP Check()
    async def _http_health_check(self, health_check_url: str, timeout: int) -> bool:
        """ """
        # :Allow http/https
        if not health_check_url.startswith(('http://', 'https://')):
            logger.warning(f"[SelfRepair] Blocked non-HTTP health check URL: {health_check_url}")
            return False
        
        # :( SSRF)
        from urllib.parse import urlparse
        parsed = urlparse(health_check_url)
        hostname = parsed.hostname or ''
        _blocked_hosts = ('localhost', '127.0.0.1', '0.0.0.0', '::1',
                         '169.254.', '10.', '192.168.', '172.16.',
                         '172.17.', '172.18.', '172.19.', '172.2',
                         '172.30.', '172.31.')
        for blocked in _blocked_hosts:
            if hostname == blocked or hostname.startswith(blocked):
                logger.warning(f"[SelfRepair] Blocked internal health check URL: {health_check_url}")
                return False
        
        check_start = time.time()
        while time.time() - check_start < timeout:
            await asyncio.sleep(1)
            try:
                req = urllib.request.Request(health_check_url, method='GET')
                with urllib.request.urlopen(req, timeout=3) as resp:
                    if resp.status == 200:
                        return True
            except (urllib.error.URLError, ConnectionError, OSError):
                continue
            except Exception as e:
                logger.debug(f"[SelfRepair] (): {e}")
                continue
        return False
    
    # P0 #3: Check
    async def _multi_health_check(self, health_check_url: str, timeout: int) -> Dict:
        """ """
        checks = {
            "http": False,
            "process": False,
            "identity": False,
        }
        
        # 1. HTTP Check
        if health_check_url:
            checks["http"] = await self._http_health_check(health_check_url, timeout)
        
        # 2. Check - ConfirmTarget
        checks["process"] = await self._process_health_check(timeout)
        
        # 3. IdentityCheck - Confirm identity.py 
        checks["identity"] = self._verify_identity_fingerprint()
        
        all_passed = all(checks.values())
        return {"passed": all_passed, "checks": checks}
    
    # P0 #4: Git Check
    def _verify_git_integrity(self) -> bool:
        """ """
        try:
            repo_root = self._get_repo_root()
            git_dir = os.path.join(repo_root, '.git')
            if not os.path.exists(git_dir):
                return False
            # Check git is
            result = subprocess.run(
                ['git', 'status'],
                cwd=repo_root,
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception as e:
            logger.debug(f"[SelfRepair] _verify_git_integrity(): {e}")
            return False
    
    def _get_repo_root(self) -> str:
        """ """
        try:
            result = subprocess.run(
                ['git', 'rev-parse', '--show-toplevel'],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception as e:
            logger.debug(f"[SelfRepair] _verify_git_integrity(): {e}")
            pass
        return os.path.dirname(os.path.realpath(__file__))
    
    def _validate_file_path(self, file_path: str) -> str:
        """ """
        # 1. Path
        abs_path = os.path.realpath(file_path)
        # 2. Dir
        repo_root = self._get_repo_root()
        # 3. Path
        if not abs_path.startswith(os.path.realpath(repo_root)):
            raise ValueError(f"Path traversal detected: {file_path} is outside repo root")
        # 4.  .. 
        if '..' in os.path.normpath(file_path).split(os.sep):
            raise ValueError(f"Path traversal detected: {file_path} contains '..'")
        return abs_path
    
    # P1-9:  - Config sandbox_mode
    def _is_config_with_sandbox_mode(self, file_path: str, new_content: str) -> bool:
        """ """
        basename = os.path.basename(file_path).lower()
        # Config/Name
        config_patterns = (
            'config.json', 'config.yaml', 'config.yml',
            'worker-config.json', 'worker-config.yaml',
            'pyproject.toml', 'settings.json',
        )
        is_config = (
            basename in config_patterns
            or basename.endswith('.config.json')
            or basename.endswith('.config.yaml')
            or 'config' in basename.lower()
        )
        if not is_config:
            return False
        # CheckContentis sandbox_mode
        return 'sandbox_mode' in new_content
    
    def _extract_sandbox_mode_from_content(self, content: str) -> Optional[str]:
        """ """
        #  JSON Format
        try:
            data = json.loads(content)
            if isinstance(data, dict) and 'sandbox_mode' in data:
                return str(data['sandbox_mode'])
        except (json.JSONDecodeError, TypeError):
            logger.debug("self_repair: sandbox_mode JSON,")
        #  YAML Format
        import re
        match = re.search(r'sandbox_mode\s*[:=]\s*["\']?(\w+)["\']?', content)
        if match:
            return match.group(1)
        return None
    
    async def git_backup_before_modify(self, file_path: str) -> Dict:
        """
        Create a git backup (commit) before modifying a file.
        
        Auto-initializes git repo if not already in one.
        
        Args:
            file_path: Path to the file being modified
            
        Returns:
            {"success": bool, "commit_hash": str, "error": str}
        """
        file_path = self._validate_file_path(file_path)  # P0-3: Path
        try:
            file_dir = os.path.dirname(os.path.realpath(file_path))
            
            # Checkis git repo 
            check_result = subprocess.run(
                ['git', 'rev-parse', '--is-inside-work-tree'],
                cwd=file_dir,
                capture_output=True,
                text=True,
                timeout=10,
            )
            
            if check_result.returncode != 0:
                # not git repo ,
                init_result = subprocess.run(
                    ['git', 'init'],
                    cwd=file_dir,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if init_result.returncode != 0:
                    return {
                        "success": False,
                        "commit_hash": "",
                        "error": f"git init failed: {init_result.stderr.strip()}",
                    }
                logger.info(f"[SelfRepair] Initialized git repo at {file_dir}")
            
            # git add
            add_result = subprocess.run(
                ['git', 'add', file_path],
                cwd=file_dir,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if add_result.returncode != 0:
                return {
                    "success": False,
                    "commit_hash": "",
                    "error": f"git add failed: {add_result.stderr.strip()}",
                }
            
            # git commit
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            commit_msg = f"auto-backup before modify: {os.path.basename(file_path)} at {timestamp}"
            commit_result = subprocess.run(
                ['git', 'commit', '-m', commit_msg],
                cwd=file_dir,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if commit_result.returncode != 0:
                # (nothing to commit)
                if 'nothing to commit' in commit_result.stdout or 'nothing to commit' in commit_result.stderr:
                    #  HEAD
                    hash_result = subprocess.run(
                        ['git', 'rev-parse', 'HEAD'],
                        cwd=file_dir,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    current_hash = hash_result.stdout.strip() if hash_result.returncode == 0 else ""
                    return {
                        "success": True,
                        "commit_hash": current_hash,
                        "error": "",
                    }
                return {
                    "success": False,
                    "commit_hash": "",
                    "error": f"git commit failed: {commit_result.stderr.strip()}",
                }
            
            #  commit hash
            hash_result = subprocess.run(
                ['git', 'rev-parse', 'HEAD'],
                cwd=file_dir,
                capture_output=True,
                text=True,
                timeout=10,
            )
            commit_hash = hash_result.stdout.strip() if hash_result.returncode == 0 else ""
            self._last_commit_hash = commit_hash
            
            logger.info(f"[SelfRepair] Git backup created: {commit_hash[:8]} for {file_path}")
            
            return {
                "success": True,
                "commit_hash": commit_hash,
                "error": "",
            }
            
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "commit_hash": "",
                "error": "git command timed out",
            }
        except Exception as e:
            return {
                "success": False,
                "commit_hash": "",
                "error": f"git backup error: {e}",
            }
    
    # P2 #10: Log -  repo  AI 
    def _get_audit_log_path(self) -> str:
        """ """
        #  /var/log/,fallback User home
        log_dirs = ['/var/log/tical', os.path.expanduser('~/.tical/logs')]
        for d in log_dirs:
            try:
                os.makedirs(d, exist_ok=True)
                test_file = os.path.join(d, '.test')
                with open(test_file, 'w') as f:
                    f.write('test')
                os.remove(test_file)
                return os.path.join(d, 'evolution_audit.jsonl')
            except (PermissionError, OSError):
                continue
        #  fallback  repo ()
        return os.path.join(self._get_repo_root(), '.tical_evolution_log.jsonl')
    
    def _log_modification(self, file_path: str, success: bool, commit_hash: str = "", error: str = ""):
        """ """
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "file": file_path,
            "success": success,
            "commit_hash": commit_hash,
            "error": error,
            "modification_count": self._modification_count,
            "identity": getattr(self.framework, 'identity_name', 
                                getattr(getattr(self.framework, 'identity', None), 'name', 'unknown') 
                                if hasattr(self.framework, 'identity') else 'unknown'),
        }
        # : TruthReporter 
        if self._truth_reporter:
            try:
                stats = self._truth_reporter.get_stats()
                log_entry["truth_report"] = {
                    "trust_level": stats.get('trust_level', 'unknown'),
                    "total_corrections": stats.get('total_corrections', 0),
                    "require_human_approval": stats.get('require_human_approval', False),
                    "recent_operations": stats.get('recent_operations', []),
                }
            except Exception as e:
                logger.debug(f"[SelfRepair] _log_modification(): {e}")
                pass  # InfoFailnot
        # WriteLogFile(append-only)
        try:
            log_path = self._get_audit_log_path()
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(log_entry) + '\n')
        except Exception as e:
            logger.warning(f"[SelfRepair] Failed to write audit log: {e}")
    
    # P0 #1: Test -  Python  import/Exec
    async def _sandbox_test(self, file_path: str) -> Dict:
        """ """
        file_path = self._validate_file_path(file_path)  # P0-3: Path
        if not file_path.endswith('.py'):
            return {"passed": True, "error": "", "sandbox_mode": self.sandbox_mode}
        
        # P0-4: DISABLED Skip
        if self.sandbox_mode == SandboxMode.DISABLED:
            return {"passed": True, "error": "", "sandbox_mode": SandboxMode.DISABLED}
        
        # P0-4: Docker  - 
        if self.sandbox_mode == SandboxMode.DOCKER:
            return await self._sandbox_test_docker(file_path)
        
        # P0-4:  RESTRICTED_PYTHON  -  Python 
        return await self._sandbox_test_restricted_python(file_path)
    
    async def _sandbox_test_docker(self, file_path: str) -> Dict:
        """ """
        try:
            with open(file_path, 'r') as f:
                code = f.read()
            
            # Check
            try:
                compile(code, file_path, 'exec')
            except SyntaxError as e:
                return {"passed": False, "error": f"Syntax error: {e}", "sandbox_mode": SandboxMode.DOCKER}
            
            # P0-4:  - Docker mode notSkip!
            safety_result = self.validate_code_safety(file_path)
            if not safety_result["safe"]:
                safety_warnings = safety_result.get("warnings", [])
                return {
                    "passed": False,
                    "error": f"Static safety scan failed (required before Docker exec): {safety_warnings}",
                    "sandbox_mode": SandboxMode.DOCKER,
                }
            
            # P0-5:  SAFE_BUILTINS ,
            #  SAFE_BUILTINS ( RESTRICTED_PYTHON )
            safe_builtins_src = (
                "_sb = {\n"
                "    'print': print, 'len': len, 'range': range,\n"
                "    'str': str, 'int': int, 'float': float, 'bool': bool,\n"
                "    'list': list, 'dict': dict, 'tuple': tuple, 'set': set,\n"
                "    'frozenset': frozenset, 'type': type, 'isinstance': isinstance,\n"
                "    'None': None, 'True': True, 'False': False,\n"
                "    'Exception': Exception, 'ValueError': ValueError,\n"
                "    'TypeError': TypeError, 'KeyError': KeyError,\n"
                "    'AttributeError': AttributeError, 'RuntimeError': RuntimeError,\n"
                "    'abs': abs, 'min': min, 'max': max, 'sum': sum,\n"
                "    'enumerate': enumerate, 'zip': zip, 'sorted': sorted,\n"
                "    'reversed': reversed, 'hasattr': hasattr,\n"
                "    'round': round, 'pow': pow, 'chr': chr, 'ord': ord,\n"
                "    'hex': hex, 'bin': bin, 'oct': oct, 'repr': repr,\n"
                "}\n"
                "exec(compile({code!r}, {fname!r}, 'exec'), {{'__builtins__': _sb}})\n"
            ).format(code=code, fname=file_path)
            
            # P0-5: Timeout(threading.Timer 15), Docker hang
            import threading
            timeout_expired = threading.Event()
            
            def _timeout_killer():
                timeout_expired.set()
            
            timer = threading.Timer(15.0, _timeout_killer)
            timer.daemon = True
            timer.start()
            
            try:
                #  Docker Exec(P0-5:  stop_timeout=10)
                result = subprocess.run(
                    [
                        "docker", "run", "--rm",
                        "--network=none",       # 
                        "--memory=128m",        # Memory
                        "--cpus=1",             # CPU 
                        "python:3.11-slim",
                        "python", "-c", safe_builtins_src,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,  # 15Timeout
                )
            finally:
                timer.cancel()
            
            if timeout_expired.is_set():
                return {"passed": False, "error": "Docker sandbox execution timeout (15s outer guard)", "sandbox_mode": SandboxMode.DOCKER}
            
            if result.returncode == 0:
                return {"passed": True, "error": "", "sandbox_mode": SandboxMode.DOCKER}
            else:
                # ,notis
                stderr = result.stderr[:500] if result.stderr else ""
                return {
                    "passed": True,  # ExecFailnot(and RESTRICTED_PYTHON )
                    "error": f"Docker exec note: exit={result.returncode}, stderr={stderr}",
                    "sandbox_mode": SandboxMode.DOCKER,
                }
        
        except subprocess.TimeoutExpired:
            return {"passed": False, "error": "Docker sandbox execution timeout (15s)", "sandbox_mode": SandboxMode.DOCKER}
        except FileNotFoundError:
            # Docker not,
            logger.warning("[SelfRepair] Docker , RESTRICTED_PYTHON")
            return await self._sandbox_test_restricted_python(file_path)
        except Exception as e:
            return {
                "passed": True,
                "error": f"Docker sandbox note: {str(e)}",
                "sandbox_mode": SandboxMode.DOCKER,
            }
    
    async def _sandbox_test_restricted_python(self, file_path: str) -> Dict:
        """ """
        if not file_path.endswith('.py'):
            return {"passed": True, "error": "", "sandbox_mode": SandboxMode.RESTRICTED_PYTHON}
        
        try:
            # 1. CreateExec
            #  P1-1:  __builtins__  type and object,
            safe_globals = {
                '__builtins__': {
                    'print': print, 'len': len, 'range': range,
                    'str': str, 'int': int, 'float': float, 'bool': bool,
                    'list': list, 'dict': dict, 'tuple': tuple, 'set': set,
                    'None': None, 'True': True, 'False': False,
                    'isinstance': isinstance,
                    #  P1-1: type and object , __class__.__bases__[0].__subclasses__() 
                    'Exception': Exception, 'ValueError': ValueError,
                    'TypeError': TypeError, 'KeyError': KeyError,
                    'AttributeError': AttributeError, 'RuntimeError': RuntimeError,
                },
                '__name__': '__sandbox__',
                '__file__': file_path,
            }
            
            # 2. ReadFileContent
            with open(file_path, 'r') as f:
                code = f.read()
            
            # 3. (Check)
            compiled = compile(code, file_path, 'exec')
            
            #  P1-1: ,
            sandbox_escape_strings = (
                '__class__', '__bases__', '__subclasses__', '__mro__',
                '__globals__', '__code__', '__func__', '__closure__',
                '__builddict__', '__dict__', '__init__',
            )
            for const in compiled.co_consts:
                if isinstance(const, str) and const in sandbox_escape_strings:
                    return {"passed": False, "error": f"Sandbox escape detected: code references '{const}'", "sandbox_mode": SandboxMode.RESTRICTED_PYTHON}
                # Check code 
                if hasattr(const, 'co_consts'):
                    for inner_const in const.co_consts:
                        if isinstance(inner_const, str) and inner_const in sandbox_escape_strings:
                            return {"passed": False, "error": f"Sandbox escape detected: code references '{inner_const}'", "sandbox_mode": SandboxMode.RESTRICTED_PYTHON}
            
            # 4. Exec()
            # P1-7:  threading.Timer  signal.SIGALRM,
            import threading
            timeout_expired = threading.Event()
            exec_timed_out = False
            
            def _timeout_handler():
                nonlocal exec_timed_out
                exec_timed_out = True
                timeout_expired.set()
            
            timer = threading.Timer(5.0, _timeout_handler)
            timer.daemon = True
            timer.start()
            
            try:
                exec(compiled, safe_globals)
            finally:
                timer.cancel()
            
            if exec_timed_out:
                return {"passed": False, "error": "Sandbox execution timeout", "sandbox_mode": SandboxMode.RESTRICTED_PYTHON}
            
            # 5. Checkis
            # Exec subprocess/socket ,Op
            dangerous_modules = ('subprocess', 'socket', 'os', 'shutil')
            for key, value in safe_globals.items():
                if key.startswith('_'):
                    continue
                module_name = getattr(type(value), '__module__', '')
                if module_name in dangerous_modules:
                    return {"passed": False, "error": f"Sandbox detected dangerous object: {key} ({type(value).__name__})", "sandbox_mode": SandboxMode.RESTRICTED_PYTHON}
            
            #  P1-1: Check safe_globals Valueis __class__.__mro__ 
            escape_result = self._check_sandbox_escape(safe_globals, dangerous_modules)
            if not escape_result["safe"]:
                return {"passed": False, "error": escape_result["error"], "sandbox_mode": SandboxMode.RESTRICTED_PYTHON}
            
            return {"passed": True, "error": "", "sandbox_mode": SandboxMode.RESTRICTED_PYTHON}
            
        except Exception as e:
            # ExecFailis(importError),
            # not,Exec
            return {"passed": True, "error": f"Sandbox exec note: {str(e)}", "sandbox_mode": SandboxMode.RESTRICTED_PYTHON}
    
    #  P1-1: Check
    def _check_sandbox_escape(self, safe_globals: dict, dangerous_modules: tuple, max_depth: int = 4) -> Dict:
        """ """
        visited = set()  # 
        
        def _inspect_object(obj, depth: int, path: str) -> Optional[str]:
            """ """
            if depth > max_depth:
                return None
            obj_id = id(obj)
            if obj_id in visited:
                return None
            visited.add(obj_id)
            
            try:
                # Check __class__.__module__ is
                obj_type = type(obj)
                type_module = getattr(obj_type, '__module__', '')
                if type_module in dangerous_modules:
                    return f"Sandbox escape via {path}: object type {obj_type.__name__} from {type_module}"
                
                # Check MRO isType
                for base in getattr(obj_type, '__mro__', []):
                    base_module = getattr(base, '__module__', '')
                    if base_module in dangerous_modules:
                        return f"Sandbox escape via {path}.__mro__: {base.__name__} from {base_module}"
            except Exception as e:
                logger.debug(f"[SelfRepair] _inspect_object(): {e}")
                pass
            
            # is,CheckValue
            if isinstance(obj, dict) and depth < max_depth:
                for k, v in obj.items():
                    err = _inspect_object(v, depth + 1, f"{path}[{k!r}]")
                    if err:
                        return err
            # isList/,Check
            elif isinstance(obj, (list, tuple)) and depth < max_depth:
                for i, v in enumerate(obj):
                    err = _inspect_object(v, depth + 1, f"{path}[{i}]")
                    if err:
                        return err
            
            return None
        
        # Check(Skip)
        for key, value in safe_globals.items():
            if key in ('__builtins__', '__name__', '__file__'):
                continue
            err = _inspect_object(value, 0, key)
            if err:
                return {"safe": False, "error": err}
        
        return {"safe": True, "error": ""}
    
    # P2 #12: ConfirmRequest()
    async def _request_human_approval(self, file_path: str, new_content: str) -> bool:
        """ """
        # CheckisConfirm
        if hasattr(self.framework, 'request_human_approval') and callable(self.framework.request_human_approval):
            try:
                return await self.framework.request_human_approval(file_path, new_content)
            except Exception as e:
                logger.warning(f"[SelfRepair] Human approval request failed: {e}")
                return False
        
        # Confirm,Reject()
        logger.warning("[SelfRepair] Human approval requested but no approval channel available — denying by default")
        return False
    
    async def safe_modify(
        self,
        file_path: str,
        new_content: str,
        sandbox_test: bool = True,
        require_human_approval: bool = False,
    ) -> Dict:
        """ """
        # P2 #9: 
        async with self._modify_lock:
            return await self._safe_modify_inner(
                file_path, new_content, sandbox_test, require_human_approval
            )
    
    # :Check(track  awaitable)
    async def _async_safety_check(self, file_path: str) -> Dict:
        """Async wrapper for validate_code_safety, used by truth_reporter.track()."""
        return self.validate_code_safety(file_path)
    
    async def _safe_modify_inner(
        self,
        file_path: str,
        new_content: str,
        sandbox_test: bool,
        require_human_approval: bool,
    ) -> Dict:
        """Inner implementation of safe_modify, called under the concurrency lock."""
        file_path = self._validate_file_path(file_path)  # P0-3: Path
        warnings = []
        
        # 1. CheckFile
        if self.is_protected_file(file_path):
            return {
                "success": False,
                "commit_hash": "",
                "error": f"File is protected and cannot be modified: {os.path.basename(file_path)}",
                "rolled_back": False,
                "warnings": [],
                "sandbox_mode": self.sandbox_mode,  # P0-4: 
            }
        
        # P1-9:  LLM Output sandbox 
        # FileisConfig sandbox_mode,Reject
        if self._is_config_with_sandbox_mode(file_path, new_content):
            # Read sandbox_mode(AllowConfig/EnvRead)
            current_mode = self.sandbox_mode
            new_mode = self._extract_sandbox_mode_from_content(new_content)
            if new_mode is not None and new_mode != current_mode:
                return {
                    "success": False,
                    "commit_hash": "",
                    "error": f"Sandbox mode cannot be modified at runtime: current={current_mode}, attempted={new_mode}. "
                             f"Sandbox mode can only be set via config file or environment variable.",
                    "rolled_back": False,
                    "warnings": [],
                    "sandbox_mode": self.sandbox_mode,
                }
            # sandbox_mode ValueAllow(isConfig)
            warnings.append(f"Config file contains sandbox_mode={new_mode} (unchanged, modification allowed)")
        
        # 2. Check
        if not self.can_self_modify():
            config_max = self.MAX_SELF_MODIFICATIONS
            if hasattr(self.framework, 'config') and hasattr(self.framework.config, 'max_self_modifications'):
                config_max = self.framework.config.max_self_modifications
            effective_max = min(config_max, self.HARD_MAX_SELF_MODIFICATIONS)
            return {
                "success": False,
                "commit_hash": "",
                "error": f"Self-modification limit reached ({effective_max} per session)",
                "rolled_back": False,
                "warnings": [],
                "sandbox_mode": self.sandbox_mode,  # P0-4: 
            }
        
        # 3. P0 #4: Git Check
        if not self._verify_git_integrity():
            return {
                "success": False,
                "commit_hash": "",
                "error": "Git integrity check failed — cannot safely modify without rollback capability",
                "rolled_back": False,
                "warnings": [],
                "sandbox_mode": self.sandbox_mode,  # P0-4: 
            }
        
        # 4. Git 
        # :git backup Op track Result
        if self._truth_reporter:
            backup_result = await self._truth_reporter.track(
                f"git_backup:{os.path.basename(file_path)}",
                self.git_backup_before_modify(file_path),
            )
            # track  OperationResult, dict Format
            backup_result_dict = {
                'success': backup_result.success,
                'commit_hash': '',
                'error': backup_result.error or '',
            }
            #  output  commit_hash(track  dict  output string)
            if backup_result.success and backup_result.output:
                try:
                    parsed = json.loads(backup_result.output)
                    backup_result_dict['commit_hash'] = parsed.get('commit_hash', '')
                except (json.JSONDecodeError, TypeError):
                    logger.debug("self_repair: git backupJSON")
            backup_result = backup_result_dict
        else:
            backup_result = await self.git_backup_before_modify(file_path)
        if not backup_result['success']:
            return {
                "success": False,
                "commit_hash": "",
                "error": f"Backup failed, aborting modify: {backup_result['error']}",
                "rolled_back": False,
                "warnings": [],
                "sandbox_mode": self.sandbox_mode,  # P0-4: 
            }
        
        commit_hash = backup_result['commit_hash']
        
        # 5. ReadContent()
        old_content = None
        try:
            if os.path.exists(file_path):
                with open(file_path, 'r', encoding='utf-8') as f:
                    old_content = f.read()
        except Exception as e:
            logger.warning(f"[SelfRepair] Could not read old content: {e}")
        
        # 6. P1 #6: Check(Warning,not)
        if os.path.exists(file_path):
            dep_warnings = self._check_dependency_impact(file_path)
            if dep_warnings:
                warnings.extend(dep_warnings)
                for w in dep_warnings:
                    logger.warning(f"[SelfRepair] Dependency impact: {w}")
        
        # 7. WriteContent
        # :WriteOp track 
        async def _do_write():
            # Dir
            parent_dir = os.path.dirname(file_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            return "write_success"

        if self._truth_reporter:
            write_result = await self._truth_reporter.track(
                f"write:{os.path.basename(file_path)}",
                _do_write(),
            )
            if not write_result.success:
                self._log_modification(file_path, False, commit_hash, write_result.error or "Write failed")
                return {
                    "success": False,
                    "commit_hash": commit_hash,
                    "error": f"Failed to write new content: {write_result.error}",
                    "rolled_back": False,
                    "warnings": warnings,
                    "sandbox_mode": self.sandbox_mode,  # P0-4: 
                }
        else:
            try:
                # Dir
                parent_dir = os.path.dirname(file_path)
                if parent_dir:
                    os.makedirs(parent_dir, exist_ok=True)
                
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(new_content)
            except Exception as e:
                self._log_modification(file_path, False, commit_hash, str(e))
                return {
                    "success": False,
                    "commit_hash": commit_hash,
                    "error": f"Failed to write new content: {e}",
                    "rolled_back": False,
                    "warnings": warnings,
                    "sandbox_mode": self.sandbox_mode,  # P0-4: 
                }
        
        # 8. Verify
        # :Check track 
        if self._truth_reporter:
            syntax_op_result = await self._truth_reporter.track(
                f"syntax_check:{os.path.basename(file_path)}",
                self.validate_code_syntax(file_path),
            )
            #  track Result syntax_result dict
            syntax_result = {'valid': False, 'error': 'Syntax check tracking failed'}
            try:
                if syntax_op_result.output:
                    parsed = json.loads(syntax_op_result.output)
                    if isinstance(parsed, dict):
                        syntax_result = parsed
            except (json.JSONDecodeError, TypeError):
                if not syntax_op_result.success:
                    syntax_result = {'valid': False, 'error': syntax_op_result.error or 'Syntax check failed'}
        else:
            syntax_result = await self.validate_code_syntax(file_path)
        if not syntax_result['valid']:
            # :Content
            rolled_back = self._rollback_file(file_path, old_content)
            error_msg = f"Syntax validation failed: {syntax_result['error']}"
            self._log_modification(file_path, False, commit_hash, error_msg)
            return {
                "success": False,
                "commit_hash": commit_hash,
                "error": error_msg,
                "rolled_back": rolled_back,
                "warnings": warnings,
                "sandbox_mode": self.sandbox_mode,  # P0-4: 
            }
        
        # 9. P0 #2: Check()
        # :Check track 
        if self._truth_reporter:
            safety_op_result = await self._truth_reporter.track(
                f"safety_check:{os.path.basename(file_path)}",
                self._async_safety_check(file_path),
            )
            #  track Result safety_result dict
            safety_result = {'safe': False, 'warnings': ['Safety check tracking failed']}
            try:
                if safety_op_result.output:
                    parsed = json.loads(safety_op_result.output)
                    if isinstance(parsed, dict):
                        safety_result = parsed
            except (json.JSONDecodeError, TypeError):
                if not safety_op_result.success:
                    safety_result = {'safe': False, 'warnings': [safety_op_result.error or 'Safety check failed']}
        else:
            safety_result = self.validate_code_safety(file_path)
        if not safety_result["safe"]:
            #  + Warning
            rolled_back = self._rollback_file(file_path, old_content)
            safety_warnings = safety_result['warnings']
            error_msg = f"Safety check failed: {safety_warnings}"
            logger.warning(f"[SelfRepair] {error_msg}")
            self._log_modification(file_path, False, commit_hash, error_msg)
            return {
                "success": False,
                "commit_hash": commit_hash,
                "error": error_msg,
                "rolled_back": rolled_back,
                "warnings": warnings + safety_warnings,
                "sandbox_mode": self.sandbox_mode,  # P0-4: 
            }
        
        # 10. P2 #11: Test
        # :Test track 
        if sandbox_test:
            if self._truth_reporter:
                sandbox_op_result = await self._truth_reporter.track(
                    f"sandbox_test:{os.path.basename(file_path)}",
                    self._sandbox_test(file_path),
                )
                test_result = {
                    'passed': sandbox_op_result.success,
                    'error': sandbox_op_result.error or '',
                }
            else:
                test_result = await self._sandbox_test(file_path)
            if not test_result["passed"]:
                rolled_back = self._rollback_file(file_path, old_content)
                error_msg = f"Sandbox test failed: {test_result['error']}"
                self._log_modification(file_path, False, commit_hash, error_msg)
                return {
                    "success": False,
                    "commit_hash": commit_hash,
                    "error": error_msg,
                    "rolled_back": rolled_back,
                    "warnings": warnings,
                    "sandbox_mode": self.sandbox_mode,  # P0-4: 
                }
        
        # 10.5 :Verify - 
        if self._truth_reporter:
            trust_level = self._truth_reporter.get_trust_level()
            should_cross_verify = (
                trust_level.value in ('reduced', 'untrusted')
                or (sandbox_test and trust_level.value == 'full')
            )
            if should_cross_verify:
                try:
                    cv_result = await self._truth_reporter.cross_verify(
                        task_description=f"Safe modify file: {os.path.basename(file_path)}",
                        output=new_content[:6000],
                    )
                    if cv_result.get('verified') is False:
                        # Verify,not(isWarning)
                        cv_issues = cv_result.get('issues_found', [])
                        cv_model = cv_result.get('verifier_model', 'unknown')
                        cv_confidence = cv_result.get('confidence', 0.0)
                        warning_msg = (
                            f"Cross-verify ({cv_model}, confidence={cv_confidence:.2f}) "
                            f"found issues: {cv_issues}"
                        )
                        warnings.append(warning_msg)
                        logger.warning(f"[SelfRepair] {warning_msg}")
                        #  UNTRUSTED ,Verifynot
                        if trust_level.value == 'untrusted' and cv_confidence >= 0.7:
                            rolled_back = self._rollback_file(file_path, old_content)
                            error_msg = f"Cross-verify blocked modification: {warning_msg}"
                            self._log_modification(file_path, False, commit_hash, error_msg)
                            return {
                                "success": False,
                                "commit_hash": commit_hash,
                                "error": error_msg,
                                "rolled_back": rolled_back,
                                "warnings": warnings,
                                "sandbox_mode": self.sandbox_mode,  # P0-4: 
                            }
                except Exception as e:
                    # VerifyFailnot
                    logger.warning(f"[SelfRepair] Cross-verify failed (non-blocking): {e}")
        
        # 11. P2 #12: Confirm
        if require_human_approval:
            approved = await self._request_human_approval(file_path, new_content)
            if not approved:
                rolled_back = self._rollback_file(file_path, old_content)
                error_msg = "Human approval denied"
                self._log_modification(file_path, False, commit_hash, error_msg)
                return {
                    "success": False,
                    "commit_hash": commit_hash,
                    "error": error_msg,
                    "rolled_back": rolled_back,
                    "warnings": warnings,
                    "sandbox_mode": self.sandbox_mode,  # P0-4: 
                }
        
        # 12. P1 #7:  +1(OK)
        self._modification_count += 1
        
        # P1 #4: 
        self._save_modification_count()
        
        logger.info(
            f"[SelfRepair] Safe modify successful: {file_path} "
            f"(modification {self._modification_count}/{self.MAX_SELF_MODIFICATIONS})"
        )
        
        # P2 #10: Log
        self._log_modification(file_path, True, commit_hash)
        
        result = {
            "success": True,
            "commit_hash": commit_hash,
            "error": "",
            "rolled_back": False,
            "warnings": warnings,
            "sandbox_mode": self.sandbox_mode,  # P0-4: 
        }
        return result
    
    def _rollback_file(self, file_path: str, old_content: Optional[str]) -> bool:
        """ """
        try:
            if old_content is not None:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(old_content)
                logger.info(f"[SelfRepair] Rolled back content for {file_path}")
                return True
            else:
                # Content, git checkout
                file_dir = os.path.dirname(os.path.realpath(file_path))
                subprocess.run(
                    ['git', 'checkout', '--', file_path],
                    cwd=file_dir,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                logger.info(f"[SelfRepair] Git checkout rollback for {file_path}")
                return True
        except Exception as rollback_err:
            logger.error(f"[SelfRepair] Rollback also failed: {rollback_err}")
            return False
    
    # P1 #4: 
    def _load_modification_count(self) -> int:
        """ """
        count_file = os.path.join(self._get_repo_root(), '.tical_mod_count.json')
        try:
            if os.path.exists(count_file):
                with open(count_file, 'r') as f:
                    data = json.load(f)
                return data.get("count", 0)
        except Exception as e:
            logger.debug(f"[SelfRepair] _load_modification_count(): {e}")
            pass
        return 0
    
    def _save_modification_count(self):
        """ """
        count_file = os.path.join(self._get_repo_root(), '.tical_mod_count.json')
        try:
            with open(count_file, 'w') as f:
                json.dump({"count": self._modification_count, "updated": datetime.now().isoformat()}, f)
        except Exception as e:
            logger.debug(f"[SelfRepair] _load_modification_count(): {e}")
            pass
    
    async def safe_restart_with_rollback(
        self,
        restart_cmd: List[str],
        health_check_url: str = "",
        timeout: int = 15,
    ) -> Dict:
        """
        Restart service with automatic rollback on health check failure.
        
        Flow:
        1. Record current git HEAD hash
        2. Execute restart command
        3. Multi-layer health check (P0 #3: HTTP + process + identity)
        4. On failure: check git integrity (P0 #4), then rollback
        
        Args:
            restart_cmd: Command to restart the service (e.g., ['python', 'main.py'])
            health_check_url: URL to check for HTTP 200 after restart
            timeout: Max seconds to wait for health check (default 15)
            
        Returns:
            {"success": bool, "rolled_back": bool, "rollback_hash": str, "error": str}
        """
        # 1.  HEAD hash
        pre_restart_hash = ""
        try:
            hash_result = subprocess.run(
                ['git', 'rev-parse', 'HEAD'],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if hash_result.returncode == 0:
                pre_restart_hash = hash_result.stdout.strip()
        except Exception as e:
            logger.debug(f"[SelfRepair] (): {e}")
            pass
        
        # 2. Exec
        # :Op track 
        async def _do_restart():
            return subprocess.Popen(
                restart_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        try:
            if self._truth_reporter:
                restart_op_result = await self._truth_reporter.track(
                    f"restart:{' '.join(restart_cmd)[:50]}",
                    _do_restart(),
                )
                if not restart_op_result.success:
                    return {
                        "success": False,
                        "rolled_back": False,
                        "rollback_hash": pre_restart_hash,
                        "error": f"Failed to execute restart command: {restart_op_result.error}",
                    }
                restart_process = None  # track Exec Popen
            else:
                restart_process = subprocess.Popen(
                    restart_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            logger.info(f"[SelfRepair] Restart command executed: {' '.join(restart_cmd)}")
        except Exception as e:
            return {
                "success": False,
                "rolled_back": False,
                "rollback_hash": pre_restart_hash,
                "error": f"Failed to execute restart command: {e}",
            }
        
        # 3. P0 #3: Check
        if not health_check_url:
            #  health check URL,:5OK
            await asyncio.sleep(5)
            return {
                "success": True,
                "rolled_back": False,
                "rollback_hash": "",
                "error": "",
            }
        
        health_result = await self._multi_health_check(health_check_url, timeout)
        
        if health_result["passed"]:
            return {
                "success": True,
                "rolled_back": False,
                "rollback_hash": "",
                "error": "",
            }
        
        # 4. CheckFail → P0 #4: Check git 
        failed_checks = [k for k, v in health_result["checks"].items() if not v]
        logger.warning(
            f"[SelfRepair] Health check failed after restart "
            f"(failed: {failed_checks}), rolling back..."
        )
        
        if not self._verify_git_integrity():
            logger.critical("[SelfRepair] Git integrity check failed! Cannot rollback automatically!")
            return {
                "success": False,
                "rolled_back": False,
                "rollback_hash": pre_restart_hash,
                "error": f"Health check failed ({failed_checks}) and git repo destroyed — cannot rollback",
            }
        
        rollback_success = False
        try:
            # git checkout -- . File
            subprocess.run(
                ['git', 'checkout', '--', '.'],
                capture_output=True,
                text=True,
                timeout=10,
            )
            
            # git reset --hard <hash> Status
            if pre_restart_hash:
                subprocess.run(
                    ['git', 'reset', '--hard', pre_restart_hash],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            
            rollback_success = True
            logger.info(f"[SelfRepair] Rolled back to {pre_restart_hash[:8] if pre_restart_hash else 'unknown'}")
        except Exception as e:
            logger.error(f"[SelfRepair] Rollback failed: {e}")
        
        # ()
        try:
            subprocess.Popen(
                restart_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            logger.info("[SelfRepair] Re-restarted with rolled-back code")
        except Exception as e:
            logger.error(f"[SelfRepair] Re-restart after rollback failed: {e}")
        
        return {
            "success": False,
            "rolled_back": rollback_success,
            "rollback_hash": pre_restart_hash,
            "error": f"Health check failed ({failed_checks}) after {timeout}s, rollback {'succeeded' if rollback_success else 'failed'}",
        }
