import sqlite3
from pathlib import Path

class CacheManager:
    MAX_ENTRIES = 100

    def __init__(self, db_path="./song_cache.db"):
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS song_cache (
                    bvid TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    download_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_play_time TIMESTAMP,
                    play_count INTEGER DEFAULT 0
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_last_play ON song_cache(last_play_time)')

    def get(self, bvid):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT bvid, title, file_path FROM song_cache WHERE bvid = ?",
                (bvid,)
            )
            row = cursor.fetchone()
            if row:
                return {"bvid": row[0], "title": row[1], "file_path": row[2]}
            return None

    def insert(self, bvid, title, file_path):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO song_cache (bvid, title, file_path, download_time) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (bvid, title, str(file_path))
            )
        self.cleanup()

    def update_stats(self, bvid):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE song_cache SET last_play_time = CURRENT_TIMESTAMP, play_count = play_count + 1 WHERE bvid = ?",
                (bvid,)
            )

    def cleanup(self, max_entries=None):
        if max_entries is None:
            max_entries = self.MAX_ENTRIES
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                DELETE FROM song_cache
                WHERE bvid NOT IN (
                    SELECT bvid FROM song_cache
                    ORDER BY last_play_time DESC
                    LIMIT ?
                )
            ''', (max_entries,))

    def get_all_files(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT file_path FROM song_cache")
            rows = cursor.fetchall()
        return [Path(row[0]) for row in rows]

    def delete_all(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT file_path FROM song_cache")
            rows = cursor.fetchall()
            for (file_path,) in rows:
                try:
                    Path(file_path).unlink()
                except:
                    pass
            conn.execute("DELETE FROM song_cache")

    def delete_by_bvid(self, bvid):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT file_path FROM song_cache WHERE bvid = ?", (bvid,))
            row = cursor.fetchone()
            if row:
                try:
                    Path(row[0]).unlink()
                except:
                    pass
                conn.execute("DELETE FROM song_cache WHERE bvid = ?", (bvid,))
                return True
            return False