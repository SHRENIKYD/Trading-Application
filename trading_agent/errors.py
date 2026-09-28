class ActionFormatError(ValueError):
    """The LLM output is not a valid action."""


class AgentFatalError(RuntimeError):
    """Unrecoverable failure (Ollama unreachable, model missing, bad configuration)."""
