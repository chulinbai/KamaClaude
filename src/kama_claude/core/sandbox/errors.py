class SandboxError(Exception):
    error_type = "sandbox_error"


class UnsupportedSandboxError(SandboxError):
    error_type = "sandbox_unsupported"


class SandboxUnavailableError(SandboxError):
    error_type = "sandbox_unavailable"


class SandboxPolicyError(SandboxError):
    error_type = "sandbox_policy_error"


class SandboxSetupError(SandboxError):
    error_type = "sandbox_setup_error"


class SandboxCleanupError(SandboxError):
    error_type = "sandbox_cleanup_error"


class SandboxExecutionError(SandboxError):
    error_type = "sandbox_execution_error"
