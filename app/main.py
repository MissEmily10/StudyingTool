"""Веб-приложение: уроки, бот для вебинаров, конспекты."""
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import markdown
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, db, pipeline, printing

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
security = HTTPBasic(auto_error=False)

AUDIO_EXT = {".mp3", ".m4a", ".wav", ".ogg", ".oga", ".opus", ".webm", ".mp4", ".mkv", ".mov", ".aac", ".flac"}


def require_login(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    if not config.APP_PASSWORD:
        return  # пароль не задан — режим разработки на своём компьютере
    ok = credentials is not None and secrets.compare_digest(
        credentials.username.encode(), config.APP_USER.encode()
    ) and secrets.compare_digest(credentials.password.encode(), config.APP_PASSWORD.encode())
    if not ok:
        raise HTTPException(401, "Нужен вход", headers={"WWW-Authenticate": 'Basic realm="StudyTool"'})


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    tasks = pipeline.start()
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="StudyTool", lifespan=lifespan, dependencies=[Depends(require_login)])
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


def lesson_or_404(lesson_id: int) -> dict:
    lesson = db.get(lesson_id)
    if not lesson:
        raise HTTPException(404, "Урок не найден")
    return lesson


def render_md(text: str) -> str:
    return markdown.markdown(printing.prepare(text), extensions=["tables", "fenced_code", "sane_lists"])


def fmt_dt(value: str | None) -> str:
    if not value:
        return ""
    return datetime.fromisoformat(value).strftime("%d.%m %H:%M")


templates.env.filters["dt"] = fmt_dt
templates.env.globals["STATUSES"] = db.STATUSES


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "lessons": db.all_lessons(),
        "now": datetime.now().strftime("%Y-%m-%dT%H:%M"),
    })


@app.post("/lessons")
async def create_lesson(
    subject: str = Form(...),
    title: str = Form(""),
    teacher: str = Form(""),
    notes: str = Form(""),
    webinar_url: str = Form(""),
    materials_url: str = Form(""),
    start_mode: str = Form("now"),
    starts_at: str = Form(""),
    audio_file: UploadFile | None = File(None),
    materials_file: UploadFile | None = File(None),
):
    webinar_url, materials_url = webinar_url.strip(), materials_url.strip()
    has_audio = audio_file is not None and audio_file.filename
    has_materials = (materials_file is not None and materials_file.filename) or materials_url
    if not (has_audio or webinar_url or has_materials):
        raise HTTPException(400, "Нужна ссылка на вебинар, файл записи или материалы урока")

    start = None
    if webinar_url and not has_audio:
        start = starts_at if start_mode == "at" and starts_at else datetime.now().isoformat(timespec="seconds")
        if len(start) == 16:
            start += ":00"

    lesson_id = db.create(
        subject=subject.strip(), title=title.strip(), teacher=teacher.strip(), notes=notes.strip(),
        webinar_url=webinar_url, materials_url=materials_url, starts_at=start,
        status="scheduled" if start else "queued",
    )
    folder = config.lesson_dir(lesson_id)
    if has_audio:
        ext = Path(audio_file.filename).suffix.lower()
        if ext not in AUDIO_EXT:
            ext = ".bin"
        with open(folder / f"upload{ext}", "wb") as f:
            while chunk := await audio_file.read(1 << 20):
                f.write(chunk)
        db.log(lesson_id, f"Загружен файл записи {audio_file.filename}")
    if materials_file is not None and materials_file.filename:
        (folder / "materials.pdf").write_bytes(await materials_file.read())
        db.log(lesson_id, f"Загружены материалы {materials_file.filename}")

    if start:
        db.log(lesson_id, f"Запись вебинара запланирована на {fmt_dt(start)}")
    else:
        pipeline.enqueue(lesson_id)
    return RedirectResponse(f"/lessons/{lesson_id}", status_code=303)


@app.get("/lessons/{lesson_id}", response_class=HTMLResponse)
def lesson_page(request: Request, lesson_id: int):
    lesson = lesson_or_404(lesson_id)
    return templates.TemplateResponse(request, "lesson.html", {
        "l": lesson,
        "konspekt_html": render_md(lesson["konspekt"]) if lesson["konspekt"] else "",
        "has_shot": (config.lesson_dir(lesson_id) / "bot.png").exists(),
        "has_audio": pipeline.audio_path(lesson_id).exists() or bool(pipeline.uploaded_audio(lesson_id)),
    })


@app.get("/lessons/{lesson_id}/status")
def lesson_status(lesson_id: int):
    lesson = lesson_or_404(lesson_id)
    return JSONResponse({"status": lesson["status"], "label": db.STATUSES.get(lesson["status"], lesson["status"]),
                         "log": lesson["log"], "error": lesson["error"], "updated_at": lesson["updated_at"]})


@app.get("/lessons/{lesson_id}/bot.png")
def bot_screenshot(lesson_id: int):
    path = config.lesson_dir(lesson_id) / "bot.png"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "no-store"})


@app.get("/lessons/{lesson_id}/audio")
def lesson_audio(lesson_id: int):
    path = pipeline.uploaded_audio(lesson_id) or pipeline.audio_path(lesson_id)
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, filename=f"urok_{lesson_id}{path.suffix}")


@app.post("/lessons/{lesson_id}/stop")
def stop_recording(lesson_id: int):
    lesson = lesson_or_404(lesson_id)
    if lesson["status"] == "scheduled":
        db.update(lesson_id, status="error", error="Запись отменена")
    else:
        db.update(lesson_id, stop_requested=1)
        db.log(lesson_id, "Запрошена остановка записи")
    return RedirectResponse(f"/lessons/{lesson_id}", status_code=303)


@app.post("/lessons/{lesson_id}/retry")
async def retry(lesson_id: int, redo_transcript: str = Form("")):
    lesson_or_404(lesson_id)
    if redo_transcript:
        db.update(lesson_id, transcript="")
    db.log(lesson_id, "Перезапуск обработки")
    pipeline.enqueue(lesson_id)
    return RedirectResponse(f"/lessons/{lesson_id}", status_code=303)


@app.post("/lessons/{lesson_id}/edit")
def edit_konspekt(lesson_id: int, konspekt: str = Form(...)):
    lesson_or_404(lesson_id)
    db.update(lesson_id, konspekt=konspekt.replace("\r\n", "\n"))
    return RedirectResponse(f"/lessons/{lesson_id}", status_code=303)


@app.post("/lessons/{lesson_id}/delete")
def delete_lesson(lesson_id: int):
    lesson = lesson_or_404(lesson_id)
    if lesson["status"] in ("joining", "recording"):
        raise HTTPException(400, "Сначала остановите запись")
    db.delete(lesson_id)
    return RedirectResponse("/", status_code=303)


def _filename(lesson: dict, ext: str) -> str:
    name = f"{lesson['subject']} {lesson['title']}".strip().replace("/", "-")
    return f"{name[:80] or 'konspekt'}{ext}"


@app.get("/lessons/{lesson_id}/konspekt.md")
def download_md(lesson_id: int):
    lesson = lesson_or_404(lesson_id)
    if not lesson["konspekt"]:
        raise HTTPException(404, "Конспекта ещё нет")
    return Response(lesson["konspekt"], media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": _content_disposition(_filename(lesson, ".md"))})


@app.get("/lessons/{lesson_id}/print.pdf")
async def download_pdf(lesson_id: int):
    lesson = lesson_or_404(lesson_id)
    if not lesson["konspekt"]:
        raise HTTPException(404, "Конспекта ещё нет")
    out = config.lesson_dir(lesson_id) / "print_A4x2.pdf"
    await printing.build_print_pdf(lesson["konspekt"], out)
    return FileResponse(out, media_type="application/pdf",
                        headers={"Content-Disposition": _content_disposition(_filename(lesson, ".pdf"))})


@app.get("/lessons/{lesson_id}/transcript.txt")
def download_transcript(lesson_id: int):
    lesson = lesson_or_404(lesson_id)
    return Response(lesson["transcript"] or "", media_type="text/plain; charset=utf-8")


def _content_disposition(filename: str) -> str:
    from urllib.parse import quote
    return f"attachment; filename=\"konspekt{Path(filename).suffix}\"; filename*=UTF-8''{quote(filename)}"
