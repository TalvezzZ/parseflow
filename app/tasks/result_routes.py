"""Task-scoped access to file-backed results, with legacy embedded-result reads."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from app.tasks.result_store import (ResultPageTooLarge, ResultStorageError, load_result_manifest,
    read_unit_records, resolve_result_resource, safe_result_path)


def result_router(task_manager, artifact_repository) -> APIRouter:
    router = APIRouter(tags=["解析结果"])

    async def task_result(task_id):
        task = await task_manager.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail={"code": "task_not_found", "message": "未找到任务"})
        if task.status not in {"succeeded", "partial"} or task.result is None:
            raise HTTPException(status_code=404, detail={"code": "result_not_ready", "message": "解析结果尚不可用"})
        return task

    def legacy_units(task):
        document = task.result.document or {}
        return [{"unit_id": f"unit-{index:06d}", "kind": key, "label": label,
                 "record_count": len(document[key]), "records": document[key]}
                for index, (key, label) in enumerate((("pages", "页面"), ("blocks", "内容块"), ("tables", "表格"), ("images", "图片")))
                if isinstance(document.get(key), list) and document[key]]

    @router.get("/api/v1/tasks/{task_id}/result")
    async def get_result(task_id: str) -> dict:
        task = await task_result(task_id)
        base = f"/api/v1/tasks/{task_id}/result"
        if not task.result.result_ref:
            return {"schema_version": 0, "legacy": True,
                    "document_type": (task.result.document or {}).get("document_type", "media"),
                    "units": [{key: value for key, value in unit.items() if key != "records"} for unit in legacy_units(task)],
                    "document_url": base + "/document", "representations": {}}
        try:
            manifest = await asyncio.to_thread(load_result_manifest, artifact_repository.workspace(task_id))
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=404, detail={"code": "result_missing", "message": "完整解析结果不可用"}) from exc
        manifest["document_url"] = base + "/document"
        external = manifest.get("external_storage") or {}
        if external.get("filename") == "workbook/manifest.json":
            from app.skills.excel.store import load_workbook_manifest
            workbook = await asyncio.to_thread(load_workbook_manifest,
                safe_result_path(artifact_repository.workspace(task_id), "workbook/manifest.json"))
            manifest["workbook"] = workbook
            external["download_url"] = base + "/files/workbook/manifest.json"
            for sheet in workbook.get("sheets", []):
                for chunk in sheet.get("chunks", []):
                    chunk["download_url"] = base + "/files/workbook/" + quote(chunk["file"], safe="/")
        for resource in manifest.get("resources", []):
            resource["download_url"] = base + "/files/" + quote(resource["storage_key"], safe="/")
        return manifest

    @router.get("/api/v1/tasks/{task_id}/result/document", response_model=None)
    async def download_document(task_id: str):
        task = await task_result(task_id)
        if not task.result.result_ref:
            return JSONResponse(task.result.document or {"conversion": task.result.conversion},
                                headers={"Content-Disposition": 'attachment; filename="document.json"'})
        try:
            path = await asyncio.to_thread(resolve_result_resource, artifact_repository.workspace(task_id), "result/document.json")
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=404, detail={"code": "result_missing", "message": "完整解析结果不可用"}) from exc
        return FileResponse(path, filename="document.json", media_type="application/json")

    @router.get("/api/v1/tasks/{task_id}/result/units/{unit_id}/records")
    async def get_records(task_id: str, unit_id: str, offset: int = Query(default=0, ge=0, le=2**63-1),
                          limit: int = Query(default=100, ge=1, le=200)) -> dict:
        task = await task_result(task_id)
        if not task.result.result_ref:
            unit = next((unit for unit in legacy_units(task) if unit["unit_id"] == unit_id), None)
            if unit is None:
                raise HTTPException(status_code=404, detail={"code": "result_unit_missing", "message": "未找到内容单元"})
            rows = []
            size = 0
            for record in unit["records"][offset:offset+limit]:
                cost = len(json.dumps(record, ensure_ascii=False).encode())
                if size + cost > 1024 * 1024:
                    if not rows:
                        raise HTTPException(status_code=413, detail={"code": "result_record_too_large", "message": "记录超过预览预算，请下载完整结果"})
                    break
                rows.append(record)
                size += cost
            end = offset + len(rows)
            return {"unit_id": unit_id, "records": rows, "next_offset": end if end < unit["record_count"] else None}
        try:
            return await asyncio.to_thread(read_unit_records, artifact_repository.workspace(task_id), unit_id, offset=offset, limit=limit)
        except ResultPageTooLarge as exc:
            raise HTTPException(status_code=413, detail={"code": "result_record_too_large", "message": str(exc)}) from exc
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail={"code": "result_unit_missing", "message": "未找到内容单元"}) from exc
        except ResultStorageError as exc:
            raise HTTPException(status_code=422, detail={"code": "invalid_result_page", "message": str(exc)}) from exc

    @router.get("/api/v1/tasks/{task_id}/result/files/{storage_key:path}")
    async def download_resource(task_id: str, storage_key: str) -> FileResponse:
        task = await task_result(task_id)
        if not task.result.result_ref:
            raise HTTPException(status_code=404, detail={"code": "result_missing", "message": "旧任务没有分块文件引用"})
        try:
            path = await asyncio.to_thread(resolve_result_resource, artifact_repository.workspace(task_id), storage_key)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=404, detail={"code": "result_file_missing", "message": "结果文件不可用"}) from exc
        return FileResponse(path, filename=path.name)

    return router
