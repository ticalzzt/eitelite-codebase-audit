"""Cron scheduler - periodic task execution."""

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# =============================================================================
# Constants
# =============================================================================

MAX_DAILY_EXECUTIONS = 100     # Exec
MAX_RETRIES = 3                # Retry

# (action)
_DANGEROUS_PATTERNS = [
    r'\bexec\s*\(',
    r'\beval\s*\(',
    r'\bos\.system\s*\(',
    r'\bsubprocess\b',
    r'\bos\.popen\s*\(',
    r'__import__\s*\(',
    r'\brm\s+-rf\b',
    r'\bchmod\s+777\b',
    r'\bdd\s+if=',
]

# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class CronTask:
    """A scheduled task definition.

    Attributes:
        id: Unique task identifier
        name: Human-readable task name
        schedule: Schedule expression ("every 1h", "at 09:00", "every 30m")
        action: Natural language description of what to do
        tool_hint: Suggested tool to use (optional)
        last_run: Unix timestamp of last execution
        next_run: Unix timestamp of next scheduled execution
        enabled: Whether the task is active
        max_retries: Maximum retry count on failure
        created_at: Task creation timestamp
        run_count: Total number of successful executions
        fail_count: Number of consecutive failures
    """
    id: str
    name: str
    schedule: str
    action: str
    tool_hint: str = ''
    last_run: float = 0.0
    next_run: float = 0.0
    enabled: bool = True
    max_retries: int = MAX_RETRIES
    created_at: float = field(default_factory=time.time)
    run_count: int = 0
    fail_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary for JSON storage."""
        return {
            'id': self.id,
            'name': self.name,
            'schedule': self.schedule,
            'action': self.action,
            'tool_hint': self.tool_hint,
            'last_run': self.last_run,
            'next_run': self.next_run,
            'enabled': self.enabled,
            'max_retries': self.max_retries,
            'created_at': self.created_at,
            'run_count': self.run_count,
            'fail_count': self.fail_count,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'CronTask':
        """Deserialize from dictionary."""
        return cls(
            id=data['id'],
            name=data['name'],
            schedule=data['schedule'],
            action=data['action'],
            tool_hint=data.get('tool_hint', ''),
            last_run=data.get('last_run', 0.0),
            next_run=data.get('next_run', 0.0),
            enabled=data.get('enabled', True),
            max_retries=data.get('max_retries', MAX_RETRIES),
            created_at=data.get('created_at', time.time()),
            run_count=data.get('run_count', 0),
            fail_count=data.get('fail_count', 0),
        )

@dataclass
class CronLogEntry:
    """A log entry for a cron task execution.

    Attributes:
        task_id: Task identifier
        task_name: Task name
        timestamp: Execution timestamp
        success: Whether execution succeeded
        result: Result description
        duration_ms: Execution duration in milliseconds
    """
    task_id: str
    task_name: str
    timestamp: float = field(default_factory=time.time)
    success: bool = False
    result: str = ''
    duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            'task_id': self.task_id,
            'task_name': self.task_name,
            'timestamp': self.timestamp,
            'success': self.success,
            'result': self.result,
            'duration_ms': self.duration_ms,
        }

# =============================================================================
# Schedule Parser
# =============================================================================

class ScheduleParser:
    """Parse simple schedule expressions into interval seconds or next run time.

    Supported formats:
    - "every 30m" / "every 1h" / "every 2d" → interval in seconds
    - "at 09:00" / "at 14:30" → next occurrence of that time today/tomorrow
    - "every 30s" → interval in seconds
    """

    # Format: "every <number><unit>"
    _INTERVAL_RE = re.compile(
        r'^every\s+(\d+)\s*(s|m|h|d)$',
        re.IGNORECASE,
    )

    # Format: "at HH:MM"
    _AT_TIME_RE = re.compile(
        r'^at\s+(\d{1,2}):(\d{2})$',
        re.IGNORECASE,
    )

    @classmethod
    def parse(cls, schedule: str) -> Tuple[str, int]:
        """Parse a schedule expression.

        Args:
            schedule: Schedule expression string

        Returns:
            Tuple of (type, value):
            - ("interval", seconds) for "every X" expressions
            - ("at_time", seconds_since_midnight) for "at HH:MM" expressions

        Raises:
            ValueError: If schedule expression is invalid
        """
        schedule = schedule.strip()

        # Format
        interval_match = cls._INTERVAL_RE.match(schedule)
        if interval_match:
            count = int(interval_match.group(1))
            unit = interval_match.group(2).lower()
            multipliers = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
            seconds = count * multipliers[unit]
            if seconds <= 0:
                raise ValueError(f"0: {schedule}")
            return ("interval", seconds)

        # Format
        at_match = cls._AT_TIME_RE.match(schedule)
        if at_match:
            hour = int(at_match.group(1))
            minute = int(at_match.group(2))
            if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                raise ValueError(f": {hour:02d}:{minute:02d}")
            seconds_since_midnight = hour * 3600 + minute * 60
            return ("at_time", seconds_since_midnight)

        raise ValueError(f": {schedule}")

    @classmethod
    def calculate_next_run(cls, schedule: str, from_time: Optional[float] = None) -> float:
        """Calculate the next run timestamp for a schedule.

        Args:
            schedule: Schedule expression string
            from_time: Base time (default: now)

        Returns:
            Unix timestamp of next run
        """
        now = from_time or time.time()
        schedule_type, value = cls.parse(schedule)

        if schedule_type == "interval":
            return now + value
        elif schedule_type == "at_time":
            # Compute/TargetTime
            dt_now = datetime.fromtimestamp(now)
            target_seconds = value
            today_seconds = dt_now.hour * 3600 + dt_now.minute * 60 + dt_now.second

            # target datetime
            target_hour = value // 3600
            target_minute = (value % 3600) // 60
            target_dt = dt_now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)

            if target_dt.timestamp() <= now:
                # Time,
                from datetime import timedelta
                target_dt += timedelta(days=1)

            return target_dt.timestamp()

        return now + 60  # 1

    @classmethod
    def is_valid(cls, schedule: str) -> bool:
        """Check if a schedule expression is valid.

        Args:
            schedule: Schedule expression string

        Returns:
            True if valid
        """
        try:
            cls.parse(schedule)
            return True
        except ValueError:
            return False

# =============================================================================
# CronScheduler
# =============================================================================

class CronScheduler:
    """ """

    def __init__(self, data_dir: str):
        """ """
        self.data_dir = os.path.expanduser(data_dir)
        os.makedirs(self.data_dir, exist_ok=True)

        # andLogFilePath
        self._tasks_file = os.path.join(self.data_dir, 'cron_tasks.json')
        self._log_file = os.path.join(self.data_dir, 'cron_log.jsonl')

        # Memory
        self._tasks: Dict[str, CronTask] = {}

        # ExecLog
        self._log: List[CronLogEntry] = []

        # Exec
        self._daily_exec_count = 0
        self._daily_exec_date = datetime.now().strftime('%Y-%m-%d')

        # Exec(System)
        self._executor: Optional[Callable] = None

        # Load
        self._load_tasks()
        self._load_log()

    def set_executor(self, executor: Callable) -> None:
        """Set the external task executor function.

        Args:
            executor: Callable that takes (task: CronTask) → str result
        """
        self._executor = executor

    # =========================================================================
    # Task Management
    # =========================================================================

    def add_task(
        self,
        name: str,
        schedule: str,
        action: str,
        tool_hint: str = '',
    ) -> CronTask:
        """Add a new scheduled task.

        Args:
            name: Task name
            schedule: Schedule expression ("every 1h", "at 09:00")
            action: What to do (natural language description)
            tool_hint: Suggested tool to use

        Returns:
            The created CronTask

        Raises:
            ValueError: If schedule is invalid or action contains dangerous patterns
        """
        # Verifyschedule
        if not ScheduleParser.is_valid(schedule):
            raise ValueError(f": {schedule}")

        # Check:
        self._validate_action(action)

        task_id = str(uuid.uuid4())[:8]

        # ComputeExecTime
        next_run = ScheduleParser.calculate_next_run(schedule)

        task = CronTask(
            id=task_id,
            name=name,
            schedule=schedule,
            action=action,
            tool_hint=tool_hint,
            next_run=next_run,
            enabled=True,
        )

        self._tasks[task_id] = task
        self._save_tasks()

        logger.info(
            f"[CronScheduler] : {name} ({schedule}) "
            f": {datetime.fromtimestamp(next_run).strftime('%Y-%m-%d %H:%M')}"
        )
        return task

    def remove_task(self, task_id: str) -> bool:
        """Remove a scheduled task.

        Args:
            task_id: Task identifier

        Returns:
            True if task was removed, False if not found
        """
        if task_id not in self._tasks:
            logger.warning(f"[CronScheduler] : {task_id}")
            return False

        task = self._tasks.pop(task_id)
        self._save_tasks()
        logger.info(f"[CronScheduler] : {task.name} ({task_id})")
        return True

    def reschedule(self, task_id: str, new_schedule: str) -> bool:
        """Change a task's schedule.

        Args:
            task_id: Task identifier
            new_schedule: New schedule expression

        Returns:
            True if rescheduled successfully
        """
        if task_id not in self._tasks:
            return False

        if not ScheduleParser.is_valid(new_schedule):
            raise ValueError(f": {new_schedule}")

        task = self._tasks[task_id]
        task.schedule = new_schedule
        task.next_run = ScheduleParser.calculate_next_run(new_schedule)

        self._save_tasks()
        logger.info(
            f"[CronScheduler] : {task.name} → {new_schedule}"
        )
        return True

    def enable_task(self, task_id: str) -> bool:
        """Enable a task.

        Args:
            task_id: Task identifier

        Returns:
            True if task was enabled
        """
        if task_id not in self._tasks:
            return False
        self._tasks[task_id].enabled = True
        self._save_tasks()
        return True

    def disable_task(self, task_id: str) -> bool:
        """Disable a task.

        Args:
            task_id: Task identifier

        Returns:
            True if task was disabled
        """
        if task_id not in self._tasks:
            return False
        self._tasks[task_id].enabled = False
        self._save_tasks()
        return True

    def get_task(self, task_id: str) -> Optional[CronTask]:
        """Get a task by ID.

        Args:
            task_id: Task identifier

        Returns:
            CronTask if found, None otherwise
        """
        return self._tasks.get(task_id)

    def list_tasks(self) -> List[CronTask]:
        """List all tasks.

        Returns:
            List of all CronTask objects
        """
        return list(self._tasks.values())

    def get_upcoming(self, n: int = 5) -> List[CronTask]:
        """Get the next n tasks to execute, sorted by next_run.

        Args:
            n: Number of tasks to return

        Returns:
            List of CronTask objects sorted by next_run
        """
        enabled = [t for t in self._tasks.values() if t.enabled]
        enabled.sort(key=lambda t: t.next_run)
        return enabled[:n]

    # =========================================================================
    # Heartbeat / Execution
    # =========================================================================

    def tick(self) -> List[Dict[str, Any]]:
        """ """
        results = []
        now = time.time()

        # CheckDate
        today = datetime.now().strftime('%Y-%m-%d')
        if today != self._daily_exec_date:
            self._daily_exec_count = 0
            self._daily_exec_date = today

        # Check
        if self._daily_exec_count >= MAX_DAILY_EXECUTIONS:
            logger.warning("[CronScheduler] ")
            return results

        for task in list(self._tasks.values()):
            if not task.enabled:
                continue
            if task.next_run <= now:
                result = self.execute_task(task)
                results.append(result)

                # Check
                if self._daily_exec_count >= MAX_DAILY_EXECUTIONS:
                    break

        return results

    def execute_task(self, task: CronTask) -> Dict[str, Any]:
        """Execute a scheduled task.

        Args:
            task: The CronTask to execute

        Returns:
            Execution result dict
        """
        start_time = time.time()
        success = False
        result_text = ''

        # Check
        today = datetime.now().strftime('%Y-%m-%d')
        if today != self._daily_exec_date:
            self._daily_exec_count = 0
            self._daily_exec_date = today

        if self._daily_exec_count >= MAX_DAILY_EXECUTIONS:
            return {
                'task_id': task.id,
                'task_name': task.name,
                'success': False,
                'result': '',
                'duration_ms': 0,
            }

        try:
            # Execor
            if self._executor:
                result_text = self._executor(task)
            else:
                result_text = f"[] {task.action}"

            success = True
            task.run_count += 1
            task.fail_count = 0  # Fail

        except Exception as e:
            success = False
            result_text = str(e)
            task.fail_count += 1
            logger.error(
                f"[CronScheduler] : {task.name} "
                f"({task.fail_count}/{task.max_retries}): {e}"
            )

            # Fail,
            if task.fail_count >= task.max_retries:
                task.enabled = False
                logger.warning(
                    f"[CronScheduler]  {task.name}  {task.fail_count} ,"
                )

        finally:
            elapsed_ms = (time.time() - start_time) * 1000
            task.last_run = time.time()
            task.next_run = ScheduleParser.calculate_next_run(task.schedule)
            self._daily_exec_count += 1

        # Log
        log_entry = CronLogEntry(
            task_id=task.id,
            task_name=task.name,
            success=success,
            result=result_text[:500],  # 
            duration_ms=elapsed_ms,
        )
        self._log.append(log_entry)
        self._append_log(log_entry)
        self._save_tasks()

        return {
            'task_id': task.id,
            'task_name': task.name,
            'success': success,
            'result': result_text[:500],
            'duration_ms': elapsed_ms,
        }

    # =========================================================================
    # Persistence
    # =========================================================================

    def _load_tasks(self) -> None:
        """Load tasks from JSON file."""
        if not os.path.exists(self._tasks_file):
            return

        try:
            with open(self._tasks_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            for task_data in data:
                try:
                    task = CronTask.from_dict(task_data)
                    self._tasks[task.id] = task
                except (KeyError, TypeError) as e:
                    logger.warning(f"[CronScheduler] : {e}")

            logger.info(f"[CronScheduler]  {len(self._tasks)} ")

        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"[CronScheduler] : {e}")

    def _save_tasks(self) -> None:
        """Save tasks to JSON file."""
        try:
            data = [task.to_dict() for task in self._tasks.values()]
            with open(self._tasks_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.error(f"[CronScheduler] : {e}")

    def _load_log(self) -> None:
        """Load execution logs from JSONL file."""
        if not os.path.exists(self._log_file):
            return

        try:
            with open(self._log_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        entry = CronLogEntry(
                            task_id=data.get('task_id', ''),
                            task_name=data.get('task_name', ''),
                            timestamp=data.get('timestamp', 0),
                            success=data.get('success', False),
                            result=data.get('result', ''),
                            duration_ms=data.get('duration_ms', 0),
                        )
                        self._log.append(entry)
                    except json.JSONDecodeError:
                        continue

            logger.info(f"[CronScheduler]  {len(self._log)} ")

        except OSError as e:
            logger.error(f"[CronScheduler] : {e}")

    def _append_log(self, entry: CronLogEntry) -> None:
        """Append a log entry to the JSONL file.

        Args:
            entry: CronLogEntry to append
        """
        try:
            with open(self._log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + '\n')
        except OSError as e:
            logger.error(f"[CronScheduler] : {e}")

    # =========================================================================
    # Safety
    # =========================================================================

    def _validate_action(self, action: str) -> None:
        """ """
        # :(Path)
        for pattern in _DANGEROUS_PATTERNS:
            if re.search(pattern, action, re.IGNORECASE):
                raise ValueError(
                    f": {pattern}."
                    f" exec/eval/ ."
                )

        # :()
        # cron action is,Allow:
        # ,,,,
        _allowed_chars = re.compile(
            r'^[\w\s\u4e00-\u9fff\u3000-\u303f\uff00-\uffef'
            r'\.\,\;\:\!\?\-\+\=\(\)\[\]\/\@\#\%\&\*\~]+$'
        )
        if not _allowed_chars.match(action):
            raise ValueError(
                "."
                ",,,."
            )

    # =========================================================================
    # Query
    # =========================================================================

    def get_task_count(self) -> int:
        """Get the total number of tasks."""
        return len(self._tasks)

    def get_enabled_count(self) -> int:
        """Get the number of enabled tasks."""
        return sum(1 for t in self._tasks.values() if t.enabled)

    def get_daily_exec_count(self) -> int:
        """Get today's execution count."""
        today = datetime.now().strftime('%Y-%m-%d')
        if today != self._daily_exec_date:
            return 0
        return self._daily_exec_count

    def get_recent_logs(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get recent execution log entries.

        Args:
            limit: Maximum number of entries

        Returns:
            List of log entry dicts
        """
        return [entry.to_dict() for entry in self._log[-limit:]]
