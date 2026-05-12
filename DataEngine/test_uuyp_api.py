import asyncio
from curl_cffi.requests import AsyncSession

__test__ = False

async def test_uuyp_api():
    url = "https://api.youpin898.com/api/homepage/pc/goods/market/queryOnSaleCommodityList"

    # 完全复刻你抓包拿到的 Header，注意这里的风控字段 uk 和 deviceUk[cite: 8]
    headers = {
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'App-Version': '5.26.0',
        'AppVersion': '5.26.0',
        'Connection': 'keep-alive',
        'Content-Type': 'application/json',
        'Origin': 'https://www.youpin898.com',
        'Referer': 'https://www.youpin898.com/',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0',
        'appType': '1',
        'deviceId': '510b7baf-9172-4e39-aa6c-98444ce89dd8',
        'deviceUk': '5HtWQS2XRzB5RC4tdWSXRpNSbgTaa4ui9Pe92KW6fygOxPSKfcPX6phBlLPHsNh1J',
        'platform': 'pc',
        'secret-v': 'h5_v1',
        'uk': '5HtWfneNmNlQXafAAwQH6vtd1CxsppnD3GYfjZwSjBoyd0LL6CtwG2LebqFQZS91M'
    }

    # UUYP 的 POST 请求体[cite: 8]
    payload = {
        "gameId": "730",
        "listType": "10",
        "templateId": "5472",  # MP9 | 橘皮涂装 (略有磨损) 在 UUYP 的专属 ID
        "listSortType": 1,
        "sortType": 0,
        "pageIndex": 1,
        "pageSize": 10
    }

    print("🚀 正在向 UUYP 发送数据获取请求...")
    
    # 依然使用 curl_cffi 伪装底层指纹
    async with AsyncSession(impersonate="chrome110") as session:
        try:
            # UUYP 是 POST 请求，并且参数必须放在 json 字段里
            response = await session.post(url, headers=headers, json=payload, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if data.get("Code") == 0:
                    print("✅ 请求成功！完美获取数据：")
                    items = data.get("Data", [])
                    print(f"该饰品当前在售总数: {data.get('TotalCount')}\n")
                    
                    # 打印前 5 个最便宜的卖单，这就是你的出货底线参考价
                    for i, item in enumerate(items[:5], 1):
                        price = item.get('price')
                        abrade = item.get('abrade', '')[:6] # 截取前几位磨损
                        store = item.get('storeName', '未知店铺')
                        print(f"卖单 {i}: 售价 ¥{price} | 磨损: {abrade} | 卖家: {store}")
                else:
                    print(f"❌ 业务拦截报错: {data.get('Msg')}")
            else:
                print(f"❌ HTTP 报错: 状态码 {response.status_code}")
        except Exception as e:
            print(f"❌ 网络请求异常: {e}")

if __name__ == "__main__":
    import sys
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(test_uuyp_api())
