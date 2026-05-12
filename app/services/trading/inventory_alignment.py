from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlmodel import select

from app.database import PlatformAction, Purchase


InventoryScanner = Callable[[], tuple[bool, list[dict[str, Any]], str]]


@dataclass
class InventoryAlignmentResult:
    scanned: int = 0
    pending: int = 0
    matched: int = 0
    updated_actions: int = 0
    skipped: list[dict[str, Any]] = field(default_factory=list)


def _item_assetid(row: dict[str, Any]) -> str:
    return str(row.get("assetid") or row.get("asset_id") or row.get("AssetId") or "").strip()


def _item_name(row: dict[str, Any]) -> str:
    return str(
        row.get("market_hash_name")
        or row.get("marketHashName")
        or row.get("hash_name")
        or row.get("name")
        or ""
    ).strip()


def _purchase_name(row: Purchase) -> str:
    return str(row.name or "").strip()


def _inventory_by_name(rows: list[dict[str, Any]], used_assetids: set[str]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        assetid = _item_assetid(row)
        name = _item_name(row)
        if not assetid or not name or assetid in used_assetids:
            continue
        grouped.setdefault(name, []).append(row)
    for items in grouped.values():
        items.sort(key=lambda item: _item_assetid(item))
    return grouped


class InventoryAlignmentService:
    """Bind received Steam inventory assets back to local Purchase rows."""

    def __init__(self, inventory_scanner: InventoryScanner | None = None):
        self.inventory_scanner = inventory_scanner

    def run(
        self,
        session,
        *,
        inventory: list[dict[str, Any]] | None = None,
        limit: int = 100,
        dry_run: bool = False,
        now: float | None = None,
    ) -> InventoryAlignmentResult:
        ts = time.time() if now is None else float(now)
        limit = max(1, min(int(limit or 100), 1000))
        result = InventoryAlignmentResult()
        inventory_rows = self._load_inventory(inventory)
        result.scanned = len(inventory_rows)

        used_assetids = {
            str(row.assetid).strip()
            for row in session.execute(select(Purchase).where(Purchase.assetid.is_not(None))).scalars().all()
            if str(row.assetid or "").strip()
        }
        available_by_name = _inventory_by_name(inventory_rows, used_assetids)
        pending_rows = session.execute(
            select(Purchase)
            .where((Purchase.assetid.is_(None)) | (Purchase.assetid == ""))
            .order_by(Purchase.at.asc(), Purchase.id.asc())
            .limit(limit)
        ).scalars().all()
        result.pending = len(pending_rows)

        for purchase in pending_rows:
            name = _purchase_name(purchase)
            if not name:
                result.skipped.append({"purchase_id": purchase.id, "reason": "purchase_name_missing"})
                continue
            candidates = available_by_name.get(name) or []
            if not candidates:
                result.skipped.append({"purchase_id": purchase.id, "name": name, "reason": "inventory_match_missing"})
                continue
            item = candidates.pop(0)
            assetid = _item_assetid(item)
            if not assetid:
                result.skipped.append({"purchase_id": purchase.id, "name": name, "reason": "assetid_missing"})
                continue

            purchase.assetid = assetid
            purchase.pending_receipt = False
            session.add(purchase)
            result.matched += 1
            used_assetids.add(assetid)

            action_id = int(purchase.source_action_id or 0)
            if action_id:
                action = session.get(PlatformAction, action_id)
                if action is not None and not str(action.assetid or "").strip():
                    action.assetid = assetid
                    action.updated_at = ts
                    session.add(action)
                    result.updated_actions += 1

        if dry_run:
            session.rollback()
        else:
            session.commit()
        return result

    def _load_inventory(self, inventory: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        if inventory is not None:
            return [row for row in inventory if isinstance(row, dict)]
        if self.inventory_scanner is None:
            return []
        ok, rows, err = self.inventory_scanner()
        if not ok:
            raise RuntimeError(err or "inventory scan failed")
        return [row for row in (rows or []) if isinstance(row, dict)]
