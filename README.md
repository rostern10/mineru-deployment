# MinerU 本地文档解析与问答平台

面向 GPU 服务器的本地 MinerU 部署项目。它将 MinerU 文档解析服务、Gradio Office 文档解析、Qwen/vLLM 本地问答接口和 FastAPI 管理桥接层组合在一起，用于将 PDF、图片和 Office 文档转换为 Markdown，并基于解析内容进行问答。

## 功能

- 通过 `mineru-api` 解析 PDF 与图片，输出 Markdown 和结果压缩包。
- 通过 `mineru-gradio` 解析 DOC、DOCX、PPT、PPTX、XLS 与 XLSX 文件。
- 支持本地 Qwen3.6-35B vLLM 推理服务的流式问答。
- 提供 FastAPI 用户、会话、文档解析任务和管理接口。
- 使用 SQLite 保存用户与会话元数据。

## 架构

```text
浏览器
  └─ FastAPI bridge（端口 9999）
       ├─ mineru-api（端口 8000）：PDF / 图片解析
       ├─ mineru-gradio（端口 7860）：Office 文档解析
       └─ vLLM Qwen3.6-35B（端口 8001）：本地流式问答
```

`bridge.py` 根据文件扩展名选择解析后端：PDF 和图片直接提交到 `mineru-api`；Office 文档通过 Gradio 服务转换。聊天请求则转发给同一网络中的本地 vLLM 服务。

## 环境要求

- Linux 主机、Docker 与 Docker Compose。
- NVIDIA GPU、驱动及 NVIDIA Container Toolkit。
- 本地 MinerU 模型目录和本地 Qwen 模型目录。
- Python 3.10+（仅在直接运行 `bridge.py` 时需要）。

镜像基于 `vllm/vllm-openai`，并安装 MinerU 3.x 与中文字体。模型不会在构建镜像时下载，而是在运行时挂载。

## 配置

### MinerU 配置文件

复制示例配置并按本地模型路径和实际服务修改：

```bash
cp mineru.example.json mineru.json
```

`mineru.json` 可能包含对象存储凭据或 DashScope API Key，因此已被 Git 忽略，禁止提交。项目当前的 DashScope 辅助标题配置默认关闭；它不是 DeepSeek 配置。

Compose 文件中的模型与配置挂载路径为部署机上的绝对路径。请修改为自己的路径后再启动。

### 本地 Qwen / vLLM

`vllm-start.sh` 用于启动 Qwen3.6-35B 的 OpenAI 兼容服务，默认使用端口 `8001`。模型目录和 Docker 网络名称需要按部署环境调整。

该项目调用的是本地 Qwen/vLLM 地址 `http://vllm-qwen36:8001/v1/chat/completions`，不调用 DeepSeek API。

## 启动 MinerU 服务

根据需要选择一个 Compose Profile：

```bash
# PDF 与图片解析 API
docker compose --profile api up -d

# Office 文档解析 Gradio 服务
docker compose --profile gradio up -d
```

| 服务 | 端口 | 用途 |
| --- | --- | --- |
| `mineru-api` | 8000 | PDF 与图片解析任务 API。 |
| `mineru-gradio` | 7860 | Office 文档转换服务。 |
| Qwen / vLLM | 8001 | 本地 OpenAI 兼容聊天接口。 |
| FastAPI bridge | 9999 | 平台页面、鉴权、解析任务与会话接口。 |

## 启动管理桥接层

`bridge.py` 是独立的 FastAPI 应用，运行它前应准备 `fastapi`、`uvicorn`、`httpx` 和 `PyJWT` 等 Python 依赖，并确保它可以访问 MinerU 与 vLLM 所在 Docker 网络。

```bash
uvicorn bridge:app --host 0.0.0.0 --port 9999
```

启动后访问 `http://localhost:9999/`。管理员入口为 `http://localhost:9999/admin`。

## API 概览

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/api/login` | POST | 登录并获取访问令牌。 |
| `/api/parse` | POST | 上传文件并创建解析任务。 |
| `/api/parse/{job_id}` | GET | 查询解析任务状态。 |
| `/api/parse/{job_id}/download` | GET | 下载 Markdown 结果。 |
| `/api/parse/{job_id}/download_zip` | GET | 下载完整结果压缩包。 |
| `/api/chat` | POST | 转发聊天请求至本地 Qwen/vLLM。 |
| `/api/conversations` | GET / POST | 查询或创建会话。 |

除登录外，平台接口需要携带 Bearer Token。

## 安全注意事项

- 项目不包含 DeepSeek API。
- `mineru.json` 中的外部服务密钥必须仅保留在部署机，不应提交或复制到镜像。
- 当前代码与启动脚本中存在硬编码的本地 vLLM 访问密钥。该值已经进入 Git 历史，不应继续使用；请立即轮换该密钥，并改为通过环境变量或密钥管理系统注入。
- 如仓库为公开仓库，轮换后还应重写包含旧密钥的 Git 历史，并强制推送清理后的分支。
- 生产环境应将 MinerU、vLLM 和 bridge 放在受限网络中，并为 bridge 配置 HTTPS、强密码和访问控制。

## 目录说明

```text
compose.yaml          MinerU API / Gradio 服务编排
Dockerfile            MinerU 与 vLLM 基础镜像定制
bridge.py             FastAPI 管理、解析任务和聊天转发
index.html            平台主页面
admin.html            管理员页面
vllm-start.sh         本地 Qwen vLLM 启动脚本
mineru.example.json   无敏感值的 MinerU 配置模板
```
