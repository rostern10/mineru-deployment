"""
Open WebUI Action: 上传文件自动注入全文
支持 .md / .txt / .pdf (通过 mineru 解析后) 的完整内容注入
"""
from open_webui.models.files import Files
import re
import requests


async def action(body: dict, __user__: dict = None, __event_emitter__=None) -> dict:
    messages = body.get("messages", [])
    if not messages:
        return body

    last_msg = messages[-1]
    if last_msg.get("role") != "user":
        return body

    content = last_msg.get("content", "")
    if isinstance(content, list):
        content = " ".join(
            c.get("text", "") if isinstance(c, dict) else str(c) for c in content
        )

    if "<attached_files>" not in content and "<file " not in content:
        return body

    # 提取文件 URL，从中获取 file_id
    file_urls = re.findall(r'<file\s+[^>]*?url="([^"]*)"[^>]*/?>', content)
    if not file_urls:
        return body

    full_content_parts = []
    for url in file_urls:
        # URL 格式: http://.../api/v1/files/{file_id}/content
        match = re.search(r'/files/([a-f0-9-]+)/content', url)
        if not match:
            continue
        file_id = match.group(1)
        try:
            file = await Files.get_file_by_id(file_id)
            if not file:
                continue
            # 尝试从 data.content 获取
            text = file.data.get("content", "") if file.data else ""
            if not text:
                # 从磁盘读取
                import os
                from open_webui.storage.provider import Storage
                try:
                    path = Storage.get_file(file.path) if file.path else None
                    if path and os.path.exists(path):
                        with open(path, "r", errors="ignore") as f:
                            text = f.read(500000)
                except Exception:
                    pass
            if text.strip():
                full_content_parts.append(
                    f"## 📄 {file.filename}\n\n{text.strip()}"
                )
        except Exception:
            pass

    if not full_content_parts:
        return body

    # 替换 XML 标签为全文
    full_text = "\n\n---\n\n".join(full_content_parts)
    content = re.sub(
        r"<attached_files>.*?</attached_files>", "", content, flags=re.DOTALL
    )
    last_msg["content"] = full_text + "\n\n" + content.strip()
    messages[-1] = last_msg
    body["messages"] = messages
    return body
