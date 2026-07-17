"""
MinerU + Qwen 一体化文档分析平台
功能: 登录 / MinerU解析 / Qwen对话 / 多轮历史 / 用户管理
"""
import asyncio
import io
import json
import os
import re
import sqlite3
import time
import uuid
import zipfile
import hashlib
from datetime import datetime, timedelta
from typing import Optional

import httpx
import jwt
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, Form, Depends
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse, FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from concurrent.futures import ThreadPoolExecutor

app = FastAPI(title="MinerU Platform")
security = HTTPBearer()

MINERU_URL = "http://mineru-api:8000"
GRADIO_URL = "http://mineru-gradio:7860"
QWEN_URL = "http://vllm-qwen36:8001/v1/chat/completions"
QWEN_KEY = "data5406"
JWT_SECRET = "mineru-platform-secret-key-change-in-production"
DB_PATH = "/app/data.db"

jobs: dict[str, dict] = {}
gradio_client: object | None = None
executor = ThreadPoolExecutor(max_workers=3)

# ─── Database ───

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id),
            title TEXT NOT NULL DEFAULT '新对话',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
            content TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
    """)
    conn.commit()
    conn.close()

# Create DB on startup
os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else ".", exist_ok=True)
init_db()

# ─── Auth ───

def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def create_token(user_id: str, username: str) -> str:
    return jwt.encode({
        "user_id": user_id,
        "username": username,
        "exp": datetime.utcnow() + timedelta(days=7)
    }, JWT_SECRET, algorithm="HS256")

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
        return {"user_id": payload["user_id"], "username": payload["username"]}
    except Exception:
        raise HTTPException(401, "登录已过期，请重新登录")

# ─── API: Auth ───

@app.post("/api/login")
async def login(body: dict):
    username = body.get("username", "").strip()
    password = body.get("password", "")
    if not username or not password:
        raise HTTPException(400, "请输入用户名和密码")
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    if not user or user["password_hash"] != hash_password(password):
        raise HTTPException(401, "用户名或密码错误")
    token = create_token(user["id"], user["username"])
    return {"token": token, "user_id": user["id"], "username": user["username"], "display_name": user["display_name"] or user["username"]}

@app.get("/api/me")
async def me(user: dict = Depends(get_current_user)):
    conn = get_db()
    u = conn.execute("SELECT id, username, display_name, created_at FROM users WHERE id=?", (user["user_id"],)).fetchone()
    conn.close()
    if not u:
        raise HTTPException(404)
    return {"user_id": u["id"], "username": u["username"], "display_name": u["display_name"] or u["username"]}

# ─── API: Admin ───

def _require_admin(user: dict):
    if user["username"] != "admin":
        raise HTTPException(403, "仅管理员可操作")

@app.get("/api/admin/users")
async def admin_list_users(user: dict = Depends(get_current_user)):
    _require_admin(user)
    conn = get_db()
    rows = conn.execute("SELECT id, username, display_name, created_at FROM users ORDER BY created_at DESC").fetchall()
    conn.close()
    return [{"id": r["id"], "username": r["username"], "display_name": r["display_name"], "created_at": r["created_at"]} for r in rows]

@app.post("/api/admin/users")
async def admin_create_user(body: dict, user: dict = Depends(get_current_user)):
    _require_admin(user)
    username = body.get("username", "").strip()
    password = body.get("password", "")
    display_name = body.get("display_name", "").strip() or username
    if not username or not password:
        raise HTTPException(400, "用户名和密码不能为空")
    conn = get_db()
    existing = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(400, "用户名已存在")
    uid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO users (id, username, password_hash, display_name, created_at) VALUES (?,?,?,?,?)",
        (uid, username, hash_password(password), display_name, int(time.time()))
    )
    conn.commit()
    conn.close()
    return {"ok": True, "id": uid}

@app.put("/api/admin/users/{uid}/pwd")
async def admin_reset_pwd(uid: str, body: dict, user: dict = Depends(get_current_user)):
    _require_admin(user)
    password = body.get("password", "")
    if not password:
        raise HTTPException(400, "密码不能为空")
    conn = get_db()
    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), uid))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.delete("/api/admin/users/{uid}")
async def admin_delete_user(uid: str, user: dict = Depends(get_current_user)):
    _require_admin(user)
    conn = get_db()
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    return {"ok": True}

# ─── API: MinerU (mineru-api for PDF/images, Gradio for Office) ───

_OFFICE_EXTS = {'.docx', '.pptx', '.xlsx', '.doc', '.ppt', '.xls'}


def _get_gradio_client():
    global gradio_client
    if gradio_client is None:
        from gradio_client import Client
        gradio_client = Client(GRADIO_URL)
    return gradio_client


def _run_gradio_parse(file_path: str, end_pages: int = 100) -> tuple:
    from gradio_client import handle_file
    client = _get_gradio_client()
    return client.predict(
        handle_file(file_path), end_pages,
        False, True, True, False,
        "ch (Chinese, English, Chinese Traditional)",
        "pipeline",
        "http://mineru-api:8000",
        api_name="/convert_to_markdown_stream",
    )


async def _parse_via_mineru_api(file_content: bytes, filename: str) -> dict:
    """Parse via mineru-api directly (PDF/images). Returns job dict."""
    safe_name = filename.encode("ascii", errors="replace").decode("ascii")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{MINERU_URL}/tasks",
            files={"files": (safe_name, file_content)},
            data={"return_md": "true", "response_format_zip": "true", "return_images": "true"},
        )
        if resp.status_code not in (200, 202):
            raise HTTPException(resp.status_code, detail=resp.text)
    return resp.json()


def _background_gradio_parse(job_id: str, file_path: str):
    try:
        status_text, result_zip_path, md_content, _, _ = _run_gradio_parse(file_path)
        job = jobs.get(job_id)
        if job:
            job["status"] = "done"
            job["result_zip_path"] = result_zip_path
            job["markdown"] = md_content
    except Exception as e:
        job = jobs.get(job_id)
        if job:
            job["status"] = "failed"
            job["error"] = str(e)
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)


@app.post("/api/parse")
async def parse(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    content = await file.read()
    job_id = str(uuid.uuid4())[:8]
    ext = os.path.splitext(file.filename or "")[1].lower()

    if ext in _OFFICE_EXTS:
        # Office docs → Gradio (has LibreOffice)
        tmpdir = "/tmp/mineru_jobs"
        os.makedirs(tmpdir, exist_ok=True)
        tmp_path = os.path.join(tmpdir, f"{job_id}_{file.filename}")
        with open(tmp_path, "wb") as f:
            f.write(content)
        jobs[job_id] = {"filename": file.filename, "status": "processing"}
        loop = asyncio.get_event_loop()
        loop.run_in_executor(executor, _background_gradio_parse, job_id, tmp_path)
    else:
        # PDF/images → mineru-api directly (faster, no GPU needed)
        data = await _parse_via_mineru_api(content, file.filename)
        jobs[job_id] = {"task_id": data["task_id"], "filename": file.filename, "status": "processing"}

    return {"job_id": job_id}


@app.get("/api/parse/{job_id}")
async def parse_status(job_id: str, user: dict = Depends(get_current_user)):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    if job["status"] in ("done", "failed"):
        return job
    if "task_id" in job:
        # Poll mineru-api
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{MINERU_URL}/tasks/{job['task_id']}")
            data = resp.json()
        st = data.get("status")
        if st == "completed":
            job["status"] = "done"
        elif st == "failed":
            job["status"] = "failed"
            job["error"] = data.get("error", "")
    return {"status": job["status"], "filename": job.get("filename"), "error": job.get("error")}


@app.get("/api/parse/{job_id}/download")
async def parse_download(job_id: str, user: dict = Depends(get_current_user)):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    if job["status"] != "done":
        raise HTTPException(425, "Still processing")

    if "markdown" in job:
        return PlainTextResponse(
            job["markdown"][:500000], media_type="text/markdown",
            headers={"Content-Disposition": f"attachment; filename={job['filename']}.md"},
        )

    # Fetch from mineru-api
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{MINERU_URL}/tasks/{job['task_id']}/result")
        if resp.status_code in (404, 425):
            raise HTTPException(resp.status_code)
        resp.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(resp.content))
    md_files = sorted(n for n in z.namelist() if n.endswith(".md"))
    result = f"# {job['filename']}\n\n"
    for name in md_files:
        result += z.read(name).decode("utf-8", errors="replace") + "\n\n---\n\n"
    return PlainTextResponse(
        result[:500000], media_type="text/markdown",
        headers={"Content-Disposition": f"attachment; filename={job['filename']}.md"},
    )


@app.get("/api/parse/{job_id}/download_zip")
async def parse_download_zip(job_id: str, user: dict = Depends(get_current_user)):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    if job["status"] != "done":
        raise HTTPException(425, "Still processing")

    if "result_zip_path" in job and os.path.exists(job["result_zip_path"]):
        with open(job["result_zip_path"], "rb") as f:
            return Response(content=f.read(), media_type="application/zip",
                            headers={"Content-Disposition": "attachment; filename=result.zip"})

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{MINERU_URL}/tasks/{job['task_id']}/result")
        if resp.status_code in (404, 425):
            raise HTTPException(resp.status_code)
        resp.raise_for_status()
    return Response(content=resp.content, media_type="application/zip",
                    headers={"Content-Disposition": "attachment; filename=result.zip"})
    with open(zip_path, "rb") as f:
        content = f.read()
    return Response(content=content, media_type="application/zip",
                    headers={"Content-Disposition": "attachment; filename=result.zip"})

# ─── API: Chat ───

@app.post("/api/conversations/{conv_id}/inject_doc")
async def inject_doc(conv_id: str, body: dict, user: dict = Depends(get_current_user)):
    """Inject parsed document content as a system message into the conversation."""
    content = body.get("content", "")
    if not content:
        raise HTTPException(400, "content required")
    conn = get_db()
    conv = conn.execute(
        "SELECT id FROM conversations WHERE id=? AND user_id=?", (conv_id, user["user_id"])
    ).fetchone()
    if not conv:
        conn.close()
        raise HTTPException(404)
    now = int(time.time())
    conn.execute(
        "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?,?,?,?)",
        (conv_id, "user", f"以下是要分析的文档内容：\n\n{content}", now)
    )
    conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/chat")
async def chat(body: dict, user: dict = Depends(get_current_user)):
    messages = body.get("messages", [])
    conversation_id = body.get("conversation_id")
    if not messages:
        raise HTTPException(400)

    # Save user message
    conn = get_db()
    now = int(time.time())
    user_msg = messages[-1]
    if user_msg["role"] == "user" and conversation_id:
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?,?,?,?)",
            (conversation_id, "user", user_msg["content"], now)
        )
        # Auto-title from first message
        conv = conn.execute("SELECT title FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if conv and conv["title"] == "新对话":
            title = user_msg["content"][:40].replace("\n", " ")
            conn.execute("UPDATE conversations SET title=? WHERE id=?", (title, conversation_id))
        conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))
    conn.commit()

    # Stream response from Qwen
    async def stream():
        full_content = ""
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST", QWEN_URL,
                json={"model": "qwen3.6-35b", "messages": messages, "stream": True, "max_tokens": 4096, "enable_thinking": False},
                headers={"Authorization": f"Bearer {QWEN_KEY}"},
            ) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            d = json.loads(line[6:])
                            delta = d["choices"][0].get("delta", {}).get("content", "")
                            full_content += delta
                        except Exception:
                            pass
                    if line.startswith("data: "):
                        yield line + "\n\n"
        # Save assistant response
        if conversation_id and full_content:
            c = get_db()
            c.execute(
                "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?,?,?,?)",
                (conversation_id, "assistant", full_content, int(time.time()))
            )
            c.execute("UPDATE conversations SET updated_at=? WHERE id=?", (int(time.time()), conversation_id))
            c.commit()
            c.close()

    return StreamingResponse(stream(), media_type="text/event-stream")

# ─── API: Conversations ───

@app.get("/api/conversations")
async def list_conversations(user: dict = Depends(get_current_user)):
    conn = get_db()
    convs = conn.execute(
        "SELECT id, title, created_at, updated_at FROM conversations WHERE user_id=? ORDER BY updated_at DESC",
        (user["user_id"],)
    ).fetchall()
    conn.close()
    return [{"id": c["id"], "title": c["title"], "created_at": c["created_at"], "updated_at": c["updated_at"]} for c in convs]

@app.post("/api/conversations")
async def create_conversation(user: dict = Depends(get_current_user)):
    now = int(time.time())
    cid = str(uuid.uuid4())
    conn = get_db()
    conn.execute(
        "INSERT INTO conversations (id, user_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
        (cid, user["user_id"], "新对话", now, now)
    )
    conn.commit()
    conn.close()
    return {"id": cid, "title": "新对话", "created_at": now, "updated_at": now}

@app.get("/api/conversations/{conv_id}")
async def get_conversation(conv_id: str, user: dict = Depends(get_current_user)):
    conn = get_db()
    conv = conn.execute(
        "SELECT * FROM conversations WHERE id=? AND user_id=?", (conv_id, user["user_id"])
    ).fetchone()
    if not conv:
        conn.close()
        raise HTTPException(404)
    msgs = conn.execute(
        "SELECT role, content, created_at FROM messages WHERE conversation_id=? ORDER BY id ASC", (conv_id,)
    ).fetchall()
    conn.close()
    return {
        "id": conv["id"], "title": conv["title"],
        "messages": [{"role": m["role"], "content": m["content"]} for m in msgs]
    }

@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: str, user: dict = Depends(get_current_user)):
    conn = get_db()
    conn.execute("DELETE FROM conversations WHERE id=? AND user_id=?", (conv_id, user["user_id"]))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.put("/api/conversations/{conv_id}")
async def rename_conversation(conv_id: str, body: dict, user: dict = Depends(get_current_user)):
    title = body.get("title", "").strip()
    if not title:
        raise HTTPException(400, "title required")
    conn = get_db()
    conn.execute("UPDATE conversations SET title=?, updated_at=? WHERE id=? AND user_id=?",
                 (title[:200], int(time.time()), conv_id, user["user_id"]))
    conn.commit()
    conn.close()
    return {"ok": True}


# ─── Frontend ───

@app.get("/marked.min.js")
async def marked_js():
    _here = os.path.dirname(os.path.abspath(__file__))
    return FileResponse(os.path.join(_here, "marked.min.js"),
                        media_type="application/javascript",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/admin")
@app.get("/admin.html")
async def admin_page():
    _here = os.path.dirname(os.path.abspath(__file__))
    fp = os.path.join(_here, "admin.html")
    if not os.path.exists(fp):
        raise HTTPException(404)
    return HTMLResponse(open(fp).read())


@app.get("/", response_class=HTMLResponse)
async def home():
    _here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(_here, "index.html"), "r") as f:
        html = f.read()
    return Response(content=html, media_type="text/html", headers={"Cache-Control": "no-store"})


# ─── CLI: User management ───

if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2:
        cmd = sys.argv[1]
        conn = sqlite3.connect(DB_PATH)
        if cmd == "adduser" and len(sys.argv) >= 4:
            uid = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO users (id, username, password_hash, display_name, created_at) VALUES (?,?,?,?,?)",
                (uid, sys.argv[2], hash_password(sys.argv[3]), sys.argv[2], int(time.time()))
            )
            conn.commit()
            print(f"User created: {sys.argv[2]}")
        elif cmd == "listusers":
            users = conn.execute("SELECT username, display_name, created_at FROM users").fetchall()
            for u in users:
                print(f"  {u[0]} ({u[1]}) created {datetime.fromtimestamp(u[2])}")
        conn.close()
    else:
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=9999)
