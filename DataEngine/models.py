from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Enum
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime

Base = declarative_base()

class ItemDictionary(Base):
    """饰品字典表：全局唯一的饰品基准库"""
    __tablename__ = 'item_dictionary'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    game = Column(String(20), default="csgo")
    market_hash_name = Column(String(255), unique=True, index=True, nullable=False) # Steam标准英文名
    cn_name = Column(String(255)) # 中文名
    buff_goods_id = Column(Integer, unique=True, index=True) # Buff的ID
    
    # 建立与价格表的关联
    prices = relationship("MarketPriceMonitor", back_populates="item")

class MarketPriceMonitor(Base):
    """市场价格快照表：记录每次爬取的盘口厚度"""
    __tablename__ = 'market_price_monitor'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(Integer, ForeignKey('item_dictionary.id'), index=True)
    platform = Column(String(20), default="buff")
    
    # --- 卖盘（供货方 - 决定你的成本） ---
    sell_min_price = Column(Float)         # 绝对最低价 (可能是不稳定的极值)
    sell_top5_avg = Column(Float)          # 卖一到卖五的均价 (真实的吃货成本)
    sell_volume = Column(Integer)          # 在售总数量 (看抛压)
    
    # --- 买盘（接盘方 - 决定你的套现底线） ---
    buy_max_price = Column(Float)          # 绝对最高求购价
    buy_top5_avg = Column(Float)           # 买一到买五的均价 (真实的批量出货价)
    
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    item = relationship("ItemDictionary", back_populates="prices")