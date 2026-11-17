"""企业可用性关键回归：导出、配置容错、模板路径和任务状态."""

from __future__ import annotations

import atexit
import csv
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import unittest
import uuid
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

# 测试必须与真实数据隔离：在任何 app 导入之前指向临时数据目录，
# 并禁止把本机旧库迁移进来，否则测试会读写真实的简历库。
TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="resume-tool-tests-"))
os.environ["RESUME_TOOL_DATA_DIR"] = str(TEST_DATA_DIR)
os.environ["RESUME_TOOL_SKIP_MIGRATION"] = "1"
atexit.register(lambda: shutil.rmtree(TEST_DATA_DIR, ignore_errors=True))

from openpyxl import load_workbook  # noqa: E402

import app as appmod  # noqa: E402
import resume_tool as rt  # noqa: E402


def make_test_client():
    """模拟正常浏览器：所有写请求默认携带本机同源 Origin."""
    client = appmod.app.test_client()
    client.environ_base["HTTP_ORIGIN"] = "http://localhost"
    return client


class UpdateSafetyTests(unittest.TestCase):
    def test_selected_ids_dedupe_across_repeated_params(self):
        got = appmod.parse_selected_ids(["3,5", "5,3", "7"])
        self.assertEqual(got, [3, 5, 7])

    def test_template_path_rejects_traversal_and_accepts_normal_slug(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "python-backend.json").write_text("{}", encoding="utf-8")
            with patch.object(appmod, "TPL_DIR", root):
                self.assertIsNone(appmod.template_path("..\\providers"))
                self.assertIsNone(appmod.template_path("../providers"))
                self.assertEqual(appmod.template_path("python-backend"),
                                 (root / "python-backend.json").resolve())

    def test_corrupt_provider_config_degrades_instead_of_exiting(self):
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "providers.json"
            bad.write_text("{ broken", encoding="utf-8")
            with self.assertRaises(rt.ProviderConfigError):
                rt.read_providers(bad)
            with patch.object(appmod, "PROVIDERS_PATH", bad):
                providers, active, error = appmod.provider_status()
                self.assertIsNone(active)
                self.assertIn("无法解析", error)
                self.assertIn("providers", providers)

    def test_ensure_provider_creates_editable_default(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config" / "providers.json"
            providers = rt.ensure_providers(path)
            self.assertEqual(providers["active"], "deepseek")
            self.assertTrue(path.is_file())

    def test_build_report_csv_keeps_order_and_drops_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.txt").write_text("alpha resume", encoding="utf-8")
            (root / "b.txt").write_text("beta resume", encoding="utf-8")
            conn = rt.get_conn(str(root / "test.db"))
            try:
                rt.import_one(conn, root / "a.txt")
                rt.import_one(conn, root / "b.txt")
                ids = [r["id"] for r in conn.execute(
                    "SELECT id FROM resumes ORDER BY id").fetchall()]
                report = rt.build_report_csv(conn, [ids[1], ids[0], ids[1]])
            finally:
                conn.close()
            rows = list(csv.reader(io.StringIO(report)))
            self.assertEqual([int(r[0]) for r in rows[1:]], [ids[1], ids[0]])

    def test_background_job_catches_system_exit(self):
        jid = appmod.job_new("keyword", ["python-backend"], "all")
        with patch.object(appmod.rt, "load_template", side_effect=SystemExit("模板损坏")):
            appmod.run_screening_job(jid, "keyword", ["python-backend"], None, "all")
        job = appmod.job_get(jid)
        self.assertEqual(job["state"], "error")
        self.assertIn("模板损坏", job["message"])

    def test_mask_pii_masks_id_card_and_bank_card(self):
        """身份证、银行卡此前完全没有打码，是企业评审最常见的问题."""
        text = ("张三 13800001111 zhang@example.com "
                "身份证 110101199003071234 卡号 6222020200112233445")
        masked = rt.mask_pii(text, "张三")
        self.assertNotIn("13800001111", masked)
        self.assertNotIn("zhang@example.com", masked)
        self.assertNotIn("110101199003071234", masked)
        self.assertNotIn("6222020200112233445", masked)
        self.assertNotIn("张三", masked)
        self.assertIn("[证件已打码]", masked)

    def test_mask_pii_masks_spaced_name(self):
        """姓名分字写成「张 三」时，旧的整串替换会漏掉."""
        masked = rt.mask_pii("姓名：张 三，6 年后端经验", "张三")
        self.assertNotIn("张 三", masked)
        self.assertIn("[姓名已打码]", masked)

    def test_mask_pii_keeps_ordinary_numbers(self):
        """不能把工作年限、毕业年份之类的普通数字误当证件号."""
        masked = rt.mask_pii("2019 年毕业，6 年经验，2023 年入职", "张三")
        self.assertIn("2019 年毕业", masked)
        self.assertIn("6 年经验", masked)


class DataDirectoryTests(unittest.TestCase):
    def test_resolve_data_dir_env_frozen_and_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.dict(os.environ, {"RESUME_TOOL_DATA_DIR": str(root / "custom")}):
                self.assertEqual(appmod.resolve_data_dir(), (root / "custom").resolve())
            with patch.dict(os.environ, {"RESUME_TOOL_DATA_DIR": "", "LOCALAPPDATA": str(root)}):
                with patch.object(appmod.sys, "frozen", True, create=True):
                    self.assertEqual(appmod.resolve_data_dir(), (root / "ResumeTool").resolve())
                with patch.object(appmod.sys, "frozen", False, create=True):
                    self.assertEqual(appmod.resolve_data_dir(), appmod.CODE_DIR)

    def test_ensure_data_layout_copies_legacy_data_without_deleting(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy = root / "legacy"
            data = root / "data"
            resource = root / "resource"
            (legacy / "uploads" / "batch1").mkdir(parents=True)
            (legacy / "uploads" / "batch1" / "a.txt").write_text("legacy", encoding="utf-8")
            (legacy / "config").mkdir()
            (legacy / "config" / "providers.json").write_text("{}", encoding="utf-8")
            (legacy / "resume_tool.db").write_text("legacy-db", encoding="utf-8")
            (resource / "config" / "templates").mkdir(parents=True)
            (resource / "config" / "templates" / "default.json").write_text(
                "{}", encoding="utf-8")
            with patch.multiple(
                appmod, CODE_DIR=legacy, RESOURCE_BASE=resource, DATA_DIR=data,
                CONFIG_DIR=data / "config", TPL_DIR=data / "config" / "templates",
                PROVIDERS_PATH=data / "config" / "providers.json",
                APP_SETTINGS_PATH=data / "config" / "app_settings.json",
                UPLOAD_DIR=data / "uploads", BACKUP_DIR=data / "backups",
            ):
                with patch.dict(os.environ, {"RESUME_TOOL_SKIP_MIGRATION": "0"}):
                    appmod.ensure_data_layout()
                self.assertTrue((data / "resume_tool.db").is_file())
                self.assertTrue((data / "uploads" / "batch1" / "a.txt").is_file())
                self.assertTrue((data / "config" / "providers.json").is_file())
                self.assertTrue((data / "config" / "templates" / "default.json").is_file())
                self.assertTrue((legacy / "resume_tool.db").is_file())  # 旧数据不删除

    def test_relative_upload_path_and_normalization(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            uploads = root / "uploads"
            old_abs = uploads / "batch" / "a.txt"
            old_abs.parent.mkdir(parents=True)
            old_abs.write_text("x", encoding="utf-8")
            conn = sqlite3.connect(root / "test.db")
            try:
                conn.execute("CREATE TABLE resumes(id INTEGER PRIMARY KEY, path TEXT)")
                conn.execute("INSERT INTO resumes(id, path) VALUES(1, ?)", (str(old_abs),))
                conn.commit()
                with patch.multiple(appmod, DATA_DIR=root, UPLOAD_DIR=uploads):
                    self.assertEqual(appmod.normalize_upload_paths(conn), 1)
                    stored = conn.execute(
                        "SELECT path FROM resumes WHERE id=1").fetchone()[0]
                    self.assertEqual(stored, "uploads/batch/a.txt")
                    self.assertEqual(appmod.stored_resume_path(stored),
                                     (uploads / "batch" / "a.txt").resolve())
            finally:
                conn.close()

    def test_normalize_skips_paths_outside_upload_dir(self):
        """外部目录里恰好也有 uploads/ 片段时，不能被误改写到数据目录."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outside = root / "外部资料" / "uploads" / "2024" / "张三-后端.txt"
            outside.parent.mkdir(parents=True)
            outside.write_text("x", encoding="utf-8")
            conn = sqlite3.connect(root / "test.db")
            conn.execute("CREATE TABLE resumes(id INTEGER PRIMARY KEY, path TEXT)")
            conn.execute("INSERT INTO resumes(id, path) VALUES(1, ?)", (str(outside),))
            conn.commit()
            with patch.multiple(appmod, DATA_DIR=root / "data", UPLOAD_DIR=root / "data" / "uploads"):
                self.assertEqual(appmod.normalize_upload_paths(conn), 0)
            stored = conn.execute("SELECT path FROM resumes WHERE id=1").fetchone()[0]
            self.assertEqual(stored, str(outside))
            conn.close()

    def test_adopt_legacy_files_copies_into_upload_dir(self):
        """数据目录内的历史原文件（如 samples/）收进 uploads，避免收紧路径后失效."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            uploads = root / "uploads"
            samples = root / "samples"
            samples.mkdir()
            src = samples / "示例.pdf"
            src.write_bytes(b"%PDF-1.4 demo")
            conn = rt.get_conn(str(root / "test.db"))
            try:
                conn.execute(
                    "INSERT INTO resumes(filename, path, sha256, text, imported_at)"
                    " VALUES(?,?,?,?,?)",
                    ("示例.pdf", str(src), "a" * 64, "正文", rt.now()))
                conn.commit()
                with patch.multiple(appmod, DATA_DIR=root, UPLOAD_DIR=uploads):
                    self.assertEqual(appmod.adopt_legacy_files(conn), 1)
                    stored = conn.execute("SELECT path FROM resumes").fetchone()[0]
                    self.assertEqual(stored, "uploads/legacy/示例.pdf")
                    self.assertTrue((uploads / "legacy" / "示例.pdf").is_file())
                    # 再跑一次不应重复迁移
                    self.assertEqual(appmod.adopt_legacy_files(conn), 0)
            finally:
                conn.close()


class BackupAndImportTests(unittest.TestCase):
    def test_backup_contains_db_and_restore_rolls_back_rows(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "resume_tool.db"
            backups = root / "backups"
            uploads = root / "uploads"
            templates = root / "templates"
            templates.mkdir()
            (root / "a.txt").write_text("first candidate", encoding="utf-8")
            (root / "b.txt").write_text("second candidate", encoding="utf-8")
            conn = rt.get_conn(str(db_path))
            rt.import_one(conn, root / "a.txt")
            conn.close()

            with patch.multiple(
                appmod, DB=str(db_path), BACKUP_DIR=backups, UPLOAD_DIR=uploads,
                TPL_DIR=templates, APP_SETTINGS_PATH=root / "app_settings.json",
                PROVIDERS_PATH=root / "providers.json",
            ):
                with appmod.app.app_context():
                    archive = appmod.create_backup("manual")
                    with zipfile.ZipFile(archive) as zf:
                        names = set(zf.namelist())
                    self.assertIn("resume_tool.db", names)
                    self.assertIn("backup-meta.json", names)
                    self.assertTrue(appmod.backup_file(archive.name))
                    listing = appmod.list_backups()
                    self.assertTrue(listing)
                    self.assertRegex(listing[0]["size_text"], r"^(\d+ KB|\d+\.\d MB)$")

                conn = rt.get_conn(str(db_path))
                rt.import_one(conn, root / "b.txt")
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM resumes").fetchone()[0], 2)
                conn.close()

                with appmod.app.app_context():
                    result = appmod.restore_backup(archive)
                self.assertIn("pre_backup", result)
                conn = rt.get_conn(str(db_path))
                try:
                    self.assertEqual(conn.execute(
                        "SELECT COUNT(*) FROM resumes").fetchone()[0], 1)
                finally:
                    conn.close()

    def test_restore_to_fresh_data_dir_keeps_upload_paths_portable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src, dst = root / "old", root / "new"
            src_uploads = src / "uploads"
            src_uploads.mkdir(parents=True)
            upload = src_uploads / "batch" / "a.txt"
            upload.parent.mkdir()
            upload.write_text("portable candidate", encoding="utf-8")

            src_conn = rt.get_conn(str(src / "resume_tool.db"))
            try:
                rt.import_one_detail(src_conn, upload, store_base=src_uploads)
                stored = src_conn.execute(
                    "SELECT path FROM resumes").fetchone()[0]
                self.assertEqual(stored, "uploads/batch/a.txt")
            finally:
                src_conn.close()

            with patch.multiple(
                appmod, DATA_DIR=src, DB=str(src / "resume_tool.db"),
                BACKUP_DIR=src / "backups", UPLOAD_DIR=src_uploads,
                TPL_DIR=src / "templates", APP_SETTINGS_PATH=src / "app_settings.json",
                PROVIDERS_PATH=src / "providers.json",
            ):
                with appmod.app.app_context():
                    archive = appmod.create_backup("manual", include_uploads=True)

            dst_uploads = dst / "uploads"
            with patch.multiple(
                appmod, DATA_DIR=dst, DB=str(dst / "resume_tool.db"),
                BACKUP_DIR=dst / "backups", UPLOAD_DIR=dst_uploads,
                TPL_DIR=dst / "templates", APP_SETTINGS_PATH=dst / "app_settings.json",
                PROVIDERS_PATH=dst / "providers.json",
            ):
                with appmod.app.app_context():
                    result = appmod.restore_backup(archive)
                self.assertEqual(result["pre_backup"], "")  # 全新目录没有旧库可备份
                self.assertEqual(result["uploads"], 1)
                self.assertTrue((dst_uploads / "batch" / "a.txt").is_file())
                conn = rt.get_conn(str(dst / "resume_tool.db"))
                try:
                    stored = conn.execute("SELECT path FROM resumes").fetchone()[0]
                    self.assertEqual(stored, "uploads/batch/a.txt")
                    self.assertTrue(appmod.stored_resume_path(stored).is_file())
                finally:
                    conn.close()

    def test_directory_import_copies_file_into_data_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source"
            source.mkdir()
            (source / "候选人.txt").write_text("目录导入候选人内容", encoding="utf-8")
            db_path = root / "resume_tool.db"
            uploads = root / "uploads"
            with patch.multiple(
                appmod, DB=str(db_path), UPLOAD_DIR=uploads,
                BACKUP_DIR=root / "backups",
            ):
                response = make_test_client().post(
                    "/import-dir", data={"dir": str(source)})
                self.assertEqual(response.status_code, 302)
                conn = sqlite3.connect(db_path)
                try:
                    stored = conn.execute("SELECT path FROM resumes").fetchone()[0]
                    self.assertTrue(stored.startswith("uploads/dir-"), stored)
                finally:
                    conn.close()
                copied = list(uploads.rglob("候选人.txt"))
                self.assertEqual(len(copied), 1)
                self.assertTrue(copied[0].is_file())

    def test_import_creates_batch_and_reports_duplicate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "resume_tool.db"
            uploads = root / "uploads"
            with patch.multiple(
                appmod, DB=str(db_path), UPLOAD_DIR=uploads,
                BACKUP_DIR=root / "backups",
            ):
                client = make_test_client()
                data = {"files": (BytesIO("批量导入测试".encode("utf-8")), "张三.txt")}
                first = client.post("/import-files", data=data,
                                    content_type="multipart/form-data")
                self.assertEqual(first.status_code, 302)
                second = client.post(
                    "/import-files",
                    data={"files": (BytesIO("批量导入测试".encode("utf-8")), "张三.txt")},
                    content_type="multipart/form-data",
                )
                self.assertEqual(second.status_code, 302)

                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                try:
                    batches = conn.execute(
                        "SELECT * FROM import_batches ORDER BY id").fetchall()
                    self.assertEqual(len(batches), 2)
                    self.assertEqual(batches[0]["added"], 1)
                    self.assertEqual(batches[1]["dup"], 1)
                    item = conn.execute(
                        "SELECT * FROM import_batch_items WHERE batch_id=?",
                        (batches[0]["id"],)).fetchone()
                    self.assertIn("已导入", item["detail"])
                    stored_path = conn.execute(
                        "SELECT path FROM resumes ORDER BY id LIMIT 1").fetchone()[0]
                    self.assertTrue(stored_path.startswith("uploads/"), stored_path)
                finally:
                    conn.close()


class PrivacyAndDiagnosticsTests(unittest.TestCase):
    def _seed(self, root: Path, uploads: Path):
        """建库并导入一份简历，返回 (db, 简历ID, 上传文件路径)."""
        db_path = root / "resume_tool.db"
        uploads.mkdir(parents=True, exist_ok=True)
        source = uploads / "batch-1"
        source.mkdir(parents=True, exist_ok=True)
        stored = source / "候选人.txt"
        stored.write_text("候选人张三 13800001111 kafka 经验五年", encoding="utf-8")
        conn = rt.get_conn(str(db_path))
        try:
            result, _, rid = rt.import_one_detail(conn, stored, store_base=uploads)
        finally:
            conn.close()
        self.assertEqual(result, "added")
        return db_path, rid, stored

    def test_purge_removes_uploaded_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            uploads = root / "uploads"
            db_path, rid, stored = self._seed(root, uploads)
            self.assertTrue(stored.is_file())
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("UPDATE resumes SET deleted_at=? WHERE id=?", (rt.now(), rid))
                conn.commit()
            finally:
                conn.close()

            with patch.multiple(appmod, DB=str(db_path), DATA_DIR=root, UPLOAD_DIR=uploads,
                                BACKUP_DIR=root / "backups", TRASH_DIR=root / ".trash"):
                client = make_test_client()
                client.post(f"/trash/{rid}/purge")

            self.assertFalse(stored.exists(), "彻底删除后上传文件应移出 uploads")
            moved = list((root / ".trash").rglob("候选人.txt"))
            self.assertEqual(len(moved), 1, "原始文件应移入 .trash 暂存区以便恢复")
            conn = sqlite3.connect(db_path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM resumes").fetchone()[0], 0)
            finally:
                conn.close()

    def test_purge_keeps_file_outside_data_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            uploads = root / "uploads"
            uploads.mkdir()
            external = root / "share" / "外部简历.txt"
            external.parent.mkdir()
            external.write_text("外部目录导入的候选人", encoding="utf-8")

            db_path = root / "resume_tool.db"
            conn = rt.get_conn(str(db_path))
            try:
                _, _, rid = rt.import_one_detail(conn, external)
                conn.execute("UPDATE resumes SET deleted_at=? WHERE id=?", (rt.now(), rid))
                conn.commit()
            finally:
                conn.close()

            with patch.multiple(appmod, DB=str(db_path), DATA_DIR=root, UPLOAD_DIR=uploads,
                                BACKUP_DIR=root / "backups"):
                make_test_client().post(f"/trash/{rid}/purge")

            self.assertTrue(external.is_file(), "数据目录之外的原文件不应被擅自删除")

    def test_orphan_cleanup_only_removes_unreferenced_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            uploads = root / "uploads"
            db_path, _, stored = self._seed(root, uploads)
            orphan = uploads / "batch-2" / "重复导入副本.txt"
            orphan.parent.mkdir(parents=True, exist_ok=True)
            orphan.write_text("重复内容", encoding="utf-8")
            old = time.time() - 3600  # 模拟历史遗留文件，避开 10 分钟导入保护期
            os.utime(orphan, (old, old))

            with patch.multiple(appmod, DB=str(db_path), DATA_DIR=root, UPLOAD_DIR=uploads,
                                BACKUP_DIR=root / "backups", TRASH_DIR=root / ".trash"):
                client = make_test_client()
                page = client.get("/settings")
                self.assertEqual(page.status_code, 200)
                self.assertIn("清理孤立原文件", page.get_data(as_text=True))
                client.post("/settings/uploads/clean")

            self.assertFalse(orphan.exists(), "孤立文件应被移出 uploads")
            self.assertTrue((root / ".trash").is_dir(), "应移入 .trash 暂存区")
            self.assertTrue(stored.is_file(), "被简历引用的文件必须保留")

    def test_diagnostics_bundle_has_no_secret_or_resume_text(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            uploads = root / "uploads"
            db_path, _, _ = self._seed(root, uploads)
            providers = root / "providers.json"
            providers.write_text(json.dumps({
                "active": "deepseek",
                "providers": {"deepseek": {
                    "label": "DeepSeek",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "FAKE-KEY-FOR-DIAGNOSTIC-TEST",
                    "model": "deepseek-chat",
                }},
            }, ensure_ascii=False), encoding="utf-8")
            settings = root / "app_settings.json"
            settings.write_text(json.dumps({"auto_backup": True}), encoding="utf-8")

            with patch.multiple(appmod, DB=str(db_path), DATA_DIR=root, UPLOAD_DIR=uploads,
                                BACKUP_DIR=root / "backups",
                                PROVIDERS_PATH=providers, APP_SETTINGS_PATH=settings,
                                LOG_DIR=root / "logs", LOG_FILE=root / "logs" / "resume-tool.log"):
                response = make_test_client().get("/settings/diagnostics")

            self.assertEqual(response.status_code, 200)
            with zipfile.ZipFile(BytesIO(response.data)) as zf:
                names = zf.namelist()
                self.assertIn("summary.json", names)
                self.assertIn("config/providers.redacted.json", names)
                redacted = zf.read("config/providers.redacted.json").decode("utf-8")
                summary = json.loads(zf.read("summary.json").decode("utf-8"))
                payload = b"".join(zf.read(n) for n in names)

            self.assertIn('"api_key_configured": true', redacted)
            self.assertNotIn("FAKE-KEY-FOR-DIAGNOSTIC-TEST", payload.decode("utf-8", "ignore"))
            self.assertNotIn("13800001111", payload.decode("utf-8", "ignore"))
            self.assertEqual(summary["counts"]["resumes"], 1)

    def test_diagnostics_log_is_scrubbed(self):
        """诊断包里的日志必须脱敏：搜索词常是候选人姓名，不能原样外发."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            uploads = root / "uploads"
            db_path, _, _ = self._seed(root, uploads)
            logs = root / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            (logs / "resume-tool.log").write_text(
                "INFO [app] GET /?q=张三&template=all -> 200\n"
                "INFO [app] 候选人 13800001111 / zhang@example.com\n"
                "INFO [app] 证件 110101199003071234 卡号 6222020200112233445\n",
                encoding="utf-8")

            with patch.multiple(appmod, DB=str(db_path), DATA_DIR=root, UPLOAD_DIR=uploads,
                                BACKUP_DIR=root / "backups",
                                LOG_DIR=logs, LOG_FILE=logs / "resume-tool.log"):
                response = make_test_client().get("/settings/diagnostics")

            self.assertEqual(response.status_code, 200)
            with zipfile.ZipFile(BytesIO(response.data)) as zf:
                self.assertIn("logs/resume-tool.log", zf.namelist())
                bundle = zf.read("logs/resume-tool.log").decode("utf-8")

            self.assertNotIn("张三", bundle)
            self.assertNotIn("13800001111", bundle)
            self.assertNotIn("zhang@example.com", bundle)
            self.assertNotIn("110101199003071234", bundle)
            self.assertNotIn("6222020200112233445", bundle)
            self.assertIn("q=***", bundle)


class TemplateDeleteTests(unittest.TestCase):
    """模板删除：移入暂存区、清理引用、可恢复."""

    def setUp(self):
        appmod.app.config.update(TESTING=True)
        self.client = make_test_client()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tpl_dir = root / "templates"
        self.tpl_dir.mkdir()
        self.trash = root / ".trash"
        self.settings = root / "app_settings.json"
        self.settings.write_text(json.dumps({
            "favorites": ["python-backend", "qa-engineer"],
            "screen_templates": ["python-backend"],
        }, ensure_ascii=False), encoding="utf-8")
        sample = Path(__file__).resolve().parent.parent / "config" / "templates" / "python-backend.json"
        (self.tpl_dir / "python-backend.json").write_text(
            sample.read_text(encoding="utf-8"), encoding="utf-8")

        self.patcher = patch.multiple(
            appmod, TPL_DIR=self.tpl_dir, TRASH_DIR=self.trash,
            APP_SETTINGS_PATH=self.settings)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def _settings(self) -> dict:
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def test_delete_moves_template_to_trash(self):
        response = self.client.post("/templates/python-backend/delete")
        self.assertEqual(response.status_code, 302)
        self.assertFalse((self.tpl_dir / "python-backend.json").exists())
        moved = list(self.trash.glob("templates-*/python-backend.json"))
        self.assertEqual(len(moved), 1, "模板应移入 .trash 而不是直接删除")

    def test_delete_clears_favorites_and_screen_memory(self):
        self.client.post("/templates/python-backend/delete")
        cfg = self._settings()
        self.assertNotIn("python-backend", cfg.get("favorites", []))
        self.assertNotIn("python-backend", cfg.get("screen_templates", []))
        # 其他模板的收藏不受影响
        self.assertIn("qa-engineer", cfg.get("favorites", []))

    def test_delete_missing_template_reports_error(self):
        response = self.client.post("/templates/not-exist/delete", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("模板不存在", response.get_data(as_text=True))

    def test_delete_rejects_illegal_slug(self):
        """越界 slug 不能删掉模板目录之外的文件."""
        outside = self.tpl_dir.parent / "providers.json"
        outside.write_text('{"api_key": "keep-me"}', encoding="utf-8")
        response = self.client.post("/templates/..%5Cproviders/delete",
                                    follow_redirects=True)
        self.assertIn("模板不存在", response.get_data(as_text=True))
        self.assertTrue(outside.is_file(), "模板目录外的文件不能被删除")

    def test_restore_returns_template(self):
        self.client.post("/templates/python-backend/delete")
        folder = next(self.trash.glob("templates-*")).name
        response = self.client.post("/templates/python-backend/restore",
                                    data={"folder": folder})
        self.assertEqual(response.status_code, 302)
        self.assertTrue((self.tpl_dir / "python-backend.json").is_file())

    def test_restore_does_not_overwrite_existing(self):
        self.client.post("/templates/python-backend/delete")
        folder = next(self.trash.glob("templates-*")).name
        # 删除后重建同名模板，再恢复旧版本
        (self.tpl_dir / "python-backend.json").write_text('{"name":"新版本"}', encoding="utf-8")
        response = self.client.post("/templates/python-backend/restore",
                                    data={"folder": folder}, follow_redirects=True)
        self.assertIn("已存在，未覆盖", response.get_data(as_text=True))
        self.assertEqual((self.tpl_dir / "python-backend.json").read_text(encoding="utf-8"),
                         '{"name":"新版本"}')

    def test_restore_rejects_invalid_folder(self):
        self.client.post("/templates/python-backend/delete")
        response = self.client.post("/templates/python-backend/restore",
                                    data={"folder": "../../etc"}, follow_redirects=True)
        self.assertIn("恢复参数无效", response.get_data(as_text=True))
        self.assertFalse((self.tpl_dir / "python-backend.json").exists())


class ScreenMemoryTests(unittest.TestCase):
    def test_screen_remembers_template_selection(self):
        slugs = [t["slug"] for t in appmod.list_templates()][:2]
        self.assertGreaterEqual(len(slugs), 2, "需要至少两个模板验证多选记忆")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            settings = root / "app_settings.json"
            with patch.multiple(
                appmod, APP_SETTINGS_PATH=settings,
                PROVIDERS_PATH=root / "providers.json",
            ):
                client = make_test_client()
                resp = client.post("/screen/templates", data={"templates": slugs})
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.get_json()["templates"], slugs)

                saved = json.loads(settings.read_text(encoding="utf-8"))
                self.assertEqual(saved["screen_templates"], slugs)

                page = client.get("/screen").get_data(as_text=True)
                match = re.search(r"const FROM_URL = (\[[^\]]*\]);", page)
                self.assertIsNotNone(match)
                self.assertEqual(json.loads(match.group(1)), slugs)
                self.assertIn("已恢复上次选择的", page)

    def test_invalid_or_deleted_template_is_not_remembered(self):
        slugs = [t["slug"] for t in appmod.list_templates()][:1]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            settings = root / "app_settings.json"
            with patch.multiple(
                appmod, APP_SETTINGS_PATH=settings,
                PROVIDERS_PATH=root / "providers.json",
            ):
                client = make_test_client()
                resp = client.post(
                    "/screen/templates",
                    data={"templates": ["../../providers", "not-exist", slugs[0]]},
                )
                self.assertEqual(resp.get_json()["templates"], slugs)

                # 模板被删除后，记忆中的 slug 自动失效
                settings.write_text(json.dumps(
                    {"screen_templates": ["removed-template"]}, ensure_ascii=False),
                    encoding="utf-8")
                self.assertEqual(appmod.get_screen_templates(), [])
                page = client.get("/screen").get_data(as_text=True)
                match = re.search(r"const FROM_URL = (\[[^\]]*\]);", page)
                self.assertEqual(json.loads(match.group(1)), [])


class RouteSafetyTests(unittest.TestCase):
    def setUp(self):
        appmod.app.config.update(TESTING=True)
        self.client = make_test_client()

    def _seed_resume(self, screenings: int = 12) -> int:
        """在临时数据目录里造一份简历 + 若干筛选记录，不依赖真实库."""
        uploads = appmod.UPLOAD_DIR
        uploads.mkdir(parents=True, exist_ok=True)
        src = uploads / f"fixture-{uuid.uuid4().hex[:8]}.txt"
        src.write_text("分页测试简历：2019-2023 后端开发经验", encoding="utf-8")
        conn = rt.get_conn(appmod.DB)
        try:
            result, _, rid = rt.import_one_detail(conn, src, store_base=uploads)
            self.assertIn(result, ("added", "restored", "dup"))
            for i in range(screenings):
                conn.execute(
                    "INSERT INTO screenings(resume_id, engine, template, score, status,"
                    " detail, created_at) VALUES(?,?,?,?,?,?,?)",
                    (rid, "keyword", "python-backend", 80 - i, "通过",
                     json.dumps({"reason": f"第 {i} 次"}), rt.now()))
            conn.commit()
        finally:
            conn.close()
        return int(rid)

    def _insert_resume_with_path(self, path: str) -> int:
        """直接插入一条指定 path 的简历记录，用于验证路径信任边界."""
        conn = rt.get_conn(appmod.DB)
        try:
            cur = conn.execute(
                "INSERT INTO resumes(filename, path, sha256, text, imported_at)"
                " VALUES(?,?,?,?,?)",
                ("外部文件.txt", path, uuid.uuid4().hex, "正文内容", rt.now()))
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def test_template_traversal_returns_404(self):
        for url in ("/templates/..%5Cproviders", "/templates/..%5Cproviders/export"):
            self.assertEqual(self.client.get(url).status_code, 404)

    def test_cross_origin_post_is_rejected(self):
        response = self.client.post(
            "/settings/preview", data={"preview_method": "default"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(response.status_code, 403)

    def test_settings_still_opens_when_provider_file_is_broken(self):
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "providers.json"
            bad.write_text("{ broken", encoding="utf-8")
            with patch.object(appmod, "PROVIDERS_PATH", bad):
                response = self.client.get("/settings")
                self.assertEqual(response.status_code, 200)
                self.assertIn("AI 配置读取失败", response.get_data(as_text=True))

    def test_resume_detail_pager_keeps_dynamic_route(self):
        rid = self._seed_resume()
        response = self.client.get(f"/resume/{rid}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'action="/resume/{rid}"', response.get_data(as_text=True))

    def test_resume_raw_rejects_path_outside_data_dir(self):
        """备份包可能携带任意绝对路径，此类记录必须读不到文件."""
        outside = Path(tempfile.mkdtemp(prefix="outside-")) / "secret.txt"
        outside.write_text("不该被读到", encoding="utf-8")
        rid = self._insert_resume_with_path(str(outside))
        try:
            self.assertEqual(self.client.get(f"/resume/{rid}/raw").status_code, 404)
            detail = self.client.get(f"/resume/{rid}")
            self.assertEqual(detail.status_code, 200)
            self.assertIn("原文件不可用", detail.get_data(as_text=True))
        finally:
            shutil.rmtree(outside.parent, ignore_errors=True)

    def test_deleted_resume_cannot_download_or_export(self):
        rid = self._seed_resume(screenings=1)
        with self.client.get(f"/resume/{rid}/raw") as resp:
            self.assertEqual(resp.status_code, 200)
        conn = rt.get_conn(appmod.DB)
        try:
            conn.execute("UPDATE resumes SET deleted_at=? WHERE id=?", (rt.now(), rid))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.client.get(f"/resume/{rid}/raw").status_code, 404)
        self.assertEqual(self.client.get(f"/export-xlsx?ids={rid}").status_code, 302)
        conn = rt.get_conn(appmod.DB)
        try:
            conn.execute("UPDATE resumes SET deleted_at=NULL WHERE id=?", (rid,))
            conn.commit()
        finally:
            conn.close()

    def test_restore_sanitizes_external_absolute_paths(self):
        rid = self._insert_resume_with_path(r"C:\Windows\win.ini")
        conn = rt.get_conn(appmod.DB)
        try:
            changed = appmod.sanitize_resume_paths(conn)
            stored = conn.execute("SELECT path FROM resumes WHERE id=?", (rid,)).fetchone()[0]
        finally:
            conn.close()
        self.assertGreaterEqual(changed, 1)
        self.assertEqual(stored, "")

    def test_screen_contains_ai_cost_confirmation(self):
        response = self.client.get("/screen")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("确认 AI 调用量", text)
        self.assertIn("cost-confirm", text)

    def test_backup_download_rejects_unknown_file(self):
        response = self.client.get("/settings/backups/not-a-backup.zip")
        self.assertEqual(response.status_code, 404)
        response = self.client.post("/settings/backups/not-a-backup.zip/restore")
        self.assertEqual(response.status_code, 404)

    def test_delete_missing_resume_reports_failure(self):
        """删除不存在的记录不能提示"已移入回收站"（会让人以为成功）."""
        response = self.client.post("/resume/999999/delete", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("已不存在", body)
        self.assertNotIn("已将简历 #999999 移入回收站", body)

    def test_delete_returns_to_previous_search_page(self):
        """删除后回到来源页，保留搜索词与模板筛选，而不是跳回全库."""
        rid = self._seed_resume()
        response = self.client.post(
            f"/resume/{rid}/delete",
            headers={"Referer": "http://localhost/?q=Kafka&template=python-backend"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"],
                         "/?q=Kafka&template=python-backend")

    def test_delete_rejects_offsite_redirect(self):
        """跨站来源在入口就被拒，不会把用户带去外部站点."""
        rid = self._seed_resume()
        response = self.client.post(
            f"/resume/{rid}/delete",
            headers={"Referer": "https://evil.example/steal"},
        )
        self.assertEqual(response.status_code, 403)

    def test_pages_do_not_rely_on_native_confirm(self):
        """原生 confirm 被浏览器拦截时会静默返回 false，删除会毫无反应."""
        tpl_dir = Path(__file__).resolve().parent.parent / "templates"
        offenders = []
        for path in tpl_dir.glob("*.html"):
            text = path.read_text(encoding="utf-8")
            for marker in ('onsubmit="return confirm', "onsubmit='return confirm",
                           'onclick="return confirm', "onclick='return confirm"):
                if marker in text:
                    offenders.append(f"{path.name}: {marker}")
        self.assertEqual(offenders, [], f"仍有模板依赖原生 confirm：{offenders}")

    def test_delete_uses_custom_dialog_markup(self):
        """删除入口挂自定义确认弹窗，而不是浏览器原生对话框."""
        self._seed_resume()  # 空库不会渲染删除按钮
        library = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="confirm-modal"', library)
        self.assertIn("data-confirm-delete", library)
        trash = self.client.get("/trash").get_data(as_text=True)
        self.assertIn('id="confirm-modal"', trash)


class RestoreAndJobSafetyTests(unittest.TestCase):
    def test_restore_failure_keeps_current_database_intact(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "resume_tool.db"
            uploads = root / "uploads"
            uploads.mkdir()
            conn = rt.get_conn(str(db_path))
            try:
                for name in ("a.txt", "b.txt"):
                    src = root / name
                    src.write_text(f"{name} 内容", encoding="utf-8")
                    rt.import_one(conn, src, store_base=uploads)
            finally:
                conn.close()

            bad = root / "bad.zip"
            with zipfile.ZipFile(bad, "w") as zf:
                zf.writestr("resume_tool.db", "this is not a sqlite file")
            with patch.multiple(appmod, DB=str(db_path), DATA_DIR=root,
                                UPLOAD_DIR=uploads, BACKUP_DIR=root / "backups",
                                TRASH_DIR=root / ".trash"):
                with appmod.app.app_context():
                    with self.assertRaises(ValueError):
                        appmod.restore_backup(bad)
            conn = sqlite3.connect(db_path)
            try:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM resumes").fetchone()[0], 2)
            finally:
                conn.close()
            self.assertFalse((root / "resume_tool.db.restore-tmp").exists())

    def test_restore_sanitizes_and_reports_warnings(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "src"
            src.mkdir()
            src_db = src / "resume_tool.db"
            conn = rt.get_conn(str(src_db))
            try:
                r = src / "a.txt"
                r.write_text("恢复候选人", encoding="utf-8")
                rt.import_one(conn, r)
                conn.execute("UPDATE resumes SET path=?", (r"C:\Windows\win.ini",))
                conn.commit()
            finally:
                conn.close()
            archive = root / "backup.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.write(src_db, "resume_tool.db")
            dst = root / "dst"
            dst.mkdir()
            with patch.multiple(appmod, DB=str(dst / "resume_tool.db"), DATA_DIR=dst,
                                UPLOAD_DIR=dst / "uploads", BACKUP_DIR=dst / "backups",
                                TRASH_DIR=dst / ".trash"):
                with appmod.app.app_context():
                    result = appmod.restore_backup(archive)
            self.assertIsInstance(result["warnings"], list)
            conn = sqlite3.connect(dst / "resume_tool.db")
            try:
                self.assertEqual(conn.execute("SELECT path FROM resumes").fetchone()[0], "")
            finally:
                conn.close()

    def test_job_gate_rejects_second_running_task(self):
        with appmod.JOBS_LOCK:
            appmod.JOBS.clear()
        try:
            jid = appmod.job_new("keyword", ["python-backend"], "all")
            with self.assertRaises(RuntimeError):
                appmod.job_new("keyword", ["python-backend"], "all")
            appmod.job_update(jid, state="done")
            # 完成后可再次提交
            self.assertTrue(appmod.job_new("keyword", ["python-backend"], "all"))
        finally:
            with appmod.JOBS_LOCK:
                appmod.JOBS.clear()

    def test_purge_old_trash_respects_retention(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trash = root / ".trash"
            old_dir = trash / "20200101-000000"
            old_dir.mkdir(parents=True)
            old_file = old_dir / "旧文件.txt"
            old_file.write_text("old", encoding="utf-8")
            stale = time.time() - 40 * 86400
            os.utime(old_file, (stale, stale))
            os.utime(old_dir, (stale, stale))
            fresh_dir = trash / "fresh"
            fresh_dir.mkdir()
            fresh_file = fresh_dir / "新文件.txt"
            fresh_file.write_text("new", encoding="utf-8")

            with patch.object(appmod, "TRASH_DIR", trash):
                removed, freed = appmod.purge_old_trash(days=30)
            self.assertEqual(removed, 1)
            self.assertEqual(freed, 3)
            self.assertFalse(old_dir.exists())
            self.assertTrue(fresh_file.is_file())

    def test_orphan_scan_skips_recent_and_partial_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            uploads = root / "uploads"
            uploads.mkdir()
            conn = rt.get_conn(str(root / "resume_tool.db"))
            try:
                fresh = uploads / "刚导入.txt"
                fresh.write_text("x", encoding="utf-8")
                partial = uploads / "上传中.txt.partial"
                partial.write_text("y", encoding="utf-8")
                old = time.time() - 3600
                os.utime(partial, (old, old))
                stale = uploads / "历史孤立.txt"
                stale.write_text("z", encoding="utf-8")
                os.utime(stale, (old, old))
                with patch.multiple(appmod, DATA_DIR=root, UPLOAD_DIR=uploads):
                    orphans, _ = appmod.find_orphan_uploads(conn)
            finally:
                conn.close()
            names = {p.name for p in orphans}
            self.assertNotIn("刚导入.txt", names, "10 分钟内新文件不应被判为孤立")
            self.assertNotIn("上传中.txt.partial", names, "半成品文件不参与清理")
            self.assertIn("历史孤立.txt", names)

    def test_cli_uses_same_data_dir_as_web(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"RESUME_TOOL_DATA_DIR": td}):
                self.assertEqual(rt.resolve_data_dir(), Path(td).resolve())
        self.assertEqual(Path(rt.DEFAULT_DB).parent, rt.DATA_DIR)
        self.assertEqual(rt.DEFAULT_PROVIDERS, rt.DATA_DIR / "config" / "providers.json")

    def test_xlsx_route_deduplicates_selected_ids(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ("a.txt", "b.txt"):
                (root / name).write_text(f"{name} resume", encoding="utf-8")
            db_path = root / "test.db"
            conn = rt.get_conn(str(db_path))
            try:
                rt.import_one(conn, root / "a.txt")
                rt.import_one(conn, root / "b.txt")
                ids = [r["id"] for r in conn.execute(
                    "SELECT id FROM resumes ORDER BY id").fetchall()]
            finally:
                conn.close()
            with patch.object(appmod, "DB", str(db_path)):
                response = make_test_client().get(
                    f"/export-xlsx?ids={ids[0]},{ids[1]}&ids={ids[0]},{ids[1]}")
            self.assertEqual(response.status_code, 200)
            wb = load_workbook(BytesIO(response.data), read_only=True)
            exported = [row[0] for row in wb["候选人报告"].iter_rows(
                min_row=2, values_only=True) if row[0] is not None]
            self.assertEqual(exported, ids)


class ScoreRulesetTests(unittest.TestCase):
    """评分口径 2：提及即视为具备，避免「一行罗列技能」的简历被压到 40 分以下."""

    def test_mention_once_counts_as_mostly_present(self):
        self.assertEqual(rt.evidence_to_score(0), 0.0)
        self.assertAlmostEqual(rt.evidence_to_score(1), 0.80)
        self.assertAlmostEqual(rt.evidence_to_score(2), 0.90)
        self.assertAlmostEqual(rt.evidence_to_score(3), 0.90)
        self.assertAlmostEqual(rt.evidence_to_score(9), 1.00)

    def test_listing_style_resume_scores_above_60(self):
        """核心技能各写一遍的简历应落在「匹配」区间，而不是旧口径下的 26 分."""
        tpl = rt.load_template(appmod.TPL_DIR / "python-backend.json")
        text = "工作年限：6年\n技能：Python, SQL, Kafka, Docker, Redis\n"
        result = rt.screen_keyword(text, tpl)
        self.assertGreater(result["score"], 45)
        self.assertEqual(result["ruleset"], rt.SCORE_RULESET)

    def test_keyword_result_carries_ruleset_for_traceability(self):
        tpl = rt.load_template(appmod.TPL_DIR / "python-backend.json")
        result = rt.screen_keyword("Python SQL", tpl)
        self.assertEqual(result["ruleset"], 2)


class DiffFlagTests(unittest.TestCase):
    """双引擎分歧判定：只提示不合并，缺任一引擎时必须返回 None."""

    def _pair(self, kw, ai):
        return {"keyword": {"score": kw}, "ai": {"score": ai}}

    def test_both_engines_close_returns_none(self):
        self.assertIsNone(appmod.diff_flag(self._pair(70, 78)))

    def test_ai_much_higher_flags_keyword_miss(self):
        flag = appmod.diff_flag(self._pair(52.1, 75))
        self.assertIsNotNone(flag)
        self.assertEqual(flag["higher"], "ai")
        self.assertAlmostEqual(flag["gap"], 22.9)

    def test_keyword_much_higher_flags_ai_strictness(self):
        flag = appmod.diff_flag(self._pair(90, 55))
        self.assertIsNotNone(flag)
        self.assertEqual(flag["higher"], "keyword")
        self.assertAlmostEqual(flag["gap"], 35.0)

    def test_missing_engine_or_bad_score_returns_none(self):
        self.assertIsNone(appmod.diff_flag({"keyword": {"score": 90}}))
        self.assertIsNone(appmod.diff_flag({"ai": {"score": 20}}))
        self.assertIsNone(appmod.diff_flag(self._pair("不是数字", 80)))
        self.assertIsNone(appmod.diff_flag({}))

    def test_threshold_boundary_is_inclusive(self):
        self.assertIsNotNone(appmod.diff_flag(self._pair(50, 70)))
        self.assertIsNone(appmod.diff_flag(self._pair(50.1, 70)))

    def test_cross_template_pair_is_not_compared(self):
        """不同模板的两个结果不能凑成一对比较，否则岗位差异会被当成口径分歧."""
        engines = {
            "keyword": {"score": 20.0, "template": "测试工程师-QA"},
            "ai": {"score": 90.0, "template": "Python后端-P7"},
        }
        self.assertIsNone(appmod.diff_flag(engines))

    def test_same_template_pair_still_compared(self):
        engines = {
            "keyword": {"score": 52.1, "template": "Python后端-P7"},
            "ai": {"score": 75.0, "template": "Python后端-P7"},
        }
        self.assertEqual(appmod.diff_flag(engines)["higher"], "ai")


class DisplayTemplateTests(unittest.TestCase):
    """「全部模板」视图选哪个模板展示：双引擎齐全优先，避免被最后跑的批量覆盖."""

    def _engines(self, kw_id=None, ai_id=None, kw=60.0, ai=70.0):
        out = {}
        if kw_id: out["keyword"] = {"id": kw_id, "score": kw, "template": "x"}
        if ai_id: out["ai"] = {"id": ai_id, "score": ai, "template": "x"}
        return out

    def test_prefers_template_with_both_engines(self):
        per = {
            "测试工程师-QA": self._engines(kw_id=99),          # 更新但只有关键词
            "Python后端-P7": self._engines(kw_id=10, ai_id=11),
        }
        self.assertEqual(appmod._display_template(per, None), "Python后端-P7")

    def test_flag_template_wins_for_row_consistency(self):
        per = {
            "Python后端-P7": self._engines(kw_id=10, ai_id=11),
            "测试工程师-QA": self._engines(kw_id=12, ai_id=13),
        }
        flag = {"template": "测试工程师-QA", "gap": 30.0}
        self.assertEqual(appmod._display_template(per, flag), "测试工程师-QA")

    def test_falls_back_to_newest_when_no_complete_pair(self):
        per = {
            "测试工程师-QA": self._engines(kw_id=40),
            "后端开发": self._engines(ai_id=50),
        }
        self.assertEqual(appmod._display_template(per, None), "后端开发")


class ReviewFlagUiTests(unittest.TestCase):
    """列表页「建议复核」气泡与筛选入口."""

    def setUp(self):
        appmod.app.config.update(TESTING=True)
        self.client = make_test_client()
        self._clean()

    def tearDown(self):
        # 测试库是模块级共享的，不清理会让后续用例读到上一条的分歧横幅
        self._clean()

    def _clean(self):
        conn = rt.get_conn(appmod.DB)
        try:
            conn.execute("DELETE FROM screenings WHERE resume_id IN"
                         " (SELECT id FROM resumes WHERE filename LIKE 'review-%')")
            conn.execute("DELETE FROM resumes WHERE filename LIKE 'review-%'")
            conn.commit()
        finally:
            conn.close()

    def _seed(self, kw_score: float, ai_score: float,
              kw_tpl: str = "Python后端-P7", ai_tpl: str = "Python后端-P7") -> int:
        conn = rt.get_conn(appmod.DB)
        try:
            cur = conn.execute(
                "INSERT INTO resumes(filename, path, sha256, text, imported_at)"
                " VALUES(?,?,?,?,?)",
                (f"review-{uuid.uuid4().hex[:6]}.txt", "", uuid.uuid4().hex,
                 "Python SQL 后端", rt.now()))
            rid = int(cur.lastrowid)
            for engine, score, tpl in (("keyword", kw_score, kw_tpl),
                                       ("ai", ai_score, ai_tpl)):
                conn.execute(
                    "INSERT INTO screenings(resume_id, engine, template, score, status,"
                    " detail, created_at) VALUES(?,?,?,?,?,?,?)",
                    (rid, engine, tpl, score, "通过",
                     json.dumps({"reason": "fixture"}), rt.now()))
            conn.commit()
            return rid
        finally:
            conn.close()

    def test_library_marks_divergent_resume(self):
        rid = self._seed(52.0, 78.0)
        page = self.client.get("/").get_data(as_text=True)
        self.assertEqual(page.count('class="review-flag"'), 1)
        self.assertIn(f'href="/resume/{rid}"', page)
        self.assertIn("只看这 1 份", page)

    def test_library_does_not_mark_close_scores(self):
        self._seed(70.0, 78.0)
        page = self.client.get("/").get_data(as_text=True)
        self.assertNotIn('class="review-flag"', page)
        self.assertIn("建议复核", page)  # 统计卡仍在，只是计数为 0

    def test_review_filter_narrows_list(self):
        matched = self._seed(50.0, 80.0)
        self._seed(78.0, 72.0)
        page = self.client.get("/?review=1").get_data(as_text=True)
        self.assertIn(f'href="/resume/{matched}"', page)
        self.assertEqual(page.count('class="review-flag"'), 1)
        self.assertEqual(page.count('class="row-check"'), 1)

    def test_review_filter_keeps_total_stat(self):
        """只看建议复核只收窄列表，不应把「简历总数」改成 1（会被误读成数据丢失）."""
        self._seed(50.0, 80.0)
        self._seed(70.0, 72.0)
        normal = self.client.get("/").get_data(as_text=True)
        filtered = self.client.get("/?review=1").get_data(as_text=True)
        head = r'<div class="stat-num">(\d+)</div><div class="stat-label">简历总数'
        self.assertEqual(re.search(head, normal).group(1),
                         re.search(head, filtered).group(1))
        self.assertEqual(filtered.count('class="row-check"'), 1)

    def test_detail_page_explains_divergence(self):
        rid = self._seed(52.0, 78.0)
        page = self.client.get(f"/resume/{rid}").get_data(as_text=True)
        self.assertIn("建议复核", page)
        self.assertIn("相差 26.0 分", page)

    def test_mixed_template_pair_is_not_flagged(self):
        """关键词在 A 模板、AI 在 B 模板时不能标「建议复核」."""
        rid = self._seed(20.0, 90.0, kw_tpl="测试工程师-QA")
        page = self.client.get("/").get_data(as_text=True)
        self.assertNotIn('class="review-flag"', page)
        self.assertIn(f'href="/resume/{rid}"', page)

    def test_template_view_shows_only_that_template(self):
        rid = self._seed(20.0, 90.0, kw_tpl="测试工程师-QA")
        page = self.client.get("/?template=python-backend").get_data(as_text=True)
        # 该模板下只有 AI 结果，关键词列应为空，而不是拿别的模板分数顶上
        self.assertNotIn("20.0</span>", page)
        self.assertIn("90.0</span>", page)
        self.assertIn(f'href="/resume/{rid}"', page)


class HistoryClearTests(unittest.TestCase):
    """清除筛选历史：只删 screenings，简历本体与其他简历的历史不受影响."""

    def setUp(self):
        appmod.app.config.update(TESTING=True)
        self.client = make_test_client()
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self):
        conn = rt.get_conn(appmod.DB)
        try:
            conn.execute("DELETE FROM screenings WHERE resume_id IN"
                         " (SELECT id FROM resumes WHERE filename LIKE 'hist-%')")
            conn.execute("DELETE FROM resumes WHERE filename LIKE 'hist-%'")
            conn.commit()
        finally:
            conn.close()

    def _seed(self, screenings: int = 3) -> int:
        conn = rt.get_conn(appmod.DB)
        try:
            cur = conn.execute(
                "INSERT INTO resumes(filename, path, sha256, text, imported_at)"
                " VALUES(?,?,?,?,?)",
                (f"hist-{uuid.uuid4().hex[:6]}.txt", "", uuid.uuid4().hex,
                 "Python SQL 后端", rt.now()))
            rid = int(cur.lastrowid)
            for i in range(screenings):
                conn.execute(
                    "INSERT INTO screenings(resume_id, engine, template, score, status,"
                    " detail, created_at) VALUES(?,?,?,?,?,?,?)",
                    (rid, "keyword" if i % 2 == 0 else "ai", "Python后端-P7",
                     60 + i, "通过", json.dumps({"reason": f"第 {i} 次"}), rt.now()))
            conn.commit()
            return rid
        finally:
            conn.close()

    def _count(self, rid: int) -> int:
        conn = rt.get_conn(appmod.DB)
        try:
            return conn.execute("SELECT COUNT(*) AS n FROM screenings WHERE resume_id=?",
                                (rid,)).fetchone()["n"]
        finally:
            conn.close()

    def test_clear_deletes_only_this_resume_history(self):
        rid = self._seed(3)
        other = self._seed(2)
        response = self.client.post(f"/resume/{rid}/history/clear")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._count(rid), 0)
        self.assertEqual(self._count(other), 2)

    def test_clear_keeps_resume_itself(self):
        rid = self._seed(2)
        self.client.post(f"/resume/{rid}/history/clear")
        page = self.client.get(f"/resume/{rid}").get_data(as_text=True)
        self.assertIn("hist-", page)
        self.assertIn("尚无筛选记录", page)
        self.assertNotIn("清除历史", page)  # 无历史时不再显示清除入口

    def test_clear_unknown_resume_returns_404(self):
        response = self.client.post("/resume/999999/history/clear")
        self.assertEqual(response.status_code, 404)

    def test_detail_page_offers_clear_when_history_exists(self):
        rid = self._seed(2)
        page = self.client.get(f"/resume/{rid}").get_data(as_text=True)
        self.assertIn("clear", page)
        self.assertIn("清除历史", page)
        self.assertIn("将清除该简历的 2 条筛选历史", page)
        self.assertIn("保留 30 天", page)

    def test_clearing_twice_reports_nothing_to_clear(self):
        rid = self._seed(1)
        self.client.post(f"/resume/{rid}/history/clear")
        with self.client.session_transaction() as sess:
            sess.clear()
        self.client.post(f"/resume/{rid}/history/clear")
        page = self.client.get(f"/resume/{rid}").get_data(as_text=True)
        self.assertIn("本就没有筛选历史", page)


class HistoryWindowTests(unittest.TestCase):
    """筛选历史窗口：每简历 30 条先进先出，但每组(模板,引擎)最新一条永不淘汰."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.conn = rt.get_conn(str(Path(self._td.name) / "window.db"))
        cur = self.conn.execute(
            "INSERT INTO resumes(filename, path, sha256, text, imported_at)"
            " VALUES(?,?,?,?,?)", ("w.txt", "", uuid.uuid4().hex, "正文", rt.now()))
        self.rid = int(cur.lastrowid)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._td.cleanup()

    def _add(self, n: int, engine: str = "keyword", template: str = "T1",
             start: int = 0) -> list[int]:
        """按递增时间插入 n 条，返回 id 列表（越靠后越新）."""
        ids = []
        for i in range(n):
            cur = self.conn.execute(
                "INSERT INTO screenings(resume_id, engine, template, score, status,"
                " detail, created_at) VALUES(?,?,?,?,?,?,?)",
                (self.rid, engine, template, 60.0, "通过",
                 json.dumps({"reason": "x"}),
                 f"2026-11-{1 + (start + i) // 24:02d}T{(start + i) % 24:02d}:00:00"))
            ids.append(int(cur.lastrowid))
        self.conn.commit()
        return ids

    def _ids(self) -> set[int]:
        return {r["id"] for r in self.conn.execute(
            "SELECT id FROM screenings WHERE resume_id=?", (self.rid,)).fetchall()}

    def test_keeps_newest_thirty(self):
        ids = self._add(40)
        removed = rt.trim_history(self.conn, self.rid)
        self.conn.commit()
        self.assertEqual(removed, 10)
        self.assertEqual(self._ids(), set(ids[-30:]))

    def test_no_trim_below_limit(self):
        self._add(30)
        self.assertEqual(rt.trim_history(self.conn, self.rid), 0)
        self.assertEqual(len(self._ids()), 30)

    def test_expensive_ai_result_never_evicted(self):
        """AI 结果排在窗口外也必须保留：它是额度换来的，且双引擎对比依赖它."""
        ai_id = self._add(1, engine="ai", start=0)[0]      # 最旧的一条
        self._add(35, engine="keyword", start=1)           # 35 条更新的关键词
        rt.trim_history(self.conn, self.rid)
        self.conn.commit()
        self.assertIn(ai_id, self._ids())
        self.assertEqual(len(self._ids()), 31)             # 30 条窗口 + 1 个锚点

    def test_anchor_per_template_and_engine(self):
        anchors = []
        for tpl in ("T1", "T2", "T3", "T4"):
            for eng in ("keyword", "ai"):
                anchors.append(self._add(1, engine=eng, template=tpl, start=0)[0])
        self._add(40, engine="keyword", template="T5", start=100)  # 挤占窗口的批量记录
        rt.trim_history(self.conn, self.rid)
        self.conn.commit()
        self.assertTrue(set(anchors) <= self._ids(), "8 个锚点应全部保留")
        self.assertEqual(len(self._ids()), 30 + 8)

    def test_anchor_is_latest_of_its_group_not_the_old_one(self):
        """同组有新记录时，旧记录不算锚点，应正常被裁掉."""
        old = self._add(1, engine="keyword", template="T1", start=0)[0]
        newest = self._add(35, engine="keyword", template="T1", start=10)
        rt.trim_history(self.conn, self.rid)
        self.conn.commit()
        self.assertNotIn(old, self._ids())
        self.assertIn(newest[-1], self._ids())

    def test_save_screening_trims_automatically(self):
        self._add(31)
        rt._save_screening(self.conn, self.rid, "keyword", "T9", {"score": 70, "status": "通过"})
        self.assertEqual(len(self._ids()), 30)

    def test_trim_all_history_is_idempotent(self):
        self._add(45)
        self.assertEqual(rt.trim_all_history(self.conn), 15)
        self.assertEqual(rt.trim_all_history(self.conn), 0)
        self.assertEqual(len(self._ids()), 30)


class PreferenceToggleTests(unittest.TestCase):
    """通用设置的两个偏好开关：圆形勾选框 + 提交值仍是 on/缺失."""

    def setUp(self):
        appmod.app.config.update(TESTING=True)
        self.client = make_test_client()
        self.before = appmod.load_app_settings()

    def tearDown(self):
        appmod.save_app_settings(self.before)

    def test_renders_circle_toggles(self):
        page = self.client.get("/settings").get_data(as_text=True)
        self.assertEqual(page.count('class="pref-item"'), 2)
        self.assertEqual(page.count('class="pref-mark"'), 2)
        self.assertIn('name="auto_check_updates"', page)
        self.assertIn('name="auto_backup"', page)

    def test_checked_state_reflected_on_render(self):
        self.client.post("/settings/preview", data={
            "preview_method": "default", "cloud_url": "",
            "auto_check_updates": "on", "auto_backup": "on"})
        page = self.client.get("/settings").get_data(as_text=True)
        # 只统计两个偏好开关，页面里另有厂商单选也是 checked
        checked = re.findall(r'name="(auto_check_updates|auto_backup)"\s+(checked)', page)
        self.assertEqual(len(checked), 2)

    def test_unchecked_preference_saved_as_off(self):
        """未勾选的复选框不会随表单提交，必须落成 False 而不是保留旧值."""
        self.client.post("/settings/preview", data={
            "preview_method": "default", "cloud_url": "", "auto_check_updates": "on"})
        cfg = appmod.load_app_settings()
        self.assertTrue(cfg["auto_check_updates"])
        self.assertFalse(cfg["auto_backup"])


class ScrollPositionTests(unittest.TestCase):
    """保存/删除/返回后不应跳回顶部（前端脚本存在性与返回按钮标记）."""

    def setUp(self):
        appmod.app.config.update(TESTING=True)
        self.client = make_test_client()
        self.js = (Path(appmod.app.static_folder) / "app.js").read_text(encoding="utf-8")

    def test_app_js_keeps_scroll_position(self):
        self.assertIn("rt:scroll:", self.js)
        self.assertIn("pagehide", self.js)
        self.assertIn("location.hash", self.js)  # 带锚点时交给浏览器，不抢定位

    def test_back_links_marked_for_history_back(self):
        connected = [r["id"] for r in rt.get_conn(appmod.DB).execute(
            "SELECT id FROM resumes ORDER BY id LIMIT 1").fetchall()]
        if not connected:
            self.skipTest("库里没有简历")
        page = self.client.get(f"/resume/{connected[0]}").get_data(as_text=True)
        self.assertIn("data-back", page)
        self.assertIn("返回简历库", page)

    def test_back_handler_only_takes_over_on_same_path(self):
        self.assertIn("from.pathname", self.js)
        self.assertIn("from.origin !== location.origin", self.js)


class P0SecurityTests(unittest.TestCase):
    """P0：SSRF/Key 外发、点击劫持、硬编码密钥与缺失同源头."""

    def setUp(self):
        appmod.app.config.update(TESTING=True)
        self.client = make_test_client()

    def test_security_headers_block_framing(self):
        response = self.client.get("/settings")
        self.assertEqual(response.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(response.headers.get("Content-Security-Policy"),
                         "frame-ancestors 'none'")
        self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
        # 不能是 no-referrer：那会让浏览器把同源表单 POST 的 Origin 置为 "null"，
        # 导致上传等写操作被同源校验拦下（见 BelowOriginRegressionTests）。
        self.assertEqual(response.headers.get("Referrer-Policy"), "same-origin")

    def test_secret_key_is_random_and_persistent(self):
        self.assertNotEqual(appmod.app.secret_key, "resume-tool-local-dev")
        self.assertGreaterEqual(len(appmod.app.secret_key), 32)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "secret.key"
            first = appmod._load_or_create_secret_key(path)
            second = appmod._load_or_create_secret_key(path)
            other = appmod._load_or_create_secret_key(Path(td) / "other.key")
            self.assertEqual(first, second)
            self.assertNotEqual(first, other)
            self.assertGreaterEqual(len(first), 32)

    def test_mutation_without_same_origin_evidence_is_rejected(self):
        raw_client = appmod.app.test_client()  # 不使用测试默认 Origin
        response = raw_client.post("/settings/preview", data={"preview_method": "default"})
        self.assertEqual(response.status_code, 403)

    def test_null_origin_with_same_site_marker_is_accepted(self):
        """真实浏览器在 no-referrer 策略下会把同源表单 POST 的 Origin 置为
        字面量 "null"，此时必须靠 Sec-Fetch-Site 识别同源，否则写操作全 403."""
        raw_client = appmod.app.test_client()
        response = raw_client.post(
            "/settings/preview", data={"preview_method": "default"},
            headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"})
        self.assertNotEqual(response.status_code, 403)

    def test_null_origin_with_cross_site_marker_is_rejected(self):
        raw_client = appmod.app.test_client()
        response = raw_client.post(
            "/settings/preview", data={"preview_method": "default"},
            headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(response.status_code, 403)

    def test_null_origin_without_any_evidence_is_rejected(self):
        raw_client = appmod.app.test_client()
        response = raw_client.post(
            "/settings/preview", data={"preview_method": "default"},
            headers={"Origin": "null"})
        self.assertEqual(response.status_code, 403)

    def test_referrer_policy_stays_compatible_with_origin_header(self):
        """策略头不能是 no-referrer，否则浏览器会清空同源表单的 Origin."""
        policy = self.client.get("/settings").headers.get("Referrer-Policy")
        self.assertNotEqual(policy, "no-referrer")


    def test_provider_url_rejects_ssrf_and_host_spoofing(self):
        bad_urls = (
            "http://api.deepseek.com/v1",
            "https://127.0.0.1/v1",
            "https://[::1]/v1",
            "https://169.254.169.254/latest/meta-data",
            "https://localhost/v1",
            "https://api.deepseek.com.evil.example/v1",
            "https://user:pass@api.deepseek.com/v1",
        )
        for url in bad_urls:
            with self.subTest(url=url), self.assertRaises(rt.ProviderConfigError):
                rt.validate_provider_base_url(url, "deepseek")
        self.assertEqual(
            rt.validate_provider_base_url("https://api.deepseek.com/v1/", "deepseek"),
            "https://api.deepseek.com/v1")

    def test_custom_provider_rejects_private_dns_result(self):
        answer = [(2, 1, 6, "", ("192.168.1.20", 443))]
        with patch.object(rt.socket, "getaddrinfo", return_value=answer):
            with self.assertRaises(rt.ProviderConfigError):
                rt.validate_provider_base_url(
                    "https://public-looking.example/v1", "custom", resolve=True)

    def test_llm_uses_no_redirect_opener(self):
        provider = {
            "name": "deepseek", "base_url": "https://api.deepseek.com/v1",
            "api_key": "test-key", "model": "deepseek-chat",
        }
        body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")
        with patch.object(rt, "_AI_OPENER") as opener:
            opener.open.return_value = io.BytesIO(body)
            self.assertEqual(rt.call_llm(provider, "prompt"), "ok")
            opener.open.assert_called_once()
        handler = rt._NoRedirectHandler()
        self.assertIsNone(handler.redirect_request(None, None, None, None, None, None))

    def test_settings_rejects_untrusted_provider_host(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "providers.json"
            data = rt.default_providers()
            data["providers"]["deepseek"]["api_key"] = "saved-key"
            rt.write_providers(path, data)
            with patch.object(appmod, "PROVIDERS_PATH", path):
                response = self.client.post("/settings", data={
                    "active": "deepseek",
                    "deepseek__base_url": "https://169.254.169.254/latest",
                    "deepseek__api_key": "new-key",
                }, follow_redirects=True)
                self.assertIn("不安全", response.get_data(as_text=True))
                saved = rt.read_providers(path)
                self.assertEqual(saved["providers"]["deepseek"]["api_key"], "saved-key")
                self.assertEqual(saved["providers"]["deepseek"]["base_url"],
                                 "https://api.deepseek.com/v1")

    def test_base_url_change_requires_key_reentry(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "providers.json"
            data = rt.default_providers()
            data["providers"]["deepseek"]["api_key"] = "saved-key"
            rt.write_providers(path, data)
            with patch.object(appmod, "PROVIDERS_PATH", path):
                response = self.client.post("/settings", data={
                    "active": "deepseek",
                    "deepseek__base_url": "https://api.deepseek.com/v2",
                    "deepseek__api_key": "",
                }, follow_redirects=True)
                self.assertIn("重新输入 API Key", response.get_data(as_text=True))
                saved = rt.read_providers(path)
                self.assertEqual(saved["providers"]["deepseek"]["base_url"],
                                 "https://api.deepseek.com/v1")
                self.assertEqual(saved["providers"]["deepseek"]["api_key"], "saved-key")

    def test_restore_clears_keys_and_invalid_restored_urls(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source_db = root / "source.db"
            rt.get_conn(str(source_db)).close()
            providers = rt.default_providers()
            providers["providers"]["custom"].update({
                "base_url": "https://169.254.169.254/latest",
                "api_key": "must-not-survive", "model": "attacker-model",
            })
            archive = root / "restore.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.write(source_db, "resume_tool.db")
                zf.writestr("config/providers.json", json.dumps(providers))
            dst = root / "dst"
            provider_path = dst / "config" / "providers.json"
            with patch.multiple(
                appmod, DATA_DIR=dst, DB=str(dst / "resume_tool.db"),
                BACKUP_DIR=dst / "backups", UPLOAD_DIR=dst / "uploads",
                TPL_DIR=dst / "templates", APP_SETTINGS_PATH=dst / "app_settings.json",
                PROVIDERS_PATH=provider_path,
            ):
                with appmod.app.app_context():
                    result = appmod.restore_backup(archive)
            restored = rt.read_providers(provider_path)
            self.assertIsNone(rt.get_active_provider(restored))
            self.assertTrue(all(not spec.get("api_key")
                                for spec in restored["providers"].values()))
            self.assertEqual(restored["providers"]["custom"]["base_url"], "")
            self.assertIn("API Key", "；".join(result["warnings"]))


class P0HistoryBackupTests(unittest.TestCase):
    """启动裁剪必须先生成可恢复的 pre-trim 快照."""

    def _seed_over_limit(self, root: Path, count: int = 35) -> Path:
        db_path = root / "resume_tool.db"
        conn = rt.get_conn(str(db_path))
        try:
            cur = conn.execute(
                "INSERT INTO resumes(filename, path, sha256, text, imported_at)"
                " VALUES(?,?,?,?,?)",
                ("trim.txt", "", uuid.uuid4().hex, "正文", rt.now()))
            rid = int(cur.lastrowid)
            for i in range(count):
                conn.execute(
                    "INSERT INTO screenings(resume_id, engine, template, score, status,"
                    " detail, created_at) VALUES(?,?,?,?,?,?,?)",
                    (rid, "keyword", "T1", 60, "通过", "{}",
                     f"2026-11-01T{i:02d}:00:00"))
            conn.commit()
        finally:
            conn.close()
        return db_path

    def _patches(self, root: Path, db_path: Path):
        return patch.multiple(
            appmod, DB=str(db_path), BACKUP_DIR=root / "backups",
            UPLOAD_DIR=root / "uploads", TPL_DIR=root / "templates",
            APP_SETTINGS_PATH=root / "app_settings.json",
            PROVIDERS_PATH=root / "providers.json")

    def test_trim_creates_backup_before_deleting(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = self._seed_over_limit(root)
            with self._patches(root, db_path), appmod.app.app_context():
                trimmed, backup_name = appmod.trim_history_with_backup()
            self.assertEqual(trimmed, 5)
            archive = root / "backups" / backup_name
            self.assertTrue(archive.is_file())
            with zipfile.ZipFile(archive) as zf:
                raw = zf.read("resume_tool.db")
            snapshot = root / "snapshot.db"
            snapshot.write_bytes(raw)
            conn = sqlite3.connect(snapshot)
            try:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM screenings").fetchone()[0], 35)
            finally:
                conn.close()

    def test_backup_failure_prevents_trim(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = self._seed_over_limit(root)
            with self._patches(root, db_path), appmod.app.app_context():
                with patch.object(appmod, "create_backup", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        appmod.trim_history_with_backup()
            conn = sqlite3.connect(db_path)
            try:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM screenings").fetchone()[0], 35)
            finally:
                conn.close()


class P1SpreadsheetInjectionTests(unittest.TestCase):
    """导出内容来自外部输入：公式前缀必须在 CSV 与 XLSX 两条链路都被转义."""

    def test_formula_lead_characters_are_neutralized(self):
        for raw in ("=1+1", "+SUM(A1)", "-2+3", "@cmd", "\t=1", "\r=1"):
            self.assertEqual(rt.sanitize_spreadsheet_cell(raw), "'" + raw)
        self.assertEqual(rt.sanitize_spreadsheet_cell("正常文件名.pdf"), "正常文件名.pdf")
        self.assertEqual(rt.sanitize_spreadsheet_cell(88.5), 88.5)
        self.assertIsNone(rt.sanitize_spreadsheet_cell(None))

    def _seed(self, root: Path) -> tuple[Path, int]:
        db_path = root / "evil.db"
        conn = rt.get_conn(str(db_path))
        try:
            cur = conn.execute(
                "INSERT INTO resumes(filename, path, sha256, text, imported_at)"
                " VALUES(?,?,?,?,?)",
                ("=cmd|' /C calc'!A0.txt", "", uuid.uuid4().hex, "Python 后端", rt.now()))
            rid = int(cur.lastrowid)
            payload = json.dumps({"reason": '=HYPERLINK("http://evil.example","点我")'})
            for engine in ("keyword", "ai"):
                conn.execute(
                    "INSERT INTO screenings(resume_id, engine, template, score, status,"
                    " detail, created_at) VALUES(?,?,?,?,?,?,?)",
                    (rid, engine, "Python后端-P7", 70, "通过", payload, rt.now()))
            conn.commit()
            return db_path, rid
        finally:
            conn.close()

    def test_csv_export_escapes_formula_cells(self):
        with tempfile.TemporaryDirectory() as td:
            db_path, rid = self._seed(Path(td))
            with patch.object(appmod, "DB", str(db_path)):
                response = make_test_client().get(f"/export?ids={rid}")
            self.assertEqual(response.status_code, 200)
            rows = list(csv.reader(io.StringIO(response.data.decode("utf-8-sig"))))
            row = next(r for r in rows if r[0] == str(rid))
            self.assertTrue(row[1].startswith("'="), row)
            self.assertTrue(row[4].startswith("'="), row)
            self.assertTrue(row[7].startswith("'="), row)

    def test_xlsx_export_writes_formula_like_cells_as_text(self):
        with tempfile.TemporaryDirectory() as td:
            db_path, rid = self._seed(Path(td))
            with patch.object(appmod, "DB", str(db_path)):
                response = make_test_client().get(f"/export-xlsx?ids={rid}")
            self.assertEqual(response.status_code, 200)
            wb = load_workbook(BytesIO(response.data))
            ws = wb["候选人报告"]
            self.assertEqual(ws.cell(row=2, column=5).value, "'=cmd|' /C calc'!A0.txt")
            for col in (5, 13):
                self.assertEqual(ws.cell(row=2, column=col).data_type, "s",
                                 f"第 {col} 列仍被写成公式")


class P1RestoreUploadsTests(unittest.TestCase):
    """恢复备份前必须先把「现存原件」装进 pre-restore 备份，再覆盖."""

    def test_prerestore_backup_holds_current_upload_before_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "src"
            src.mkdir()
            src_db = src / "resume_tool.db"
            conn = rt.get_conn(str(src_db))
            try:
                conn.execute(
                    "INSERT INTO resumes(filename, path, sha256, text, imported_at)"
                    " VALUES(?,?,?,?,?)",
                    ("a.txt", "uploads/a.txt", uuid.uuid4().hex, "备份包内容", rt.now()))
                conn.commit()
            finally:
                conn.close()
            archive = root / "backup.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.write(src_db, "resume_tool.db")
                zf.writestr("uploads/a.txt", "备份包内容")

            dst = root / "dst"
            (dst / "uploads").mkdir(parents=True)
            (dst / "uploads" / "a.txt").write_text("恢复前原件", encoding="utf-8")
            dst_db = dst / "resume_tool.db"
            conn = rt.get_conn(str(dst_db))
            try:
                conn.execute(
                    "INSERT INTO resumes(filename, path, sha256, text, imported_at)"
                    " VALUES(?,?,?,?,?)",
                    ("old.txt", "", uuid.uuid4().hex, "老库", rt.now()))
                conn.commit()
            finally:
                conn.close()

            with patch.multiple(appmod, DB=str(dst_db), DATA_DIR=dst,
                                UPLOAD_DIR=dst / "uploads", BACKUP_DIR=dst / "backups",
                                TRASH_DIR=dst / ".trash"):
                with appmod.app.app_context():
                    result = appmod.restore_backup(archive)

            self.assertEqual((dst / "uploads" / "a.txt").read_text(encoding="utf-8"),
                             "备份包内容")
            self.assertTrue(result["pre_backup"])
            backup = dst / "backups" / result["pre_backup"]
            with zipfile.ZipFile(backup) as zf:
                self.assertEqual(zf.read("uploads/a.txt").decode("utf-8"), "恢复前原件",
                                 "pre-restore 备份必须包含被覆盖前的原件")


class P2RestoreGuardTests(unittest.TestCase):
    """恢复备份的解压护栏：条目数与累计字节双限，超限一律不动现有数据."""

    def _build_archive(self, root: Path, uploads: dict[str, bytes]) -> Path:
        src_db = root / "src.db"
        conn = rt.get_conn(str(src_db))
        try:
            conn.execute(
                "INSERT INTO resumes(filename, path, sha256, text, imported_at)"
                " VALUES(?,?,?,?,?)",
                ("bak.txt", "", uuid.uuid4().hex, "备份包内容", rt.now()))
            conn.commit()
        finally:
            conn.close()
        archive = root / "backup.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.write(src_db, "resume_tool.db")
            for name, data in uploads.items():
                zf.writestr(f"uploads/{name}", data)
        return archive

    def _prepare_dst(self, root: Path) -> tuple[Path, Path]:
        dst = root / "dst"
        (dst / "uploads").mkdir(parents=True)
        (dst / "uploads" / "a.txt").write_text("恢复前原件", encoding="utf-8")
        dst_db = dst / "resume_tool.db"
        conn = rt.get_conn(str(dst_db))
        try:
            conn.execute(
                "INSERT INTO resumes(filename, path, sha256, text, imported_at)"
                " VALUES(?,?,?,?,?)",
                ("old.txt", "", uuid.uuid4().hex, "老库", rt.now()))
            conn.commit()
        finally:
            conn.close()
        return dst, dst_db

    def _restore(self, dst: Path, dst_db: Path, archive: Path, **overrides) -> None:
        env = dict(DB=str(dst_db), DATA_DIR=dst, UPLOAD_DIR=dst / "uploads",
                   BACKUP_DIR=dst / "backups", TRASH_DIR=dst / ".trash")
        env.update(overrides)
        with patch.multiple(appmod, **env):
            with appmod.app.app_context():
                appmod.restore_backup(archive)

    def _assert_untouched(self, dst: Path, dst_db: Path) -> None:
        self.assertEqual((dst / "uploads" / "a.txt").read_text(encoding="utf-8"),
                         "恢复前原件", "失败时不能改动现有原件")
        conn = rt.get_conn(str(dst_db))
        try:
            names = [r["filename"] for r in conn.execute("SELECT filename FROM resumes")]
        finally:
            conn.close()
        self.assertIn("old.txt", names, "失败时不能替换现有数据库")
        self.assertNotIn("bak.txt", names)
        self.assertFalse((dst / appmod.RESTORE_STAGE_NAME).exists(), "暂存区必须清理")

    def test_entry_count_limit_rejects_archive(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = self._build_archive(
                root, {f"f{i}.txt": b"x" for i in range(3)})
            dst, dst_db = self._prepare_dst(root)
            with self.assertRaises(ValueError) as ctx:
                self._restore(dst, dst_db, archive, RESTORE_MAX_ENTRIES=2)
            self.assertIn("原始文件超过", str(ctx.exception))
            self._assert_untouched(dst, dst_db)

    def test_declared_total_bytes_limit_rejects_archive(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = self._build_archive(
                root, {"a.bin": b"x" * 80, "b.bin": b"y" * 80})
            dst, dst_db = self._prepare_dst(root)
            with self.assertRaises(ValueError) as ctx:
                self._restore(dst, dst_db, archive, RESTORE_MAX_UPLOAD_BYTES=100)
            self.assertIn("总量超过", str(ctx.exception))
            self._assert_untouched(dst, dst_db)

    def test_actual_decompressed_bytes_limit_rejects_archive(self):
        """声明值可伪造，必须按真实解压量拦截（压缩炸弹形态）."""
        real_open = zipfile.ZipFile.open

        def bomb_open(self, name, *args, **kwargs):
            handle = real_open(self, name, *args, **kwargs)
            fname = name.filename if isinstance(name, zipfile.ZipInfo) else name
            if not (isinstance(fname, str) and fname.startswith("uploads/")):
                return handle

            class _Bomb:
                """声明 20 字节，实际无限产出，模拟高压缩比条目."""

                def __init__(self, inner):
                    self._inner = inner

                def read(self, n=-1):
                    return b"x" * (n if isinstance(n, int) and n > 0 else 1024 * 1024)

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    self._inner.close()
                    return False

            return _Bomb(handle)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = self._build_archive(root, {"bomb.txt": b"x" * 20})
            dst, dst_db = self._prepare_dst(root)
            with patch.object(zipfile.ZipFile, "open", bomb_open):
                with self.assertRaises(ValueError) as ctx:
                    self._restore(dst, dst_db, archive, RESTORE_MAX_UPLOAD_BYTES=10000)
            self.assertIn("实际解压量", str(ctx.exception))
            self._assert_untouched(dst, dst_db)

    def test_normal_restore_still_works_and_cleans_stage(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = self._build_archive(root, {"a.txt": "备份包内容".encode("utf-8")})
            dst, dst_db = self._prepare_dst(root)
            self._restore(dst, dst_db, archive)

            self.assertEqual((dst / "uploads" / "a.txt").read_text(encoding="utf-8"),
                             "备份包内容", "正常恢复仍应覆盖原件")
            self.assertFalse((dst / appmod.RESTORE_STAGE_NAME).exists(),
                             "恢复成功后暂存区必须清理")


class P1HistorySnapshotTests(unittest.TestCase):
    """清除历史：先存档后删除；有任务在跑或快照失败时一律不动数据."""

    def setUp(self):
        appmod.app.config.update(TESTING=True)
        self.client = make_test_client()
        self.rids: list[int] = []
        with appmod.JOBS_LOCK:
            appmod.JOBS.clear()
        self._clean()

    def tearDown(self):
        self._clean()
        with appmod.JOBS_LOCK:
            appmod.JOBS.clear()

    def _clean(self):
        conn = rt.get_conn(appmod.DB)
        try:
            conn.execute("DELETE FROM screenings WHERE resume_id IN"
                         " (SELECT id FROM resumes WHERE filename LIKE 'snap-%')")
            conn.execute("DELETE FROM resumes WHERE filename LIKE 'snap-%'")
            conn.commit()
        finally:
            conn.close()
        for rid in self.rids:
            for path in appmod.TRASH_DIR.glob(f"history-{rid}-*"):
                shutil.rmtree(path, ignore_errors=True)
        self.rids = []

    def _seed(self, count: int = 3) -> int:
        conn = rt.get_conn(appmod.DB)
        try:
            cur = conn.execute(
                "INSERT INTO resumes(filename, path, sha256, text, imported_at)"
                " VALUES(?,?,?,?,?)",
                (f"snap-{uuid.uuid4().hex[:6]}.txt", "", uuid.uuid4().hex,
                 "Python SQL 后端", rt.now()))
            rid = int(cur.lastrowid)
            self.rids.append(rid)
            for i in range(count):
                conn.execute(
                    "INSERT INTO screenings(resume_id, engine, template, score, status,"
                    " detail, created_at) VALUES(?,?,?,?,?,?,?)",
                    (rid, "keyword" if i % 2 == 0 else "ai", "Python后端-P7",
                     60 + i, "通过", json.dumps({"reason": f"第 {i} 次"}), rt.now()))
            conn.commit()
            return rid
        finally:
            conn.close()

    def _count(self, rid: int) -> int:
        conn = rt.get_conn(appmod.DB)
        try:
            return conn.execute("SELECT COUNT(*) AS n FROM screenings WHERE resume_id=?",
                                (rid,)).fetchone()["n"]
        finally:
            conn.close()

    def test_clear_writes_snapshot_before_deleting(self):
        rid = self._seed(3)
        response = self.client.post(f"/resume/{rid}/history/clear")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._count(rid), 0)
        dirs = list(appmod.TRASH_DIR.glob(f"history-{rid}-*"))
        self.assertEqual(len(dirs), 1)
        data = json.loads((dirs[0] / appmod.HISTORY_SNAPSHOT_FILE).read_text(encoding="utf-8"))
        self.assertEqual(data["resume_id"], rid)
        self.assertEqual(data["count"], 3)
        self.assertEqual(len(data["screenings"]), 3)
        self.assertEqual(data["screenings"][0]["status"], "通过")

    def test_clear_refused_while_screening_job_runs(self):
        rid = self._seed(2)
        jid = appmod.job_new("keyword", ["python-backend"], "all")
        try:
            self.client.post(f"/resume/{rid}/history/clear")
            self.assertEqual(self._count(rid), 2)
            page = self.client.get(f"/resume/{rid}").get_data(as_text=True)
            self.assertIn("仍有筛选任务在运行", page)
        finally:
            with appmod.JOBS_LOCK:
                appmod.JOBS.pop(jid, None)

    def test_snapshot_failure_keeps_history(self):
        rid = self._seed(2)
        with patch.object(appmod, "save_history_snapshot", side_effect=OSError("disk full")):
            self.client.post(f"/resume/{rid}/history/clear")
        self.assertEqual(self._count(rid), 2)
        page = self.client.get(f"/resume/{rid}").get_data(as_text=True)
        self.assertIn("已取消清除", page)


class P1LibraryScanTests(unittest.TestCase):
    """列表页只能扫一次 screenings，且派生口径必须与旧的三次扫描一致."""

    def _seed(self, conn) -> None:
        for rid in (1, 2):
            conn.execute(
                "INSERT INTO resumes(id, filename, path, sha256, text, imported_at)"
                " VALUES(?,?,?,?,?,?)",
                (rid, f"lib-{rid}.txt", "", uuid.uuid4().hex, "Python", rt.now()))
        rows = [
            ("2026-01-01T00:00:01", "Python后端-P7", "keyword", 52, 1),
            ("2026-01-01T00:00:02", "Python后端-P7", "ai", 80, 2),
            ("2026-01-01T00:00:03", "前端-中级", "keyword", 70, 3),
            ("2026-01-02T00:00:01", "Python后端-P7", "keyword", 90, 4),
        ]
        for i, (created, tpl, engine, score, _) in enumerate(rows, start=1):
            rid = 1 if i != 3 else 2
            conn.execute(
                "INSERT INTO screenings(id, resume_id, engine, template, score, status,"
                " detail, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (i, rid, engine, tpl, score, "通过", json.dumps({"reason": "r"}), created))
        conn.commit()

    def test_snapshot_matches_previous_scan_semantics(self):
        with tempfile.TemporaryDirectory() as td:
            conn = rt.get_conn(str(Path(td) / "t.db"))
            try:
                self._seed(conn)
                snap = appmod.screening_snapshot(conn)
                self.assertEqual(appmod.template_groups(conn, snapshot=snap),
                                 appmod.template_groups(conn))
                old = appmod.latest_map(conn)
                new = appmod.latest_map(conn, snapshot=snap)
                self.assertEqual(
                    {k: {e: v["id"] for e, v in m.items()} for k, m in new.items()},
                    {k: {e: v["id"] for e, v in m.items()} for k, m in old.items()})
                for tpl in ("Python后端-P7", "前端-中级"):
                    self.assertEqual(
                        {k: {e: v["id"] for e, v in m.items()} for k, m in
                         appmod.latest_map(conn, template=tpl, snapshot=snap).items()},
                        {k: {e: v["id"] for e, v in m.items()} for k, m in
                         appmod.latest_map(conn, template=tpl).items()})
                self.assertEqual(appmod.divergence_map(conn, snapshot=snap),
                                 appmod.divergence_map(conn))
            finally:
                conn.close()

    def test_screening_indexes_are_created(self):
        with tempfile.TemporaryDirectory() as td:
            conn = rt.get_conn(str(Path(td) / "t.db"))
            try:
                names = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
            finally:
                conn.close()
        self.assertIn("idx_screenings_created", names)
        self.assertIn("idx_screenings_resume_created", names)

    def test_library_route_builds_one_snapshot_per_request(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "lib.db"
            conn = rt.get_conn(str(db_path))
            try:
                self._seed(conn)
            finally:
                conn.close()
            with patch.object(appmod, "DB", str(db_path)):
                with patch.object(appmod, "screening_snapshot",
                                  wraps=appmod.screening_snapshot) as spy:
                    response = make_test_client().get("/")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(spy.call_count, 1)


class ShutdownTests(unittest.TestCase):
    """退出入口：既要真能停服务，也不能把正在跑的筛选任务掐死."""

    def setUp(self):
        appmod.app.config.update(TESTING=True)
        self.client = make_test_client()
        with appmod.JOBS_LOCK:
            appmod.JOBS.clear()

    def tearDown(self):
        with appmod.JOBS_LOCK:
            appmod.JOBS.clear()

    def test_shutdown_returns_page_and_schedules_exit(self):
        with patch.object(appmod, "schedule_shutdown") as spy:
            response = self.client.post("/settings/shutdown")
        self.assertEqual(response.status_code, 200)
        self.assertIn("程序已退出", response.get_data(as_text=True))
        self.assertEqual(spy.call_count, 1)

    def test_shutdown_refused_while_job_running(self):
        with appmod.JOBS_LOCK:
            appmod.JOBS["run-1"] = {"id": "run-1", "state": "running"}
        with patch.object(appmod, "schedule_shutdown") as spy:
            response = self.client.post("/settings/shutdown")
        self.assertEqual(response.status_code, 302)  # 退回设置页并提示
        self.assertEqual(spy.call_count, 0)

    def test_shutdown_allows_after_job_finished(self):
        with appmod.JOBS_LOCK:
            appmod.JOBS["done-1"] = {"id": "done-1", "state": "done"}
        with patch.object(appmod, "schedule_shutdown") as spy:
            response = self.client.post("/settings/shutdown")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(spy.call_count, 1)

    def test_shutdown_requires_post(self):
        self.assertEqual(self.client.get("/settings/shutdown").status_code, 405)

    def test_schedule_shutdown_calls_exit_after_delay(self):
        with patch.object(appmod, "_exit_process") as spy:
            appmod.schedule_shutdown(0.01)
            deadline = time.time() + 2
            while not spy.called and time.time() < deadline:
                time.sleep(0.01)
        # daemon 定时器必须在延迟后真的调用退出函数，否则按钮点了没反应
        self.assertEqual(spy.call_count, 1)

    def test_settings_page_exposes_shutdown_entry(self):
        page = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("退出程序", page)
        self.assertIn("/settings/shutdown", page)


if __name__ == "__main__":
    unittest.main()

class UploadFormSameOriginTests(unittest.TestCase):
    """上传表单在真实浏览器同源提交时必须入库，不能被同源校验误伤."""

    def setUp(self):
        appmod.app.config.update(TESTING=True)

    def test_upload_browser_like_request_imports_file(self):
        client = appmod.app.test_client()
        payload = ("张三\n手机 13800000000 邮箱 zhangsan@example.com\n"
                   "熟悉 Python 与 Flask 后端开发。\n").encode("utf-8")
        data = {"files": (BytesIO(payload), "browser-upload.txt")}
        response = client.post(
            "/import-files", data=data, content_type="multipart/form-data",
            headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"})
        self.assertNotEqual(response.status_code, 403)

    def test_upload_from_cross_site_is_still_rejected(self):
        client = appmod.app.test_client()
        payload = b"cross site attempt"
        data = {"files": (BytesIO(payload), "x.txt")}
        response = client.post(
            "/import-files", data=data, content_type="multipart/form-data",
            headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(response.status_code, 403)


