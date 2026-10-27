import json

from client import call
from dify_plugin import Tool
from dify_plugin.entities import I18nObject, ParameterOption


class ConsumerKnowledge(Tool):
    def _fetch_parameter_options(self, parameter):
        if parameter != "knowledge_base_ids":
            return []
        data = call(self.runtime.credentials, "/v1/internal/consumer-knowledge/catalogue")
        return [
            ParameterOption(
                value=row["knowledge_base_id"], label=I18nObject(en_US=row["name"], zh_Hans=row["name"])
            )
            for row in data["libraries"]
        ]

    def _invoke(self, tool_parameters):
        libraries = tool_parameters.get("knowledge_base_ids")
        grant = tool_parameters.get("data_grant")
        if not isinstance(libraries, list) or not libraries or not isinstance(grant, str) or not grant:
            raise ValueError("请选择知识库，并通过已授权的试用或App入口执行")
        result = call(
            self.runtime.credentials,
            "/v1/internal/consumer-data/knowledge",
            grant=grant,
            body={"knowledge_base_ids": libraries, "image_text": tool_parameters.get("image_text", "")},
        )
        yield self.create_text_message(json.dumps(result, ensure_ascii=False))
