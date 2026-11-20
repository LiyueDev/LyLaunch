"""Router 的 HTTP 客户端：health / models / load / unload / chat / chat_stream。

所有插件对推理的访问都收口到这里，`model` 字段、503 重试、超时只在一处维护。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .runtime import local_open

_LOADED_STATES = ("loaded", "running", "ready")

# 状态查询（/models）的超时。绝不能沿用聊天那 600 秒：
# router 半死时（端口 accept 但不回包，实测 18090 出现过）一个
# `status_of()` 就能把调用它的 UI 线程卡到分钟级 —— 「点下载模型，
# 半天才弹确认框」就是这么来的。状态查询只值几秒。
_STATUS_TIMEOUT = 3.0
_MODELS_TTL = 0.3          # /models 结果的复用时窗（秒）


class EngineError(Exception):
    """引擎/推理相关错误（启动失败、模型缺失、请求报错）。"""


class ModelNotInstalled(EngineError):
    """模型文件还没下载 / 不完整 —— 属于「正常状态」，不该弹错误框。

    插件捕获后只需显示「未下载模型」并把用户引到下载入口（0.39.1）。
    继承 EngineError：老插件里 `except EngineError` 仍能兜住，向后兼容。
    """


class RouterClient:
    def __init__(self, port: int = 18090, api_key: str = "",
                 default_timeout: int = 600) -> None:
        self.port = int(port)
        self.api_key = api_key or ""
        self.default_timeout = default_timeout
        # /models 的短缓存：界面上「就绪了吗」会被频繁问（切模型、刷状态、轮询），
        # 每次都是一次 HTTP。300ms 内复用上一次结果，既省请求也不会让状态滞后。
        self._models_cache: list[dict] | None = None
        self._models_at = 0.0

    # ---------------------------------------------------------------- 基础请求
    def _base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _headers(self, json_body: bool = True) -> dict:
        h = {}
        if json_body:
            h["Content-Type"] = "application/json"
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _request(self, path: str, payload: dict | None = None,
                 method: str = "GET", timeout: int | None = None) -> bytes:
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._base() + path, data=data,
            headers=self._headers(payload is not None), method=method)
        with local_open(req, timeout=timeout or self.default_timeout) as r:
            return r.read()

    def _json(self, path: str, payload: dict | None = None,
              method: str = "GET", timeout: int | None = None) -> dict:
        raw = self._request(path, payload, method, timeout)
        return json.loads(raw.decode("utf-8")) if raw else {}

    # ---------------------------------------------------------------- 健康 / 模型管理
    def health(self, timeout: float = 2) -> bool:
        try:
            with local_open(self._base() + "/health", timeout=timeout) as r:
                return r.status == 200
        except Exception:
            return False

    def models(self, timeout: float | None = None) -> list[dict]:
        """GET /models → 每项含 id/model 名与状态（loaded/loading/unloaded…）。

        ``timeout`` 默认 3 秒（状态查询，不是聊天）。
        """
        now = time.monotonic()
        if self._models_cache is not None and now - self._models_at < _MODELS_TTL:
            return self._models_cache
        try:
            data = self._json("/models", timeout=timeout or _STATUS_TIMEOUT)
        except Exception:
            # 失败也缓存：router 半死时连点几次不该每次都等满 3 秒
            self._models_cache, self._models_at = [], time.monotonic()
            return []
        # 不同构建可能是 {"data":[...]} 或 {"models":[...]} 或直接数组
        result: list[dict] = []
        if isinstance(data, list):
            result = data
        else:
            for key in ("data", "models"):
                v = data.get(key)
                if isinstance(v, list):
                    result = v
                    break
        self._models_cache, self._models_at = result, time.monotonic()
        return result

    @staticmethod
    def _mid_of(item: dict) -> str:
        return str(item.get("id") or item.get("model") or item.get("name") or "")

    @staticmethod
    def _status_of(item: dict) -> str:
        # b12047 的 /models 里 status 是对象 {"value": "loaded", "args": [...], "preset": "..."}
        s = item.get("status")
        if isinstance(s, dict):
            s = s.get("value") or s.get("state") or ""
        return str(s or "").lower()

    def loaded_ids(self, timeout: float | None = None) -> list[str]:
        """当前所有已加载模型（--models-max 2 时可能不止一个）。"""
        return [self._mid_of(i) for i in self.models(timeout)
                if self._status_of(i) in _LOADED_STATES]

    def loaded_id(self, timeout: float | None = None) -> str | None:
        """第一个已加载模型；只为展示/兼容保留，按模型操作请用 loaded_ids()。"""
        ids = self.loaded_ids(timeout)
        return ids[0] if ids else None

    def status_of(self, mid: str, timeout: float | None = None) -> str:
        for item in self.models(timeout):
            if self._mid_of(item) == mid:
                return self._status_of(item)
        return ""

    def load_model(self, mid: str, timeout: int = 60) -> None:
        try:
            self._json("/models/load", {"model": mid}, "POST", timeout)
            self._models_cache = None          # 状态变了，别让短缓存挡住
        except urllib.error.HTTPError as e:
            body = e.read()[:400].decode("utf-8", "replace")
            raise EngineError(f"加载模型 {mid} 失败：{body}") from None
        except Exception as e:
            raise EngineError(f"加载模型 {mid} 失败：{e}") from None

    def unload_model(self, mid: str, timeout: int = 30) -> None:
        try:
            self._json("/models/unload", {"model": mid}, "POST", timeout)
            self._models_cache = None
        except Exception:
            # 卸载失败不该阻塞后续流程：router 的 LRU 仍会兜底
            pass

    # ---------------------------------------------------------------- 推理（非流式）
    def chat(self, model_id: str, messages: list, timeout: int | None = None,
             retries: int = 4, on_wait=None, **params) -> dict:
        """POST /v1/chat/completions（非流式）。503/连接失败时退避重试。

        返回完整 JSON（调用方取 choices[0].message.content / usage）。
        """
        payload = {
            "model": model_id,
            "messages": messages,
            "stream": False,
        }
        payload.update({k: v for k, v in params.items() if v is not None})
        assert payload.get("model"), "payload 必须带 model 字段（Router 靠它路由）"

        delays = [0.5, 1.0, 2.0, 4.0]
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                raw = self._request("/v1/chat/completions", payload, "POST",
                                    timeout or self.default_timeout)
                return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read()[:400].decode("utf-8", "replace")
                # 503 = 模型正在加载；429 = 并发受限；都值得等
                if e.code in (503, 429) and attempt < retries:
                    last_err = EngineError(f"服务忙（{e.code}），稍候重试")
                    if on_wait:
                        try:
                            on_wait(f"模型加载中，请稍候…（{attempt + 1}/{retries}）")
                        except Exception:
                            pass
                    time.sleep(delays[min(attempt, len(delays) - 1)])
                    continue
                if "context" in body.lower():
                    raise EngineError("内容超出模型上下文，请缩短后再试") from None
                raise EngineError(f"推理服务返回错误：{body}") from None
            except Exception as e:
                last_err = e
                if attempt < retries:
                    time.sleep(delays[min(attempt, len(delays) - 1)])
                    continue
                raise EngineError(f"推理请求失败：{e}") from None
        raise EngineError(str(last_err or "推理请求失败"))

    # ---------------------------------------------------------------- 推理（流式）
    def chat_stream(self, model_id: str, messages: list, timeout: int | None = None,
                    **params):
        """流式 SSE，逐条 yield ("text"|"think"|"usage", payload)。"""
        payload = {
            "model": model_id,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        payload.update({k: v for k, v in params.items() if v is not None})
        assert payload.get("model"), "payload 必须带 model 字段（Router 靠它路由）"

        req = urllib.request.Request(
            self._base() + "/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(True), method="POST")
        with local_open(req, timeout=timeout or 600) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                usage = obj.get("usage")
                if usage:
                    yield ("usage", usage)
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                think = delta.get("reasoning_content") or delta.get("reasoning")
                if think:
                    yield ("think", think)
                text = delta.get("content")
                if text:
                    yield ("text", text)
