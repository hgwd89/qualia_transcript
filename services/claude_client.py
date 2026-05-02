"""
Anthropic Claude API の共通クライアント。
構造化出力（json_schema）＋ 適応型思考 をデフォルトとする。
"""
import json
import anthropic
import config
from models.setting import AppSetting

MODEL = "claude-opus-4-7"


def _client() -> anthropic.Anthropic:
    api_key = AppSetting.get("anthropic_api_key") or config.ANTHROPIC_API_KEY
    return anthropic.Anthropic(api_key=api_key)


def call_structured(
    system: str,
    user: str,
    json_schema: dict,
    schema_name: str = "result",
    use_thinking: bool = True,
) -> dict:
    """
    構造化出力（json_schema）で Claude を呼び出し、パース済み dict を返す。
    """
    output_config: dict = {
        "format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "schema": json_schema,
            },
        }
    }
    kwargs: dict = {
        "model": MODEL,
        "max_tokens": 8192,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": output_config,
    }
    if use_thinking:
        kwargs["thinking"] = {"type": "adaptive"}

    client = _client()
    response = client.messages.create(**kwargs)

    for block in response.content:
        if block.type == "text":
            return json.loads(block.text)
    return {}


def call_text_streaming(system: str, user: str) -> str:
    """
    長文テキスト生成（考察レポート等）をストリーミングで取得し、完全テキストを返す。
    """
    client = _client()
    with client.messages.stream(
        model=MODEL,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": user}],
        thinking={"type": "adaptive"},
    ) as stream:
        return stream.get_final_text()
