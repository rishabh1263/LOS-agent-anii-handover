class AgenticAIError(Exception):
    pass


class ConfigurationError(AgenticAIError):
    pass


class AgentDisabledError(AgenticAIError):
    pass


class DocumentTypeNotSupportedError(AgenticAIError):
    pass


class LegacyAPIError(AgenticAIError):
    pass
