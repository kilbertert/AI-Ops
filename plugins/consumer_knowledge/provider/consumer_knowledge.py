from client import call
from dify_plugin import ToolProvider
from dify_plugin.errors.tool import ToolProviderCredentialValidationError


class ConsumerKnowledgeProvider(ToolProvider):
    def _validate_credentials(self, credentials):
        try:
            call(credentials, "/v1/internal/consumer-knowledge/catalogue")
        except ValueError as exc:
            raise ToolProviderCredentialValidationError(str(exc)) from exc
