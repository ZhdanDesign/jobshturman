"""HTTP API of the key server. See API.md.

Client (hrs):  POST /v1/activate, GET /v1/status, GET /v1/bundle/latest; header X-Hrs-Version
Admin:         /v1/admin/... with `Authorization: Bearer <HRS_KEYS_ADMIN_TOKEN>`
Methodologist: /v1/admin/methodology/... and the bundle list with a token of HRS_KEYS_METHODOLOGIST_TOKEN
"""
import hmac

from fastapi import Depends, FastAPI, Form, Header, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from .service import KeyServer, KeyServerError, Settings


class Activate(BaseModel):
    key: str = Field(max_length=64)
    install_id: str = Field(max_length=100)
    client: str | None = Field(None, max_length=40)


class Issue(BaseModel):
    tariff_id: str
    months: int | None = Field(None, ge=1, le=60)
    expires_at: str | None = None
    order_ref: str | None = Field(None, max_length=64, description="номер заказа в платёжном сервисе, без ФИО")
    device_limit: int | None = Field(None, ge=1, le=10)


class Extend(BaseModel):
    months: int | None = Field(None, ge=1, le=60)
    until: str | None = None


class Channel(BaseModel):
    channel: str = Field(pattern="^(stable|test)$")


class Publish(BaseModel):
    channel: str = Field(pattern="^(stable|test)$")
    note: str = Field(min_length=1, max_length=1000)  # what changed: people see it before they update
    important: bool = False  # installed without asking, with the note as the explanation


class Accept(BaseModel):
    version: str = Field(min_length=1, max_length=80)


class FileSave(BaseModel):
    path: str = Field(max_length=300)
    text: str = Field(max_length=2_000_000)
    base_sha: str | None = Field(None, max_length=64)
    message: str | None = Field(None, max_length=200)


class RouteSave(BaseModel):
    steps: list[dict]
    stages: list[dict]
    version: str | None = Field(None, max_length=40)


class Revoke(BaseModel):
    reason: str = Field(pattern="^(refund|violation|reissue|other)$")


class TariffPatch(BaseModel):
    title: str | None = Field(None, max_length=60)
    price_rub_from: int | None = Field(None, ge=0)
    price_rub_to: int | None = Field(None, ge=0)
    months: int | None = Field(None, ge=1, le=60)
    device_limit: int | None = Field(None, ge=1, le=10)
    active: bool | None = None


def create_app(settings: Settings | None = None, admin_origins: list[str] | None = None) -> FastAPI:
    ks = KeyServer(settings or Settings.from_env())
    app = FastAPI(title="JobShturman keys", version="1", docs_url=None, redoc_url=None, openapi_url="/v1/openapi.json")
    app.state.ks = ks
    if admin_origins:
        app.add_middleware(CORSMiddleware, allow_origins=admin_origins, allow_methods=["*"],
                           allow_headers=["Authorization", "Content-Type"])

    @app.exception_handler(KeyServerError)
    async def on_error(request: Request, exc: KeyServerError):
        return JSONResponse({"error": exc.code, "message": str(exc)}, status_code=exc.status)

    def bearer(authorization: str | None) -> str:
        scheme, _, value = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not value:
            raise KeyServerError("unauthorized", "Нужен заголовок Authorization: Bearer ...", 401)
        return value.strip()

    # HRS_KEYS_ADMIN_TOKEN may list several tokens, comma separated: one per admin, each revoked on its own.
    admin_tokens = [t.strip() for t in (ks.s.admin_token or "").split(",") if t.strip()]

    # The methodologist (the author of the methodology) edits and publishes it and sees nothing else.
    method_tokens = [t.strip() for t in (ks.s.methodologist_token or "").split(",") if t.strip()]

    def role_of(authorization: str | None) -> str:
        token = bearer(authorization)
        if any(hmac.compare_digest(token, known) for known in admin_tokens):
            return "admin"
        if any(hmac.compare_digest(token, known) for known in method_tokens):
            return "methodologist"
        raise KeyServerError("forbidden", "Неверный токен администратора", 403)

    def admin(authorization: str | None = Header(None)):
        if role_of(authorization) != "admin":
            raise KeyServerError("forbidden", "Этот раздел только для администратора", 403)

    def editor(authorization: str | None = Header(None)) -> str:
        return role_of(authorization)

    # --- client ---

    @app.post("/v1/activate")
    def activate(body: Activate, x_hrs_version: str | None = Header(None)):
        return ks.activate(body.key, body.install_id, x_hrs_version)

    @app.get("/v1/status")
    def status(authorization: str | None = Header(None), x_hrs_version: str | None = Header(None)):
        return ks.status(bearer(authorization), x_hrs_version)

    @app.get("/v1/bundle/latest")
    def bundle(authorization: str | None = Header(None), x_hrs_version: str | None = Header(None)):
        meta, data = ks.bundle(bearer(authorization), x_hrs_version)
        return Response(data, media_type="application/zip", headers={
            "X-Bundle-Version": meta["version"], "X-Bundle-Sha256": meta["sha256"],
            "X-Bundle-Signature": meta["signature"],
            "Content-Disposition": f'attachment; filename="methodology-{meta["version"]}.zip"'})

    @app.post("/v1/bundle/accept")
    def accept(body: Accept, authorization: str | None = Header(None), x_hrs_version: str | None = Header(None)):
        return ks.accept(bearer(authorization), body.version, x_hrs_version)

    @app.get("/v1/health")
    def health():
        return {"ok": True}

    # Public: the landing shows prices from here, so an admin edit reaches the page without a redeploy.
    @app.get("/v1/tariffs")
    def public_tariffs(response: Response):
        response.headers["Cache-Control"] = "public, max-age=60"
        return [{"id": t["id"], "title": t["title"], "price_rub_from": t["price_rub_from"],
                 "price_rub_to": t["price_rub_to"], "months": t["months"], "device_limit": t["device_limit"],
                 "active": bool(t["active"])} for t in ks.tariffs()]

    # --- admin ---

    a = [Depends(admin)]

    @app.get("/v1/admin/stats", dependencies=a)
    def stats():
        return ks.stats()

    @app.get("/v1/admin/tariffs", dependencies=a)
    def tariffs():
        return ks.tariffs()

    @app.patch("/v1/admin/tariffs/{tariff_id}", dependencies=a)
    def tariff_patch(tariff_id: str, body: TariffPatch):
        fields = body.model_dump(exclude_none=True)
        if "active" in fields:
            fields["active"] = int(fields["active"])
        return ks.update_tariff(tariff_id, **fields)

    @app.get("/v1/admin/keys", dependencies=a)
    def keys(state: str | None = None):
        return ks.keys(state)

    @app.post("/v1/admin/keys", dependencies=a, status_code=201)
    def issue(body: Issue):
        return ks.issue(**body.model_dump())

    @app.get("/v1/admin/keys/{key_id}", dependencies=a)
    def key(key_id: str):
        return ks.key(key_id)

    @app.post("/v1/admin/keys/{key_id}/extend", dependencies=a)
    def extend(key_id: str, body: Extend):
        return ks.extend(key_id, body.months, body.until)

    @app.post("/v1/admin/keys/{key_id}/revoke", dependencies=a)
    def revoke(key_id: str, body: Revoke):
        return ks.revoke(key_id, body.reason)

    @app.post("/v1/admin/keys/{key_id}/restore", dependencies=a)
    def restore(key_id: str):
        return ks.restore(key_id)

    @app.post("/v1/admin/keys/{key_id}/reset-devices", dependencies=a)
    def reset_devices(key_id: str):
        return ks.reset_devices(key_id)

    @app.delete("/v1/admin/keys/{key_id}/devices/{install}", dependencies=a)
    def unbind_device(key_id: str, install: str):
        return ks.unbind_device(key_id, install)

    @app.get("/v1/admin/keys/{key_id}/events", dependencies=a)
    def key_events(key_id: str, limit: int = Query(100, ge=1, le=1000)):
        return ks.events(key_id, limit)

    @app.get("/v1/admin/whoami")
    def whoami(role: str = Depends(editor)):
        return {"role": role}

    @app.post("/v1/admin/keys/{key_id}/channel", dependencies=a)
    def key_channel(key_id: str, body: Channel):
        return ks.set_channel(key_id, body.channel)

    @app.get("/v1/admin/bundles")
    def bundles(role: str = Depends(editor)):
        return ks.bundle_list()

    @app.post("/v1/admin/bundles/{version}/current")
    def bundle_current(version: str, body: Channel, role: str = Depends(editor)):
        return ks.set_current(version, body.channel)

    # --- the methodology editor ---

    m = "/v1/admin/methodology"

    @app.get(m)
    def draft(role: str = Depends(editor)):
        return ks.draft()

    @app.get(m + "/file")
    def draft_file(path: str, revision: int | None = None, role: str = Depends(editor)):
        return ks.draft_file(path, revision)

    @app.put(m + "/file")
    def draft_save(body: FileSave, role: str = Depends(editor)):
        return ks.draft_save(body.path, body.text, body.base_sha, role, body.message)

    @app.delete(m + "/file")
    def draft_delete(path: str, base_sha: str, role: str = Depends(editor)):
        return ks.draft_delete(path, base_sha, role)

    @app.post(m + "/upload")
    async def draft_upload(file: UploadFile, path: str | None = Query(None, max_length=300),
                           role: str = Depends(editor)):
        return ks.draft_upload(file.filename or "file", await file.read(), role, path)

    @app.post(m + "/import-current")
    def draft_import(role: str = Depends(editor)):
        return ks.draft_import_current(role)

    @app.put(m + "/route")
    def draft_route(body: RouteSave, role: str = Depends(editor)):
        return ks.draft_route(body.steps, body.stages, role, body.version)

    @app.get(m + "/revisions")
    def draft_revisions(limit: int = Query(100, ge=1, le=1000), role: str = Depends(editor)):
        return ks.draft_revisions(limit)

    @app.get(m + "/revisions/{revision}/diff")
    def draft_diff(revision: int, against: int | None = None, role: str = Depends(editor)):
        return ks.draft_diff(revision, against)

    @app.post(m + "/revisions/{revision}/restore")
    def draft_restore(revision: int, role: str = Depends(editor)):
        return ks.draft_restore(revision, role)

    @app.post(m + "/check")
    def draft_check(role: str = Depends(editor)):
        return ks.draft_check()

    @app.post(m + "/publish", status_code=201)
    def draft_publish(body: Publish, role: str = Depends(editor)):
        return ks.draft_publish(body.channel, role, body.note, body.important)

    @app.get(m + "/export")
    def draft_export(revision: int | None = None, role: str = Depends(editor)):
        return Response(ks.draft_export(revision), media_type="application/zip", headers={
            "Content-Disposition": 'attachment; filename="methodology-draft.zip"'})

    @app.post("/v1/admin/bundles", dependencies=a, status_code=201)
    async def publish(file: UploadFile, note: str = Form(""), important: bool = Form(False)):
        return ks.publish(await file.read(), note=note, important=important)

    @app.get("/v1/admin/signing-key", dependencies=a)
    def signing_key():
        return {"public_key": ks.public_key(), "algorithm": "Ed25519"}

    return app

