import pytest
import os
import sqlite3
from backend.app.models.database import init_db
from backend.app.config import settings

@pytest.mark.asyncio
async def test_init_db(tmp_path):
    # Override settings for tests
    settings.DB_PATH = str(tmp_path / "test.db")
    
    await init_db()
    
    # Verify tables were created
    conn = sqlite3.connect(settings.DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [row[0] for row in cursor.fetchall()]
    
    assert "positions" in tables
    assert "daily_picks" in tables
    assert "alert_logs" in tables
    
    conn.close()
