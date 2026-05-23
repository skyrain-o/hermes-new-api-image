# hermes-new-api-image

Hermes Agent 的图片生成 plugin —— 通过 **New API / OneAPI / LiteLLM** 等 OpenAI-compatible 网关的 **codex 渠道**驱动 `gpt-image-2`，复用你已经付费的 OpenAI Codex 订阅额度生图。

这是对 Hermes 自带的 `openai-codex` plugin 的 fork，改了三点：

1. Base URL 走你自己的网关（而不是 `chatgpt.com/backend-api/codex`）
2. 认证从 ChatGPT OAuth 换成 Bearer Token（你的网关 token）
3. 容忍 New API 类网关常见的 chunked transfer encoding 不合规问题

## 它解决什么问题

你有 ChatGPT Plus / Pro 订阅（含 Codex 配额），但通过 New API / OneAPI 等网关包装成 OpenAI-compatible 接口对外。你想在 Hermes 里说 "画一张赛博朋克夜景"，让它走这个网关把图给你 —— 而**不需要再额外付一个普通 OpenAI API key 或 FAL.ai 订阅**。

> ⚠️ **前提硬限制**：你网关上的 codex 渠道**必须开启了图片能力**。OpenAI 在 ChatGPT 订阅的 Codex 路径上默认**剥离** `image_generation` tool，标准网关包装层无法绕过。新版本部分网关支持配置一个"含 image 能力的 codex 渠道"（具体看你网关后台），如果没配，本 plugin 也救不了 —— 那种情况下 plugin 会卡 60 秒后报错。

## 安装

```bash
git clone https://github.com/skyrain-o/hermes-new-api-image \
  ~/.hermes/plugins/image_gen/new-api-image
hermes plugins enable image_gen/new-api-image
```

## 配置

在 `~/.hermes/.env` 加两个变量：

```bash
NEW_API_KEY=sk-your-gateway-token
NEW_API_BASE_URL=https://your-gateway.example.com/v1
```

然后设为默认 image_gen provider：

```bash
hermes config set image_gen.provider new-api-image
hermes gateway restart
```

## 用法

直接在 Hermes 里说话：

```
画一张赛博朋克风格的夜景
```

或者命令行：

```bash
hermes -z "Generate a serene mountain landscape with cherry blossoms"
```

图片会存到 `~/.hermes/cache/images/new_api_<model>_<timestamp>_<uuid>.png`。

## 模型档位

| 模型 ID | 速度 | 适合场景 |
|---|---|---|
| `gpt-image-2-low` | ~15s | 快速迭代、最低成本 |
| `gpt-image-2-medium` (默认) | ~40s | 平衡 |
| `gpt-image-2-high` | ~2min | 最高保真、最严提示词遵循 |

切换默认档位：

```bash
hermes config set image_gen.model gpt-image-2-high
hermes gateway restart
```

## 配置参考

`~/.hermes/config.yaml`：

```yaml
image_gen:
  provider: new-api-image
  model: gpt-image-2-medium      # 全局默认
  new-api-image:
    model: gpt-image-2-medium    # plugin 专属覆盖（可选）

plugins:
  enabled:
    - image_gen/new-api-image
```

## 工作原理

OpenAI Codex 订阅的 `/v1/images/generations` 端点是**不开放**的（直接调会返回 `endpoint not supported`），但 `/v1/responses` 配合 `image_generation` tool **可以**出图。本 plugin 走后者：

```
POST /v1/responses
{
  "model": "gpt-5.4",
  "stream": true,
  "input": [{"role": "user", "content": [{"type": "input_text", "text": "..."}]}],
  "tools": [{
    "type": "image_generation",
    "model": "gpt-image-2",
    "size": "1024x1024",
    "quality": "medium",
    "output_format": "png",
    "partial_images": 1
  }],
  "tool_choice": {"type": "allowed_tools", "mode": "required", "tools": [{"type": "image_generation"}]}
}
```

返回是 SSE 流，图片 base64 数据来自 `response.image_generation_call.partial_image` 事件的 `partial_image_b64` 字段（一次给完整图，不是流式切片）。

## 已知坑：chunked encoding 容错

很多 New API / OneAPI 部署的 chunked transfer encoding 最后一个 chunk footer 是 `0\r`，而不是 RFC 7230 规定的 `0\r\n\r\n`。`httpx`（OpenAI SDK 用的）严格按 RFC 解析，会爆 `RemoteProtocolError`。但 `curl` 宽容、不报错，所以裸 curl 测试通过会误以为没问题。

本 plugin 的解法是**绕过 OpenAI SDK 的 stream 助手，自己用 raw httpx 解析 SSE，catch `RemoteProtocolError`**：如果错误发生时图片字节已经收到了（partial_image 事件先到），就静默吞掉；如果错误真的发生在图片之前，才向上抛。

代码片段（`__init__.py`）：

```python
except httpx.RemoteProtocolError as exc:
    _consume_events()           # 把 buffer 里残留事件吃干净
    if not image_b64:
        raise RuntimeError(...) # 没收到图才报错
    logger.debug(...)           # 收到了就当无事发生
```

## 限制

- 仅支持 `gpt-image-2`（OpenAI Codex 渠道支持的图片模型，目前就这一个）
- 仅支持 PNG 输出
- `partial_images: 1`（一次返回完整图，没有真正的流式生成视觉反馈）
- 不支持图生图 / inpainting（Codex 渠道没暴露这些）

## 相关

- 基于 [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) 的 `openai-codex` plugin fork
- 顺便给 Hermes 提了一个相关 bug：[#30653](https://github.com/NousResearch/hermes-agent/issues/30653) (`/model` picker 不读 `key_env` for custom_providers list entries)

## License

MIT — 与上游 `openai-codex` plugin 一致。
