"""FAQ 知识库、SQLite 存储及后台安全认证路由。"""
import base64
import binascii
import hashlib
import hmac
import html
import os
import random
import re
import secrets
import sqlite3
import struct
import time
from contextlib import contextmanager
from urllib.parse import quote
from pathlib import Path
from typing import Final, Iterator
from uuid import uuid4
from xml.etree import ElementTree
from zipfile import ZipFile

from aiohttp.web import FileResponse, HTTPBadRequest, HTTPFound, HTTPNotFound, Request, Response, json_response
from jinja2 import Environment, FileSystemLoader

DB_PATH: Final[Path] = Path(__file__).parent / "data" / "faq.db"
FAQ_IMAGE_DIR: Final[Path] = Path(__file__).parent / "data" / "images"
SESSION_COOKIE: Final[str] = "faq_admin_session"
CAPTCHA_COOKIE: Final[str] = "faq_login_captcha"
CSRF_FIELD: Final[str] = "csrf_token"
SESSION_TTL: Final[int] = 8 * 60 * 60
CAPTCHA_TTL: Final[int] = 60
TOTP_PERIOD: Final[int] = 30
TOTP_DIGITS: Final[int] = 6
ADMIN_MFA_PENDING_TTL: Final[int] = 300
LOG_RETENTION_DAYS: Final[int] = int(os.environ.get("FAQ_LOG_RETENTION_DAYS", "30").strip() or "30")
MAX_TITLE: Final[int] = 160
MAX_CATEGORY: Final[int] = 60
MAX_SUMMARY: Final[int] = 400
MAX_CONTENT: Final[int] = 50_000
MIN_ADMIN_PASSWORD: Final[int] = 8
_PROCESS_SECRET: Final[bytes] = secrets.token_bytes(32)
_SESSIONS: dict[str, dict[str, object]] = {}

# 初始化 Jinja2 模板环境
_TEMPLATE_ENV: Final[Environment] = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=True,
    trim_blocks=True,
    lstrip_blocks=True
)


def _env(name: str, default: str = "") -> str:
    """读取去除首尾空格后的环境变量。"""
    return os.environ.get(name, default).strip()


def _secret() -> bytes:
    """使用配置密钥签名 Cookie；未配置时仅限本进程有效。"""
    return _env("FAQ_SESSION_SECRET").encode("utf-8") or _PROCESS_SECRET


def _sign(value: str) -> str:
    digest = hmac.new(_secret(), value.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _equal(left: str, right: str) -> bool:
    """使用恒定时间比较认证信息。"""
    return hmac.compare_digest(left.encode(), right.encode())


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """获取启用 WAL 与外键保护的 SQLite 连接。"""
    connection = sqlite3.connect(DB_PATH, timeout=5)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database() -> None:
    """创建 FAQ、管理员账号与前端提问日志表。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS faqs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', content TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_published INTEGER NOT NULL DEFAULT 0 CHECK(is_published IN (0,1)),
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_faq_public_order ON faqs(is_published, sort_order, id)")
        conn.execute("""CREATE TABLE IF NOT EXISTS admin_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
            mfa_enabled INTEGER NOT NULL DEFAULT 0 CHECK(mfa_enabled IN (0,1)),
            mfa_secret TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            last_login INTEGER NOT NULL DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS webchat_question_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at INTEGER NOT NULL,
            ip_address TEXT NOT NULL DEFAULT '',
            login_account TEXT NOT NULL DEFAULT '',
            os_version TEXT NOT NULL DEFAULT '',
            browser_version TEXT NOT NULL DEFAULT '',
            question_content TEXT NOT NULL DEFAULT ''
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_webchat_question_logs_created_at ON webchat_question_logs(created_at DESC, id DESC)")
        conn.execute("""CREATE TABLE IF NOT EXISTS webchat_access_grants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            object_id TEXT NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            display_name TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','denied','revoked')),
            approved_by_admin_id INTEGER,
            approved_at INTEGER NOT NULL DEFAULT 0,
            denial_reason TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            UNIQUE(tenant_id, object_id)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_webchat_access_grants_status ON webchat_access_grants(status, updated_at DESC)")

        # 迁移：旧数据库添加 mfa_secret 列。
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(admin_accounts)").fetchall()}
        if "mfa_secret" not in existing_cols:
            conn.execute("ALTER TABLE admin_accounts ADD COLUMN mfa_secret TEXT")

        log_columns = {row[1] for row in conn.execute("PRAGMA table_info(webchat_question_logs)").fetchall()}
        if "login_account" not in log_columns:
            conn.execute("ALTER TABLE webchat_question_logs ADD COLUMN login_account TEXT NOT NULL DEFAULT ''")

        access_grant_columns = {row[1] for row in conn.execute("PRAGMA table_info(webchat_access_grants)").fetchall()}
        if "denial_reason" not in access_grant_columns:
            conn.execute("ALTER TABLE webchat_access_grants ADD COLUMN denial_reason TEXT NOT NULL DEFAULT ''")

        count_row = conn.execute("SELECT COUNT(*) AS c FROM admin_accounts").fetchone()
        if int(count_row["c"]) == 0:
            username = _env("FAQ_ADMIN_USERNAME", "admin")
            password = _env("FAQ_ADMIN_PASSWORD")
            if password:
                now = int(time.time())
                conn.execute(
                    "INSERT INTO admin_accounts(username,display_name,password_hash,is_active,mfa_enabled,created_at,updated_at,last_login) VALUES(?,?,?,?,?,?,?,?)",
                    (username, username, _hash_password(password), 1, 0, now, now, 0),
                )


def _hash_password(password: str) -> str:
    """使用 PBKDF2 生成可持久化密码哈希。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return "pbkdf2_sha256$120000$" + base64.urlsafe_b64encode(salt).decode("ascii") + "$" + base64.urlsafe_b64encode(digest).decode("ascii")


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds_text, salt_b64, digest_b64 = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        rounds = int(rounds_text)
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
    except (ValueError, TypeError, binascii.Error):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(actual, expected)


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _safe_content_url(value: str) -> str:
    if value.startswith("/faq/images/"):
        return value
    return ""


def _render_markdown_inline(value: str) -> str:
    def image_replacement(match: re.Match[str]) -> str:
        alt, url = match.group(1), _safe_content_url(html.unescape(match.group(2)))
        return f'<img src="{_e(url)}" alt="{alt}">' if url else alt

    def link_replacement(match: re.Match[str]) -> str:
        text, url = match.group(1), _safe_content_url(html.unescape(match.group(2)))
        return f'<a href="{_e(url)}" target="_blank" rel="noopener noreferrer">{text}</a>' if url else text

    value = re.sub(r"!\[([^\]]*)\]\(([^\s)]+)\)", image_replacement, value)
    value = re.sub(r"(?<!!)\[([^\]]+)\]\(([^\s)]+)\)", link_replacement, value)
    value = re.sub(r"`([^`]+)`", r"<code>\1</code>", value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", value)
    value = re.sub(r"~~([^~]+)~~", r"<s>\1</s>", value)
    value = re.sub(r"\+\+([^+]+)\+\+", r"<u>\1</u>", value)
    value = re.sub(r"==([^=]+)==", r"<mark>\1</mark>", value)
    value = re.sub(
        r"\{\{color:(#[0-9a-fA-F]{6})\|(.+?)\}\}",
        r'<span style="color:\1">\2</span>',
        value,
    )
    value = re.sub(
        r"\{\{style:(#[0-9a-fA-F]{6}):?(#[0-9a-fA-F]{6})?\|(.+?)\}\}",
        lambda match: f'<span style="color:{match.group(1)}{f";background-color:{match.group(2)}" if match.group(2) else ""}">{match.group(3)}</span>',
        value,
    )
    return re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", value)


def _heading_label(value: str) -> str:
    label = re.sub(r"[`*_~+=]+", "", html.unescape(value)).strip()
    return label or "未命名章节"


def _render_markdown(content: str) -> tuple[str, list[tuple[int, str, str]]]:
    escaped = _e(content)
    blocks: list[str] = []
    headings: list[tuple[int, str, str]] = []
    heading_count = 0
    parts = re.split(r"(^```[\s\S]*?^```$)", escaped, flags=re.MULTILINE)
    for part in parts:
        if not part:
            continue
        if part.startswith("```") and part.endswith("```"):
            blocks.append(f"<pre><code>{part[3:-3].strip()}</code></pre>")
            continue
        lines = part.splitlines()
        list_type = ""
        list_items: list[str] = []

        def flush_list() -> None:
            nonlocal list_type, list_items
            if list_items:
                blocks.append(f"<{list_type}>{''.join(list_items)}</{list_type}>")
            list_type, list_items = "", []

        paragraph: list[str] = []

        def flush_paragraph() -> None:
            nonlocal paragraph
            if paragraph:
                blocks.append(f"<p>{'<br>'.join(_render_markdown_inline(line) for line in paragraph)}</p>")
            paragraph = []

        def is_table_separator(value: str) -> bool:
            cells = [cell.strip() for cell in value.strip().strip("|").split("|")]
            return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)

        def table_cells(value: str) -> list[str]:
            return [cell.strip() for cell in value.strip().strip("|").split("|")]

        line_index = 0
        while line_index < len(lines):
            line = lines[line_index]
            heading = re.match(r"^(#{1,3})\s+(.+)$", line)
            bullet = re.match(r"^-\s+(.+)$", line)
            ordered = re.match(r"^\d+\.\s+(.+)$", line)
            alignment = re.fullmatch(r":::(?:align:)?(left|center|right)", line)
            if alignment:
                flush_paragraph()
                flush_list()
                aligned_lines: list[str] = []
                line_index += 1
                while line_index < len(lines) and lines[line_index] != ":::":
                    aligned_lines.append(lines[line_index])
                    line_index += 1
                blocks.append(f'<div class="faq-text-align-{alignment.group(1)}">{"<br>".join(_render_markdown_inline(item) for item in aligned_lines)}</div>')
            elif line_index + 1 < len(lines) and "|" in line and is_table_separator(lines[line_index + 1]):
                flush_paragraph()
                flush_list()
                header_cells = table_cells(line)
                rows: list[list[str]] = []
                line_index += 2
                while line_index < len(lines) and "|" in lines[line_index] and lines[line_index].strip():
                    rows.append(table_cells(lines[line_index]))
                    line_index += 1
                header_html = "".join(f"<th>{_render_markdown_inline(cell)}</th>" for cell in header_cells)
                row_html = "".join(
                    f"<tr>{''.join(f'<td>{_render_markdown_inline(cell)}</td>' for cell in row)}</tr>"
                    for row in rows
                )
                blocks.append(f'<div class="faq-table-wrap"><table><thead><tr>{header_html}</tr></thead><tbody>{row_html}</tbody></table></div>')
                line_index -= 1
            elif heading:
                flush_paragraph()
                flush_list()
                level = len(heading.group(1))
                heading_count += 1
                heading_id = f"faq-heading-{heading_count}"
                headings.append((level, _heading_label(heading.group(2)), heading_id))
                blocks.append(f'<h{level} id="{heading_id}">{_render_markdown_inline(heading.group(2))}</h{level}>')
            elif line.startswith("&gt; "):
                flush_paragraph()
                flush_list()
                blocks.append(f"<blockquote>{_render_markdown_inline(line[5:])}</blockquote>")
            elif bullet or ordered:
                flush_paragraph()
                current_type = "ul" if bullet else "ol"
                if list_type and list_type != current_type:
                    flush_list()
                list_type = current_type
                list_items.append(f"<li>{_render_markdown_inline((bullet or ordered).group(1))}</li>")
            elif not line.strip():
                flush_paragraph()
                flush_list()
            else:
                flush_list()
                paragraph.append(line)
            line_index += 1
        flush_paragraph()
        flush_list()
    return "".join(blocks), headings


def _docx_run_markdown(run: ElementTree.Element, namespaces: dict[str, str]) -> str:
    text_parts: list[str] = []
    for node in run:
        if node.tag.endswith("}t"):
            text_parts.append(node.text or "")
        elif node.tag.endswith("}tab"):
            text_parts.append("\t")
        elif node.tag.endswith("}br") or node.tag.endswith("}cr"):
            text_parts.append("  \n")
    text = "".join(text_parts)
    if not text:
        return ""
    properties = run.find("w:rPr", namespaces)
    if properties is not None:
        if properties.find("w:b", namespaces) is not None:
            text = f"**{text}**"
        if properties.find("w:i", namespaces) is not None:
            text = f"*{text}*"
        if properties.find("w:u", namespaces) is not None:
            text = f"++{text}++"
        if properties.find("w:strike", namespaces) is not None:
            text = f"~~{text}~~"
        color = properties.find("w:color", namespaces)
        shading = properties.find("w:shd", namespaces)
        color_value = color.attrib.get(f"{{{namespaces['w']}}}val", "") if color is not None else ""
        shading_value = shading.attrib.get(f"{{{namespaces['w']}}}fill", "") if shading is not None else ""
        normalized_color = f"#{color_value}" if re.fullmatch(r"[0-9a-fA-F]{6}", color_value) else "#0f172a"
        normalized_shading = f"#{shading_value}" if re.fullmatch(r"[0-9a-fA-F]{6}", shading_value) else ""
        if normalized_color != "#0f172a" or normalized_shading:
            text = f"{{{{style:{normalized_color}{f':{normalized_shading}' if normalized_shading else ''}|{text}}}}}"
    return text


def _docx_to_markdown(filename: str, content: bytes) -> str:
    namespaces = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main", "a": "http://schemas.openxmlformats.org/drawingml/2006/main", "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    try:
        with ZipFile(__import__("io").BytesIO(content)) as archive:
            document = ElementTree.fromstring(archive.read("word/document.xml"))
            relations = ElementTree.fromstring(archive.read("word/_rels/document.xml.rels"))
            relation_targets = {item.attrib.get("Id", ""): item.attrib.get("Target", "") for item in relations}
            numbering = ElementTree.fromstring(archive.read("word/numbering.xml")) if "word/numbering.xml" in archive.namelist() else None
            numbering_formats: dict[tuple[str, str], str] = {}
            number_to_abstract: dict[str, str] = {}
            if numbering is not None:
                for item in numbering.findall("w:num", namespaces):
                    abstract_id = item.find("w:abstractNumId", namespaces)
                    if abstract_id is not None:
                        number_to_abstract[item.attrib.get(f"{{{namespaces['w']}}}numId", "")] = abstract_id.attrib.get(f"{{{namespaces['w']}}}val", "")
                for item in numbering.findall("w:abstractNum", namespaces):
                    abstract_id = item.attrib.get(f"{{{namespaces['w']}}}abstractNumId", "")
                    for level in item.findall("w:lvl", namespaces):
                        number_format = level.find("w:numFmt", namespaces)
                        if number_format is not None:
                            numbering_formats[(abstract_id, level.attrib.get(f"{{{namespaces['w']}}}ilvl", "0"))] = number_format.attrib.get(f"{{{namespaces['w']}}}val", "")
            image_urls: dict[str, str] = {}
            for relation_id, target in relation_targets.items():
                if not target.startswith("media/"):
                    continue
                image_path = f"word/{target}"
                if image_path not in archive.namelist():
                    continue
                suffix = Path(target).suffix.lower()
                if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
                    continue
                stored_name = f"{uuid4()}{suffix}"
                FAQ_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
                (FAQ_IMAGE_DIR / stored_name).write_bytes(archive.read(image_path))
                image_urls[relation_id] = f"/faq/images/{stored_name}"
            markdown: list[str] = []
            body = document.find("w:body", namespaces)
            if body is None:
                return ""
            for node in body:
                if node.tag.endswith("}p"):
                    style = node.find("w:pPr/w:pStyle", namespaces)
                    style_name = style.attrib.get(f"{{{namespaces['w']}}}val", "") if style is not None else ""
                    prefix = ""
                    if style_name.lower().startswith("heading"):
                        try:
                            prefix = "#" * min(int(style_name[-1]), 3) + " "
                        except ValueError:
                            prefix = "## "
                    else:
                        numbering_properties = node.find("w:pPr/w:numPr", namespaces)
                        if numbering_properties is not None:
                            number_id = numbering_properties.find("w:numId", namespaces)
                            level = numbering_properties.find("w:ilvl", namespaces)
                            number_format = numbering_formats.get((number_to_abstract.get(number_id.attrib.get(f"{{{namespaces['w']}}}val", ""), ""), level.attrib.get(f"{{{namespaces['w']}}}val", "0") if level is not None else "0"), "bullet") if number_id is not None else "bullet"
                            prefix = "1. " if number_format in {"decimal", "decimalZero", "decimalFullWidth", "decimalHalfWidth"} else "- "
                    text_parts: list[str] = []
                    for child in node:
                        if child.tag.endswith("}r"):
                            text_parts.append(_docx_run_markdown(child, namespaces))
                        elif child.tag.endswith("}hyperlink"):
                            relation_id = child.attrib.get(f"{{{namespaces['r']}}}id", "")
                            link_text = "".join(_docx_run_markdown(run, namespaces) for run in child.findall("w:r", namespaces))
                            target = relation_targets.get(relation_id, "")
                            text_parts.append(f"[{link_text}]({target})" if target.startswith(("http://", "https://")) else link_text)
                    text = "".join(text_parts)
                    images = [f"![图片]({image_urls[embed]})" for embed in (item.attrib.get(f"{{{namespaces['r']}}}embed", "") for item in node.findall(".//a:blip", namespaces)) if embed in image_urls]
                    if text.strip():
                        markdown.append(prefix + text.strip())
                    markdown.extend(images)
                elif node.tag.endswith("}tbl"):
                    rows = []
                    for row in node.findall("w:tr", namespaces):
                        cells = ["".join(_docx_run_markdown(run, namespaces) for run in cell.findall(".//w:r", namespaces)).strip().replace("|", "\\|").replace("\n", "<br>") for cell in row.findall("w:tc", namespaces)]
                        if cells:
                            rows.append(cells)
                    if rows:
                        markdown.append("| " + " | ".join(rows[0]) + " |")
                        markdown.append("| " + " | ".join("---" for _ in rows[0]) + " |")
                        markdown.extend("| " + " | ".join(row) + " |" for row in rows[1:])
            return "\n\n".join(markdown).strip()
    except (KeyError, ValueError, ElementTree.ParseError):
        raise HTTPBadRequest(text=f"无法解析 Word 文件：{Path(filename).name}")


def _page(title: str, body: str, admin: bool = False, status: int = 200) -> Response:
    document = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{_e(title)}</title><style>
    :root{{--blue:#3166df;--ink:#10233d;--muted:#5f738a;--line:#dce3ea;--bg:#fff;--danger:#b42318;--input:#dfe8f4}}*{{box-sizing:border-box}}body{{margin:0;font-family:"Microsoft YaHei",Arial,sans-serif;color:var(--ink);background:#fff;line-height:1.65}}a{{color:var(--blue);text-decoration:none}}a:hover{{text-decoration:underline}}main{{max-width:1080px;margin:32px auto;padding:0 20px}}.card{{background:#fff;border:1px solid var(--line);border-radius:12px;padding:24px;box-shadow:0 2px 8px #1525360d}}.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:16px}}.faq{{display:block;color:inherit;background:#fff;border:1px solid var(--line);border-radius:10px;padding:20px}}.faq:hover{{border-color:var(--blue);text-decoration:none}}.tag{{display:inline-block;color:#075e54;background:#e2f6f0;border-radius:99px;padding:2px 10px;font-size:13px}}.faq-detail-heading{{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:12px}}.faq-detail-heading h1{{margin:0}}.faq-detail-heading .tag{{flex:none}}h1{{margin:0 0 12px;font-size:28px}}h2{{margin:10px 0 8px;font-size:19px}}.muted{{color:var(--muted)}}.content{{word-break:break-word}}.markdown-content p{{margin:10px 0}}.markdown-content h1,.markdown-content h2,.markdown-content h3{{margin:18px 0 8px}}.markdown-content pre{{margin:12px 0;padding:14px;border-radius:8px;background:#f1f5f9;overflow:auto;white-space:pre-wrap}}.markdown-content code{{padding:1px 5px;border-radius:4px;background:#f1f5f9}}.markdown-content pre code{{padding:0;background:transparent}}.markdown-content blockquote{{margin:12px 0;padding:4px 12px;border-left:3px solid #94a3b8;color:#475569}}.markdown-content img{{display:block;max-width:100%;height:auto;margin:12px 0;border-radius:8px}}.markdown-content table{{margin:12px 0;border-collapse:collapse}}.markdown-content th,.markdown-content td{{border:1px solid var(--line);padding:8px}}.actions{{display:flex;gap:10px;flex-wrap:wrap}}button,.button{{font:inherit;border:0;border-radius:7px;padding:9px 14px;background:var(--blue);color:#fff;cursor:pointer}}.secondary{{background:#fff!important;border:1px solid var(--line)!important;color:var(--ink)!important}}.danger{{background:var(--danger)}}input,textarea{{width:100%;font:inherit;padding:9px;border:1px solid #afbdca;border-radius:6px}}textarea{{min-height:150px;resize:vertical}}label{{font-weight:600;display:block;margin:14px 0 5px}}table{{width:100%;border-collapse:collapse;background:#fff}}th,td{{padding:11px 8px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}.notice{{border-radius:7px;padding:10px 12px;margin:12px 0;background:#fff1f0;color:var(--danger)}}.login-shell{{min-height:calc(100vh - 86px);display:flex;align-items:center;justify-content:center}}.login{{width:min(100%,520px);margin:24px auto;background:#fff;border:1px solid #d7dee9;border-radius:16px;padding:28px 24px 32px;box-shadow:0 8px 24px rgba(29,42,57,.12)}}.login h1{{margin:0;text-align:center;font-size:20px;font-weight:700;color:#0f2850}}.login-subtitle{{margin:10px 0 20px;text-align:center;color:#58708a;font-size:14px}}.login label{{display:block;margin:16px 0 8px;font-size:14px;font-weight:600;color:#132d4a}}.login input{{height:40px;padding:10px 12px;border:1px solid #c5d0df;border-radius:10px;background:var(--input);box-shadow:inset 0 1px 0 rgba(255,255,255,.6)}}.login input:focus{{outline:none;border-color:#7ea5f6;box-shadow:0 0 0 3px rgba(49,102,223,.12)}}.captcha-row{{display:grid;grid-template-columns:minmax(0,1fr) 150px 110px;gap:10px;align-items:center}}.captcha-image{{width:150px;height:42px;border:1px solid #cfd7e3;border-radius:8px;background:#f8fafc;display:flex;align-items:center;justify-content:center;overflow:hidden}}.captcha-image img{{display:block;width:100%;height:100%;object-fit:cover}}.captcha-refresh{{font-size:14px;color:#6a7f95;white-space:nowrap;cursor:pointer}}.captcha-refresh:hover{{text-decoration:underline}}.login .notice{{margin:0 0 14px}}.login .actions{{margin-top:18px}}.login .actions button{{width:100%;height:40px;border-radius:10px;background:#3166df;font-size:16px;font-weight:700}}.login .actions button:hover{{background:#2858cb}}.inline{{display:inline}}@media(max-width:640px){{main{{margin:20px auto}}.card{{padding:18px}}table{{font-size:13px}}.faq-detail-heading{{align-items:flex-start;gap:10px}}.faq-detail-heading h1{{font-size:24px}}.login{{padding:24px 18px 28px}}.captcha-row{{grid-template-columns:1fr;gap:8px}}.captcha-image{{width:100%}}}}</style></head><body><main>{body}</main></body></html>'''
    return Response(text=document, status=status, content_type="text/html")


def _faq_id(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise HTTPBadRequest(text="FAQ 编号无效") from error
    if parsed <= 0:
        raise HTTPBadRequest(text="FAQ 编号无效")
    return parsed


def _session(request: Request) -> dict[str, object] | None:
    """验证签名 Cookie，并只接受未过期的服务端会话。"""
    now = time.time()
    for token in [key for key, item in _SESSIONS.items() if _as_float(item.get("expires", 0.0)) <= now]:
        _SESSIONS.pop(token, None)
    raw = request.cookies.get(SESSION_COOKIE, "")
    if "." not in raw:
        return None
    token, signature = raw.rsplit(".", 1)
    return _SESSIONS.get(token) if token and _equal(_sign(token), signature) else None


def _require(request: Request) -> dict[str, object]:
    session = _session(request)
    if session is None:
        raise HTTPFound("/admin/login")
    return session


def _is_admin_session(session: dict[str, object]) -> bool:
    """仅接受仍处于启用状态的管理员账号会话。"""
    if session.get("role") != "admin":
        return False
    admin_id = _as_int(session.get("admin_id", 0))
    if admin_id <= 0:
        return False
    with _db() as conn:
        row = conn.execute("SELECT is_active FROM admin_accounts WHERE id=?", (admin_id,)).fetchone()
    return row is not None and int(row["is_active"] or 0) == 1


def _require_admin(request: Request) -> dict[str, object]:
    session = _require(request)
    if not _is_admin_session(session):
        raise HTTPFound("/admin/login")
    return session


def _can_access_from_index(request: Request) -> bool:
    """允许管理员会话，或从 /webchat 页面发起的同源请求访问 FAQ 详情与目录。"""
    session = _session(request)
    if session is not None and _is_admin_session(session):
        return True
    referer = request.headers.get("Referer", "")
    if not referer:
        return False
    parsed = urlparse(referer)
    return parsed.path.startswith("/webchat")


def _csrf_header_valid(request: Request, session: dict[str, object]) -> bool:
    token = request.headers.get("X-CSRF-Token", "")
    return bool(token and _equal(token, str(session.get("csrf", ""))))


async def admin_faq_image_upload(request: Request) -> Response:
    session = _require_admin(request)
    if not _csrf_header_valid(request, session):
        return json_response({"error": "CSRF 验证失败。"}, status=403)
    reader = await request.multipart()
    field = await reader.next()
    if field is None or field.name != "file" or not field.filename:
        return json_response({"error": "缺少图片文件。"}, status=400)
    content = await field.read(decode=False)
    suffix = Path(field.filename).suffix.lower()
    signatures = {
        ".png": (b"\x89PNG\r\n\x1a\n", "image/png"),
        ".jpg": (b"\xff\xd8\xff", "image/jpeg"),
        ".jpeg": (b"\xff\xd8\xff", "image/jpeg"),
        ".webp": (b"RIFF", "image/webp"),
    }
    if not content:
        return json_response({"error": "图片文件不能为空。"}, status=400)
    if len(content) > 5 * 1024 * 1024:
        return json_response({"error": "图片文件不能超过 5 MB。"}, status=413)
    detected = signatures.get(suffix)
    if detected is None or not content.startswith(detected[0]) or (suffix == ".webp" and content[8:12] != b"WEBP"):
        return json_response({"error": "仅支持 PNG、JPG、WEBP 图片。"}, status=415)
    stored_name = f"{uuid4()}{suffix}"
    FAQ_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    (FAQ_IMAGE_DIR / stored_name).write_bytes(content)
    return json_response({"name": Path(field.filename).name[:160], "contentType": detected[1], "contentUrl": f"/faq/images/{stored_name}"})


async def faq_image(request: Request) -> Response:
    if not _can_access_from_index(request):
        raise HTTPFound("/admin/login")
    filename = Path(request.match_info["filename"]).name
    if filename != request.match_info["filename"] or not filename:
        raise HTTPNotFound()
    image_path = FAQ_IMAGE_DIR / filename
    if not image_path.is_file():
        raise HTTPNotFound()
    return FileResponse(image_path, headers={"Cache-Control": "private, max-age=300"})


def _to_beijing_time(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts + 8 * 3600))


def _user_agent_meta(user_agent: str) -> tuple[str, str]:
    ua = user_agent or ""
    os_version = "Unknown"
    browser = "Unknown"
    def _extract(marker: str) -> str:
        if marker not in ua:
            return ""
        tail = ua.split(marker, 1)[1]
        token = tail.split(" ", 1)[0].split(";", 1)[0].strip()
        return token

    if "Windows NT" in ua:
        nt = _extract("Windows NT ")
        os_version = "Windows " + (nt or "NT")
    elif "Android" in ua:
        os_version = "Android " + (_extract("Android ") or "")
    elif "iPhone OS" in ua:
        os_version = "iOS " + (_extract("iPhone OS ").replace("_", ".") or "")
    elif "CPU iPhone OS" in ua:
        os_version = "iOS " + (_extract("CPU iPhone OS ").replace("_", ".") or "")
    elif "Mac OS X" in ua:
        os_version = "macOS " + (_extract("Mac OS X ").replace("_", ".") or "")
    elif "Linux" in ua:
        os_version = "Linux"

    if "Edg/" in ua:
        browser = "Edge " + (_extract("Edg/") or "")
    elif "Chrome/" in ua and "Edg/" not in ua:
        browser = "Chrome " + (_extract("Chrome/") or "")
    elif "Firefox/" in ua:
        browser = "Firefox " + (_extract("Firefox/") or "")
    elif "Version/" in ua and "Safari/" in ua and "Chrome/" not in ua:
        browser = "Safari " + (_extract("Version/") or "")
    elif "Safari/" in ua and "Chrome/" not in ua:
        browser = "Safari " + (_extract("Safari/") or "")
    return os_version, browser


def record_webchat_question(request: Request, question: str, login_account: str = "") -> None:
    content = str(question or "").strip()
    if not content:
        return
    forwarded = request.headers.get("X-Forwarded-For", "")
    ip_address = forwarded.split(",")[0].strip() if forwarded else (request.remote or "")
    os_version, browser_version = _user_agent_meta(request.headers.get("User-Agent", ""))
    now = int(time.time())
    cutoff = now - LOG_RETENTION_DAYS * 86400
    with _db() as conn:
        conn.execute(
            "INSERT INTO webchat_question_logs(created_at,ip_address,login_account,os_version,browser_version,question_content) VALUES(?,?,?,?,?,?)",
            (now, ip_address[:80], str(login_account or "").strip()[:320], os_version[:80], browser_version[:80], content[:4000]),
        )
        conn.execute("DELETE FROM webchat_question_logs WHERE created_at < ?", (cutoff,))


def record_faq_visit(request: Request, title: str, login_account: str = "") -> None:
    record_webchat_question(request, f"访问常见问题：{title}", login_account)


def ensure_webchat_access(user: dict[str, object]) -> tuple[str, str]:
    tenant_id = str(user.get("tenantId") or "").strip()
    object_id = str(user.get("objectId") or "").strip()
    if not tenant_id or not object_id:
        return "pending", ""
    username = str(user.get("username") or "").strip()[:320]
    display_name = str(user.get("name") or username).strip()[:320]
    now = int(time.time())
    with _db() as conn:
        row = conn.execute(
            "SELECT username,display_name,status,denial_reason FROM webchat_access_grants WHERE tenant_id=? AND object_id=?",
            (tenant_id, object_id),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO webchat_access_grants(tenant_id,object_id,username,display_name,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (tenant_id, object_id, username, display_name, "pending", now, now),
            )
            return "pending", ""
        if username != str(row["username"]) or display_name != str(row["display_name"]):
            conn.execute(
                "UPDATE webchat_access_grants SET username=?,display_name=?,updated_at=? WHERE tenant_id=? AND object_id=?",
                (username, display_name, now, tenant_id, object_id),
            )
        return str(row["status"]), str(row["denial_reason"] or "")


def _generate_totp_secret() -> str:
    """生成随机 Base32 TOTP 密钥。"""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _totp(secret: str, code: str) -> bool:
    """实现 RFC 6238 TOTP，相邻一个时间窗口容错。"""
    try:
        key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret.replace(" ", "")) % 8), casefold=True)
    except (binascii.Error, ValueError):
        return False
    if not key or not code.isdigit() or len(code) != TOTP_DIGITS:
        return False
    counter = int(time.time() // TOTP_PERIOD)
    for offset in (-1, 0, 1):
        digest = hmac.new(key, struct.pack(">Q", counter + offset), hashlib.sha1).digest()
        start = digest[-1] & 15
        value = (struct.unpack(">I", digest[start:start + 4])[0] & 0x7fffffff) % (10 ** TOTP_DIGITS)
        if _equal(f"{value:06d}", code):
            return True
    return False


def _captcha_svg(code: str) -> str:
    chars = []
    for index, char in enumerate(code):
        x = 22 + index * 24
        y = 28 + (index % 2) * 2
        rotate = random.randint(-22, 18)
        color = random.choice(["#133d6b", "#0b6b8f", "#5a3ec8", "#2f6b2f", "#8a4b2c"])
        chars.append(f'<text x="{x}" y="{y}" font-size="20" font-family="Arial" fill="{color}" transform="rotate({rotate} {x} {y})">{char}</text>')
    lines = "".join(
        f'<line x1="{random.randint(0, 150)}" y1="{random.randint(0, 42)}" x2="{random.randint(0, 150)}" y2="{random.randint(0, 42)}" stroke="{random.choice(["#a7b5c8", "#93a5bf", "#c0ccd9"])}" stroke-width="1" />'
        for _ in range(6)
    )
    dots = "".join(
        f'<circle cx="{random.randint(4, 146)}" cy="{random.randint(4, 38)}" r="1" fill="{random.choice(["#c8a27f", "#88a4c4", "#b7bec9"])}" />'
        for _ in range(24)
    )
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="150" height="42" viewBox="0 0 150 42"><rect width="150" height="42" rx="8" fill="#f4f6fa"/>{lines}{dots}{"".join(chars)}</svg>'
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def _new_captcha() -> tuple[str, str, str]:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    code = "".join(secrets.choice(alphabet) for _ in range(5))
    payload = f"{code}:{int(time.time()) + CAPTCHA_TTL}:{secrets.token_urlsafe(8)}"
    return code, f"{payload}.{_sign(payload)}", _captcha_svg(code)


def _captcha(request: Request, code: str) -> bool:
    raw = request.cookies.get(CAPTCHA_COOKIE, "")
    normalized_code = code.strip().upper()
    if "." not in raw or not normalized_code:
        return False
    payload, signature = raw.rsplit(".", 1)
    try:
        answer, expires, _nonce = payload.split(":", 2)
        return _equal(_sign(payload), signature) and int(expires) >= time.time() and _equal(answer.upper(), normalized_code)
    except ValueError:
        return False


async def faq_directory(request: Request) -> Response:
    with _db() as conn:
        rows = conn.execute(
            "SELECT id,title,category FROM faqs WHERE is_published=1 ORDER BY sort_order,id DESC"
        ).fetchall()
    return json_response(
        [
            {"id": row["id"], "title": row["title"], "category": row["category"]}
            for row in rows
        ]
    )


async def faq_detail(request: Request) -> Response:
    faq_id = _faq_id(request.match_info["faq_id"])
    with _db() as conn:
        row = conn.execute("SELECT title,category,content FROM faqs WHERE id=? AND is_published=1", (faq_id,)).fetchone()
    if row is None:
        return _page("未找到 FAQ", '<section class="card"><h1>内容不存在</h1><p><a href="/">返回智能助手</a></p></section>', status=404)
    record_faq_visit(request, str(row["title"]), str(request.get("login_account", "")))
    content_html, headings = _render_markdown(str(row["content"]))
    detail_styles = """<style>.faq-detail-heading{position:relative}.faq-detail-heading h1{width:100%;text-align:center}.faq-detail-heading .tag{position:absolute;right:0}.markdown-content p{text-indent:2em}.markdown-content .faq-text-center{text-align:center}.markdown-content .faq-table-wrap{overflow-x:auto}.markdown-content table{width:100%;border-collapse:collapse}.markdown-content th,.markdown-content td{padding:8px;border:1px solid #cbd5e1;text-align:left}.markdown-content th{background:#f1f5f9}</style>"""
    article = f'{detail_styles}<article class="card"><div class="faq-detail-heading"><h1>{_e(row["title"])}</h1><span class="tag">{_e(row["category"] or "未分类")}</span></div><div class="content markdown-content">{content_html}</div></article>'
    if headings:
        links = "".join(f'<a class="toc-level-{level}" href="#{heading_id}">{_e(label)}</a>' for level, label, heading_id in headings)
        styles = """<style>body:has(.faq-detail-layout) main{max-width:1360px;padding:0 24px}.faq-detail-layout{display:grid;grid-template-columns:280px minmax(0,1fr);gap:10px;align-items:start}.faq-detail-layout .card{padding:30px}.faq-toc{position:sticky;top:24px;padding:8px 0}.faq-toc-title{margin:0 0 10px;font-size:14px;font-weight:700}.faq-toc a{display:block;padding:5px 10px;border-left:2px solid transparent;color:var(--muted);font-size:14px;line-height:1.4}.faq-toc a:hover,.faq-toc a.active{border-left-color:var(--blue);color:var(--blue);text-decoration:none}.faq-toc .toc-level-2{padding-left:22px}.faq-toc .toc-level-3{padding-left:34px;font-size:13px}.markdown-content h1,.markdown-content h2,.markdown-content h3{scroll-margin-top:24px}@media(max-width:760px){body:has(.faq-detail-layout) main{padding:0 20px}.faq-detail-layout{grid-template-columns:1fr}.faq-toc{display:none}.faq-detail-layout .card{padding:18px}}</style>"""
        script = """<script>const links=[...document.querySelectorAll('.faq-toc a')];const activate=()=>{let current=null;for(const link of links){const heading=document.getElementById(link.hash.slice(1));if(heading&&heading.getBoundingClientRect().top<=120)current=link;}links.forEach(link=>link.classList.toggle('active',link===current));};addEventListener('scroll',activate,{passive:true});activate();</script>"""
        body = f'{styles}<div class="faq-detail-layout"><nav class="faq-toc" aria-label="内容大纲"><p class="faq-toc-title">本页目录</p>{links}</nav>{article}</div>{script}'
    else:
        body = article
    return _page(str(row["title"]), body)


async def admin_login_method(request: Request) -> Response:
    """GET /admin/login-method?username=xxx — 返回该账号的登录验证方式。"""
    username = request.rel_url.query.get("username", "").strip()
    if not username:
        return json_response({"method": "captcha"})
    with _db() as conn:
        row = conn.execute(
            "SELECT is_active, mfa_enabled FROM admin_accounts WHERE username=?",
            (username,),
        ).fetchone()
    if row and int(row["is_active"] or 0) == 1 and int(row["mfa_enabled"] or 0) == 1:
        return json_response({"method": "mfa"})
    return json_response({"method": "captcha"})


async def admin_login_captcha(request: Request) -> Response:
    """GET /admin/login/captcha — 生成新验证码并返回 JSON，供前端刷新使用。"""
    _captcha_code, captcha_cookie, captcha_image = _new_captcha()
    resp = json_response({"image_data_url": captcha_image, "expire_seconds": CAPTCHA_TTL})
    resp.set_cookie(CAPTCHA_COOKIE, captcha_cookie, max_age=CAPTCHA_TTL, httponly=True, samesite="Strict", secure=request.secure, path="/admin")
    return resp


async def admin_login(request: Request) -> Response:
    """执行用户名、密码与验证码/OTP的组合登录。"""
    current = _session(request)
    if current is not None and _is_admin_session(current):
        raise HTTPFound("/admin")
    error = ""
    username = "admin"
    if request.method == "POST":
        submitted = {key: str(value) for key, value in (await request.post()).items()}
        username = submitted.get("username", "")[:256]
        password = submitted.get("password", "")[:256]
        with _db() as conn:
            account = conn.execute(
                "SELECT id,username,display_name,password_hash,is_active,mfa_enabled,mfa_secret FROM admin_accounts WHERE username=?",
                (username,),
            ).fetchone()
            if account is not None and int(account["is_active"] or 0) == 1 and _verify_password(password, str(account["password_hash"])):
                if int(account["mfa_enabled"] or 0) == 1:
                    valid = _totp(str(account["mfa_secret"] or ""), submitted.get("otp_code", "")[:16])
                else:
                    valid = _captcha(request, submitted.get("captcha", "")[:16])
            else:
                valid = False
            if valid and account is not None:
                conn.execute("UPDATE admin_accounts SET last_login=?,updated_at=? WHERE id=?", (int(time.time()), int(time.time()), int(account["id"])))
        if valid and account is not None:
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            _SESSIONS[token] = {
                "csrf": csrf,
                "expires": time.time() + SESSION_TTL,
                "admin_id": int(account["id"]),
                "username": str(account["username"]),
                "display_name": str(account["display_name"]),
                "role": "admin",
            }
            response = HTTPFound("/admin")
            response.set_cookie(SESSION_COOKIE, f"{token}.{_sign(token)}", max_age=SESSION_TTL, httponly=True, samesite="Strict", secure=request.secure, path="/")
            response.del_cookie(CAPTCHA_COOKIE, path="/admin")
            raise response
        error = "登录失败。请检查账号、密码和验证码。"
    _captcha_code, captcha_cookie, captcha_image = _new_captcha()
    with _db() as conn:
        has_admin = conn.execute("SELECT 1 FROM admin_accounts LIMIT 1").fetchone() is not None
    hint = "" if has_admin else "尚未初始化管理员账号，请先配置 FAQ_ADMIN_USERNAME / FAQ_ADMIN_PASSWORD 并重启服务。"
    
    template = _TEMPLATE_ENV.get_template("admin_login.html")
    html = template.render(
        username=username,
        error=error,
        hint=hint,
        captcha_image=captcha_image
    )
    response = Response(text=html, content_type="text/html")
    response.set_cookie(CAPTCHA_COOKIE, captcha_cookie, max_age=CAPTCHA_TTL, httponly=True, samesite="Strict", secure=request.secure, path="/admin")
    return response


async def admin_dashboard(request: Request) -> Response:
    session = _session(request)
    if session is None or not _is_admin_session(session):
        return await admin_login(request)
    return await admin_list(request)


def _form_data(form: dict[str, str]) -> tuple[dict[str, object] | None, str]:
    """验证和转换 FAQ CRUD 表单字段。"""
    title, category = form.get("title", "").strip(), form.get("category", "").strip()
    summary, content = form.get("summary", "").strip(), form.get("content", "").strip()
    if not title or not content:
        return None, "标题和正文不能为空。"
    if len(title) > MAX_TITLE or len(category) > MAX_CATEGORY or len(summary) > MAX_SUMMARY or len(content) > MAX_CONTENT:
        return None, "输入内容超过允许长度。"
    try:
        sort_order = int(form.get("sort_order", "0"))
    except ValueError:
        return None, "排序值必须是整数。"
    if not -1_000_000 <= sort_order <= 1_000_000:
        return None, "排序值超出允许范围。"
    return {"title": title, "category": category, "summary": summary, "content": content, "sort_order": sort_order, "is_published": int(form.get("is_published") == "1")}, ""


def _valid_csrf(form: dict[str, str], session: dict[str, object]) -> bool:
    return bool(form.get(CSRF_FIELD) and _equal(form[CSRF_FIELD], str(session["csrf"])))


async def admin_faq_import_content(request: Request) -> Response:
    session = _require_admin(request)
    if not _csrf_header_valid(request, session):
        return json_response({"error": "请求校验失败。"}, status=403)
    reader = await request.multipart()
    field = await reader.next()
    if field is None or field.name != "file" or not field.filename:
        return json_response({"error": "请选择要导入的 Word 或 Markdown 文件。"}, status=400)
    content = await field.read(decode=False)
    if not content:
        return json_response({"error": "导入文件不能为空。"}, status=400)
    if len(content) > 10 * 1024 * 1024:
        return json_response({"error": "导入文件不能超过 10 MB。"}, status=413)
    suffix = Path(field.filename).suffix.lower()
    try:
        if suffix in {".md", ".markdown"}:
            markdown = content.decode("utf-8-sig")
        elif suffix == ".docx":
            markdown = _docx_to_markdown(field.filename, content)
        else:
            return json_response({"error": "仅支持 DOCX、MD 和 MARKDOWN 格式文件。"}, status=415)
    except UnicodeDecodeError:
        return json_response({"error": "Markdown 文件必须使用 UTF-8 编码。"}, status=415)
    except HTTPBadRequest as error:
        return json_response({"error": error.text}, status=400)
    if not markdown.strip():
        return json_response({"error": "未能从文件中提取正文内容。"}, status=400)
    if len(markdown) > MAX_CONTENT:
        return json_response({"error": "导入后的正文超过允许长度。"}, status=413)
    return json_response({"content": markdown})


def _editor(faq: dict[str, object], csrf: str, action: str, error: str = "") -> str:
    with _db() as conn:
        category_rows = conn.execute(
            "SELECT DISTINCT category FROM faqs WHERE TRIM(category) <> '' ORDER BY category COLLATE NOCASE"
        ).fetchall()
    current_category = str(faq.get("category", "")).strip()
    category_options = '<button type="button" class="faq-category-option" data-category-value="">未分类</button>'
    category_options += "".join(
        f'<button type="button" class="faq-category-option" data-category-value="{_e(row["category"])}">{_e(row["category"])}</button>'
        for row in category_rows
    )
    error_html = f'<p class="notice">{_e(error)}</p>' if error else ""
    checked = " checked" if faq.get("is_published") else ""
    headline = "编辑" if faq.get("id") else "新增"
    editor_script = r'''
<script>
(() => {
  const textarea = document.getElementById("faqContentEditor");
  const visualEditor = document.getElementById("faqVisualEditor");
  document.getElementById("faqEditMode")?.remove();
  document.getElementById("faqPreviewMode")?.remove();
  const uploadInput = document.getElementById("faqAttachmentInput");
  const importInput = document.getElementById("faqContentImportInput");
  const editorShell = document.getElementById("faqMarkdownEditor");
  const fullscreenButton = document.getElementById("faqFullscreenMode");
  const categoryInput = document.getElementById("faqCategoryInput");
  const categoryMenu = document.getElementById("faqCategoryMenu");
  const categoryToggle = document.getElementById("faqCategoryToggle");
  let popovers = [];

  function resizeEditor() {
    if (editorShell.classList.contains("page-fullscreen")) return;
    visualEditor.style.minHeight = `${Math.max(320, visualEditor.scrollHeight)}px`;
  }

  function escapeHtml(value) {
    return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function renderMarkdown(value) {
    const codeBlocks = [];
    let html = escapeHtml(value).replace(/```([\s\S]*?)```/g, (_, code) => {
      const token = `@@CODE${codeBlocks.length}@@`;
      codeBlocks.push(`<pre><code>${code.trim()}</code></pre>`);
      return token;
    });
    html = html.replace(/^:::(?:align:)?(left|center|right)\n([\s\S]*?)\n:::/gm, '<div class="faq-text-align-$1">$2</div>');
    html = html.replace(/^### (.*)$/gm, "<h3>$1</h3>").replace(/^## (.*)$/gm, "<h2>$1</h2>").replace(/^# (.*)$/gm, "<h1>$1</h1>");
    html = html.replace(/\{\{style:(#[0-9a-fA-F]{6}):?(#[0-9a-fA-F]{6})?\|(.+?)\}\}/g, (_, color, background, text) => `<span style="color:${color}${background ? `;background-color:${background}` : ""}">${text}</span>`).replace(/\{\{color:(#[0-9a-fA-F]{6})\|(.+?)\}\}/g, '<span style="color:$1">$2</span>').replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/~~([^~]+)~~/g, "<s>$1</s>").replace(/\*([^*]+)\*/g, "<em>$1</em>").replace(/\+\+([^+]+)\+\+/g, "<u>$1</u>").replace(/==([^=]+)==/g, "<mark>$1</mark>");
    html = html.replace(/!\[([^\]]*)\]\(([^\s)]+)\)/g, '<img src="$2" alt="$1">').replace(/\[([^\]]+)\]\(([^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    html = html.replace(/^&gt; (.*)$/gm, "<blockquote>$1</blockquote>").replace(/^- (.*)$/gm, "<li>$1</li>").replace(/^\d+\. (.*)$/gm, "<li>$1</li>");
    html = html.replace(/(<li>.*<\/li>)(\n<li>.*<\/li>)+/g, match => `<ul>${match.replace(/\n/g, "")}</ul>`);
    html = html.replace(/^\|(.+)\|\n\|(?:\s*:?-{3,}:?\s*\|)+\n((?:\|.+\|(?:\n|$))*)/gm, (_, header, rows) => {
      const cells = value => value.split("|").filter(Boolean).map(cell => cell.trim());
      const heading = cells(header).map(cell => `<th>${cell}</th>`).join("");
      const body = rows.trim().split("\n").map(row => `<tr>${cells(row).map(cell => `<td>${cell}</td>`).join("")}</tr>`).join("");
      return `<div class="faq-table-wrap"><table><thead><tr>${heading}</tr></thead><tbody>${body}</tbody></table></div>`;
    });
    html = html.split(/\n{2,}/).map(block => /^(<h[1-3]|<pre|<ul|<blockquote)/.test(block) ? block : `<p>${block.replace(/\n/g, "<br>")}</p>`).join("");
    codeBlocks.forEach((block, index) => {
      html = html.replace(`@@CODE${index}@@`, block);
    });
    return html;
  }

  function escapeMarkdownText(value) {
    return value.replace(/\\/g, "\\\\").replace(/([*_~+=`])/g, "\\$1");
  }

  function normalizeColor(value) {
    const match = value.match(/^rgb\((\d+),\s*(\d+),\s*(\d+)\)$/);
    if (!match) return value;
    return `#${match.slice(1).map(channel => Number(channel).toString(16).padStart(2, "0")).join("")}`;
  }

  function serializeChildren(node) {
    return Array.from(node.childNodes).map(serializeNode).join("");
  }

  function serializeNode(node) {
    if (node.nodeType === Node.TEXT_NODE) return escapeMarkdownText(node.nodeValue || "");
    if (node.nodeType !== Node.ELEMENT_NODE) return "";
    const element = node;
    const content = serializeChildren(element);
    if (/^H[1-3]$/.test(element.tagName)) return `${"#".repeat(Number(element.tagName[1]))} ${content}\n\n`;
    if (element.tagName === "P") return `${content}\n\n`;
    if (element.tagName === "BR") return "\n";
    if (element.tagName === "STRONG" || element.tagName === "B") return `**${content}**`;
    if (element.tagName === "EM" || element.tagName === "I") return `*${content}*`;
    if (element.tagName === "S" || element.tagName === "STRIKE" || element.tagName === "DEL") return `~~${content}~~`;
    if (element.tagName === "U") return `++${content}++`;
    if (element.tagName === "MARK") return `==${content}==`;
    if (element.tagName === "CODE" && element.parentElement?.tagName !== "PRE") return `\`${content}\``;
    if (element.tagName === "PRE") return `\`\`\`\n${element.textContent || ""}\n\`\`\`\n\n`;
    if (element.tagName === "A") return `[${content}](${element.getAttribute("href") || ""})`;
    if (element.tagName === "IMG") return `![${element.getAttribute("alt") || ""}](${element.getAttribute("src") || ""})`;
    if (element.tagName === "BLOCKQUOTE") return `${content.trim().split("\n").map(line => `> ${line}`).join("\n")}\n\n`;
    if (element.tagName === "UL" || element.tagName === "OL") return `${Array.from(element.children).map((item, index) => `${element.tagName === "OL" ? `${index + 1}.` : "-"} ${serializeChildren(item).trim()}`).join("\n")}\n\n`;
    if (element.tagName === "TABLE") {
      const rows = Array.from(element.querySelectorAll("tr")).map(row => Array.from(row.children).map(cell => serializeChildren(cell).trim().replace(/\|/g, "\\|")).join(" | "));
      return rows.length ? `| ${rows[0]} |\n| ${rows[0].split(" |").map(() => "---").join(" | ")} |\n${rows.slice(1).map(row => `| ${row} |`).join("\n")}\n\n` : "";
    }
    if (element.tagName === "SPAN" && (element.style.color || element.style.backgroundColor)) {
      const color = normalizeColor(element.style.color || "#0f172a");
      const background = normalizeColor(element.style.backgroundColor);
      return `{{style:${color}${background ? `:${background}` : ""}|${content}}}`;
    }
    const alignment = element.style.textAlign || element.className.match(/faq-text-align-(left|center|right)/)?.[1] || "";
    if (alignment && alignment !== "left") return `:::align:${alignment}\n${content.trim()}\n:::\n\n`;
    if (element.tagName === "DIV") return `${content}\n`;
    return content;
  }

  function syncMarkdown() {
    textarea.value = serializeChildren(visualEditor).replace(/\n{3,}/g, "\n\n").trim();
  }

  function renderVisualEditor() {
    visualEditor.innerHTML = renderMarkdown(textarea.value) || "<p><br></p>";
    resizeEditor();
  }

  function execute(command, value = null) {
    visualEditor.focus();
    document.execCommand(command, false, value);
    syncMarkdown();
    resizeEditor();
  }

  function insertHtml(html) {
    execute("insertHTML", html);
  }

  function toggleInlineFormat(marker) {
    const command = { "**": "bold", "*": "italic", "~~": "strikeThrough", "++": "underline", "==": "hiliteColor", "`": "formatBlock" }[marker];
    if (marker === "==") execute(command, "#fef08a");
    else if (marker === "`") execute(command, "code");
    else execute(command);
  }

  function insertTable(columns, rows) {
    const header = Array.from({ length: columns }, (_, index) => `<th>标题${index + 1}</th>`).join("");
    const body = Array.from({ length: rows }, () => `<tr>${Array.from({ length: columns }, () => "<td>内容</td>").join("")}</tr>`).join("");
    insertHtml(`<table><thead><tr>${header}</tr></thead><tbody>${body}</tbody></table><p><br></p>`);
  }

  function insertStyledText(color, background = "") {
    execute(background ? "hiliteColor" : "foreColor", background || color);
    if (background) execute("foreColor", "#0f172a");
  }

  function togglePopover(name, trigger) {
    const target = document.querySelector(`[data-editor-popover="${name}"]`);
    const shouldShow = target.hidden;
    popovers.forEach(popover => { popover.hidden = true; });
    target.style.left = `${trigger.offsetLeft}px`;
    target.hidden = !shouldShow;
  }

  function createPopover(name, content) {
    const toolbar = editorShell.querySelector('[data-markdown-action="heading"]').parentElement;
    toolbar.classList.add("faq-editor-control");
    const popover = document.createElement("div");
    popover.className = "faq-editor-popover";
    popover.classList.add(`faq-editor-popover-${name}`);
    popover.dataset.editorPopover = name;
    popover.hidden = true;
    popover.innerHTML = content;
    toolbar.append(popover);
    popovers = [...document.querySelectorAll(".faq-editor-popover")];
    return popover;
  }

  const alignPopover = createPopover("align", `
    <button type="button" data-align-value="left"><span>☰</span>左对齐<b>✓</b></button>
    <button type="button" data-align-value="center"><span>☰</span>居中对齐</button>
    <button type="button" data-align-value="right"><span>☰</span>右对齐</button>
  `);
  const headingPopover = createPopover("heading", `
    <button type="button" data-heading-level="1"><span>H₁</span>一级标题</button>
    <button type="button" data-heading-level="2"><span>H₂</span>二级标题</button>
    <button type="button" data-heading-level="3"><span>H₃</span>三级标题</button>
    <hr>
    <button type="button" data-list-type="ordered"><span>1☷</span>有序列表</button>
    <button type="button" data-list-type="bullet"><span>☷</span>无序列表</button>
  `);
  const colorPopover = createPopover("color", `
    <p>字体颜色</p>
    <div class="faq-color-row">
      ${["#c00000", "#ff0000", "#ffc000", "#ffff00", "#92d050", "#00b050", "#00b0f0", "#0070c0", "#002060", "#7030a0"].map(color => `<button type="button" data-font-color="${color}" style="color:${color}">A</button>`).join("")}
    </div>
    <p>背景颜色</p>
    <div class="faq-color-row">
      ${["#ffff00", "#00ff00", "#00ffff", "#ff00ff", "#0000ff", "#ff0000", "#000080", "#008080", "#008000", "#800080", "#800000", "#808000", "#808080", "#c0c0c0", "#000000"].map(color => `<button type="button" data-background-color="${color}" style="background:${color}"></button>`).join("")}
    </div>
    <button type="button" id="faqColorReset" class="faq-popover-reset">恢复默认</button>
  `);
  const tablePopover = createPopover("table", `
    <p>插入表格</p>
    <label>列数<input id="faqTableColumns" type="number" min="1" max="10" value="3"></label>
    <label>数据行数<input id="faqTableRows" type="number" min="1" max="20" value="2"></label>
    <button type="button" id="faqTableInsert" class="faq-popover-confirm">插入表格</button>
  `);
  const clearButton = document.createElement("button");
  clearButton.type = "button";
  clearButton.id = "faqClearContent";
  clearButton.title = "清空正文内容";
  clearButton.textContent = "清空";
  const toolbar = editorShell.querySelector('[data-markdown-action="heading"]').parentElement;
  toolbar.append(clearButton);
  const saveButton = document.createElement("button");
  saveButton.type = "button";
  saveButton.id = "faqToolbarSave";
  saveButton.title = "保存 FAQ";
  saveButton.textContent = "保存";
  toolbar.insertBefore(saveButton, clearButton.nextSibling);
  const clearDialog = document.createElement("div");
  clearDialog.id = "faqClearDialog";
  clearDialog.hidden = true;
  clearDialog.innerHTML = `<div class="faq-clear-dialog-card" role="dialog" aria-modal="true" aria-labelledby="faqClearDialogTitle"><h3 id="faqClearDialogTitle">清空正文内容</h3><p>确定要清空当前正文吗？此操作在保存前仍可继续编辑。</p><div><button type="button" id="faqClearDialogCancel">取消</button><button type="button" id="faqClearDialogConfirm">确认清空</button></div></div>`;
  document.body.append(clearDialog);
  const toolbarStyle = document.createElement("style");
  toolbarStyle.textContent = `#faqEditorContent>div:first-child{gap:6px !important;padding:8px !important;background:#f8fafc}#faqContentEditor{min-height:320px !important}#faqEditorContent>div:first-child>button{display:inline-flex;align-items:center;justify-content:center;min-width:34px;height:34px;padding:0 9px;border:1px solid #cbd5e1;border-radius:6px;background:#fff;color:#334155;font-size:13px;line-height:1;font-weight:600;cursor:pointer;transition:background-color .15s ease,border-color .15s ease,color .15s ease,box-shadow .15s ease}#faqEditorContent>div:first-child>button:hover{border-color:#94a3b8;background:#f1f5f9;color:#0f172a}#faqEditorContent>div:first-child>button:active{border-color:#2563eb;background:#dbeafe;color:#1d4ed8;transform:translateY(1px)}#faqEditorContent>div:first-child>button:focus-visible{outline:2px solid #2563eb;outline-offset:2px}#faqEditorContent>div:first-child>button:disabled{border-color:#e2e8f0;background:#f8fafc;color:#94a3b8;cursor:not-allowed;opacity:.7}#faqEditorContent>div:first-child>[data-markdown-action="color"]{border-color:#ca8a04;background:#facc15;color:#422006}#faqEditorContent>div:first-child>[data-markdown-action="color"]:hover{border-color:#a16207;background:#fde047;color:#422006}#faqEditorContent>div:first-child>[data-markdown-action="color"]:active{border-color:#854d0e;background:#eab308;color:#422006}#faqEditorContent>div:first-child>#faqToolbarSave{border-color:#2563eb;background:#2563eb;color:#fff;font-weight:600}#faqEditorContent>div:first-child>#faqToolbarSave:hover{border-color:#1d4ed8;background:#1d4ed8}#faqEditorContent>div:first-child>#faqClearContent{border-color:#fecaca;background:#fff1f2;color:#b91c1c;font-weight:500}#faqEditorContent>div:first-child>#faqClearContent:hover{border-color:#fca5a5;background:#fee2e2;color:#991b1b}#faqClearDialog{position:fixed;z-index:1100;inset:0;display:grid;place-items:center;padding:20px;background:rgba(15,23,42,.36)}#faqClearDialog[hidden]{display:none}.faq-clear-dialog-card{width:min(360px,100%);padding:22px;border:1px solid #dbe2ec;border-radius:12px;background:#fff;color:#0f172a;box-shadow:0 20px 48px rgba(15,23,42,.22)}.faq-clear-dialog-card h3{margin:0 0 8px;color:#1d4ed8;font-size:18px}.faq-clear-dialog-card p{margin:0;color:#475569;font-size:13px;line-height:1.6}.faq-clear-dialog-card>div{display:flex;justify-content:flex-end;gap:8px;margin-top:18px}.faq-clear-dialog-card button{height:32px;padding:0 12px;border:1px solid #cbd5e1;border-radius:6px;background:#fff;color:#334155;cursor:pointer}.faq-clear-dialog-card #faqClearDialogConfirm{border-color:#dc2626;background:#dc2626;color:#fff}.faq-clear-dialog-card #faqClearDialogConfirm:hover{background:#b91c1c}@media(max-width:760px){#faqEditorContent>div:first-child{gap:4px !important;padding:6px !important}#faqEditorContent>div:first-child>button{min-width:32px;height:32px;padding:0 8px;font-size:12px}#faqEditorContent>div:first-child>span[style="flex:1;"]{display:none}}`;
  document.head.append(toolbarStyle);
  const popoverStyle = document.createElement("style");
  popoverStyle.textContent = `.faq-editor-control{position:relative}.faq-editor-popover{position:absolute;z-index:30;top:calc(100% + 4px);width:290px;padding:10px 0;background:#fff;border:1px solid #cbd5e1;border-radius:8px;box-shadow:0 10px 24px rgba(15,23,42,.16);font-size:14px;color:#334155}.faq-editor-popover-heading{width:124px}.faq-editor-popover-align{width:112px}.faq-editor-popover-color{width:134px;padding:6px 0 4px}.faq-editor-popover button{width:100%;min-height:32px;padding:0 10px;border:0 !important;border-radius:0 !important;background:#fff;color:#334155;text-align:left;font-size:12px;line-height:1.2;white-space:nowrap;cursor:pointer}.faq-editor-popover button:hover{background:#f1f5f9}.faq-editor-popover button:active{background:#dbeafe;color:#1d4ed8}.faq-editor-popover button:focus-visible{outline:2px solid #2563eb;outline-offset:-2px}.faq-editor-popover button[disabled]{color:#94a3b8;background:#f8fafc;cursor:not-allowed}.faq-editor-popover button span{display:inline-block;width:24px;font-size:17px;vertical-align:middle}.faq-editor-popover button b{float:right;color:#2563eb}.faq-editor-popover-heading hr{display:block;height:1px;margin:5px 10px;border:0;background:#dbe2ec}.faq-editor-popover p{margin:0;padding:0 8px 3px;font-size:12px;color:#334155}.faq-color-row{display:grid;grid-template-columns:repeat(5,22px);gap:2px;padding:0 8px 4px}.faq-editor-popover .faq-color-row button{width:22px;height:24px;min-height:24px;padding:0;border:1px solid #e2e8f0;border-radius:3px;text-align:center;font-size:16px}.faq-editor-popover .faq-color-row button:hover{outline:2px solid #2563eb;outline-offset:1px}.faq-editor-popover label{display:flex;align-items:center;justify-content:space-between;padding:5px 12px}.faq-editor-popover input{width:72px;height:30px;border:1px solid #cbd5e1;border-radius:4px;padding:0 8px}.faq-editor-popover .faq-popover-confirm,.faq-editor-popover .faq-popover-reset{width:calc(100% - 24px);margin:4px 12px 2px;border:1px solid #dbe2ec;border-radius:4px;text-align:center}.faq-editor-popover-color .faq-popover-reset{min-height:26px;height:26px;margin:0 8px;width:calc(100% - 16px)}.faq-text-align-left{text-align:left}.faq-text-align-center{text-align:center}.faq-text-align-right{text-align:right}@media(max-width:760px){.faq-editor-popover{width:min(290px,calc(100vw - 32px))}.faq-editor-popover-heading{width:min(124px,calc(100vw - 32px))}.faq-editor-popover-align{width:min(112px,calc(100vw - 32px))}.faq-editor-popover-color{width:min(134px,calc(100vw - 32px))}}`;
  document.head.append(popoverStyle);

  function insertAlignment(alignment) {
    const selection = window.getSelection();
    const block = selection?.anchorNode?.parentElement?.closest("p,h1,h2,h3,div,blockquote,li") || visualEditor;
    block.style.textAlign = alignment;
    syncMarkdown();
    popovers.forEach(popover => { popover.hidden = true; });
  }

  function applyHeading(level) {
    execute("formatBlock", `h${level}`);
    popovers.forEach(popover => { popover.hidden = true; });
  }

  document.querySelectorAll("[data-markdown-action]").forEach(button => {
    button.addEventListener("click", () => {
      const action = button.dataset.markdownAction;
      if (action === "heading") togglePopover("heading", button);
      if (action === "align" || action === "center") togglePopover("align", button);
      if (action === "color") togglePopover("color", button);
      if (action === "table") togglePopover("table", button);
      if (action === "bold") toggleInlineFormat("**");
      if (action === "strike") toggleInlineFormat("~~");
      if (action === "italic") toggleInlineFormat("*");
      if (action === "underline") toggleInlineFormat("++");
      if (action === "highlight") toggleInlineFormat("==");
      if (action === "code") toggleInlineFormat("`");
      if (action === "quote") execute("formatBlock", "blockquote");
      if (action === "codeblock") insertHtml("<pre><code>代码内容</code></pre><p><br></p>");
      if (action === "link") {
        const url = window.prompt("请输入链接地址", "https://");
        if (url) execute("createLink", url);
      }
      if (action === "attachment") uploadInput.click();
      if (action === "import") importInput.click();
    });
  });

  document.querySelectorAll("[data-heading-level]").forEach(button => {
    button.addEventListener("click", () => applyHeading(button.dataset.headingLevel));
  });
  document.querySelectorAll("[data-list-type]").forEach(button => {
    button.addEventListener("click", () => {
      execute(button.dataset.listType === "ordered" ? "insertOrderedList" : "insertUnorderedList");
      popovers.forEach(popover => { popover.hidden = true; });
    });
  });

  document.querySelectorAll("[data-align-value]").forEach(button => {
    button.addEventListener("click", () => insertAlignment(button.dataset.alignValue));
  });
  document.querySelectorAll("[data-font-color]").forEach(button => {
    button.addEventListener("click", () => {
      insertStyledText(button.dataset.fontColor);
      popovers.forEach(popover => { popover.hidden = true; });
    });
  });
  document.querySelectorAll("[data-background-color]").forEach(button => {
    button.addEventListener("click", () => {
      insertStyledText("#0f172a", button.dataset.backgroundColor);
      popovers.forEach(popover => { popover.hidden = true; });
    });
  });
  document.getElementById("faqColorReset").addEventListener("click", () => {
    execute("removeFormat");
    popovers.forEach(popover => { popover.hidden = true; });
  });
  document.getElementById("faqTableInsert").addEventListener("click", () => {
    const columns = Math.min(10, Math.max(1, Number.parseInt(document.getElementById("faqTableColumns").value, 10) || 3));
    const rows = Math.min(20, Math.max(1, Number.parseInt(document.getElementById("faqTableRows").value, 10) || 2));
    insertTable(columns, rows);
    popovers.forEach(popover => { popover.hidden = true; });
  });
  saveButton.addEventListener("click", () => editorShell.closest("form").requestSubmit());
  clearButton.addEventListener("click", () => { clearDialog.hidden = false; });
  document.getElementById("faqClearDialogCancel").addEventListener("click", () => { clearDialog.hidden = true; });
  document.getElementById("faqClearDialogConfirm").addEventListener("click", () => {
    visualEditor.innerHTML = "<p><br></p>";
    syncMarkdown();
    clearDialog.hidden = true;
    resizeEditor();
    visualEditor.focus();
  });
  clearDialog.addEventListener("click", event => {
    if (event.target === clearDialog) clearDialog.hidden = true;
  });

  visualEditor.addEventListener("input", () => {
    syncMarkdown();
    resizeEditor();
  });
  visualEditor.addEventListener("paste", event => {
    event.preventDefault();
    const text = event.clipboardData?.getData("text/plain") || "";
    execute("insertText", text);
  });
  function showCategoryMenu() {
    categoryMenu.hidden = false;
    categoryToggle.setAttribute("aria-expanded", "true");
  }
  function hideCategoryMenu() {
    categoryMenu.hidden = true;
    categoryToggle.setAttribute("aria-expanded", "false");
  }
  function filterCategoryOptions() {
    const keyword = categoryInput.value.trim().toLocaleLowerCase();
    categoryMenu.querySelectorAll(".faq-category-option").forEach(option => {
      option.hidden = Boolean(keyword) && !option.dataset.categoryValue.toLocaleLowerCase().includes(keyword);
    });
  }
  categoryToggle.addEventListener("click", () => {
    if (categoryMenu.hidden) showCategoryMenu();
    else hideCategoryMenu();
  });
  categoryInput.addEventListener("focus", showCategoryMenu);
  categoryInput.addEventListener("input", () => {
    filterCategoryOptions();
    showCategoryMenu();
  });
  categoryMenu.addEventListener("click", event => {
    const option = event.target.closest(".faq-category-option");
    if (!option) return;
    categoryInput.value = option.dataset.categoryValue;
    hideCategoryMenu();
    categoryInput.focus();
  });
  document.addEventListener("click", event => {
    if (!event.target.closest("#faqCategoryControl")) hideCategoryMenu();
    if (!event.target.closest(".faq-editor-control")) popovers.forEach(popover => { popover.hidden = true; });
  });
  fullscreenButton.addEventListener("click", () => {
    const isFullscreen = editorShell.classList.toggle("page-fullscreen");
    fullscreenButton.textContent = isFullscreen ? "退出全屏" : "全屏";
    fullscreenButton.title = isFullscreen ? "退出全屏编辑" : "全屏编辑";
    if (isFullscreen) editorShell.scrollTop = 0;
    if (!isFullscreen) resizeEditor();
  });

  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && editorShell.classList.contains("page-fullscreen")) {
      editorShell.classList.remove("page-fullscreen");
      fullscreenButton.textContent = "全屏";
      fullscreenButton.title = "全屏编辑";
      resizeEditor();
    }
  });

  uploadInput.addEventListener("change", async () => {
    const [file] = uploadInput.files;
    if (!file) return;
    const formData = new FormData();
    formData.append("file", file);
    try {
      const response = await fetch("/admin/faq/images", { method: "POST", headers: { "X-CSRF-Token": document.querySelector('input[name="csrf_token"]').value }, body: formData });
      const attachment = await response.json();
      if (!response.ok) throw new Error(attachment.error || "附件上传失败");
      const html = attachment.contentType.startsWith("image/") ? `<img src="${attachment.contentUrl}" alt="${attachment.name}">` : `<a href="${attachment.contentUrl}" target="_blank" rel="noopener noreferrer">${attachment.name}</a>`;
      insertHtml(html);
    } catch (error) {
      alert(error.message || "附件上传失败");
    } finally {
      uploadInput.value = "";
    }
  });

  importInput.addEventListener("change", async () => {
    const [file] = importInput.files;
    if (!file) return;
    const formData = new FormData();
    formData.append("file", file);
    try {
      const response = await fetch("/admin/faq/import-content", {
        method: "POST",
        headers: { "X-CSRF-Token": document.querySelector('input[name="csrf_token"]').value },
        body: formData,
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "正文导入失败");
      textarea.value = result.content;
      renderVisualEditor();
      resizeEditor();
      visualEditor.focus();
    } catch (error) {
      alert(error.message || "正文导入失败");
    } finally {
      importInput.value = "";
    }
  });

  editorShell.closest("form").addEventListener("submit", syncMarkdown);
  renderVisualEditor();
})();
</script>'''
    return f'''<section class="card" style="max-width:1080px;margin:0 auto;padding:18px;"><div style="margin-bottom:8px;"><h2 style="margin:0;font-size:18px;">{headline} FAQ</h2></div>{error_html}<form method="post" action="{_e(action)}" style="font-size:12px;"><input type="hidden" name="{CSRF_FIELD}" value="{_e(csrf)}"><label style="font-size:12px;">标题<input name="title" value="{_e(faq.get("title", ""))}" maxlength="{MAX_TITLE}" required style="font-size:12px;height:32px;font-weight:400;"></label><div style="display:flex;align-items:end;gap:28px;flex-wrap:wrap;"><div id="faqCategoryControl" style="position:relative;font-size:12px;margin:14px 0 5px;"><label for="faqCategoryInput">分类</label><div style="position:relative;width:220px;margin-top:4px;"><input id="faqCategoryInput" name="category" value="{_e(current_category)}" maxlength="{MAX_CATEGORY}" placeholder="选择或输入新分类" autocomplete="off" style="display:block;width:100%;height:32px;border:1px solid #c7d1de;border-radius:8px;padding:0 32px 0 10px;background:#fff;color:#0f172a;font-size:12px;font-weight:400;"><button id="faqCategoryToggle" type="button" aria-label="显示分类选项" aria-expanded="false" style="position:absolute;top:3px;right:4px;width:26px;height:26px;border:0;border-radius:0;background:transparent;color:#1e3a5f;font-size:12px;font-weight:700;line-height:1;cursor:pointer;">▼</button></div><div id="faqCategoryMenu" hidden style="position:absolute;z-index:20;top:calc(100% + 2px);left:0;width:220px;max-height:194px;overflow-y:auto;border:1px solid #9ca3af;background:#fff;box-shadow:0 2px 5px rgba(15,23,42,.16);"><div class="faq-category-options">{category_options}</div></div></div><label style="font-size:12px;margin:14px 0 5px;">排序值（越小越靠前）<input type="number" name="sort_order" value="{_e(faq.get("sort_order", 0))}" min="-1000000" max="1000000" required style="width:104px;font-size:12px;height:32px;font-weight:400;"></label><label style="font-size:12px;margin:14px 0 11px;">发布到前台<input style="width:auto;margin-left:8px;vertical-align:middle;" type="checkbox" name="is_published" value="1"{checked}></label></div><label style="font-size:12px;">正文</label><div id="faqMarkdownEditor" style="border:1px solid #c7d1de;border-radius:8px;overflow:hidden;"><div id="faqEditorContent"><div style="display:flex;align-items:center;gap:2px;padding:6px;border-bottom:1px solid #dbe2ec;background:#f8fafc;flex-wrap:wrap;"><button type="button" data-markdown-action="heading" title="标题">T</button><button type="button" data-markdown-action="center" title="文字居中">≡</button><button type="button" data-markdown-action="color" title="字体颜色">A</button><button type="button" data-markdown-action="bold" title="加粗"><strong>B</strong></button><button type="button" data-markdown-action="strike" title="删除线">S</button><button type="button" data-markdown-action="italic" title="斜体"><em>I</em></button><button type="button" data-markdown-action="underline" title="下划线">U</button><button type="button" data-markdown-action="code" title="行内代码">&lt;/&gt;</button><button type="button" data-markdown-action="highlight" title="高亮">A</button><button type="button" data-markdown-action="quote" title="引用">❝</button><button type="button" data-markdown-action="link" title="链接">🔗</button><button type="button" data-markdown-action="table" title="插入表格">▦</button><button type="button" data-markdown-action="codeblock" title="代码块">{{}}</button><button type="button" data-markdown-action="attachment" title="插入图片或文件">📎</button><span style="flex:1;"></span><button type="button" data-markdown-action="import" title="导入 DOCX 或 Markdown">导入</button><button type="button" id="faqEditMode" class="active" title="可视化编辑">编辑</button><button type="button" id="faqPreviewMode" title="最终预览">预览</button><button type="button" id="faqFullscreenMode" title="全屏编辑">全屏</button></div><div id="faqEditorPanel"><div id="faqVisualEditor" contenteditable="true" role="textbox" aria-multiline="true" aria-label="FAQ 正文可视化编辑器"></div><textarea id="faqContentEditor" name="content" maxlength="{MAX_CONTENT}" required hidden>{_e(faq.get("content", ""))}</textarea></div><div id="faqPreviewPanel" hidden style="min-height:220px;padding:14px;background:#fff;line-height:1.7;"><div id="faqContentPreview"></div></div></div></div><input id="faqAttachmentInput" type="file" accept="image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp,.log,.txt,text/plain" hidden><input id="faqContentImportInput" type="file" accept=".docx,.md,.markdown,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/markdown,text/plain" hidden><p class="actions"><button type="submit">保存</button><a class="button secondary" href="/admin/faq">取消</a></p></form></section><style>#faqVisualEditor{{min-height:320px;padding:14px;outline:none;font-size:13px;line-height:1.7;overflow-wrap:anywhere}}#faqVisualEditor h1{{font-size:22px}}#faqVisualEditor h2{{font-size:18px}}#faqVisualEditor h3{{font-size:16px}}#faqVisualEditor pre{{padding:10px;border-radius:6px;background:#f1f5f9;overflow:auto}}#faqVisualEditor code{{padding:1px 4px;border-radius:4px;background:#f1f5f9}}#faqVisualEditor blockquote{{margin:8px 0;padding-left:10px;border-left:3px solid #94a3b8;color:#475569}}#faqVisualEditor img{{max-width:100%;height:auto}}#faqVisualEditor table{{width:100%;border-collapse:collapse}}#faqVisualEditor th,#faqVisualEditor td{{padding:8px;border:1px solid #cbd5e1;text-align:left}}#faqVisualEditor th{{background:#f1f5f9}}.faq-category-option{{display:block;width:100%;min-height:28px;border:0;background:#fff;color:#0f172a;padding:3px 16px;text-align:left;font-size:12px;line-height:1.4;cursor:pointer}}.faq-category-option:hover,.faq-category-option:focus{{background:#7f7f7f;color:#fff;outline:none}}#faqMarkdownEditor.page-fullscreen{{position:fixed;inset:0;z-index:1000;display:flex;align-items:flex-start;justify-content:center;padding:32px;background:rgba(241,245,249,.96);overflow-y:auto !important;overflow-x:hidden;overscroll-behavior:contain}}#faqMarkdownEditor.page-fullscreen #faqEditorContent{{width:min(1120px,100%);min-height:calc(100vh - 64px);display:flex;flex-direction:column;border:1px solid #c7d1de;border-radius:10px;background:#fff;box-shadow:0 12px 32px rgba(15,23,42,.16)}}#faqMarkdownEditor.page-fullscreen #faqEditorPanel{{flex:1;min-height:0}}#faqMarkdownEditor.page-fullscreen #faqPreviewPanel{{flex:none;min-height:0;height:auto}}#faqMarkdownEditor.page-fullscreen #faqVisualEditor{{height:100%;min-height:0}}[data-markdown-action],#faqEditMode,#faqPreviewMode,#faqFullscreenMode{{height:28px;min-width:28px;padding:0 7px;border:0;border-radius:5px;background:transparent;color:#334155;cursor:pointer;font-size:13px}}[data-markdown-action]:hover,#faqEditMode:hover,#faqPreviewMode:hover,#faqFullscreenMode:hover{{background:#e2e8f0}}#faqEditMode.active,#faqPreviewMode.active{{background:#dbeafe;color:#1d4ed8}}#faqContentPreview h1{{font-size:22px}}#faqContentPreview h2{{font-size:18px}}#faqContentPreview h3{{font-size:16px}}#faqContentPreview pre{{padding:10px;border-radius:6px;background:#f1f5f9;overflow:auto}}#faqContentPreview code{{padding:1px 4px;border-radius:4px;background:#f1f5f9}}#faqContentPreview blockquote{{margin:8px 0;padding-left:10px;border-left:3px solid #94a3b8;color:#475569}}#faqContentPreview img{{max-width:100%;height:auto}}#faqContentPreview .faq-text-center{{text-align:center}}#faqContentPreview .faq-table-wrap{{overflow-x:auto}}#faqContentPreview table{{width:100%;border-collapse:collapse}}#faqContentPreview th,#faqContentPreview td{{padding:8px;border:1px solid #cbd5e1;text-align:left}}#faqContentPreview th{{background:#f1f5f9}}</style>''' + editor_script


async def admin_list(request: Request) -> Response:
    session = _require_admin(request)
    with _db() as conn:
        rows = conn.execute("SELECT id,title,category,sort_order,is_published FROM faqs ORDER BY sort_order,id DESC").fetchall()
        # 将数据库行对象转换为字典列表
        faq_rows = [dict(row) for row in rows]
    
    display_name = session.get("display_name", session.get("username", "admin"))
    csrf_token = session.get("csrf", "")
    
    template = _TEMPLATE_ENV.get_template("admin.html")
    html = template.render(
        display_name=display_name,
        csrf_token=csrf_token,
        csrf_field=CSRF_FIELD,
        faq_rows=faq_rows
    )
    return Response(text=html, content_type="text/html")


async def admin_new(request: Request) -> Response:
    session = _require_admin(request)
    faq: dict[str, object] = {"sort_order": 0, "is_published": 0}
    error = ""
    if request.method == "POST":
        form = {key: str(value) for key, value in (await request.post()).items()}
        if not _valid_csrf(form, session):
            return _page("请求无效", '<section class="card"><h1>请求校验失败</h1></section>', admin=True, status=403)
        payload, error = _form_data(form)
        if payload is not None:
            now = int(time.time())
            with _db() as conn:
                conn.execute("INSERT INTO faqs(title,category,summary,content,sort_order,is_published,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (*payload.values(), now, now))
            raise HTTPFound("/admin/faq")
        # 验证失败时回填用户输入，避免重复填写长正文。
        faq.update({key: value for key, value in form.items() if key != CSRF_FIELD})
    return _page("新增 FAQ", _editor(faq, str(session["csrf"]), "/admin/faq/new", error), admin=True)


async def admin_edit(request: Request) -> Response:
    session = _require_admin(request)
    faq_id = _faq_id(request.match_info["faq_id"])
    with _db() as conn:
        row = conn.execute("SELECT id,title,category,summary,content,sort_order,is_published FROM faqs WHERE id=?", (faq_id,)).fetchone()
    if row is None:
        return _page("未找到 FAQ", '<section class="card"><h1>内容不存在</h1></section>', admin=True, status=404)
    faq = dict(row)
    error = ""
    if request.method == "POST":
        form = {key: str(value) for key, value in (await request.post()).items()}
        if not _valid_csrf(form, session):
            return _page("请求无效", '<section class="card"><h1>请求校验失败</h1></section>', admin=True, status=403)
        payload, error = _form_data(form)
        if payload is not None:
            with _db() as conn:
                conn.execute("UPDATE faqs SET title=?,category=?,summary=?,content=?,sort_order=?,is_published=?,updated_at=? WHERE id=?", (*payload.values(), int(time.time()), faq_id))
            raise HTTPFound("/admin/faq")
        # 验证失败时保留用户提交内容，避免编辑长正文后丢失。
        faq.update({key: value for key, value in form.items() if key != CSRF_FIELD})
    return _page("编辑 FAQ", _editor(faq, str(session["csrf"]), f"/admin/faq/{faq_id}/edit", error), admin=True)


async def admin_delete(request: Request) -> Response:
    session = _require_admin(request)
    form = {key: str(value) for key, value in (await request.post()).items()}
    if not _valid_csrf(form, session):
        return _page("请求无效", '<section class="card"><h1>请求校验失败</h1></section>', admin=True, status=403)
    with _db() as conn:
        conn.execute("DELETE FROM faqs WHERE id=?", (_faq_id(request.match_info["faq_id"]),))
    raise HTTPFound("/admin/faq")


async def admin_logout(request: Request) -> Response:
    raw = request.cookies.get(SESSION_COOKIE, "")
    if "." in raw:
        token, _signature = raw.rsplit(".", 1)
        _SESSIONS.pop(token, None)
    response = HTTPFound("/admin")
    response.del_cookie(SESSION_COOKIE, path="/")
    return response


def _active_admin_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM admin_accounts WHERE is_active=1").fetchone()
    return int(row["c"] or 0)


async def admin_accounts_list(request: Request) -> Response:
    _require_admin(request)
    with _db() as conn:
        rows = conn.execute(
            "SELECT id,username,display_name,is_active,mfa_enabled,created_at,last_login FROM admin_accounts ORDER BY id"
        ).fetchall()
    return json_response(
        [
            {
                "id": int(row["id"]),
                "username": str(row["username"]),
                "display_name": str(row["display_name"]),
                "is_active": int(row["is_active"] or 0) == 1,
                "mfa_enabled": int(row["mfa_enabled"] or 0) == 1,
                "created_at": _to_beijing_time(int(row["created_at"] or 0)),
                "last_login": _to_beijing_time(int(row["last_login"] or 0)) if int(row["last_login"] or 0) > 0 else "",
            }
            for row in rows
        ]
    )


async def admin_accounts_create(request: Request) -> Response:
    session = _require_admin(request)
    if not _csrf_header_valid(request, session):
        return json_response({"error": "forbidden"}, status=403)
    payload = await request.json()
    username = str(payload.get("username", "")).strip()
    display_name = str(payload.get("display_name", "")).strip() or username
    password = str(payload.get("password", ""))
    if len(username) < 3:
        return json_response({"error": "账号长度至少 3 位"}, status=400)
    if len(password) < MIN_ADMIN_PASSWORD:
        return json_response({"error": f"密码长度至少 {MIN_ADMIN_PASSWORD} 位"}, status=400)
    now = int(time.time())
    with _db() as conn:
        exists = conn.execute("SELECT 1 FROM admin_accounts WHERE username=?", (username,)).fetchone()
        if exists is not None:
            return json_response({"error": "管理员账号已存在"}, status=400)
        conn.execute(
            "INSERT INTO admin_accounts(username,display_name,password_hash,is_active,mfa_enabled,created_at,updated_at,last_login) VALUES(?,?,?,?,?,?,?,?)",
            (username, display_name[:120], _hash_password(password), 1, 0, now, now, 0),
        )
    return json_response({"ok": True})


async def admin_accounts_update(request: Request) -> Response:
    session = _require_admin(request)
    if not _csrf_header_valid(request, session):
        return json_response({"error": "forbidden"}, status=403)
    admin_id = _faq_id(request.match_info["admin_id"])
    payload = await request.json()
    username = str(payload.get("username", "")).strip()
    display_name = str(payload.get("display_name", "")).strip() or username
    is_active = bool(payload.get("is_active", True))
    if len(username) < 3:
        return json_response({"error": "账号长度至少 3 位"}, status=400)
    with _db() as conn:
        row = conn.execute("SELECT id,is_active FROM admin_accounts WHERE id=?", (admin_id,)).fetchone()
        if row is None:
            return json_response({"error": "管理员账号不存在"}, status=404)
        duplicate = conn.execute("SELECT id FROM admin_accounts WHERE username=? AND id<>?", (username, admin_id)).fetchone()
        if duplicate is not None:
            return json_response({"error": "管理员账号已存在"}, status=400)
        if not is_active and int(row["is_active"] or 0) == 1 and _active_admin_count(conn) <= 1:
            return json_response({"error": "至少保留一个启用管理员"}, status=400)
        conn.execute(
            "UPDATE admin_accounts SET username=?,display_name=?,is_active=?,updated_at=? WHERE id=?",
            (username, display_name[:120], int(is_active), int(time.time()), admin_id),
        )
    return json_response({"ok": True})


async def admin_accounts_change_password(request: Request) -> Response:
    session = _require_admin(request)
    if not _csrf_header_valid(request, session):
        return json_response({"error": "forbidden"}, status=403)
    admin_id = _faq_id(request.match_info["admin_id"])
    payload = await request.json()
    password = str(payload.get("password", ""))
    if len(password) < MIN_ADMIN_PASSWORD:
        return json_response({"error": f"密码长度至少 {MIN_ADMIN_PASSWORD} 位"}, status=400)
    with _db() as conn:
        row = conn.execute("SELECT id FROM admin_accounts WHERE id=?", (admin_id,)).fetchone()
        if row is None:
            return json_response({"error": "管理员账号不存在"}, status=404)
        conn.execute(
            "UPDATE admin_accounts SET password_hash=?,updated_at=? WHERE id=?",
            (_hash_password(password), int(time.time()), admin_id),
        )
    return json_response({"ok": True})


async def admin_mfa_setup(request: Request) -> Response:
    """GET: 生成 MFA 绑定密钥，任意登录管理员均可操作。"""
    session = _require_admin(request)
    if not _csrf_header_valid(request, session):
        return json_response({"error": "forbidden"}, status=403)
    admin_id = _faq_id(request.match_info["admin_id"])
    with _db() as conn:
        row = conn.execute("SELECT id,username,is_active FROM admin_accounts WHERE id=?", (admin_id,)).fetchone()
        if row is None:
            return json_response({"error": "管理员账号不存在"}, status=404)
        if int(row["is_active"] or 0) != 1:
            return json_response({"error": "当前账号已禁用，无法配置MFA"}, status=400)
    secret = _generate_totp_secret()
    expire_at = int(time.time()) + ADMIN_MFA_PENDING_TTL
    session["admin_mfa_pending"] = {
        "account_id": admin_id,
        "secret": secret,
        "expire_at": expire_at,
    }
    issuer = "FAQ Admin"
    label = f"{issuer}:{row['username']}"
    otpauth_uri = (
        f"otpauth://totp/{quote(label)}"
        f"?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"
    )
    return json_response({
        "username": str(row["username"]),
        "secret": secret,
        "otpauth_uri": otpauth_uri,
        "expire_seconds": ADMIN_MFA_PENDING_TTL,
    })


async def admin_mfa_enable(request: Request) -> Response:
    """POST: 校验 OTP 并启用 MFA，任意登录管理员均可操作。"""
    session = _require_admin(request)
    if not _csrf_header_valid(request, session):
        return json_response({"error": "forbidden"}, status=403)
    admin_id = _faq_id(request.match_info["admin_id"])
    payload = await request.json()
    otp_code = str(payload.get("otp_code", "")).strip()
    if not otp_code:
        return json_response({"error": "请输入OTP验证码"}, status=400)
    pending = session.get("admin_mfa_pending") or {}
    if _as_int(pending.get("account_id"), 0) != admin_id:
        return json_response({"error": "未找到有效的MFA绑定会话，请重新发起绑定"}, status=400)
    if int(time.time()) > _as_int(pending.get("expire_at"), 0):
        session.pop("admin_mfa_pending", None)
        return json_response({"error": "MFA绑定会话已过期，请重新发起绑定"}, status=400)
    secret = str(pending.get("secret") or "")
    if not _totp(secret, otp_code):
        return json_response({"error": "OTP验证码错误，请重试"}, status=400)
    with _db() as conn:
        row = conn.execute("SELECT id FROM admin_accounts WHERE id=?", (admin_id,)).fetchone()
        if row is None:
            return json_response({"error": "管理员账号不存在"}, status=404)
        conn.execute(
            "UPDATE admin_accounts SET mfa_enabled=1,mfa_secret=?,updated_at=? WHERE id=?",
            (secret, int(time.time()), admin_id),
        )
    session.pop("admin_mfa_pending", None)
    return json_response({"ok": True})


async def admin_mfa_disable(request: Request) -> Response:
    """POST: 禁用 MFA 并清除密钥，任意登录管理员均可操作。"""
    session = _require_admin(request)
    if not _csrf_header_valid(request, session):
        return json_response({"error": "forbidden"}, status=403)
    admin_id = _faq_id(request.match_info["admin_id"])
    with _db() as conn:
        row = conn.execute("SELECT id,mfa_enabled FROM admin_accounts WHERE id=?", (admin_id,)).fetchone()
        if row is None:
            return json_response({"error": "管理员账号不存在"}, status=404)
        if int(row["mfa_enabled"] or 0) != 1:
            return json_response({"error": "当前账号未启用MFA，无需删除"}, status=400)
        conn.execute(
            "UPDATE admin_accounts SET mfa_enabled=0,mfa_secret=NULL,updated_at=? WHERE id=?",
            (int(time.time()), admin_id),
        )
    session.pop("admin_mfa_pending", None)
    return json_response({"ok": True})


async def admin_accounts_delete(request: Request) -> Response:
    session = _require_admin(request)
    if not _csrf_header_valid(request, session):
        return json_response({"error": "forbidden"}, status=403)
    admin_id = _faq_id(request.match_info["admin_id"])
    current_admin_id = _as_int(session.get("admin_id", 0))
    if admin_id == current_admin_id:
        return json_response({"error": "不能删除当前登录账号"}, status=400)
    with _db() as conn:
        row = conn.execute("SELECT id,is_active FROM admin_accounts WHERE id=?", (admin_id,)).fetchone()
        if row is None:
            return json_response({"error": "管理员账号不存在"}, status=404)
        if int(row["is_active"] or 0) == 1 and _active_admin_count(conn) <= 1:
            return json_response({"error": "至少保留一个启用管理员"}, status=400)
        conn.execute("DELETE FROM admin_accounts WHERE id=?", (admin_id,))
    return json_response({"ok": True})


async def admin_logs(request: Request) -> Response:
    _require_admin(request)
    cutoff = int(time.time()) - LOG_RETENTION_DAYS * 86400
    with _db() as conn:
        rows = conn.execute(
            "SELECT created_at,ip_address,login_account,os_version,browser_version,question_content FROM webchat_question_logs WHERE created_at >= ? ORDER BY id DESC LIMIT 500",
            (cutoff,),
        ).fetchall()
    return json_response(
        [
            {
                "access_time": _to_beijing_time(int(row["created_at"] or 0)),
                "ip_address": str(row["ip_address"] or ""),
                "login_account": str(row["login_account"] or ""),
                "os_version": str(row["os_version"] or ""),
                "browser_version": str(row["browser_version"] or ""),
                "question_content": str(row["question_content"] or ""),
            }
            for row in rows
        ]
    )


async def admin_webchat_access_list(request: Request) -> Response:
    _require_admin(request)
    with _db() as conn:
        rows = conn.execute(
            "SELECT id,tenant_id,object_id,username,display_name,status,approved_at,denial_reason,created_at,updated_at FROM webchat_access_grants ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,updated_at DESC"
        ).fetchall()
    return json_response([
        {
            "id": int(row["id"]),
            "username": str(row["username"]),
            "display_name": str(row["display_name"]),
            "status": str(row["status"]),
            "denial_reason": str(row["denial_reason"] or ""),
            "created_at": _to_beijing_time(int(row["created_at"])),
            "updated_at": _to_beijing_time(int(row["updated_at"])),
        }
        for row in rows
    ])


async def admin_webchat_access_update(request: Request) -> Response:
    session = _require_admin(request)
    if not _csrf_header_valid(request, session):
        return json_response({"error": "forbidden"}, status=403)
    payload = await request.json()
    action = str(payload.get("action", "")).strip()
    grant_ids = payload.get("grant_ids", [])
    denial_reason = str(payload.get("denial_reason", "")).strip()
    if action not in {"approve", "deny", "restore", "revoke"}:
        return json_response({"error": "无效的授权操作"}, status=400)
    if action == "deny" and not denial_reason:
        return json_response({"error": "请填写拒绝理由"}, status=400)
    if len(denial_reason) > 500:
        return json_response({"error": "拒绝理由不能超过 500 个字符"}, status=400)
    if not isinstance(grant_ids, list) or not grant_ids or len(grant_ids) > 500:
        return json_response({"error": "请选择要操作的用户"}, status=400)
    ids = sorted({_as_int(value, 0) for value in grant_ids if _as_int(value, 0) > 0})
    if not ids:
        return json_response({"error": "请选择有效用户"}, status=400)
    status = "approved" if action in {"approve", "restore"} else ("denied" if action == "deny" else "revoked")
    now = int(time.time())
    placeholders = ",".join("?" for _ in ids)
    with _db() as conn:
        conn.execute(
            f"UPDATE webchat_access_grants SET status=?,approved_by_admin_id=?,approved_at=?,denial_reason=?,updated_at=? WHERE id IN ({placeholders})",
            (status, _as_int(session.get("admin_id", 0), 0), now if status == "approved" else 0, denial_reason if status == "denied" else "", now, *ids),
        )
    return json_response({"ok": True, "updated": len(ids)})
