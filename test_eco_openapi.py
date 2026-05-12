from __future__ import annotations

import json
import sys

from eco import EcoBuyer
from eco.openapi_client import EcoOpenAPIClient, load_eco_openapi_config


def main() -> int:
    cfg = load_eco_openapi_config()
    if cfg is None:
        print("ECO OpenAPI credentials missing")
        return 2

    client = EcoOpenAPIClient(cfg, timeout=20)
    signed = client.signed_payload({"GameID": "730"})
    print("signed_payload_keys", sorted(k for k in signed.keys() if k != "Sign"))
    print("sign_length", len(signed.get("Sign", "")))

    sample = sys.argv[1] if len(sys.argv) > 1 else "Operation Broken Fang Case"
    price_data = client.post("/Api/Market/BatchSearchSellingPrice", {"GameID": "730", "HashName": [sample]})
    print("batch_price_code", price_data.get("ResultCode"), price_data.get("ResultMsg"))
    result_data = price_data.get("ResultData")
    print("batch_price_shape", type(result_data).__name__)
    print("batch_price_preview", json.dumps(result_data, ensure_ascii=False)[:500])

    buyer = EcoBuyer(client=client)
    listings = buyer.sell_goods_list(sample, page_size=3)
    print("sell_goods_list_count", len(listings))
    if listings:
        first = listings[0]
        print(
            "first_listing",
            {
                "GoodsNum": first.get("GoodsNum"),
                "AssetId": first.get("AssetId"),
                "SellingPrice": first.get("SellingPrice"),
                "HashName": first.get("HashName"),
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
