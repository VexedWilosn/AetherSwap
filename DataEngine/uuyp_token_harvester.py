import json
import logging
import os
from DrissionPage import ChromiumPage, ChromiumOptions

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(CURRENT_DIR, "uuyp_headers.json")

def harvest_uuyp_headers() -> dict:
    logging.info("🔧 采蜜机启动：正在无声潜入，提取完美指纹 Token...")
    
    co = ChromiumOptions()
    co.headless(True)  # 无头模式即可，因为不需要 Cookie
    co.set_argument('--blink-settings=imagesEnabled=false')
    
    page = ChromiumPage(co)
    
    try:
        page.listen.start('api.youpin898.com')
        page.get('https://www.youpin898.com/market/csgo')
        
        logging.info("⏳ 正在等待网页执行 JS 计算签名...")
        
        for packet in page.listen.steps(timeout=15):
            if packet.request.method.upper() == 'OPTIONS':
                continue
                
            raw_headers = packet.request.headers
            h_lower = {k.lower(): v for k, v in raw_headers.items()}
            
            if 'uk' in h_lower:
                extracted_headers = {
                    'uk': h_lower.get('uk'),
                    'deviceUk': h_lower.get('deviceuk'),
                    'deviceId': h_lower.get('deviceid'),
                    'App-Version': h_lower.get('app-version', '5.26.0'),
                    'AppVersion': h_lower.get('appversion', '5.26.0'),
                    'secret-v': h_lower.get('secret-v', 'h5_v1'),
                    # 🎯 核心：将浏览器的真实身份矩阵全部提取！
                    'User-Agent': h_lower.get('user-agent'),
                    'sec-ch-ua': h_lower.get('sec-ch-ua'),
                    'sec-ch-ua-mobile': h_lower.get('sec-ch-ua-mobile'),
                    'sec-ch-ua-platform': h_lower.get('sec-ch-ua-platform')
                }
                
                # 清除可能为空的值
                extracted_headers = {k: v for k, v in extracted_headers.items() if v}
                
                with open(TOKEN_FILE, 'w', encoding='utf-8') as f:
                    json.dump(extracted_headers, f, indent=4)
                    
                logging.info(f"✅ 采蜜成功！真实的 Chrome 身份指纹已封存。")
                return extracted_headers
                
        logging.error("❌ 采蜜失败：未能在 15 秒内抓取到业务请求。")
        return {}
        
    except Exception as e:
        logging.error(f"❌ 采蜜机发生异常: {e}")
        return {}
    finally:
        page.quit()