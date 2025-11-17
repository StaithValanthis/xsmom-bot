"""
Config versioning and deployment manager.

Handles safe deployment of optimized configs with rollback capability.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime
import json
import yaml

log = logging.getLogger("optimizer.config_manager")


class ConfigManager:
    """Manages config versioning, deployment, and rollback."""
    
    def __init__(
        self,
        live_config_path: str | Path,
        optimized_dir: str | Path = "config/optimized",
        max_versions: int = 20,
    ):
        """
        Initialize config manager.
        
        Args:
            live_config_path: Path to live config file (e.g., config/config.yaml)
            optimized_dir: Directory to store versioned optimized configs
            max_versions: Maximum number of versions to keep
        """
        self.live_config_path = Path(live_config_path)
        self.optimized_dir = Path(optimized_dir)
        self.max_versions = max_versions
        
        self.optimized_dir.mkdir(parents=True, exist_ok=True)
    
    def save_versioned_config(
        self,
        config_dict: Dict[str, Any],
        metadata: Dict[str, Any],
        timestamp: Optional[datetime] = None,
    ) -> Path:
        """
        Save a versioned optimized config with metadata.
        
        Args:
            config_dict: Config dictionary
            metadata: Metadata dict (metrics, params, etc.)
            timestamp: Optional timestamp (defaults to now)
        
        Returns:
            Path to saved config file
        """
        if timestamp is None:
            timestamp = datetime.utcnow()
        
        ts_str = timestamp.strftime("%Y%m%d_%H%M%S")
        config_filename = f"config_{ts_str}.yaml"
        metadata_filename = f"metadata_{ts_str}.json"
        
        config_path = self.optimized_dir / config_filename
        metadata_path = self.optimized_dir / metadata_filename
        
        # Save config
        with open(config_path, "w") as f:
            yaml.safe_dump(config_dict, f, sort_keys=False, default_flow_style=False)
        
        # Save metadata
        metadata_with_ts = {
            **metadata,
            "timestamp": timestamp.isoformat(),
            "config_path": str(config_path),
        }
        with open(metadata_path, "w") as f:
            json.dump(metadata_with_ts, f, indent=2)
        
        log.info(f"Saved versioned config: {config_path}")
        return config_path
    
    def deploy_config(
        self,
        config_path: Path,
        create_backup: bool = True,
    ) -> bool:
        """
        Deploy a config to live location.
        
        Args:
            config_path: Path to config file to deploy
            create_backup: If True, backup current live config first
        
        Returns:
            True if successful
        """
        if not config_path.exists():
            log.error(f"Config file does not exist: {config_path}")
            return False
        
        if create_backup:
            self.backup_live_config()
        
        try:
            # Copy to live location
            shutil.copy2(config_path, self.live_config_path)
            log.info(f"Deployed config to {self.live_config_path}")
            
            # Update current_live_config pointer
            self._update_live_pointer(config_path)
            return True
        except Exception as e:
            log.error(f"Failed to deploy config: {e}")
            return False
    
    def backup_live_config(self) -> Optional[Path]:
        """
        Backup current live config.
        
        Returns:
            Path to backup file, or None if failed
        """
        if not self.live_config_path.exists():
            log.warning(f"Live config does not exist: {self.live_config_path}")
            return None
        
        timestamp = datetime.utcnow()
        ts_str = timestamp.strftime("%Y%m%d_%H%M%S")
        backup_path = self.optimized_dir / f"backup_{ts_str}.yaml"
        
        try:
            shutil.copy2(self.live_config_path, backup_path)
            log.info(f"Backed up live config to {backup_path}")
            return backup_path
        except Exception as e:
            log.error(f"Failed to backup live config: {e}")
            return None
    
    def rollback_to_version(
        self,
        timestamp_str: str,
        create_backup: bool = True,
    ) -> bool:
        """
        Rollback to a specific versioned config.
        
        Args:
            timestamp_str: Timestamp string (YYYYMMDD_HHMMSS) or "latest"
            create_backup: If True, backup current live config first
        
        Returns:
            True if successful
        """
        if timestamp_str == "latest":
            config_path = self.get_latest_config()
        else:
            config_path = self.optimized_dir / f"config_{timestamp_str}.yaml"
        
        if not config_path or not config_path.exists():
            log.error(f"Config version not found: {timestamp_str}")
            return False
        
        return self.deploy_config(config_path, create_backup=create_backup)
    
    def get_latest_config(self) -> Optional[Path]:
        """Get path to latest versioned config."""
        config_files = sorted(
            self.optimized_dir.glob("config_*.yaml"),
            key=lambda p: p.stem,
            reverse=True,
        )
        return config_files[0] if config_files else None
    
    def list_versions(self) -> List[Dict[str, Any]]:
        """
        List all versioned configs with metadata.
        
        Returns:
            List of version info dicts
        """
        versions = []
        
        for config_file in sorted(
            self.optimized_dir.glob("config_*.yaml"),
            key=lambda p: p.stem,
            reverse=True,
        ):
            ts_str = config_file.stem.replace("config_", "")
            metadata_file = self.optimized_dir / f"metadata_{ts_str}.json"
            
            version_info = {
                "timestamp": ts_str,
                "config_path": str(config_file),
                "metadata_path": str(metadata_file) if metadata_file.exists() else None,
            }
            
            # Try to load metadata
            if metadata_file.exists():
                try:
                    with open(metadata_file, "r") as f:
                        metadata = json.load(f)
                        version_info["metadata"] = metadata
                except Exception as e:
                    log.warning(f"Failed to load metadata for {ts_str}: {e}")
            
            versions.append(version_info)
        
        return versions
    
    def cleanup_old_versions(self) -> int:
        """
        Remove old versions beyond max_versions limit.
        
        Returns:
            Number of versions removed
        """
        versions = self.list_versions()
        if len(versions) <= self.max_versions:
            return 0
        
        removed = 0
        for version in versions[self.max_versions:]:
            try:
                # Remove config and metadata files
                config_path = Path(version["config_path"])
                if config_path.exists():
                    config_path.unlink()
                
                metadata_path = Path(version.get("metadata_path", ""))
                if metadata_path and metadata_path.exists():
                    metadata_path.unlink()
                
                removed += 1
            except Exception as e:
                log.warning(f"Failed to remove version {version['timestamp']}: {e}")
        
        log.info(f"Cleaned up {removed} old config versions")
        return removed
    
    def _update_live_pointer(self, config_path: Path) -> None:
        """Update current_live_config.json pointer."""
        pointer_path = self.optimized_dir / "current_live_config.json"
        pointer_data = {
            "config_path": str(config_path),
            "updated_at": datetime.utcnow().isoformat(),
        }
        with open(pointer_path, "w") as f:
            json.dump(pointer_data, f, indent=2)
    
    def validate_config(self, config_path: Path) -> bool:
        """
        Validate a config file.
        
        Args:
            config_path: Path to config file
        
        Returns:
            True if valid
        """
        try:
            from ..config import load_config
            load_config(str(config_path))
            return True
        except Exception as e:
            log.error(f"Config validation failed: {e}")
            return False

