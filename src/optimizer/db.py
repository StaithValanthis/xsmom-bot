"""
Database layer for optimizer persistence.

Stores Optuna studies, trials, and bad parameter combinations in SQLite.
"""
from __future__ import annotations

import sqlite3
import json
import hashlib
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timezone

log = logging.getLogger("optimizer.db")


class OptimizerDB:
    """SQLite database for optimizer persistence."""
    
    def __init__(self, db_path: str | Path):
        """
        Initialize optimizer database.
        
        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
    
    def _get_connection(self) -> sqlite3.Connection:
        """Get database connection."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn
    
    def _init_schema(self) -> None:
        """Initialize database schema."""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            
            # Studies table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS studies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    description TEXT,
                    config_hash TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Trials table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trials (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    study_id INTEGER NOT NULL,
                    optuna_trial_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    params_hash TEXT NOT NULL,
                    metrics_json TEXT,
                    score REAL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (study_id) REFERENCES studies(id),
                    UNIQUE(study_id, optuna_trial_number)
                )
            """)
            
            # Bad combinations table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS bad_combinations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    study_id INTEGER,
                    params_hash TEXT NOT NULL,
                    reason TEXT,
                    score REAL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (study_id) REFERENCES studies(id),
                    UNIQUE(study_id, params_hash)
                )
            """)
            
            # Indexes for performance
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trials_study_id ON trials(study_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trials_params_hash ON trials(params_hash)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trials_score ON trials(score)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_bad_combos_params_hash ON bad_combinations(params_hash)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_bad_combos_study_id ON bad_combinations(study_id)")
            
            conn.commit()
        finally:
            conn.close()
    
    def get_or_create_study(
        self,
        study_name: str,
        description: Optional[str] = None,
        config_hash: Optional[str] = None,
    ) -> int:
        """
        Get or create a study.
        
        Args:
            study_name: Unique study name
            description: Optional description
            config_hash: Optional config hash for versioning
        
        Returns:
            Study ID
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            
            # Try to get existing study
            cursor.execute(
                "SELECT id FROM studies WHERE name = ?",
                (study_name,)
            )
            row = cursor.fetchone()
            
            if row:
                study_id = row[0]
                # Update updated_at
                cursor.execute(
                    "UPDATE studies SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (study_id,)
                )
                conn.commit()
                return study_id
            
            # Create new study
            cursor.execute(
                """
                INSERT INTO studies (name, description, config_hash)
                VALUES (?, ?, ?)
                """,
                (study_name, description, config_hash)
            )
            study_id = cursor.lastrowid
            conn.commit()
            log.info(f"Created new study: {study_name} (id={study_id})")
            return study_id
        finally:
            conn.close()
    
    def record_trial_result(
        self,
        study_id: int,
        optuna_trial_number: int,
        params: Dict[str, Any],
        metrics: Optional[Dict[str, Any]] = None,
        score: Optional[float] = None,
        status: str = "complete",
    ) -> int:
        """
        Record a trial result.
        
        Args:
            study_id: Study ID
            optuna_trial_number: Optuna trial number
            params: Parameter dictionary
            metrics: Optional metrics dictionary
            score: Objective score
            status: Trial status (complete, pruned, fail, etc.)
        
        Returns:
            Trial ID
        """
        params_json = json.dumps(params, sort_keys=True)
        params_hash = self._hash_params(params)
        
        metrics_json = json.dumps(metrics, sort_keys=True) if metrics else None
        
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            
            # Insert or update trial
            cursor.execute(
                """
                INSERT OR REPLACE INTO trials 
                (study_id, optuna_trial_number, status, params_json, params_hash, 
                 metrics_json, score, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (study_id, optuna_trial_number, status, params_json, params_hash,
                 metrics_json, score)
            )
            trial_id = cursor.lastrowid
            conn.commit()
            return trial_id
        finally:
            conn.close()
    
    def find_existing_trial_by_params(
        self,
        study_id: int,
        params: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        Find an existing trial with the same parameters.
        
        Args:
            study_id: Study ID
            params: Parameter dictionary
        
        Returns:
            Trial dict if found, None otherwise
        """
        params_hash = self._hash_params(params)
        
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, optuna_trial_number, status, params_json, metrics_json, score
                FROM trials
                WHERE study_id = ? AND params_hash = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (study_id, params_hash)
            )
            row = cursor.fetchone()
            
            if row:
                return {
                    "id": row[0],
                    "optuna_trial_number": row[1],
                    "status": row[2],
                    "params": json.loads(row[3]),
                    "metrics": json.loads(row[4]) if row[4] else None,
                    "score": row[5],
                }
            return None
        finally:
            conn.close()
    
    def mark_bad_combination(
        self,
        study_id: Optional[int],
        params: Dict[str, Any],
        reason: str,
        score: Optional[float] = None,
    ) -> int:
        """
        Mark a parameter combination as bad.
        
        Args:
            study_id: Optional study ID (None for global bad combos)
            params: Parameter dictionary
            reason: Reason for marking as bad
            score: Optional score (if known)
        
        Returns:
            Bad combination ID
        """
        params_hash = self._hash_params(params)
        
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO bad_combinations 
                (study_id, params_hash, reason, score)
                VALUES (?, ?, ?, ?)
                """,
                (study_id, params_hash, reason, score)
            )
            combo_id = cursor.lastrowid
            conn.commit()
            return combo_id
        finally:
            conn.close()
    
    def is_bad_combination(
        self,
        study_id: Optional[int],
        params: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        Check if a parameter combination is marked as bad.
        
        Args:
            study_id: Optional study ID (None checks global bad combos)
            params: Parameter dictionary
        
        Returns:
            Bad combo dict if found, None otherwise
        """
        params_hash = self._hash_params(params)
        
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            
            # Check study-specific first, then global
            if study_id is not None:
                cursor.execute(
                    """
                    SELECT id, reason, score, created_at
                    FROM bad_combinations
                    WHERE (study_id = ? OR study_id IS NULL) AND params_hash = ?
                    ORDER BY study_id DESC NULLS LAST
                    LIMIT 1
                    """,
                    (study_id, params_hash)
                )
            else:
                cursor.execute(
                    """
                    SELECT id, reason, score, created_at
                    FROM bad_combinations
                    WHERE params_hash = ?
                    ORDER BY study_id DESC NULLS LAST
                    LIMIT 1
                    """,
                    (params_hash,)
                )
            
            row = cursor.fetchone()
            if row:
                return {
                    "id": row[0],
                    "reason": row[1],
                    "score": row[2],
                    "created_at": row[3],
                }
            return None
        finally:
            conn.close()
    
    def get_study_trials(
        self,
        study_id: int,
        limit: Optional[int] = None,
        order_by: str = "score DESC",
    ) -> List[Dict[str, Any]]:
        """
        Get trials for a study.
        
        Args:
            study_id: Study ID
            limit: Optional limit
            order_by: ORDER BY clause (default: score DESC)
        
        Returns:
            List of trial dicts
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            query = f"""
                SELECT id, optuna_trial_number, status, params_json, metrics_json, score, created_at
                FROM trials
                WHERE study_id = ?
                ORDER BY {order_by}
            """
            if limit:
                query += f" LIMIT {limit}"
            
            cursor.execute(query, (study_id,))
            rows = cursor.fetchall()
            
            trials = []
            for row in rows:
                trials.append({
                    "id": row[0],
                    "optuna_trial_number": row[1],
                    "status": row[2],
                    "params": json.loads(row[3]),
                    "metrics": json.loads(row[4]) if row[4] else None,
                    "score": row[5],
                    "created_at": row[6],
                })
            return trials
        finally:
            conn.close()
    
    def list_studies(self) -> List[Dict[str, Any]]:
        """List all studies with basic stats."""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 
                    s.id,
                    s.name,
                    s.description,
                    s.created_at,
                    s.updated_at,
                    COUNT(t.id) as trial_count,
                    MAX(t.score) as best_score,
                    MIN(t.score) as worst_score,
                    AVG(t.score) as avg_score
                FROM studies s
                LEFT JOIN trials t ON s.id = t.study_id
                GROUP BY s.id
                ORDER BY s.created_at DESC
            """)
            
            studies = []
            for row in cursor.fetchall():
                studies.append({
                    "id": row[0],
                    "name": row[1],
                    "description": row[2],
                    "created_at": row[3],
                    "updated_at": row[4],
                    "trial_count": row[5] or 0,
                    "best_score": row[6],
                    "worst_score": row[7],
                    "avg_score": row[8],
                })
            return studies
        finally:
            conn.close()
    
    def _hash_params(self, params: Dict[str, Any]) -> str:
        """Compute deterministic hash of parameters."""
        params_json = json.dumps(params, sort_keys=True)
        return hashlib.sha256(params_json.encode()).hexdigest()

