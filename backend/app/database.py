import aiosqlite
from backend.app.config import settings

async def init_db(db_path: str = None):
    target_path = db_path or settings.DB_PATH
    async with aiosqlite.connect(target_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                buy_price REAL NOT NULL,
                quantity REAL NOT NULL,
                take_profit_price REAL,
                stop_loss_price REAL,
                check_interval_hours REAL DEFAULT 0.5,
                ladder_alerts TEXT DEFAULT '[]',
                include_ai_analysis BOOLEAN DEFAULT 0,
                is_active BOOLEAN DEFAULT 1
            )
        """)

        # Migration: add include_ai_analysis to existing tables if missing
        try:
            await db.execute("ALTER TABLE positions ADD COLUMN include_ai_analysis BOOLEAN DEFAULT 0")
        except Exception:
            pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_picks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                reason TEXT NOT NULL,
                target_price REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS alert_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id INTEGER,
                symbol TEXT NOT NULL,
                message TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(position_id) REFERENCES positions(id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS system_configs (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.commit()
