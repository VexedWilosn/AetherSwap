import asyncio
import logging
import time
from typing import Any, Dict, List
from DrissionPage import ChromiumPage, ChromiumOptions

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def fetch_steamdt_via_browser(max_pages: int = 3) -> List[Dict[str, Any]]:
    logging.info("🔗 启动 SteamDT 浏览器寄生拦截引擎 (9222端口)...")
    
    co = ChromiumOptions()
    # 连接你已经打开的、带 9222 端口的真实浏览器
    co.set_browser_path("127.0.0.1:9222") 
    
    page = ChromiumPage(co)
    all_items = []
    
    try:
        # 监听 SteamDT 的挂刀 API 接口
        page.listen.start('api/user/ranking/v1/hanging-knife')
        
        # 让你的真实浏览器直接打开挂刀页面
        page.get('https://www.steamdt.com/hanging')
        time.sleep(3) # 等待页面初始加载
        
        for p in range(1, max_pages + 1):
            logging.info(f"⏳ 正在拦截第 {p} 页数据...")
            
            # 等待网页前端自动发出请求并拦截响应
            packet = page.listen.wait(timeout=10)
            
            if packet and packet.response.body:
                data = packet.response.body
                if isinstance(data, str):
                    import json
                    data = json.loads(data)
                
                # 兼容解析逻辑
                code = data.get("code")
                if code in [200, "200", 0, "0"]:
                    item_list = data.get("data", {}).get("list", [])
                    if not item_list and isinstance(data.get("data"), list):
                        item_list = data.get("data")
                    
                    logging.info(f"✅ 成功抓取第 {p} 页，获得 {len(item_list)} 条数据！")
                    all_items.extend(item_list)
                else:
                    logging.error(f"❌ 页面业务报错: {data}")
            else:
                logging.warning(f"⚠️ 第 {p} 页拦截超时，请检查网页是否还在正常加载。")
                
            # 模拟真人翻页动作
            if p < max_pages:
                # 寻找网页上的“下一页”按钮并点击 (这里的 CSS 选择器可能需要你根据实际页面微调)
                # 一般是寻找 class 包含 next 的按钮
                next_btn = page.ele('.btn-next', timeout=2) 
                if next_btn:
                    page.listen.clear() # 清空上一页的监听记录
                    next_btn.click()
                    time.sleep(random.uniform(2, 4)) # 模拟真人停顿
                else:
                    logging.info("找不到下一页按钮，抓取结束。")
                    break
                    
    except Exception as e:
        logging.error(f"❌ 引擎运行异常: {e}")
        
    logging.info(f"🎉 寄生拦截完成！总计获取到 {len(all_items)} 个极品挂刀机会。")
    return all_items

if __name__ == "__main__":
    fetch_steamdt_via_browser(max_pages=3)