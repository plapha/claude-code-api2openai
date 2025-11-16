from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import os
import json
import requests
import hashlib
import time
import uuid
from typing import Iterator, List, Dict, Any, Optional
from functools import wraps
import socket
from urllib.parse import urlparse
import base64
import mimetypes

app = Flask(__name__)

# 启用 CORS - 支持外界访问（可通过环境变量 CORS_ORIGINS 配置多个来源，逗号分隔；默认 *）
_origins_env = os.getenv('CORS_ORIGINS', '*').strip()
_cors_origins: Any = '*'
if _origins_env not in ('*', '"*"'):
    _cors_origins = [o.strip() for o in _origins_env.split(',') if o.strip()]

CORS(app, resources={
    r"/*": {
        "origins": _cors_origins,
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"]
    }
})

# 配置
# 上游 Claude 兼容 API（fizzlycode 兼容层）
API_URL = os.getenv("UPSTREAM_API_URL", "https://fizzlycode.com/api/v1/messages?beta=true")

# 上游鉴权（务必通过环境变量覆盖默认值）
UPSTREAM_API_KEY_PLACEHOLDER = "cr_set_upstream_api_key"
UPSTREAM_API_KEY = os.getenv("UPSTREAM_API_KEY", UPSTREAM_API_KEY_PLACEHOLDER)
if UPSTREAM_API_KEY == UPSTREAM_API_KEY_PLACEHOLDER:
    print("⚠️ 未检测到 UPSTREAM_API_KEY 环境变量，默认占位值会导致上游 401。请在部署前设置真实值。")


def _parse_allowed_api_keys(raw: str) -> set:
    return {
        token for token in (
            segment.strip()
            for segment in (raw or "").split(",")
        ) if token
    }


# 代理自身鉴权（给你的客户端用）
ALLOWED_API_KEYS = _parse_allowed_api_keys(os.getenv("ALLOWED_API_KEYS", "sk-test123,sk-test456"))
if not ALLOWED_API_KEYS:
    print("⚠️ ALLOWED_API_KEYS 为空，所有请求都会被拒绝。请配置允许访问的客户端 key。")

# 默认模型可通过环境变量覆盖
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "claude-3-5-sonnet-latest")

# 允许在环境中配置模型别名映射（逗号分隔的 from:to 列表）
# 例如：MODEL_ALIASES="claude-sonnet-4-5-20250929:claude-3-5-sonnet-latest"
RAW_MODEL_ALIASES = os.getenv("MODEL_ALIASES", "")
MODEL_ALIASES: Dict[str, str] = {}
for pair in filter(None, (s.strip() for s in RAW_MODEL_ALIASES.split(","))):
    if ":" in pair:
        src, dst = pair.split(":", 1)
        if src.strip() and dst.strip():
            MODEL_ALIASES[src.strip()] = dst.strip()

DEFAULT_SYSTEM_PROMPT = os.getenv(
    "DEFAULT_SYSTEM_PROMPT",
    "You are Claude Code, Anthropic's official CLI for Claude."
)
# Default outbound max tokens (upper bound). Many upstreams reject very large values.
# Keep a conservative default to avoid 5xx from vendors that cannot honor huge outputs.
DEFAULT_MAX_TOKENS = int(os.getenv("DEFAULT_MAX_TOKENS", 4096))
# Optional hard ceiling regardless of request/body; can be raised via env if your upstream allows it.
MAX_TOKENS_HARD_LIMIT = int(os.getenv("MAX_TOKENS_HARD_LIMIT", 16384))
 
# Dynamic max_tokens settings
def _strtobool(val: Optional[str]) -> bool:
    if val is None:
        return False
    return str(val).strip().lower() in {"1", "true", "yes", "y", "on"}

MAX_TOKENS_DYNAMIC = _strtobool(os.getenv("MAX_TOKENS_DYNAMIC", "false"))
TOKEN_EST_CHARS_PER_TOKEN = float(os.getenv("TOKEN_EST_CHARS_PER_TOKEN", "4.0"))  # ~4 chars per token heuristic
IMAGE_TOKEN_EQUIV = int(os.getenv("IMAGE_TOKEN_EQUIV", "256"))  # rough cost per image block when estimating
DYNAMIC_SAFETY_MARGIN = int(os.getenv("DYNAMIC_SAFETY_MARGIN", "1024"))  # headroom to avoid hitting context limit

# Optional per-model context window limits, JSON, e.g.: {"claude-3-5-sonnet-latest":200000}
try:
    _MODEL_CONTEXT_LIMITS_JSON = os.getenv("MODEL_CONTEXT_LIMITS_JSON", "").strip()
    MODEL_CONTEXT_LIMITS = json.loads(_MODEL_CONTEXT_LIMITS_JSON) if _MODEL_CONTEXT_LIMITS_JSON else {}
    if not isinstance(MODEL_CONTEXT_LIMITS, dict):
        MODEL_CONTEXT_LIMITS = {}
except Exception:
    MODEL_CONTEXT_LIMITS = {}
DEFAULT_PROXY_URL = os.getenv("DEFAULT_PROXY_URL", "http://127.0.0.1:7890")
UPSTREAM_PROXY_URL = os.getenv("UPSTREAM_PROXY_URL")
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", 5 * 1024 * 1024))
IMAGE_FETCH_TIMEOUT = int(os.getenv("IMAGE_FETCH_TIMEOUT", 15))


def build_proxy_config():
    """Return proxy configuration if reachable, otherwise None."""
    def _format_proxy(url: str):
        return {'http': url, 'https': url}

    if UPSTREAM_PROXY_URL is not None:
        cleaned = UPSTREAM_PROXY_URL.strip()
        if cleaned:
            print(f"🛜 Using explicit proxy {cleaned}")
            return _format_proxy(cleaned)
        print("ℹ️ UPSTREAM_PROXY_URL is empty, proxy disabled")
        return None

    if not DEFAULT_PROXY_URL:
        return None

    parsed = urlparse(DEFAULT_PROXY_URL)
    host, port = parsed.hostname, parsed.port
    if not host or not port:
        return None

    try:
        with socket.create_connection((host, port), timeout=0.5):
            print(f"🛜 Detected proxy at {DEFAULT_PROXY_URL}")
            return _format_proxy(DEFAULT_PROXY_URL)
    except OSError:
        print(f"ℹ️ Proxy {DEFAULT_PROXY_URL} unreachable, calling upstream directly")
        return None


PROXIES = build_proxy_config()

# 上游兼容层常用头，允许通过环境变量自定义/禁用
UPSTREAM_HEADERS_BASE: Dict[str, str] = {
    'accept': 'application/json',
    'anthropic-version': os.getenv('UPSTREAM_ANTHROPIC_VERSION', '2023-06-01'),
    # 某些 fizzlycode 部署要求带 beta/streaming 标记，否则会 5xx/降级
    'anthropic-beta': os.getenv(
        'UPSTREAM_ANTHROPIC_BETA',
        'interleaved-thinking-2025-05-14,fine-grained-tool-streaming-2025-05-14'
    ),
    'anthropic-dangerous-direct-browser-access': os.getenv(
        'UPSTREAM_ANTHROPIC_DANGEROUS', 'true'
    ),
    'content-type': 'application/json; charset=utf-8',
    'user-agent': os.getenv('UPSTREAM_USER_AGENT', 'claude-cli/2.0.25 (external, proxy)'),
    'x-app': os.getenv('UPSTREAM_X_APP', 'cli'),
}

# 允许用户通过 JSON 字符串追加/覆盖/删除（值为空或 null 即视为删除）
_extra_headers_raw = os.getenv('UPSTREAM_EXTRA_HEADERS_JSON', '').strip()
if _extra_headers_raw:
    try:
        _extra = json.loads(_extra_headers_raw)
        if isinstance(_extra, dict):
            for _k, _v in _extra.items():
                if _v in ('', None):
                    UPSTREAM_HEADERS_BASE.pop(_k, None)
                else:
                    UPSTREAM_HEADERS_BASE[_k] = str(_v)
    except Exception as _exc:
        print(f"⚠️ UPSTREAM_EXTRA_HEADERS_JSON parse error: {_exc}")

# 清理空值，避免发送空头
UPSTREAM_HEADERS_BASE = {k: v for k, v in UPSTREAM_HEADERS_BASE.items() if v}


def _guess_media_type(source: str, fallback: str = "application/octet-stream") -> str:
    media_type, _ = mimetypes.guess_type(source)
    return media_type or fallback


def _encode_data_url(data_url: str) -> Dict[str, str]:
    try:
        header, encoded = data_url.split(',', 1)
        meta = header.split(';')
        media_type = meta[0][5:] if meta[0].startswith('data:') else 'application/octet-stream'
        if 'base64' in meta:
            return {
                'media_type': media_type or 'application/octet-stream',
                'data': encoded
            }
        return {
            'media_type': media_type or 'application/octet-stream',
            'data': base64.b64encode(encoded.encode('utf-8')).decode('ascii')
        }
    except Exception as exc:  # noqa: BLE001 - provide user-facing error
        raise ValueError(f"无法解析 data URL：{exc}")


def _download_image(url: str) -> Dict[str, str]:
    headers = {'User-Agent': 'claude-proxy/1.0'}
    try:
        resp = requests.get(
            url,
            stream=True,
            timeout=IMAGE_FETCH_TIMEOUT,
            proxies=PROXIES,
            headers=headers
        )
    except requests.RequestException as exc:  # noqa: BLE001
        raise ValueError(f"下载图片失败：{exc}")

    if resp.status_code >= 400:
        raise ValueError(f"下载图片失败：上游返回 {resp.status_code}")

    content_type = resp.headers.get('Content-Type') or _guess_media_type(url)
    total = 0
    data = bytearray()
    for chunk in resp.iter_content(64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_IMAGE_BYTES:
            raise ValueError(f"图片大小超过限制 {MAX_IMAGE_BYTES // (1024 * 1024)}MB")
        data.extend(chunk)

    if total == 0:
        raise ValueError("下载图片失败：内容为空")

    encoded = base64.b64encode(data).decode('ascii')
    return {'media_type': content_type, 'data': encoded}


def _image_block_from_part(part: Dict[str, Any]) -> Dict[str, Any]:
    payload = part.get('image_url') or part.get('image') or {}
    if isinstance(payload, str):
        url = payload
    else:
        url = payload.get('url')
    if not url:
        raise ValueError('缺少图片 URL')

    if url.startswith('data:'):
        source = _encode_data_url(url)
    else:
        source = _download_image(url)

    return {
        'type': 'image',
        'source': {
            'type': 'base64',
            'media_type': source['media_type'],
            'data': source['data']
        }
    }


def _coerce_positive_int(value: Any) -> Optional[int]:
    """Best-effort convert inbound values (str/json/query) to a positive int."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        candidate = int(value)
    except (TypeError, ValueError):
        return None
    return candidate if candidate > 0 else None


def _clamp_max_tokens(model: str, requested: Optional[int]) -> int:
    """Clamp outbound max_tokens to a safe ceiling.

    Rationale: Several upstream Claude-compatible services (including proxies) return 5xx
    instead of a clean 4xx when `max_tokens` is too large. To make the proxy resilient,
    we cap the value using a conservative hard limit that can be tuned via env.
    """
    # Start with defaults; prefer explicit request, then DEFAULT_MAX_TOKENS
    desired = requested or DEFAULT_MAX_TOKENS
    # Hard cap
    cap = MAX_TOKENS_HARD_LIMIT if MAX_TOKENS_HARD_LIMIT > 0 else 8192
    if desired > cap:
        print(f"🔧 max_tokens clamped from {desired} -> {cap} for model '{model}'")
        return cap
    return desired


def _get_model_context_limit(model: str) -> Optional[int]:
    # Exact match first, then alias-insensitive fallback
    if model in MODEL_CONTEXT_LIMITS:
        return int(MODEL_CONTEXT_LIMITS[model])
    mapped = MODEL_ALIASES.get(model)
    if mapped and mapped in MODEL_CONTEXT_LIMITS:
        return int(MODEL_CONTEXT_LIMITS[mapped])
    return None


def _estimate_input_tokens(anthropic_messages: List[Dict[str, Any]], system_blocks: List[Dict[str, Any]]) -> int:
    total_chars = 0
    for m in anthropic_messages or []:
        for b in m.get('content', []) or []:
            btype = b.get('type')
            if btype == 'text':
                total_chars += len(b.get('text') or '')
            elif btype == 'image':
                total_chars += IMAGE_TOKEN_EQUIV * int(TOKEN_EST_CHARS_PER_TOKEN)
    for b in system_blocks or []:
        if b.get('type') == 'text':
            total_chars += len(b.get('text') or '')
    if TOKEN_EST_CHARS_PER_TOKEN <= 0:
        return max(1, total_chars // 4)
    return max(1, int(total_chars / TOKEN_EST_CHARS_PER_TOKEN))


def _apply_dynamic_max_tokens(model: str, requested: Optional[int], anthropic_messages: List[Dict[str, Any]], system_blocks: List[Dict[str, Any]]) -> int:
    static = _clamp_max_tokens(model, requested)
    if not MAX_TOKENS_DYNAMIC:
        return static
    context_limit = _get_model_context_limit(model)
    if not context_limit:
        return static
    used = _estimate_input_tokens(anthropic_messages, system_blocks)
    budget = max(0, context_limit - used - max(0, DYNAMIC_SAFETY_MARGIN))
    if budget <= 0:
        minimal = min(256, MAX_TOKENS_HARD_LIMIT) if MAX_TOKENS_HARD_LIMIT > 0 else 256
        print(f"🔧 dynamic budget <= 0 (used={used}, ctx={context_limit}), setting max_tokens={minimal}")
        return minimal
    dynamic_cap = min(budget, MAX_TOKENS_HARD_LIMIT if MAX_TOKENS_HARD_LIMIT > 0 else budget)
    desired = requested or DEFAULT_MAX_TOKENS
    final = min(desired, int(dynamic_cap))
    if final != desired:
        print(f"🔧 dynamic max_tokens adjusted {desired} -> {final} (budget={budget}, cap={MAX_TOKENS_HARD_LIMIT}, model='{model}')")
    return max(1, int(final))


def _normalize_model_name(model: str) -> str:
    """Map inbound model to upstream-accepted value via env alias map."""
    if not model:
        return DEFAULT_MODEL
    # 环境别名优先
    mapped = MODEL_ALIASES.get(model)
    if mapped:
        return mapped
    return model


def _convert_content_to_blocks(content: Any) -> List[Dict[str, Any]]:
    if content is None:
        return [{'type': 'text', 'text': ''}]

    if isinstance(content, str):
        return [{'type': 'text', 'text': content}]

    if isinstance(content, dict):
        return _convert_content_to_blocks([content])

    if isinstance(content, list):
        blocks: List[Dict[str, Any]] = []
        for part in content:
            if part is None:
                continue
            if isinstance(part, str):
                blocks.append({'type': 'text', 'text': part})
                continue

            if not isinstance(part, dict):
                raise ValueError('消息 content 中存在无法识别的结构')

            part_type = part.get('type', 'text')
            if part_type in ('text', 'input_text', 'output_text'):
                text_value = part.get('text') or part.get('value')
                if text_value is None:
                    raise ValueError('text 类型内容缺少 text 字段')
                blocks.append({'type': 'text', 'text': text_value})
            elif part_type in ('image_url', 'input_image', 'image'):
                blocks.append(_image_block_from_part(part))
            else:
                raise ValueError(f"暂不支持的内容类型: {part_type}")

        if not blocks:
            return [{'type': 'text', 'text': ''}]
        return blocks

    raise ValueError('消息 content 必须是字符串、列表或对象')


def _stringify_tool_result_content(content: Any) -> str:
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        fragments: List[str] = []
        for part in content:
            if part is None:
                continue
            if isinstance(part, str):
                fragments.append(part)
                continue
            if isinstance(part, dict):
                part_type = part.get('type')
                if part_type in ('text', 'input_text', 'output_text'):
                    text_value = part.get('text') or part.get('value')
                    if text_value:
                        fragments.append(text_value)
                else:
                    fragments.append(json.dumps(part, ensure_ascii=False))
            else:
                fragments.append(str(part))
        return '\n'.join(fragment for fragment in fragments if fragment)
    if isinstance(content, dict):
        part_type = content.get('type')
        if part_type in ('text', 'input_text', 'output_text'):
            return content.get('text') or content.get('value') or ''
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def _parse_tool_call_arguments(arguments: Any) -> Any:
    if arguments is None:
        return {}
    if isinstance(arguments, (dict, list)):
        return arguments
    if isinstance(arguments, str):
        stripped = arguments.strip()
        if not stripped:
            return {}
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return {'raw_arguments': stripped}
    return arguments


def _tool_calls_to_blocks(tool_calls: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if not tool_calls:
        return []

    blocks: List[Dict[str, Any]] = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        function = call.get('function', {}) or {}
        name = function.get('name')
        if not name:
            continue
        block = {
            'type': 'tool_use',
            'id': call.get('id') or f"toolu_{uuid.uuid4().hex}",
            'name': name,
            'input': _parse_tool_call_arguments(function.get('arguments'))
        }
        blocks.append(block)
    return blocks


def _serialize_arguments_for_openai(arguments: Any) -> str:
    if isinstance(arguments, str):
        return arguments
    try:
        return json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps({'raw_arguments': str(arguments)}, ensure_ascii=False)


def _convert_tools(tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    if not tools:
        return None

    converted: List[Dict[str, Any]] = []
    for item in tools:
        if not isinstance(item, dict):
            continue

        if 'input_schema' in item and 'name' in item:
            converted.append(item)
            continue

        if item.get('type') == 'function' and 'function' in item:
            fn = item['function'] or {}
            name = fn.get('name')
            if not name:
                continue
            converted.append({
                'name': name,
                'description': fn.get('description', ''),
                'input_schema': fn.get('parameters') or {'type': 'object', 'properties': {}}
            })
            continue

    return converted or None


def _convert_tool_choice(tool_choice: Any) -> Optional[Any]:
    if tool_choice in (None, '', {}, []):
        return None
    if isinstance(tool_choice, str):
        if tool_choice in ('auto', 'none', 'auto_multi'):
            return tool_choice
        if tool_choice == 'required':
            return 'auto'
    if isinstance(tool_choice, dict):
        if tool_choice.get('type') == 'function':
            name = tool_choice.get('function', {}).get('name')
            if name:
                return {'type': 'tool', 'name': name}
        if tool_choice.get('type') == 'tool' and tool_choice.get('name'):
            return tool_choice
    return None


def _map_stop_reason(reason: Optional[str]) -> str:
    if not reason:
        return 'stop'
    mapping = {
        'end_turn': 'stop',
        'stop_sequence': 'stop',
        'max_tokens': 'length',
        'tool_use': 'tool_calls',
        'content_filter': 'content_filter'
    }
    return mapping.get(reason, 'stop')


def _build_stream_chunk(message_id: str, delta: Dict[str, Any], finish_reason: Optional[str] = None) -> bytes:
    chunk = {
        'id': message_id,
        'object': 'chat.completion.chunk',
        'created': int(time.time()),
        'model': DEFAULT_MODEL,
        'choices': [{
            'index': 0,
            'delta': delta,
            'finish_reason': finish_reason
        }]
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode('utf-8')

# User ID 管理
CURRENT_USER_ID = None
LAST_UPDATE_TIME = 0
UPDATE_INTERVAL = 5 * 60 * 60  # 5小时（秒）

def generate_user_id():
    """随机生成 user_id，格式与真实的一致"""
    # 生成随机的 account hash (64位16进制)
    random_account = str(uuid.uuid4()) + str(time.time())
    user_hash = hashlib.sha256(random_account.encode()).hexdigest()
    
    # 生成随机的 session UUID
    session = str(uuid.uuid4())
    
    user_id = f"user_{user_hash}_account__session_{session}"
    print(f"✅ Generated user_id: {user_id}")
    return user_id

def get_current_user_id():
    """获取当前 user_id，超过5小时自动刷新"""
    global CURRENT_USER_ID, LAST_UPDATE_TIME
    
    current_time = time.time()
    
    # 首次运行或超过5小时，重新生成
    if CURRENT_USER_ID is None or (current_time - LAST_UPDATE_TIME) > UPDATE_INTERVAL:
        CURRENT_USER_ID = generate_user_id()
        LAST_UPDATE_TIME = current_time
        print(f"🔄 User ID updated at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    return CURRENT_USER_ID

# API Key 验证
def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        api_key = auth.replace('Bearer ', '').strip()

        if api_key not in ALLOWED_API_KEYS:
            return jsonify({'error': 'Invalid API key'}), 401

        return f(*args, **kwargs)

    return decorated

def convert_messages_to_anthropic(messages):
    anthropic_messages = []
    system_text_fragments: List[str] = []

    for msg in messages:
        role = msg.get('role', 'user')

        if role == 'system':
            blocks = _convert_content_to_blocks(msg.get('content', ''))
            system_text_fragments.extend(
                block['text']
                for block in blocks
                if block.get('type') == 'text'
            )
            continue

        if role == 'tool':
            tool_use_id = msg.get('tool_call_id')
            if not tool_use_id:
                raise ValueError('tool 消息缺少 tool_call_id 字段')
            tool_result_block: Dict[str, Any] = {
                'type': 'tool_result',
                'tool_use_id': tool_use_id,
                'content': _stringify_tool_result_content(msg.get('content'))
            }
            if 'is_error' in msg:
                tool_result_block['is_error'] = bool(msg.get('is_error'))
            elif msg.get('status') == 'error':
                tool_result_block['is_error'] = True
            anthropic_messages.append({
                'role': 'user',
                'content': [tool_result_block]
            })
            continue

        normalized_role = role if role in ('user', 'assistant') else 'user'
        blocks = _convert_content_to_blocks(msg.get('content', ''))

        if normalized_role == 'assistant':
            blocks.extend(_tool_calls_to_blocks(msg.get('tool_calls')))

        anthropic_messages.append({
            'role': normalized_role,
            'content': blocks
        })

    system_content = '\n'.join(fragment.strip() for fragment in system_text_fragments if fragment.strip()) or None
    return anthropic_messages, system_content


def convert_anthropic_content_to_openai(content_blocks: List[Dict[str, Any]]):
    text_fragments: List[str] = []
    tool_calls: List[Dict[str, Any]] = []

    for block in content_blocks or []:
        block_type = block.get('type')
        if block_type == 'text':
            text_fragments.append(block.get('text', ''))
        elif block_type == 'tool_use':
            tool_calls.append({
                'id': block.get('id') or f"toolu_{uuid.uuid4().hex}",
                'type': 'function',
                'function': {
                    'name': block.get('name', ''),
                    'arguments': _serialize_arguments_for_openai(block.get('input'))
                }
            })

    return ''.join(text_fragments), tool_calls

def stream_anthropic_to_openai(response) -> Iterator[bytes]:
    message_id = f"chatcmpl-{int(time.time())}"
    sent_role = False
    tool_call_index = 0
    pending_stop_reason: Optional[str] = None

    try:
        for line in response.iter_lines(decode_unicode=False):
            if not line:
                continue

            if not line.startswith(b'data:'):
                continue

            data = line[5:].strip()

            if data == b'[DONE]':
                yield b"data: [DONE]\n\n"
                break

            try:
                event = json.loads(data.decode('utf-8'))
            except (json.JSONDecodeError, UnicodeDecodeError) as err:
                print(f"⚠️ Stream decode error: {err}, line: {line[:100]}")
                continue

            event_type = event.get('type')

            if event_type == 'content_block_start':
                block = event.get('content_block', {}) or {}
                if block.get('type') == 'tool_use':
                    delta: Dict[str, Any] = {
                        'tool_calls': [{
                            'index': tool_call_index,
                            'id': block.get('id') or f"toolu_{uuid.uuid4().hex}",
                            'type': 'function',
                            'function': {
                                'name': block.get('name', ''),
                                'arguments': _serialize_arguments_for_openai(block.get('input'))
                            }
                        }]
                    }
                    if not sent_role:
                        delta['role'] = 'assistant'
                        sent_role = True
                    tool_call_index += 1
                    yield _build_stream_chunk(message_id, delta)
                continue

            if event_type == 'content_block_delta':
                delta_text = event.get('delta', {}).get('text', '')
                if delta_text:
                    delta_payload: Dict[str, Any] = {'content': delta_text}
                    if not sent_role:
                        delta_payload['role'] = 'assistant'
                        sent_role = True
                    yield _build_stream_chunk(message_id, delta_payload)
                continue

            if event_type == 'message_delta':
                pending_stop_reason = event.get('delta', {}).get('stop_reason') or pending_stop_reason
                continue

            if event_type == 'message_stop':
                stop_reason = event.get('stop_reason') or event.get('message', {}).get('stop_reason') or pending_stop_reason
                yield _build_stream_chunk(message_id, {}, _map_stop_reason(stop_reason))
                yield b"data: [DONE]\n\n"
                break

    except Exception as exc:
        print(f"❌ Stream error: {exc}")
        yield _build_stream_chunk(message_id, {}, 'stop')
        yield b"data: [DONE]\n\n"

@app.route('/v1/chat/completions', methods=['POST', 'OPTIONS'])
@require_api_key
def chat_completions():
    if request.method == 'OPTIONS':
        return '', 204

    try:
        data = request.get_json() or {}
        messages = data.get('messages', [])
        model = _normalize_model_name(data.get('model', DEFAULT_MODEL))
        query_max_tokens = _coerce_positive_int(request.args.get('max_tokens'))
        body_max_tokens = _coerce_positive_int(data.get('max_tokens'))
        requested_max_tokens = query_max_tokens or body_max_tokens
        stream = data.get('stream', False)

        try:
            anthropic_messages, system_content = convert_messages_to_anthropic(messages)
        except ValueError as err:
            return jsonify({'error': str(err)}), 400

        body = {
            'model': model,
            'messages': anthropic_messages,
            'metadata': {'user_id': get_current_user_id()},  # 使用动态生成的 user_id
            'stream': stream
        }

        converted_tools = _convert_tools(data.get('tools'))
        if converted_tools:
            body['tools'] = converted_tools

        converted_tool_choice = _convert_tool_choice(data.get('tool_choice'))
        if converted_tool_choice:
            body['tool_choice'] = converted_tool_choice

        # fizzlycode 的兼容层要求固定的 system 前缀，否则直接 400
        system_blocks = [{'type': 'text', 'text': DEFAULT_SYSTEM_PROMPT}]
        if system_content:
            system_blocks.append({'type': 'text', 'text': system_content})
        body['system'] = system_blocks

        # Compute final max_tokens (dynamic if enabled)
        max_tokens = _apply_dynamic_max_tokens(model, requested_max_tokens, anthropic_messages, system_blocks)
        body['max_tokens'] = max_tokens

        headers = dict(UPSTREAM_HEADERS_BASE)
        headers['authorization'] = f'Bearer {UPSTREAM_API_KEY}'

        print(f"📤 Request body: {json.dumps(body, ensure_ascii=False)}")

        common_kwargs = {
            'headers': headers,
            'json': body
        }
        if PROXIES:
            common_kwargs['proxies'] = PROXIES

        if stream:
            # 流式请求，增加超时时间，禁用缓冲
            resp = requests.post(
                API_URL,
                stream=True,
                timeout=(10, 300),  # (连接超时, 读取超时)
                **common_kwargs
            )

            if resp.status_code != 200:
                # 尝试把上游错误透传，便于排查（避免只有 500）
                try:
                    err_json = resp.json()
                except Exception:
                    err_json = {'error': resp.text}
                print(f"❌ API Error ({resp.status_code}): {err_json}")
                return jsonify({'upstream_status': resp.status_code, 'upstream_error': err_json}), resp.status_code

            # 创建流式响应
            response = Response(
                stream_anthropic_to_openai(resp),
                content_type='text/event-stream; charset=utf-8',
                direct_passthrough=True  # 禁用 Flask 缓冲
            )
            response.headers['Cache-Control'] = 'no-cache, no-transform'
            response.headers['X-Accel-Buffering'] = 'no'  # 禁用 Nginx 缓冲
            response.headers['Connection'] = 'keep-alive'
            return response

        else:
            resp = requests.post(
                API_URL,
                timeout=120,
                **common_kwargs
            )
            if resp.status_code != 200:
                try:
                    err_json = resp.json()
                except Exception:
                    err_json = {'error': resp.text}
                print(f"❌ API Error ({resp.status_code}): {err_json}")
                return jsonify({'upstream_status': resp.status_code, 'upstream_error': err_json}), resp.status_code

            result = json.loads(resp.content.decode('utf-8'))
            message_text, tool_calls = convert_anthropic_content_to_openai(result.get('content', []))
            finish_reason = _map_stop_reason(result.get('stop_reason'))

            message_payload: Dict[str, Any] = {'role': 'assistant', 'content': message_text}
            if tool_calls:
                message_payload['tool_calls'] = tool_calls

            return jsonify({
                'id': result.get('id', f"chatcmpl-{int(time.time())}"),
                'object': 'chat.completion',
                'created': int(time.time()),
                'model': model,
                'choices': [{
                    'index': 0,
                    'message': message_payload,
                    'finish_reason': finish_reason
                }],
                'usage': {
                    'prompt_tokens': result.get('usage', {}).get('input_tokens', 0),
                    'completion_tokens': result.get('usage', {}).get('output_tokens', 0),
                    'total_tokens': result.get('usage', {}).get('input_tokens', 0) + result.get('usage', {}).get(
                        'output_tokens', 0)
                }
            })

    except Exception as e:
        print(f"❌ Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/v1/models', methods=['GET', 'OPTIONS'])
@require_api_key
def list_models():
    if request.method == 'OPTIONS':
        return '', 204

    return jsonify({
        'object': 'list',
        'data': [{'id': DEFAULT_MODEL, 'object': 'model', 'created': 0, 'owned_by': 'anthropic'}]
    })

@app.route('/health', methods=['GET'])
def health():
    safe_key_set = len(ALLOWED_API_KEYS)
    return jsonify({
        'status': 'ok',
        'upstream_url': API_URL,
        'default_model': DEFAULT_MODEL,
        'model_aliases': MODEL_ALIASES,
        'allowed_api_keys_count': safe_key_set,
        'current_user_id': CURRENT_USER_ID,
        'last_update': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(LAST_UPDATE_TIME)) if LAST_UPDATE_TIME > 0 else 'Never',
        'next_update_in': f"{int((UPDATE_INTERVAL - (time.time() - LAST_UPDATE_TIME)) / 60)} minutes" if LAST_UPDATE_TIME > 0 else 'On first request'
    })

if __name__ == '__main__':
    PORT = int(os.getenv('PORT', 5000))
    print(f"🚀 Claude Proxy running on port {PORT}")
    print(f"🔑 Allowed API keys: {len(ALLOWED_API_KEYS)}")
    print(f"⏱️  User ID refresh interval: {UPDATE_INTERVAL / 3600} hours")
    app.config['JSON_AS_ASCII'] = False

    # 禁用 Flask 自带的请求日志，减少延迟
    import logging

    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)

    # 使用 threaded=True 支持并发
    app.run(host='0.0.0.0', port=PORT, threaded=True)
