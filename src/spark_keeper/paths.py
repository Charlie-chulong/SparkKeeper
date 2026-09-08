from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AppPaths:
    root: Path
    work: Path
    data: Path
    outputs: Path
    database: Path
    auth_state: Path
    scheduler_xml: Path

    @classmethod
    def discover(cls) -> AppPaths:
        configured = os.environ.get("SPARK_KEEPER_ROOT")
        if configured:
            root = Path(configured).resolve()
            work = root / "work"
            data = work / "local-data"
            outputs = root / "outputs"
            scheduler_xml = work / "spark-keeper-task.xml"
        elif getattr(sys, "frozen", False):
            root = Path(sys.executable).resolve().parent
            local_app_data = Path(
                os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
            )
            work = local_app_data / "SparkKeeper"
            data = work / "data"
            outputs = work / "outputs"
            scheduler_xml = work / "spark-keeper-task.xml"
        else:
            root = Path(__file__).resolve().parents[2]
            work = root / "work"
            data = work / "local-data"
            outputs = root / "outputs"
            scheduler_xml = work / "spark-keeper-task.xml"
        return cls(
            root=root,
            work=work,
            data=data,
            outputs=outputs,
            database=data / "spark-keeper.sqlite3",
            auth_state=data / "auth-state.bin",
            scheduler_xml=scheduler_xml,
        )

    def ensure_runtime_dirs(self) -> None:
        self.work.mkdir(parents=True, exist_ok=True)
        self.data.mkdir(parents=True, exist_ok=True)
        self.outputs.mkdir(parents=True, exist_ok=True)
