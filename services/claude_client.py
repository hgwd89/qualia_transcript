"""
Anthropic Claude API 共通クライアント。

構造化出力は output_config.format.json_schema 方式（SDK 0.86.0 公式仕様）を
第一候補とする。thinking={"type":"adaptive"} との併用はこの方式でのみ動作確認済み。

tool_use + tool_choice 方式は output_config が利用できない環境向けの
フォールバックとして定義する。adaptive thinking は tool_use との
併用が保証されないため、フォールバック時は thinking を送らない。
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
    schema_name: str = "result",  # API には渡さない（ログ・デバッグ用途で保持）
    use_thinking: bool = True,
) -> dict:
    """
    構造化出力（json_schema）で Claude を呼び出し、パース済み dict を返す。

    output_config.format.schema（SDK 0.86.0 公式）を第一候補とする。
    text ブロックが得られない場合は tool_use 方式でリトライする。
    """
    # ── 第一候補: output_config.format.json_schema（SDK 0.86.0 公式 API）
    output_config: dict = {
        "format": {
            "type": "json_schema",
            "schema": json_schema,
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

    try:
        response = client.messages.create(**kwargs)
        for block in response.content:
            if block.type == "text":
                return json.loads(block.text)
    except (anthropic.BadRequestError, anthropic.APIStatusError):
        # output_config 方式が環境依存のエラーで失敗した場合にフォールバック
        pass

    # ── 第二候補: tool_use 方式（output_config 非対応環境・予期せぬエラー時）
    # thinking との併用が保証されないため use_thinking=False 相当で実行する
    return _call_structured_fallback(client, system, user, json_schema, schema_name)


def _call_structured_fallback(
    client: anthropic.Anthropic,
    system: str,
    user: str,
    json_schema: dict,
    schema_name: str,
) -> dict:
    """
    tool_use 経由の構造化出力（output_config 方式失敗時のフォールバック）。
    tool name は英数字＋アンダースコアのみ使用可能。
    """
    response = client.messages.create(
        model=MODEL,
        max_tokens=8192,
        system=system,
        messages=[{"role": "user", "content": user}],
        tools=[{
            "name": schema_name,
            "description": f"Return structured output as {schema_name}",
            "input_schema": json_schema,
        }],
        tool_choice={"type": "tool", "name": schema_name},
    )
    for block in response.content:
        if block.type == "tool_use":
            return block.input
    return {}


def call_text(system: str, user: str, max_tokens: int = 16000) -> str:
    """長文テキスト生成（ストリーミングなし）。"""
    client = _client()
    response = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        thinking={"type": "adaptive"},
    )
    for block in response.content:
        if block.type == "text":
            return block.text
    return ""


def call_text_streaming(system: str, user: str) -> str:
    """長文テキスト生成（ストリーミング）。"""
    client = _client()
    with client.messages.stream(
        model=MODEL,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": user}],
        thinking={"type": "adaptive"},
    ) as stream:
        return stream.get_final_text()
