from typing import Any, Dict, List, Optional, Tuple, Union
from app.services.retry import with_retry
from app.services.platform_sessions import resolve_buff_pay_method
from buff.buyer import BuffAuthExpired, BuffBuyer, PAY_METHOD_WECHAT
buff_timeout = 15
buff_retry_attempts = 2
def count_lowest_price_orders(orders: List[dict]) -> Tuple[float, int]:
    if not orders:
        return 0.0, 0
    lowest = float(orders[0].get("price", 0))
    if lowest <= 0:
        return 0.0, 0
    count = 0
    for o in orders:
        try:
            p = float(o.get("price", 0))
        except (ValueError, TypeError):
            continue
        if abs(p - lowest) < 1e-6:
            count += 1
        elif p < lowest:
            lowest = p
            count = 1
    return lowest, count
def first_order_at_price(orders: List[dict], price: float) -> Optional[dict]:
    for o in orders:
        try:
            p = float(o.get("price", 0))
        except (ValueError, TypeError):
            continue
        if abs(p - price) < 1e-6:
            return o
    return None
class BuffClient:
    def __init__(
        self,
        cookies: str,
        pay_method: str = "alipay",
        timeout_sec: int = buff_timeout,
    ) -> None:
        pm = resolve_buff_pay_method(pay_method)
        self._buyer = BuffBuyer(cookies, pay_method=pm)
        self._pay_method = pay_method
        self._timeout = timeout_sec
    def get_sell_orders(self, goods_id: int, game: str = "csgo") -> Optional[list]:
        return self._buyer.get_sell_orders(goods_id, game)
    def get_goods_steam_price_cny(self, search_name: str, game: str = "csgo") -> Optional[float]:
        return self._buyer.get_goods_steam_price_cny(search_name, game)
    def ask_seller_to_send(self, bill_order_id_or_ids: Union[str, List[str]], game: str = "csgo") -> dict[str, Any]:
        return self._buyer.ask_seller_to_send(bill_order_id_or_ids, game)
    @with_retry(max_attempts=buff_retry_attempts, fatal_exceptions=(BuffAuthExpired,))
    def lock_and_get_pay_url(
        self,
        game: str,
        goods_id: int,
        sell_order_id: str,
        price: str,
    ) -> Dict[str, Any]:
        return self._buyer.lock_and_get_pay_url(game, goods_id, sell_order_id, price)
    @with_retry(max_attempts=buff_retry_attempts, fatal_exceptions=(BuffAuthExpired,))
    def try_batch_buy(
        self,
        goods_id: int,
        game: str,
        orders: List[dict],
        unit_price: float,
        num: int,
    ) -> Optional[Dict[str, Any]]:
        if num < 1 or self._buyer.pay_method != PAY_METHOD_WECHAT:
            return None
        batch_id = self._buyer.batch_buy_create(goods_id, unit_price, num, game)
        if not batch_id:
            return None
        pay_url = self._buyer.batch_buy_wx_qrcode(batch_id, game)
        if not pay_url:
            return None
        return {
            "success": True,
            "pay_url": pay_url,
            "pay_type": "wechat",
            "batch_id": batch_id,
            "unit_price": unit_price,
            "num": num,
            "total_price": unit_price * num,
        }
    @with_retry(max_attempts=buff_retry_attempts, fatal_exceptions=(BuffAuthExpired,))
    def batch_buy_find_and_finalize(
        self,
        goods_id: int,
        game: str,
        max_price: float,
        num: int,
        batch_id: str,
    ) -> List[Dict[str, Any]]:
        orders = self.get_sell_orders(goods_id, game)
        if not orders:
            return []
        matched = []
        for o in orders:
            if len(matched) >= num:
                break
            try:
                p = float(o.get("price", 0))
            except (ValueError, TypeError):
                continue
            if p <= max_price:
                bill_order_id = self._buyer.batch_buy_finalize(
                    game, goods_id, str(o.get("id", "")), str(o.get("price", "")), batch_id
                )
                if bill_order_id:
                    matched.append({"id": o.get("id"), "price": p, "bill_order_id": bill_order_id})
        return matched
def create_buff_client_from_config(credentials: dict, config: dict) -> BuffClient:
    cookies = credentials.get("cookies", "")
    buff_cfg = config.get("buff", {})
    pay_method = buff_cfg.get("pay_method", "alipay")
    return BuffClient(cookies, pay_method=pay_method, timeout_sec=buff_timeout)
