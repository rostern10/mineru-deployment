"""
MinerU 文档解析工具
用途：在 Open WebUI 对话中直接解析 PDF/DOCX/XLSX/PPTX，输出完整 Markdown
"""

import httpx
import tempfile
import os

async def parse_document(file_path: str) -> str:
    """
    调用 MinerU API 解析文档，返回 Markdown 文本
    
    参数:
        file_path: 上传文件的本地路径（Open WebUI 自动传入）
    
    返回:
        解析后的 Markdown 字符串
    """
    api_url = "http://mineru-api:8000/file_parse"
    
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            with open(file_path, "rb") as f:
                files = {"files": (os.path.basename(file_path), f)}
                data = {"return_md": "true"}
                response = await client.post(api_url, files=files, data=data)
                response.raise_for_status()
                
                # 返回的是 zip，解压提取 md 文件
                content_type = response.headers.get("content-type", "")
                
                if "application/zip" in content_type:
                    import zipfile, io
                    z = zipfile.ZipFile(io.BytesIO(response.content))
                    md_files = [n for n in z.namelist() if n.endswith('.md')]
                    if md_files:
                        result = ""
                        for md_file in md_files:
                            result += f"\n## {md_file}\n\n"
                            result += z.read(md_file).decode('utf-8', errors='replace')
                        return result
                
                return response.text[:100000]  # 限 10 万字符
    
    except Exception as e:
        return f"解析失败：{str(e)}"
