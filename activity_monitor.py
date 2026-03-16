import psutil
import logging
import time
import threading
from functools import wraps
import os
import platform

# per-thread state for request monitoring
_thread_monitor = threading.local()

# Setup logger for activity monitoring
logger = logging.getLogger('activity_monitor')
logger.setLevel(logging.INFO)

# Enable/disable logging via env var. Defaults to enabled.
# Set ACTIVITY_MONITOR_LOG=0 (or false/no/off) to disable writing activity_monitor.log.
ACTIVITY_MONITOR_LOG = str(os.getenv('ACTIVITY_MONITOR_LOG', '1')).strip().lower()
ACTIVITY_MONITOR_LOG_ENABLED = ACTIVITY_MONITOR_LOG not in ('0', 'false', 'no', 'off', '')

if ACTIVITY_MONITOR_LOG_ENABLED:
    # Use relative path for the log file
    log_file_path = os.path.join(os.getcwd(), 'activity_monitor.log')
    handler = logging.FileHandler(log_file_path)
    handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(handler)
else:
    logger.addHandler(logging.NullHandler())

# Prevent propagation to the root logger (avoid duplicate console output)
logger.propagate = False

_system_info_logged = False

def log_system_info():
    """Log system information at startup."""
    if not ACTIVITY_MONITOR_LOG_ENABLED:
        return

    global _system_info_logged
    if _system_info_logged:
        return

    try:
        # CPU info
        cpu_count = psutil.cpu_count(logical=True)
        cpu_count_physical = psutil.cpu_count(logical=False)

        # Memory info
        mem = psutil.virtual_memory()
        total_ram_gb = mem.total / (1024 ** 3)

        # Disk info
        disk = psutil.disk_usage('/')
        total_disk_gb = disk.total / (1024 ** 3)

        # System info
        system = platform.system()
        release = platform.release()
        version = platform.version()

        logger.info(f"System Info - OS: {system} {release} {version}, CPU: {cpu_count_physical} physical / {cpu_count} logical cores, RAM: {total_ram_gb:.2f} GB, Disk: {total_disk_gb:.2f} GB")
        _system_info_logged = True
    except Exception as e:
        logger.error(f"Error logging system info: {e}")

# Log system info at import time
log_system_info()


def _set_monitor_state(state: dict):
    _thread_monitor.state = state


def _get_monitor_state() -> dict | None:
    return getattr(_thread_monitor, 'state', None)


def start_request_monitoring(label: str):
    """Start monitoring a request (Flask request, background job, etc.)"""
    try:
        # use non-blocking sampling
        start_cpu = psutil.cpu_percent(interval=0.0)
        start_memory = psutil.virtual_memory().percent
        start_time = time.time()
        state = {
            'label': label,
            'start_time': start_time,
            'start_cpu': start_cpu,
            'start_memory': start_memory,
        }
        _set_monitor_state(state)
        logger.info(f"[REQUEST START] {label} - CPU {start_cpu:.2f}%, Mem {start_memory:.2f}%")
    except Exception as e:
        logger.error(f"Error starting request monitoring for {label}: {e}")


def end_request_monitoring(status_code: int | None = None):
    """End monitoring for the current request or operation."""
    state = _get_monitor_state()
    if not state:
        return

    label = state.get('label')
    try:
        current_cpu = psutil.cpu_percent(interval=0.0)
        current_memory = psutil.virtual_memory().percent
        memory_mb = psutil.virtual_memory().used / (1024 * 1024)

        duration = time.time() - state['start_time']
        cpu_delta = current_cpu - state['start_cpu']
        memory_delta = current_memory - state['start_memory']
        status = f" status={status_code}" if status_code is not None else ""
        logger.info(
            f"[REQUEST END] {label}{status} - Duration: {duration:.2f}s, CPU: {current_cpu:.2f}% (Δ{cpu_delta:.2f}%), Memory: {current_memory:.2f}% ({memory_mb:.2f}MB) (Δ{memory_delta:.2f}%)"
        )
    except Exception as e:
        logger.error(f"Error ending request monitoring for {label}: {e}")
    finally:
        _set_monitor_state(None)


def log_resource_usage(operation_name: str, start_time: float = None, start_cpu: float = None, start_memory: float = None):
    """Log current CPU and memory usage for a given operation."""
    try:
        current_cpu = psutil.cpu_percent(interval=0.0)
        current_memory = psutil.virtual_memory().percent
        memory_mb = psutil.virtual_memory().used / (1024 * 1024)

        if start_time is not None and start_cpu is not None and start_memory is not None:
            duration = time.time() - start_time
            cpu_delta = current_cpu - start_cpu
            memory_delta = current_memory - start_memory
            logger.info(f"Operation '{operation_name}' completed - Duration: {duration:.2f}s, CPU: {current_cpu:.2f}% (Δ{cpu_delta:.2f}%), Memory: {current_memory:.2f}% ({memory_mb:.2f}MB) (Δ{memory_delta:.2f}%)")
        else:
            logger.info(f"Operation '{operation_name}' started - CPU: {current_cpu:.2f}%, Memory: {current_memory:.2f}% ({memory_mb:.2f}MB)")
    except Exception as e:
        logger.error(f"Error logging resource usage: {e}")

def monitor_resources(operation_name: str):
    """Decorator to monitor CPU and memory usage for a function."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Log start
            start_time = time.time()
            start_cpu = psutil.cpu_percent(interval=0.1)
            start_memory = psutil.virtual_memory().percent
            log_resource_usage(operation_name, start_time=None, start_cpu=None, start_memory=None)

            try:
                result = func(*args, **kwargs)
                # Log end
                log_resource_usage(operation_name, start_time, start_cpu, start_memory)
                return result
            except Exception as e:
                # Log end with error
                log_resource_usage(operation_name, start_time, start_cpu, start_memory)
                logger.error(f"Operation '{operation_name}' failed with error: {str(e)}")
                raise
        return wrapper
    return decorator