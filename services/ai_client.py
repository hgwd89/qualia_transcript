"""
OpenAI GPT API 共通クライアント。

構造化出力は response_format.type="json_schema"（Structured Outputs）を使用。
"""
import json
import openai
import config
from models.setting import AppSetting

MODEL = "gpt-4o"


def _client() -> openai.OpenAI:
    api_key = AppSetting.get("openai_api_key") or config.OPENAI_API_KEY
    return openai.OpenAI(api_key=api_key)


def call_structured(
    system: str,
    user: str,
    json_schema: dict,
    schema_name: str = "result",
    use_thinking: bool = True,  # GPT-4o では無視（将来 o1/o3 対応の拡張余地）
) -> dict:
    """構造化出力（json_schema）で GPT を呼び出し、パース済み dict を返す。"""
    client = _client()
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name":   schema_name,
                "schema": json_schema,
                "strict": True,
            },
        },
    )
    return json.loads(response.choices[0].message.content)


def call_text(system: str, user: str, max_tokens: int = 16000) -> str:
    """長文テキスト生成（ストリーミングなし）。"""
    client = _client()
    response = client.chat.completions.create(
        model=MODEL,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    )
    return response.choices[0].message.content or ""


def call_text_streaming(system: str, user: str) -> str:
    """長文テキスト生成（ストリーミング）。"""
    client = _client()
    stream = client.chat.completions.create(
        model=MODEL,
        max_tokens=16000,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        stream=True,
    )
    return "".join(chunk.choices[0].delta.content or "" for chunk in stream)
