from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.services.uuyp_orders import (
    get_manual_status,
    list_uuyp_order_records,
    prepare_manual_direct,
    record_manual_direct_order,
    set_manual_control,
    submit_purchase_order,
)

router = APIRouter()


class ManualControlBody(BaseModel):
    enabled: Optional[bool] = None
    paused: Optional[bool] = None


class UuypOrderBody(BaseModel):
    market_hash_name: str = ""
    template_id: Optional[str] = None
    price: float = 0
    quantity: int = 1


class ManualOpenBody(UuypOrderBody):
    target_price: Optional[float] = None
    open_browser: bool = True


class ManualRecordBody(UuypOrderBody):
    order_no: Optional[str] = None


@router.get("/api/uuyp/manual-direct/status")
def api_uuyp_manual_direct_status():
    return {"ok": True, "status": get_manual_status()}


@router.post("/api/uuyp/manual-direct/control")
def api_uuyp_manual_direct_control(body: ManualControlBody):
    return {"ok": True, "status": set_manual_control(enabled=body.enabled, paused=body.paused)}


@router.post("/api/uuyp/manual-direct/open")
def api_uuyp_manual_direct_open(body: ManualOpenBody):
    target = body.target_price if body.target_price is not None else body.price
    return prepare_manual_direct(
        body.market_hash_name,
        target_price=float(target or 0),
        quantity=body.quantity,
        template_id=body.template_id or "",
        open_browser=body.open_browser,
    )


@router.post("/api/uuyp/manual-direct/record")
def api_uuyp_manual_direct_record(body: ManualRecordBody):
    return record_manual_direct_order(
        body.market_hash_name,
        price=body.price,
        quantity=body.quantity,
        template_id=body.template_id or "",
        order_no=body.order_no or "",
    )


@router.post("/api/uuyp/purchase-order")
def api_uuyp_purchase_order(body: UuypOrderBody):
    return submit_purchase_order(
        body.market_hash_name,
        price=body.price,
        quantity=body.quantity,
        template_id=body.template_id or "",
    )


@router.get("/api/uuyp/orders")
def api_uuyp_orders():
    return list_uuyp_order_records()
