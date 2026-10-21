"""SQLite boundary for immutable analysis reports, schedule claims and delivery state."""
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import aiosqlite

from backend.app.config import settings
from backend.app.services.analysis_errors import AnalysisBusy


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class AnalysisStore:
    def __init__(self, db_path=None):
        self._db_path = db_path

    @property
    def db_path(self):
        return self._db_path or settings.DB_PATH

    async def rows(self, sql, params=()):
        async with aiosqlite.connect(self.db_path, timeout=15) as db:
            db.row_factory = aiosqlite.Row
            return [dict(row) for row in await (await db.execute(sql, params)).fetchall()]

    async def execute(self, sql, params=()):
        async with aiosqlite.connect(self.db_path, timeout=15) as db:
            cursor = await db.execute(sql, params)
            await db.commit()
            return cursor.rowcount

    async def begin_run(self, run_key=None, push=False):
        async with aiosqlite.connect(self.db_path, timeout=15) as db:
            await db.execute('BEGIN IMMEDIATE')
            if run_key:
                row = await (await db.execute('SELECT id,status,finished_at FROM analysis_runs WHERE run_key=?', (run_key,))).fetchone()
                if row:
                    if row[1] != 'failed' or not row[2] or datetime.now(timezone.utc)-datetime.fromisoformat(row[2]) < timedelta(minutes=5):
                        return row[0], False
                    try:
                        await db.execute("UPDATE analysis_runs SET status='running',started_at=?,finished_at=NULL WHERE id=?", (timestamp(),row[0]))
                    except sqlite3.IntegrityError:
                        raise AnalysisBusy('已有分析正在运行，请稍后查看结果') from None
                    await db.commit()
                    return row[0], True
            try:
                cursor = await db.execute('''INSERT INTO analysis_runs
                    (run_key,status,push_requested,started_at) VALUES (?,'running',?,?)''', (run_key,int(push),timestamp()))
            except sqlite3.IntegrityError:
                raise AnalysisBusy('已有分析正在运行，请稍后查看结果') from None
            await db.commit()
            return cursor.lastrowid, True

    async def finish_run(self, run_id, status, picks, errors, diagnostics, message):
        async with aiosqlite.connect(self.db_path, timeout=15) as db:
            for pick in picks:
                await db.execute('''INSERT INTO daily_picks (symbol,reason,target_price,run_id,report_json)
                    VALUES (?,?,?,?,?)''', (pick['symbol'],pick['reason'],pick['target_price'],run_id,encode(pick)))
            await db.execute('''UPDATE analysis_runs SET status=?,count=?,errors_json=?,diagnostics_json=?,
                message=?,finished_at=? WHERE id=?''', (status,len(picks),encode(errors),encode(diagnostics),message,timestamp(),run_id))
            await db.commit()

    async def recover_interrupted(self):
        # One application worker is the supported deployment mode.
        await self.execute("UPDATE analysis_runs SET status='failed',message='服务重启中断分析，可重新运行',finished_at=? WHERE status='running'", (timestamp(),))
        await self.execute("UPDATE analysis_deliveries SET status='failed',next_attempt_at=? WHERE status='sending'", (timestamp(),))

    @staticmethod
    def _run(row):
        row['errors'] = json.loads(row.pop('errors_json','[]'))
        row['diagnostics'] = json.loads(row.pop('diagnostics_json','[]'))
        return row

    async def list_runs(self, limit=20):
        return [self._run(row) for row in await self.rows('SELECT * FROM analysis_runs ORDER BY id DESC LIMIT ?', (limit,))]

    async def get_run(self, run_id):
        rows = await self.rows('SELECT * FROM analysis_runs WHERE id=?', (run_id,))
        return self._run(rows[0]) if rows else None

    async def list_picks(self, run_id=None, include_hidden=False, all_runs=False):
        params = []
        where = [] if include_hidden else ['hidden=0']
        if run_id is None and not all_runs:
            runs = await self.rows("SELECT id FROM analysis_runs WHERE status IN ('completed','no_candidates') OR (status='partial' AND count>0) ORDER BY id DESC LIMIT 1")
            if runs:
                run_id = runs[0]['id']
            else:
                where.append('run_id IS NULL')
        if run_id is not None:
            where.append('run_id=?')
            params.append(run_id)
        rows = await self.rows('SELECT * FROM daily_picks'+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY id DESC',params)
        result=[]
        for row in rows:
            report = json.loads(row['report_json']) if row.get('report_json') else {}
            result.append({**row,**report,'id':row['id'],'run_id':row['run_id']})
            result[-1].pop('report_json',None)
        return result

    async def hide_picks(self, pick_id=None):
        return await self.execute('UPDATE daily_picks SET hidden=1'+(' WHERE id=?' if pick_id is not None else ''),
            (pick_id,) if pick_id is not None else ())

    async def queue_deliveries(self, run_id, channels, chunks):
        async with aiosqlite.connect(self.db_path, timeout=15) as db:
            for channel in channels:
                for part, body in enumerate(chunks):
                    await db.execute('''INSERT OR IGNORE INTO analysis_deliveries (run_id,channel,part,body)
                        VALUES (?,?,?,?)''', (run_id,channel,part,body))
            await db.execute("UPDATE analysis_runs SET push_requested=1,push_status='pending' WHERE id=?", (run_id,))
            await db.commit()

    async def claim_delivery(self, row_id, force=False):
        now=timestamp()
        async with aiosqlite.connect(self.db_path, timeout=15) as db:
            sql="UPDATE analysis_deliveries SET status='sending',attempts=attempts+1,updated_at=? WHERE id=? AND status IN ('pending','failed')"
            params=[now,row_id]
            if not force:
                sql+=' AND attempts<5 AND (next_attempt_at IS NULL OR next_attempt_at<=?)'
                params.append(now)
            cursor=await db.execute(sql,params)
            await db.commit()
            return cursor.rowcount == 1

    async def delivery_result(self, delivery, success):
        retry=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()
        status='success' if success else 'failed'
        async with aiosqlite.connect(self.db_path, timeout=15) as db:
            await db.execute('UPDATE analysis_deliveries SET status=?,next_attempt_at=?,updated_at=? WHERE id=?',
                (status,None if success else retry,timestamp(),delivery['id']))
            await db.execute('''INSERT INTO alert_logs (position_id,symbol,message,alert_type,status)
                VALUES (NULL,'DAILY_PICKS',?,'daily_pick',?)''', (f"[{delivery['channel']}] "+delivery['body'],status))
            await db.commit()

    async def update_push_status(self, run_id):
        rows=await self.rows('SELECT status FROM analysis_deliveries WHERE run_id=?',(run_id,))
        states=[row['status'] for row in rows]
        status='success' if states and all(s=='success' for s in states) else 'partial' if 'success' in states else 'failed'
        if any(s in ('pending','sending') for s in states) and status != 'partial': status='pending'
        await self.execute('UPDATE analysis_runs SET push_status=? WHERE id=?',(status,run_id))
        return status

    async def list_reviews(self):
        return await self.rows('''SELECT r.*,p.symbol,json_extract(p.report_json,'$.underlying_symbol') AS underlying_symbol
            FROM analysis_reviews r JOIN daily_picks p ON p.id=r.pick_id ORDER BY r.id DESC LIMIT 200''')
