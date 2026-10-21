from pathlib import Path

import aiosqlite
from backend.app.config import DEFAULT_CHECK_INTERVAL_HOURS, settings


async def _add_columns(db, table, columns):
    existing = {row[1] for row in await (await db.execute(f'PRAGMA table_info({table})')).fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            await db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')


async def init_db(db_path: str = None):
    target_path = db_path or settings.DB_PATH
    if target_path != ':memory:':
        Path(target_path).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(target_path) as db:
        await db.execute('PRAGMA journal_mode=WAL')
        await db.execute(f'''
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL, buy_price REAL NOT NULL, quantity REAL NOT NULL,
                take_profit_price REAL, stop_loss_price REAL,
                check_interval_hours REAL DEFAULT {DEFAULT_CHECK_INTERVAL_HOURS},
                ladder_alerts TEXT DEFAULT '[]', include_ai_analysis BOOLEAN DEFAULT 0,
                preferences_customized BOOLEAN DEFAULT 0, is_active BOOLEAN DEFAULT 1
            )
        ''')
        await _add_columns(db, 'positions', {'include_ai_analysis':'BOOLEAN DEFAULT 0',
            'preferences_customized':'BOOLEAN DEFAULT 0', 'alert_reset_after_id':'INTEGER NOT NULL DEFAULT 0'})
        await db.execute('''CREATE TABLE IF NOT EXISTS daily_picks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, reason TEXT NOT NULL,
            target_price REAL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        await _add_columns(db, 'daily_picks', {'run_id':'INTEGER', 'report_json':'TEXT',
            'hidden':'INTEGER NOT NULL DEFAULT 0'})
        await db.execute('''CREATE TABLE IF NOT EXISTS alert_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, position_id INTEGER, symbol TEXT NOT NULL,
            message TEXT NOT NULL, alert_type TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(position_id) REFERENCES positions(id)
        )''')
        await _add_columns(db, 'alert_logs', {'status':"TEXT NOT NULL DEFAULT 'success'"})
        await db.execute('''CREATE TABLE IF NOT EXISTS system_configs (
            key TEXT PRIMARY KEY, value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS analysis_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_key TEXT UNIQUE,
            status TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
            push_status TEXT NOT NULL DEFAULT 'not_requested', push_requested INTEGER DEFAULT 0,
            started_at TEXT NOT NULL, finished_at TEXT, message TEXT NOT NULL DEFAULT '',
            errors_json TEXT NOT NULL DEFAULT '[]', diagnostics_json TEXT NOT NULL DEFAULT '[]'
        )''')
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS single_analysis_run ON analysis_runs((1)) WHERE status='running'")
        await db.execute('CREATE INDEX IF NOT EXISTS picks_run_id ON daily_picks(run_id)')
        await db.execute('''CREATE TABLE IF NOT EXISTS analysis_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL,
            channel TEXT NOT NULL, part INTEGER NOT NULL, body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT, updated_at TEXT,
            UNIQUE(run_id,channel,part), FOREIGN KEY(run_id) REFERENCES analysis_runs(id)
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS analysis_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT, pick_id INTEGER NOT NULL,
            horizon_sessions INTEGER NOT NULL, as_of TEXT NOT NULL,
            return_pct REAL NOT NULL, benchmark_return_pct REAL NOT NULL,
            excess_return_pct REAL NOT NULL, UNIQUE(pick_id,horizon_sessions),
            FOREIGN KEY(pick_id) REFERENCES daily_picks(id)
        )''')
        await db.commit()
