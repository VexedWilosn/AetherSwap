import json
import logging
import os
from sqlalchemy.orm import Session
from database import SessionLocal, ItemBase, PlatformMapping, init_db

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

# 开源库的根目录名
BASE_DIR = "SteamTradingSite-ID-Mapper-main"

def load_json_data(file_path: str) -> dict:
    if not os.path.exists(file_path):
        logging.warning(f"⚠️ 找不到文件: {file_path}")
        return {}
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"❌ 读取文件 {file_path} 失败: {e}")
        return {}

def import_steam_base(db: Session, game: str):
    """专门从 steam 文件夹读取基础信息（带中文名）打底"""
    file_path = os.path.join(BASE_DIR, 'steam', f'{game}.json')
    steam_data = load_json_data(file_path)
    if not steam_data: return

    logging.info(f"🚀 开始建立 {game} 的 Steam 基准库，共 {len(steam_data)} 条...")
    count = 0
    
    # 提前缓存已存在的数据，防止重复插入（使用内存查表极大提升速度）
    existing_items = {item.market_hash_name: item for item in db.query(ItemBase).all()}
    
    new_items = []
    for hash_name, details in steam_data.items():
        if hash_name not in existing_items:
            # 兼容：有些 JSON value 是字典，有些可能是别的
            cn_name = details.get('cn_name', '') if isinstance(details, dict) else ''
            new_items.append(ItemBase(
                market_hash_name=hash_name,
                cn_name=cn_name,
                game="csgo" if game == "730" else "dota2"
            ))
            count += 1
        
        # 每一万条批量写入一次数据库
        if len(new_items) >= 10000:
            db.add_all(new_items)
            db.commit()
            new_items = []

    if new_items:
        db.add_all(new_items)
        db.commit()
    logging.info(f"✅ {game} 基准库建立完成！新增了 {count} 条基础饰品。")

def import_platform_mapping(db: Session, platform_name: str, game: str):
    """读取 buff/uuyp 的文件，建立 ID 映射"""
    file_path = os.path.join(BASE_DIR, platform_name, f'{game}.json')
    mapping_data = load_json_data(file_path)
    if not mapping_data: return
    
    logging.info(f"🔗 开始导入 {platform_name} ({game}) 的映射数据...")
    
    # 获取主表所有 ID，用于绑定外键
    all_items = db.query(ItemBase.id, ItemBase.market_hash_name).all()
    item_id_map = {name: id for id, name in all_items}
    
    # 找到数据库中已经存在的当前平台映射，避免报错
    existing_mappings = set(m.item_id for m in db.query(PlatformMapping.item_id).filter(PlatformMapping.platform_name == platform_name).all())

    new_mappings = []
    count = 0
    for hash_name, platform_id in mapping_data.items():
        if platform_id == -1 or platform_id == "-1":
            continue
        
        item_id = item_id_map.get(hash_name)
        if item_id and item_id not in existing_mappings:
            new_mappings.append(PlatformMapping(
                item_id=item_id,
                platform_name=platform_name,
                platform_item_id=str(platform_id)
            ))
            existing_mappings.add(item_id)
            count += 1
            
        if len(new_mappings) >= 10000:
            db.add_all(new_mappings)
            db.commit()
            new_mappings = []

    if new_mappings:
        db.add_all(new_mappings)
        db.commit()
    logging.info(f"✅ {platform_name} ({game}) 映射导入完成！新增了 {count} 条。")

def main():
    init_db() # 确保表已经创建
    db = SessionLocal()
    
    try:
        # 1. 先用 Steam 数据打底（创建基础表记录并写入中文名）
        import_steam_base(db, '730')
        import_steam_base(db, '570')
        
        # 2. 导入 Buff 映射
        import_platform_mapping(db, 'buff', '730')
        import_platform_mapping(db, 'buff', '570')
        
        # 3. 导入 UUYP 映射 (UUYP没有 dota2)
        import_platform_mapping(db, 'uuyp', '730')
        
    except Exception as e:
        logging.error(f"全局执行出错: {e}")
    finally:
        db.close()
    
    logging.info("🎉 所有字典初始化任务圆满完成！你的底层数据基石已彻底打好。")

if __name__ == "__main__":
    main()