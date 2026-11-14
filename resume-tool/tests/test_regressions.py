"""企业可用性关键回归：导出、配置容错、模板路径和任务状态."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

import app as appmod
import resume_tool as rt


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
                appmod.ensure_data_layout()
                self.assertTrue((data / "resume_tool.db").is_file())
                self.assertTrue((data / "uploads" / "batch1" / "a.txt").is_file())
                self.assertTrue((data / "config" / "providers.json").is_file())
                self.assertTrue((data / "config" / "templates" / "default.json").is_file())
                self.assertTrue((legacy / "resume_tool.db").is_file())  # 旧数据不删除

    def test_relative_upload_path_and_normalization(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old_abs = root / "old" / "uploads" / "batch" / "a.txt"
            old_abs.parent.mkdir(parents=True)
            old_abs.write_text("x", encoding="utf-8")
            conn = sqlite3.connect(root / "test.db")
            conn.execute("CREATE TABLE resumes(id INTEGER PRIMARY KEY, path TEXT)")
            conn.execute("INSERT INTO resumes(id, path) VALUES(1, ?)", (str(old_abs),))
            conn.commit()
            with patch.object(appmod, "DATA_DIR", root):
                changed = appmod.normalize_upload_paths(conn)
                self.assertEqual(changed, 1)
                stored = conn.execute("SELECT path FROM resumes WHERE id=1").fetchone()[0]
                self.assertEqual(stored, "uploads/batch/a.txt")
                self.assertEqual(appmod.stored_resume_path(stored),
                                 (root / "uploads" / "batch" / "a.txt").resolve())
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
                response = appmod.app.test_client().post(
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
                client = appmod.app.test_client()
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
                                BACKUP_DIR=root / "backups"):
                client = appmod.app.test_client()
                client.post(f"/trash/{rid}/purge")

            self.assertFalse(stored.exists(), "彻底删除后上传文件应被清理")
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
                appmod.app.test_client().post(f"/trash/{rid}/purge")

            self.assertTrue(external.is_file(), "数据目录之外的原文件不应被擅自删除")

    def test_orphan_cleanup_only_removes_unreferenced_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            uploads = root / "uploads"
            db_path, _, stored = self._seed(root, uploads)
            orphan = uploads / "batch-2" / "重复导入副本.txt"
            orphan.parent.mkdir(parents=True, exist_ok=True)
            orphan.write_text("重复内容", encoding="utf-8")

            with patch.multiple(appmod, DB=str(db_path), DATA_DIR=root, UPLOAD_DIR=uploads,
                                BACKUP_DIR=root / "backups"):
                client = appmod.app.test_client()
                page = client.get("/settings")
                self.assertEqual(page.status_code, 200)
                self.assertIn("清理孤立原文件", page.get_data(as_text=True))
                client.post("/settings/uploads/clean")

            self.assertFalse(orphan.exists(), "孤立文件应被清理")
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
                response = appmod.app.test_client().get("/settings/diagnostics")

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
                client = appmod.app.test_client()
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
                client = appmod.app.test_client()
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
        self.client = appmod.app.test_client()

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
        response = self.client.get("/resume/11")
        self.assertEqual(response.status_code, 200)
        self.assertIn('action="/resume/11"', response.get_data(as_text=True))

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
                response = self.client.get(
                    f"/export-xlsx?ids={ids[0]},{ids[1]}&ids={ids[0]},{ids[1]}")
            self.assertEqual(response.status_code, 200)
            wb = load_workbook(BytesIO(response.data), read_only=True)
            exported = [row[0] for row in wb["候选人报告"].iter_rows(
                min_row=2, values_only=True) if row[0] is not None]
            self.assertEqual(exported, ids)


if __name__ == "__main__":
    unittest.main()
