import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from microsoft_agents.hosting.aiohttp import CloudAdapter
from microsoft_agents.hosting.core import AgentApplication, MemoryStorage, TurnState

import faq
import start_server


class ApplicationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = AgentApplication[TurnState](
            storage=MemoryStorage(),
            adapter=CloudAdapter(),
        )

    def _create_application(self):
        with patch.object(start_server, "initialize_database"):
            return start_server.create_application(self.agent, None)

    def test_public_route_contract(self) -> None:
        application = self._create_application()
        resources = {
            resource.canonical
            for resource in application.router.resources()
        }
        self.assertIn("/", resources)
        self.assertIn("/webchat", resources)
        self.assertIn("/faq/directory", resources)
        self.assertIn("/faq/{faq_id}", resources)
        self.assertIn("/admin", resources)
        self.assertNotIn("/faq", resources)

    def test_agent_api_uses_anonymous_configuration_by_default(self) -> None:
        application = self._create_application()
        agent_application = next(iter(application._subapps))
        configuration = agent_application["agent_configuration"]
        self.assertTrue(configuration.ANONYMOUS_ALLOWED)

    def test_faq_markdown_renders_editor_extended_formatting(self) -> None:
        rendered, _headings = faq._render_markdown(
            ":::align:center\n居中文字\n:::\n\n{{style:#2563eb:#fef08a|蓝色文字}}\n\n| 名称 | 值 |\n| --- | --- |\n| 示例 | 内容 |"
        )
        self.assertIn('class="faq-text-align-center">居中文字', rendered)
        self.assertIn('<span style="color:#2563eb;background-color:#fef08a">蓝色文字</span>', rendered)
        self.assertIn("<table>", rendered)
        self.assertIn("<th>名称</th>", rendered)
        self.assertIn("<td>内容</td>", rendered)

    def test_faq_editor_contains_extended_formatting_actions(self) -> None:
        with patch.object(faq, "_db") as database:
            database.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = []
            editor = faq._editor({}, "csrf-token", "/admin/faq/new")
        self.assertIn('data-markdown-action="center"', editor)
        self.assertIn('data-markdown-action="color"', editor)
        self.assertIn('data-markdown-action="table"', editor)
        self.assertIn('createPopover("align"', editor)
        self.assertIn('createPopover("color"', editor)
        self.assertIn('createPopover("table"', editor)
        self.assertIn('createPopover("heading"', editor)
        self.assertIn('data-heading-level="1"', editor)
        self.assertIn('data-heading-level="2"', editor)
        self.assertIn('data-heading-level="3"', editor)
        self.assertIn('function applyHeading(level)', editor)
        self.assertIn('id="faqVisualEditor" contenteditable="true"', editor)
        self.assertIn('function renderVisualEditor()', editor)
        self.assertIn('function syncMarkdown()', editor)
        self.assertIn('function serializeNode(node)', editor)
        self.assertIn('editorShell.closest("form").addEventListener("submit", syncMarkdown)', editor)
        self.assertNotIn('function insertHeading(level)', editor)
        self.assertIn('function toggleInlineFormat(marker)', editor)
        self.assertIn('function insertStyledText(color, background = "")', editor)
        self.assertIn('visualEditor.addEventListener("input"', editor)
        self.assertIn('visualEditor.addEventListener("paste"', editor)
        self.assertIn('fetch("/admin/faq/images"', editor)
        self.assertNotIn('fetch("/webchat/upload", { method: "POST", body: formData })', editor)
        self.assertNotIn('`${["一", "二", "三"][Number(level) - 1]}级标题`', editor)
        self.assertNotIn('"列表项"', editor)
        self.assertNotIn('data-heading-level="0"', editor)
        self.assertNotIn('其他标题', editor)
        self.assertIn('data-list-type="ordered"', editor)
        self.assertIn('data-list-type="bullet"', editor)
        self.assertNotIn('增加缩进', editor)
        self.assertNotIn('减少缩进', editor)
        self.assertIn('faq-editor-popover-heading{width:124px}', editor)
        self.assertIn('faq-editor-popover-align{width:112px}', editor)
        self.assertIn('faq-editor-popover-color{width:134px;padding:6px 0 4px}', editor)
        self.assertIn('min-height:32px;padding:0 10px', editor)
        self.assertIn('font-size:12px;line-height:1.2', editor)
        self.assertIn('white-space:nowrap', editor)
        self.assertIn('"#c00000", "#ff0000", "#ffc000", "#ffff00"', editor)
        self.assertIn('"#92d050", "#00b050", "#00b0f0", "#0070c0", "#002060", "#7030a0"', editor)
        self.assertIn('"#ffff00", "#00ff00", "#00ffff", "#ff00ff", "#0000ff"', editor)
        self.assertIn('"#ff0000", "#000080", "#008080", "#008000", "#800080"', editor)
        self.assertIn('"#800000", "#808000", "#808080", "#c0c0c0", "#000000"', editor)
        self.assertIn('.faq-color-row{display:grid;grid-template-columns:repeat(5,22px);gap:2px;padding:0 8px 4px}', editor)
        self.assertIn('.faq-editor-popover-color .faq-popover-reset{min-height:26px;height:26px;margin:0 8px;width:calc(100% - 16px)}', editor)
        self.assertIn('width:22px;height:24px;min-height:24px', editor)
        self.assertIn('#faqVisualEditor{min-height:320px', editor)
        self.assertIn('Math.max(320, visualEditor.scrollHeight)', editor)
        self.assertIn('#faqEditorContent>div:first-child>button', editor)
        self.assertIn('.faq-editor-popover button{width:100%;min-height:32px;padding:0 10px;border:0 !important;border-radius:0 !important', editor)
        self.assertIn('.faq-editor-popover-heading hr{display:block;height:1px;margin:5px 10px', editor)
        self.assertNotIn('data-markdown-action="bullet"', editor)
        self.assertNotIn('data-markdown-action="ordered"', editor)
        self.assertIn('[data-markdown-action="color"]{border-color:#ca8a04;background:#facc15', editor)
        self.assertIn('min-width:34px;height:34px;padding:0 9px', editor)
        self.assertIn('button:disabled', editor)
        self.assertIn('@media(max-width:760px)', editor)
        self.assertIn('target.style.left = `${trigger.offsetLeft}px`', editor)
        self.assertIn('faqClearContent', editor)
        self.assertIn('faqToolbarSave', editor)
        self.assertIn('toolbar.insertBefore(saveButton, clearButton.nextSibling)', editor)
        self.assertIn('editorShell.closest("form").requestSubmit()', editor)
        self.assertIn('faqClearDialog', editor)
        self.assertIn('faq-clear-dialog-card', editor)
        self.assertIn('position:fixed;z-index:1100;inset:0;display:grid;place-items:center', editor)
        self.assertIn('faqClearDialogConfirm', editor)
        self.assertNotIn('window.confirm(', editor)

    def test_docx_import_preserves_supported_formatting(self) -> None:
        document_xml = '''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>标题</w:t></w:r></w:p>
<w:p><w:r><w:rPr><w:b/><w:i/><w:strike/><w:color w:val="FF0000"/><w:shd w:fill="FFFF00"/></w:rPr><w:t>格式文本</w:t><w:br/><w:t>续行</w:t></w:r></w:p>
<w:p><w:hyperlink r:id="rId1"><w:r><w:t>链接</w:t></w:r></w:hyperlink></w:p>
</w:body></w:document>'''.encode("utf-8")
        relations_xml = '''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="https://example.com"/></Relationships>'''.encode("utf-8")
        buffer = BytesIO()
        with ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", document_xml)
            archive.writestr("word/_rels/document.xml.rels", relations_xml)
        markdown = faq._docx_to_markdown("format.docx", buffer.getvalue())
        self.assertIn("# 标题", markdown)
        self.assertIn("{{style:#FF0000:#FFFF00|~~***格式文本  \n续行***~~}}", markdown)
        self.assertIn("[链接](https://example.com)", markdown)

    def test_faq_markdown_allows_dedicated_image_path(self) -> None:
        rendered, _headings = faq._render_markdown("![示例](/faq/images/example.png)")
        self.assertIn('<img src="/faq/images/example.png" alt="示例">', rendered)

    def test_faq_markdown_rejects_external_and_legacy_image_paths(self) -> None:
        rendered, _headings = faq._render_markdown(
            "![外部](https://example.com/image.png)\n\n![历史](/webchat/uploads/image.png)"
        )
        self.assertNotIn("example.com", rendered)
        self.assertNotIn("/webchat/uploads/", rendered)

    def test_cleanup_expired_webchat_uploads_uses_configured_retention(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            upload_directory = Path(temporary_directory)
            expired_file = upload_directory / "expired.png"
            active_file = upload_directory / "active.png"
            expired_file.write_bytes(b"expired")
            active_file.write_bytes(b"active")
            current_time = time.time()
            expired_time = current_time - 11 * 24 * 60 * 60
            active_time = current_time - 9 * 24 * 60 * 60
            expired_file.touch()
            active_file.touch()
            os.utime(expired_file, (expired_time, expired_time))
            os.utime(active_file, (active_time, active_time))
            with (
                patch.object(start_server, "WEBCHAT_UPLOAD_DIR", upload_directory),
                patch.dict(start_server.environ, {"WEBCHAT_UPLOAD_RETENTION_DAYS": "10"}),
            ):
                self.assertEqual(1, start_server._cleanup_expired_webchat_uploads(current_time))
            self.assertFalse(expired_file.exists())
            self.assertTrue(active_file.exists())


class AgentApiContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_health_endpoint_returns_success(self) -> None:
        agent = AgentApplication[TurnState](
            storage=MemoryStorage(),
            adapter=CloudAdapter(),
        )
        with patch.object(start_server, "initialize_database"):
            application = start_server.create_application(agent, None)
        async with TestClient(TestServer(application)) as client:
            response = await client.get("/api/messages")
            self.assertEqual(200, response.status)


class WebchatAccessContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "faq.db"
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                connection.execute(
                    """CREATE TABLE webchat_access_grants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    approved_by_admin_id INTEGER,
                    approved_at INTEGER NOT NULL DEFAULT 0,
                    denial_reason TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(tenant_id, object_id)
                )"""
                )
        self.user = {
            "tenantId": "tenant-1",
            "objectId": "object-1",
            "username": "user@example.com",
            "name": "测试用户",
        }

    def _ensure_access(self, current_time: int) -> tuple[str, str]:
        with (
            patch.object(faq, "DB_PATH", self.database_path),
            patch.object(faq.time, "time", return_value=current_time),
        ):
            return faq.ensure_webchat_access(self.user)

    def _get_access_row(self) -> sqlite3.Row:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM webchat_access_grants WHERE tenant_id=? AND object_id=?",
                ("tenant-1", "object-1"),
            ).fetchone()
        self.assertIsNotNone(row)
        return row

    def test_first_access_creates_pending_grant(self) -> None:
        self.assertEqual(("pending", ""), self._ensure_access(100))
        row = self._get_access_row()
        self.assertEqual("pending", row["status"])
        self.assertEqual(100, row["created_at"])
        self.assertEqual(100, row["updated_at"])

    def test_stable_identity_does_not_update_timestamp(self) -> None:
        self._ensure_access(100)
        self.assertEqual(("pending", ""), self._ensure_access(200))
        self.assertEqual(100, self._get_access_row()["updated_at"])

    def test_identity_change_preserves_decision_and_updates_profile(self) -> None:
        self._ensure_access(100)
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                connection.execute(
                    "UPDATE webchat_access_grants SET status='denied',denial_reason='拒绝原因'"
                )
        self.user["username"] = "renamed@example.com"
        self.user["name"] = "新名称"

        self.assertEqual(("denied", "拒绝原因"), self._ensure_access(300))
        row = self._get_access_row()
        self.assertEqual("renamed@example.com", row["username"])
        self.assertEqual("新名称", row["display_name"])
        self.assertEqual(300, row["updated_at"])
        self.assertEqual("denied", row["status"])
        self.assertEqual("拒绝原因", row["denial_reason"])


class CoreHttpRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "faq.db"
        self.agent = AgentApplication[TurnState](
            storage=MemoryStorage(),
            adapter=CloudAdapter(),
        )
        start_server._LOCAL_CONVERSATIONS.clear()

    def _create_application(self):
        with patch.object(faq, "DB_PATH", self.database_path):
            return start_server.create_application(self.agent, None)

    async def test_local_webchat_start_send_and_poll(self) -> None:
        with (
            patch.object(faq, "DB_PATH", self.database_path),
            patch.object(start_server, "_entra_enabled", return_value=False),
            patch.object(start_server, "_entra_china_enabled", return_value=False),
            patch.object(start_server, "_get_webchat_backend", return_value="local"),
        ):
            application = self._create_application()
            async with TestClient(TestServer(application)) as client:
                page_response = await client.get("/webchat")
                self.assertEqual(200, page_response.status)
                self.assertIn("Copilot Studio Web Chat", await page_response.text())

                config_response = await client.get("/webchat/config")
                self.assertEqual(200, config_response.status)
                self.assertEqual("local", (await config_response.json())["backendMode"])

                start_response = await client.post("/webchat/start", json={})
                self.assertEqual(200, start_response.status)
                conversation_id = (await start_response.json())["conversationId"]

                send_response = await client.post(
                    "/webchat/send",
                    json={
                        "conversationId": conversation_id,
                        "text": "测试问题",
                        "userId": "test-user",
                        "userName": "测试用户",
                    },
                )
                self.assertEqual(200, send_response.status)

                poll_response = await client.post(
                    "/webchat/poll",
                    json={"conversationId": conversation_id, "watermark": "0"},
                )
                self.assertEqual(200, poll_response.status)
                poll_data = await poll_response.json()
                self.assertEqual("3", poll_data["watermark"])
                self.assertEqual(
                    [
                        "Welcome to the Echo Agent sample. Type /help for help or send a message to see the echo feature in action.",
                        "测试问题",
                        "you said: 测试问题",
                    ],
                    [activity["text"] for activity in poll_data["activities"]],
                )

                next_poll_response = await client.post(
                    "/webchat/poll",
                    json={"conversationId": conversation_id, "watermark": "3"},
                )
                next_poll_data = await next_poll_response.json()
                self.assertEqual([], next_poll_data["activities"])
                self.assertEqual("3", next_poll_data["watermark"])

        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                "SELECT question_content FROM webchat_question_logs"
            ).fetchone()
        self.assertEqual("测试问题", row[0])

    async def test_webchat_input_validation(self) -> None:
        with (
            patch.object(faq, "DB_PATH", self.database_path),
            patch.object(start_server, "_entra_enabled", return_value=False),
            patch.object(start_server, "_entra_china_enabled", return_value=False),
            patch.object(start_server, "_get_webchat_backend", return_value="local"),
        ):
            application = self._create_application()
            async with TestClient(TestServer(application)) as client:
                missing_conversation = await client.post(
                    "/webchat/send",
                    json={"text": "测试"},
                )
                self.assertEqual(400, missing_conversation.status)

                empty_message = await client.post(
                    "/webchat/send",
                    json={"conversationId": "conversation-1", "text": ""},
                )
                self.assertEqual(400, empty_message.status)

                missing_poll_conversation = await client.post(
                    "/webchat/poll",
                    json={},
                )
                self.assertEqual(400, missing_poll_conversation.status)

    async def test_webchat_template_uses_module_cache(self) -> None:
        with (
            patch.object(faq, "DB_PATH", self.database_path),
            patch.object(start_server, "_entra_enabled", return_value=False),
            patch.object(start_server, "_entra_china_enabled", return_value=False),
        ):
            application = self._create_application()
            with patch.object(
                Path,
                "read_text",
                side_effect=AssertionError("请求期间不应重复读取模板"),
            ):
                async with TestClient(TestServer(application)) as client:
                    responses = [await client.get("/webchat") for _ in range(100)]
                    self.assertTrue(
                        all(response.status == 200 for response in responses)
                    )

    def test_webchat_clear_history_uses_centered_themed_dialog(self) -> None:
        template = (Path(__file__).parent.parent / "templates" / "webchat.html").read_text(encoding="utf-8")
        self.assertIn('id="clearDialogBackdrop"', template)
        self.assertIn(".clear-dialog-backdrop", template)
        self.assertIn("html.dark .clear-dialog", template)
        self.assertIn("await confirmClearHistory()", template)
        self.assertNotIn('confirm("确定要清空所有对话记录和本地数据吗？")', template)

    def test_webchat_uploads_are_disabled_by_default_and_can_be_enabled(self) -> None:
        template = (Path(__file__).parent.parent / "templates" / "webchat.html").read_text(encoding="utf-8")
        self.assertIn("__WEBCHAT_UPLOAD_BUTTON_HIDDEN__", template)
        self.assertIn(".composer-tools[__WEBCHAT_UPLOAD_BUTTON_HIDDEN__]", template)
        self.assertIn(".composer:has(.composer-tools[hidden])", template)
        self.assertIn('aria-hidden="true" hidden', template.replace("__WEBCHAT_UPLOAD_BUTTON_HIDDEN__", "hidden"))
        with patch.dict(start_server.environ, {}, clear=True):
            self.assertFalse(start_server._webchat_uploads_enabled())
            self.assertFalse(start_server._webchat_upload_button_visible())
        with patch.dict(start_server.environ, {"WEBCHAT_UPLOADS_ENABLED": "true"}, clear=True):
            self.assertTrue(start_server._webchat_uploads_enabled())
            self.assertFalse(start_server._webchat_upload_button_visible())
        with patch.dict(start_server.environ, {"WEBCHAT_UPLOAD_BUTTON_VISIBLE": "true"}, clear=True):
            self.assertTrue(start_server._webchat_upload_button_visible())

    async def test_faq_directory_detail_and_visit_log(self) -> None:
        with (
            patch.object(faq, "DB_PATH", self.database_path),
            patch.object(start_server, "_entra_enabled", return_value=False),
            patch.object(start_server, "_entra_china_enabled", return_value=False),
            patch.object(start_server, "_approved_webchat_user", return_value=({"username": "user@example.com"}, "approved", "")),
        ):
            application = self._create_application()
            with faq._db() as connection:
                connection.execute(
                    "INSERT INTO faqs(title,category,summary,content,sort_order,is_published,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    ("公开问题", "测试分类", "摘要", "# 概览\n正文内容\n\n## 操作步骤\n具体步骤", 1, 1, 100, 100),
                )
                connection.execute(
                    "INSERT INTO faqs(title,category,summary,content,sort_order,is_published,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    ("草稿问题", "测试分类", "摘要", "草稿内容", 2, 0, 100, 100),
                )

            async with TestClient(TestServer(application)) as client:
                directory_response = await client.get("/faq/directory")
                self.assertEqual(200, directory_response.status)
                directory = await directory_response.json()
                self.assertEqual(1, len(directory))
                self.assertEqual("公开问题", directory[0]["title"])

                detail_response = await client.get(f"/faq/{directory[0]['id']}")
                self.assertEqual(200, detail_response.status)
                detail_html = await detail_response.text()
                self.assertIn("公开问题", detail_html)
                self.assertIn("正文内容", detail_html)
                self.assertIn('aria-label="内容大纲"', detail_html)
                self.assertIn('href="#faq-heading-1"', detail_html)
                self.assertIn('id="faq-heading-2"', detail_html)

                missing_response = await client.get("/faq/99999")
                self.assertEqual(404, missing_response.status)

            with faq._db() as connection:
                log_row = connection.execute(
                    "SELECT question_content FROM webchat_question_logs"
                ).fetchone()
            self.assertEqual("访问常见问题：公开问题", log_row["question_content"])

    async def test_faq_detail_requires_approved_user(self) -> None:
        with (
            patch.object(faq, "DB_PATH", self.database_path),
            patch.object(start_server, "_approved_webchat_user", return_value=(None, "", "")),
        ):
            application = self._create_application()
            async with TestClient(TestServer(application)) as client:
                response = await client.get("/faq/1")
                self.assertEqual(200, response.status)
                self.assertIn("身份验证 - 智能助手", await response.text())


class AdminAccessRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "faq.db"
        self.agent = AgentApplication[TurnState](
            storage=MemoryStorage(),
            adapter=CloudAdapter(),
        )
        faq._SESSIONS.clear()

    def _create_application(self):
        with patch.object(faq, "DB_PATH", self.database_path):
            application = start_server.create_application(self.agent, None)
        with faq._db() as connection:
            now = 100
            cursor = connection.execute(
                "INSERT INTO admin_accounts(username,display_name,password_hash,is_active,mfa_enabled,created_at,updated_at,last_login) VALUES(?,?,?,?,?,?,?,?)",
                ("admin", "测试管理员", faq._hash_password("test-password"), 1, 0, now, now, 0),
            )
            self.admin_id = int(cursor.lastrowid)
            grant_cursor = connection.execute(
                "INSERT INTO webchat_access_grants(tenant_id,object_id,username,display_name,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                ("tenant-1", "object-1", "user@example.com", "测试用户", "pending", now, now),
            )
            self.grant_id = int(grant_cursor.lastrowid)
        self.session_token = "admin-session-token"
        self.csrf_token = "csrf-token"
        faq._SESSIONS[self.session_token] = {
            "csrf": self.csrf_token,
            "expires": faq.time.time() + 3600,
            "admin_id": self.admin_id,
            "username": "admin",
            "display_name": "测试管理员",
            "role": "admin",
        }
        return application

    async def test_access_management_requires_admin_and_csrf(self) -> None:
        with patch.object(faq, "DB_PATH", self.database_path):
            application = self._create_application()
            async with TestClient(TestServer(application)) as client:
                anonymous_response = await client.get(
                    "/admin/webchat-access",
                    allow_redirects=False,
                )
                self.assertEqual(302, anonymous_response.status)
                self.assertEqual("/admin/login", anonymous_response.headers["Location"])

                client.session.cookie_jar.update_cookies(
                    {
                        faq.SESSION_COOKIE: (
                            f"{self.session_token}.{faq._sign(self.session_token)}"
                        )
                    }
                )
                list_response = await client.get("/admin/webchat-access")
                self.assertEqual(200, list_response.status)
                self.assertEqual("pending", (await list_response.json())[0]["status"])

                no_csrf_response = await client.post(
                    "/admin/webchat-access",
                    json={"action": "approve", "grant_ids": [self.grant_id]},
                )
                self.assertEqual(403, no_csrf_response.status)

    async def test_access_state_transitions_and_denial_reason_validation(self) -> None:
        with patch.object(faq, "DB_PATH", self.database_path):
            application = self._create_application()
            async with TestClient(TestServer(application)) as client:
                client.session.cookie_jar.update_cookies(
                    {
                        faq.SESSION_COOKIE: (
                            f"{self.session_token}.{faq._sign(self.session_token)}"
                        )
                    }
                )
                headers = {"X-CSRF-Token": self.csrf_token}

                missing_reason = await client.post(
                    "/admin/webchat-access",
                    headers=headers,
                    json={"action": "deny", "grant_ids": [self.grant_id]},
                )
                self.assertEqual(400, missing_reason.status)

                denied_response = await client.post(
                    "/admin/webchat-access",
                    headers=headers,
                    json={
                        "action": "deny",
                        "grant_ids": [self.grant_id],
                        "denial_reason": "测试拒绝理由",
                    },
                )
                self.assertEqual(200, denied_response.status)
                with faq._db() as connection:
                    denied_row = connection.execute(
                        "SELECT status,denial_reason,approved_by_admin_id FROM webchat_access_grants WHERE id=?",
                        (self.grant_id,),
                    ).fetchone()
                self.assertEqual("denied", denied_row["status"])
                self.assertEqual("测试拒绝理由", denied_row["denial_reason"])
                self.assertEqual(self.admin_id, denied_row["approved_by_admin_id"])

                restored_response = await client.post(
                    "/admin/webchat-access",
                    headers=headers,
                    json={"action": "restore", "grant_ids": [self.grant_id]},
                )
                self.assertEqual(200, restored_response.status)
                with faq._db() as connection:
                    restored_row = connection.execute(
                        "SELECT status,denial_reason,approved_at FROM webchat_access_grants WHERE id=?",
                        (self.grant_id,),
                    ).fetchone()
                self.assertEqual("approved", restored_row["status"])
                self.assertEqual("", restored_row["denial_reason"])
                self.assertGreater(restored_row["approved_at"], 0)

                revoked_response = await client.post(
                    "/admin/webchat-access",
                    headers=headers,
                    json={"action": "revoke", "grant_ids": [self.grant_id]},
                )
                self.assertEqual(200, revoked_response.status)
                with faq._db() as connection:
                    revoked_row = connection.execute(
                        "SELECT status,approved_at FROM webchat_access_grants WHERE id=?",
                        (self.grant_id,),
                    ).fetchone()
                self.assertEqual("revoked", revoked_row["status"])
                self.assertEqual(0, revoked_row["approved_at"])


class SecurityBoundaryRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "faq.db"
        self.upload_directory = Path(self.temporary_directory.name) / "uploads"
        self.agent = AgentApplication[TurnState](
            storage=MemoryStorage(),
            adapter=CloudAdapter(),
        )
        faq._SESSIONS.clear()

    def _create_application(self):
        with patch.object(faq, "DB_PATH", self.database_path):
            return start_server.create_application(self.agent, None)

    async def test_entra_access_http_contracts(self) -> None:
        with (
            patch.object(faq, "DB_PATH", self.database_path),
            patch.object(start_server, "_entra_enabled", return_value=True),
            patch.object(start_server, "_entra_china_enabled", return_value=False),
        ):
            application = self._create_application()
            async with TestClient(TestServer(application)) as client:
                login_page = await client.get("/webchat")
                self.assertEqual(200, login_page.status)
                self.assertIn("Entra ID", await login_page.text())

                unauthorized_config = await client.get("/webchat/config")
                self.assertEqual(401, unauthorized_config.status)

                pending_session = {
                    "user": {
                        "tenantId": "tenant-1",
                        "objectId": "object-1",
                        "username": "user@example.com",
                        "name": "测试用户",
                    }
                }
                with patch.object(
                    start_server,
                    "_entra_get_session",
                    return_value=pending_session,
                ):
                    pending_page = await client.get("/webchat")
                    self.assertEqual(200, pending_page.status)
                    pending_html = await pending_page.text()
                    self.assertIn("等待管理员审批", pending_html)

                    pending_config = await client.get("/webchat/config")
                    self.assertEqual(403, pending_config.status)

                with faq._db() as connection:
                    connection.execute(
                        "UPDATE webchat_access_grants SET status='denied',denial_reason='测试拒绝理由'"
                    )
                with patch.object(
                    start_server,
                    "_entra_get_session",
                    return_value=pending_session,
                ):
                    denied_page = await client.get("/webchat")
                    denied_html = await denied_page.text()
                    self.assertIn("测试拒绝理由", denied_html)
                    denied_config = await client.get("/webchat/config")
                    self.assertEqual(403, denied_config.status)
                    self.assertEqual(
                        "访问申请已被拒绝",
                        (await denied_config.json())["error"],
                    )

    async def test_webchat_upload_validation_and_storage(self) -> None:
        with (
            patch.object(faq, "DB_PATH", self.database_path),
            patch.object(start_server, "WEBCHAT_UPLOAD_DIR", self.upload_directory),
            patch.object(start_server, "_entra_enabled", return_value=False),
            patch.object(start_server, "_entra_china_enabled", return_value=False),
            patch.dict(start_server.environ, {"WEBCHAT_PUBLIC_BASE_URL": "https://help.example.com"}),
        ):
            application = self._create_application()
            async with TestClient(TestServer(application)) as client:
                valid_form = FormData()
                valid_form.add_field(
                    "file",
                    BytesIO("日志内容".encode("utf-8")),
                    filename="sample.log",
                    content_type="text/plain",
                )
                valid_response = await client.post(
                    "/webchat/upload",
                    data=valid_form,
                )
                self.assertEqual(200, valid_response.status)
                valid_data = await valid_response.json()
                self.assertEqual("sample.log", valid_data["name"])
                self.assertEqual("text/plain", valid_data["contentType"])
                self.assertTrue(valid_data["contentUrl"].startswith("https://help.example.com/webchat/uploads/"))
                self.assertEqual(1, len(list(self.upload_directory.iterdir())))

                invalid_form = FormData()
                invalid_form.add_field(
                    "file",
                    BytesIO(b"not an image"),
                    filename="fake.png",
                    content_type="image/png",
                )
                invalid_response = await client.post(
                    "/webchat/upload",
                    data=invalid_form,
                )
                self.assertEqual(415, invalid_response.status)

                empty_form = FormData()
                empty_form.add_field(
                    "file",
                    BytesIO(b""),
                    filename="empty.txt",
                    content_type="text/plain",
                )
                empty_response = await client.post(
                    "/webchat/upload",
                    data=empty_form,
                )
                self.assertEqual(400, empty_response.status)

    async def test_faq_markdown_import_boundaries(self) -> None:
        with patch.object(faq, "DB_PATH", self.database_path):
            application = self._create_application()
            with faq._db() as connection:
                cursor = connection.execute(
                    "INSERT INTO admin_accounts(username,display_name,password_hash,is_active,mfa_enabled,created_at,updated_at,last_login) VALUES(?,?,?,?,?,?,?,?)",
                    ("admin", "测试管理员", faq._hash_password("test-password"), 1, 0, 100, 100, 0),
                )
                admin_id = int(cursor.lastrowid)
            session_token = "import-session"
            csrf_token = "import-csrf"
            faq._SESSIONS[session_token] = {
                "csrf": csrf_token,
                "expires": faq.time.time() + 3600,
                "admin_id": admin_id,
                "username": "admin",
                "display_name": "测试管理员",
                "role": "admin",
            }

            async with TestClient(TestServer(application)) as client:
                client.session.cookie_jar.update_cookies(
                    {
                        faq.SESSION_COOKIE: (
                            f"{session_token}.{faq._sign(session_token)}"
                        )
                    }
                )
                headers = {"X-CSRF-Token": csrf_token}

                markdown_form = FormData()
                markdown_form.add_field(
                    "file",
                    BytesIO("# 标题\n正文".encode("utf-8")),
                    filename="faq.md",
                    content_type="text/markdown",
                )
                markdown_response = await client.post(
                    "/admin/faq/import-content",
                    headers=headers,
                    data=markdown_form,
                )
                self.assertEqual(200, markdown_response.status)
                self.assertEqual("# 标题\n正文", (await markdown_response.json())["content"])

                unsupported_form = FormData()
                unsupported_form.add_field(
                    "file",
                    BytesIO(b"plain text"),
                    filename="faq.txt",
                    content_type="text/plain",
                )
                unsupported_response = await client.post(
                    "/admin/faq/import-content",
                    headers=headers,
                    data=unsupported_form,
                )
                self.assertEqual(415, unsupported_response.status)

                oversized_content_form = FormData()
                oversized_content_form.add_field(
                    "file",
                    BytesIO(("字" * (faq.MAX_CONTENT + 1)).encode("utf-8")),
                    filename="large.md",
                    content_type="text/markdown",
                )
                oversized_response = await client.post(
                    "/admin/faq/import-content",
                    headers=headers,
                    data=oversized_content_form,
                )
                self.assertEqual(413, oversized_response.status)


if __name__ == "__main__":
    unittest.main()
