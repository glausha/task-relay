from __future__ import annotations

from enum import Enum


class TaskRelayError(Exception):
    pass


class ConfigError(TaskRelayError):
    pass


class JournalError(TaskRelayError):
    pass


class RouterError(TaskRelayError):
    pass


class LeaseError(TaskRelayError):
    pass


class AdapterError(TaskRelayError):
    pass


class ProjectionError(TaskRelayError):
    pass


class FailureClass(str, Enum):
    TRANSIENT = "transient"
    UNKNOWN = "unknown"
    FATAL = "fatal"


class FailureCode(str, Enum):
    AUTH_ERROR = "auth_error"
    PERMISSION_ERROR = "permission_error"
    RATE_LIMITED = "rate_limited"
    NETWORK_UNREACHABLE = "network_unreachable"
    TIMEOUT = "timeout"
    OOM_KILLED = "oom_killed"
    INVALID_PLAN_OUTPUT = "invalid_plan_output"
    INVALID_REVIEW_OUTPUT = "invalid_review_output"
    ADAPTER_PARSE_ERROR = "adapter_parse_error"
    TOOL_INTERNAL_ERROR = "tool_internal_error"
    SYSTEM_DEGRADED = "system_degraded"


FAILURE_CLASS: dict[FailureCode, FailureClass] = {
    FailureCode.AUTH_ERROR: FailureClass.FATAL,
    FailureCode.PERMISSION_ERROR: FailureClass.FATAL,
    FailureCode.RATE_LIMITED: FailureClass.TRANSIENT,
    FailureCode.NETWORK_UNREACHABLE: FailureClass.TRANSIENT,
    FailureCode.TIMEOUT: FailureClass.UNKNOWN,
    FailureCode.OOM_KILLED: FailureClass.UNKNOWN,
    FailureCode.INVALID_PLAN_OUTPUT: FailureClass.UNKNOWN,
    FailureCode.INVALID_REVIEW_OUTPUT: FailureClass.UNKNOWN,
    FailureCode.ADAPTER_PARSE_ERROR: FailureClass.FATAL,
    FailureCode.TOOL_INTERNAL_ERROR: FailureClass.UNKNOWN,
    FailureCode.SYSTEM_DEGRADED: FailureClass.FATAL,
}
