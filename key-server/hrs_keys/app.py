"""HTTP API of the key server. See API.md.

Client (hrs):  POST /v1/activate, GET /v1/status, GET /v1/bundle/latest
Admin:         /v1/admin/... with `Authorization: Bearer <HRS_KEYS_ADMIN_TOKEN>`
"""
import hmac

from fastapi import Depends, FastAPI, Header, Query, Request, UploadFile
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

    def admin(authorization: str | None = Header(None)):
        token = bearer(authorization)
        if not ks.s.admin_token or not hmac.compare_digest(token, ks.s.admin_token):
            raise KeyServerError("forbidden", "Неверный токен администратора", 403)

    # --- client ---

    @app.post("/v1/activate")
    def activate(body: Activate):
        return ks.activate(body.key, body.install_id)

    @app.get("/v1/status")
    def status(authorization: str | None = Header(None)):
        return ks.status(bearer(authorization))

    @app.get("/v1/bundle/latest")
    def bundle(authorization: str | None = Header(None)):
        meta, data = ks.bundle(bearer(authorization))
        return Response(data, media_type="application/zip", headers={
            "X-Bundle-Version": meta["version"], "X-Bundle-Sha256": meta["sha256"],
            "X-Bundle-Signature": meta["signature"],
            "Content-Disposition": f'attachment; filename="methodology-{meta["version"]}.zip"'})

    @app.get("/v1/health")
    def health():
        return {"ok": True}

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

    @app.get("/v1/admin/bundles", dependencies=a)
    def bundles():
        return ks.bundle_list()

    @app.post("/v1/admin/bundles", dependencies=a, status_code=201)
    async def publish(file: UploadFile):
        return ks.publish(await file.read())

    @app.get("/v1/admin/signing-key", dependencies=a)
    def signing_key():
        return {"public_key": ks.public_key(), "algorithm": "Ed25519"}

    return app

