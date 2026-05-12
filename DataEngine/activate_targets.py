import logging
from database import SessionLocal, ItemBase

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

def activate_items():
    db = SessionLocal()
    
    # 在这里填入你想监控的饰品英文名
    target_names = [
        "MP9 | Orange Peel (Minimal Wear)",
        "SSG 08 | Halftone Whorl (Minimal Wear)",
        "Prisma 2 Case" # 也可以加一些高流动性的箱子测试
    ]
    
    try:
        # 查询这些饰品
        items = db.query(ItemBase).filter(ItemBase.market_hash_name.in_(target_names)).all()
        
        if not items:
            logging.warning("❌ 在数据库中没有找到这些饰品，请检查拼写是否和数据库一致。")
            return
            
        count = 0
        for item in items:
            item.crawl_priority = 1  # 将优先级提升为最高
            item.is_active = True    # 确保是激活状态
            count += 1
            logging.info(f"✅ 已激活监控: {item.market_hash_name} (中文名: {item.cn_name})")
            
        db.commit()
        logging.info(f"🎉 成功将 {count} 个饰品加入高频爬取队列！")
        
    finally:
        db.close()

if __name__ == "__main__":
    activate_items()